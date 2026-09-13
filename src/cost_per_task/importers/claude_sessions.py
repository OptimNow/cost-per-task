"""Claude Code and Cowork session transcripts as a cost-per-task source.

Claude Code (terminal and desktop app) writes one JSONL transcript per session
under ``<config dir>/projects``. Cowork runs Claude Code inside each of its
sessions and keeps the same transcripts under the Claude app's
``local-agent-mode-sessions`` folder. Every model response in them carries the
usage block Anthropic returned, so sessions already run can be priced with no
proxy and no API spend. Sessions on a subscription are not billed per token:
the result is a shadow cost at API list prices.

Rules learnt from real transcripts (Claude Code 2.1.x, September 2026):

- one model response is written on several lines, one per content block,
  sharing ``message.id`` and ``requestId``; input and cache counts repeat but
  the output count grows as the response streams, so each response is counted
  once with its highest counts
- sub-agent transcripts (``<session>/subagents/agent-*.jsonl``) carry the
  parent's ``sessionId``, so grouping by sessionId folds their work into the
  parent session
- a Cowork session is its ``local_<id>`` folder, which can hold several
  transcripts; its ``audit.jsonl`` is not a transcript and is ignored
- model ``<synthetic>`` marks messages generated locally without an API call
- a response seen in two sessions (history copied on resume) is counted once

Only usage, model, effort, tool names, timestamps, the product and the name of
the working folder are read. Prompts, answers and tool inputs are never copied.
The session title (desktop) or output file names (Cowork) are read only for the
labelling sheet, never for the log, and can be left out.

The transcript format is internal to Anthropic's products, not a published
interface. Lines that cannot be read are skipped and counted, never guessed.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..labels import Label
from ..pricing import PricingTable, price_step
from ..schema import StepRecord

PRODUCTS = {
    "claude-desktop": "Claude Code desktop",
    "cli": "Claude Code CLI",
    "local-agent": "Cowork",
    "sdk-cli": "Agent SDK",
    "sdk-py": "Agent SDK",
    "sdk-ts": "Agent SDK",
}
_TOOL_BLOCKS = ("tool_use", "server_tool_use")
_STANDARD_TIERS = (None, "standard")
# Older transcripts (March to May 2026) write an empty string here.
_STANDARD_GEO = (None, "", "not_available", "global")

SHEET_COLUMNS = [
    "session_id", "product", "started", "ended", "project", "title", "main_model",
    "models", "calls", "api_cost_usd", "task", "task_type", "outcome", "leaked", "note",
]
USER_COLUMNS = ("task", "task_type", "outcome", "leaked", "note")
_YES = {"yes", "y", "true", "1", "x", "oui", "o", "igen", "i"}
_NO = {"", "no", "n", "false", "0", "non", "nem"}

WARNING_TEXT = {
    "unreadable lines": "transcript lines could not be read and were skipped",
    "fast mode responses": "responses used fast mode, which costs more than list price; priced at standard rates",
    "non-standard service tier responses": "responses used a non-standard service tier; priced at standard rates",
    "regional inference responses": "responses ran with regional inference pricing; priced at standard rates",
}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(value: str) -> str:
    """UTC ISO timestamp to local 'YYYY-MM-DD HH:MM' for the sheet."""
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass
class Call:
    """One model response, counted once."""

    timestamp: str
    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0
    effort: str | None = None
    tool_names: list[str] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    source: str  # "code" or "cowork"
    entrypoint: str = ""
    project: str = ""
    title: str = ""
    versions: set[str] = field(default_factory=set)
    calls: dict[tuple, Call] = field(default_factory=dict)

    @property
    def product(self) -> str:
        if self.source == "cowork":
            return "Cowork"
        return PRODUCTS.get(self.entrypoint, self.entrypoint or "Claude Code")

    def ordered_calls(self) -> list[Call]:
        return sorted(self.calls.values(), key=lambda c: c.timestamp)

    @property
    def started(self) -> str:
        stamps = [c.timestamp for c in self.calls.values() if c.timestamp]
        return min(stamps) if stamps else ""

    @property
    def ended(self) -> str:
        stamps = [c.timestamp for c in self.calls.values() if c.timestamp]
        return max(stamps) if stamps else ""

    def records(self, task_id: str, task_type: str | None = None) -> list[StepRecord]:
        out = []
        for step, call in enumerate(self.ordered_calls(), start=1):
            out.append(
                StepRecord(
                    task_id=task_id,
                    attempt_id=self.session_id,
                    step_id=step,
                    timestamp=call.timestamp,
                    provider="anthropic",
                    model=call.model,
                    input_tokens=call.input_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_write_tokens=call.cache_write_tokens,
                    cache_write_1h_tokens=min(call.cache_write_1h_tokens, call.cache_write_tokens),
                    reasoning_tokens=None,
                    output_tokens=call.output_tokens,
                    tool_call_count=len(call.tool_names),
                    tool_names=list(call.tool_names),
                    latency_ms=0,
                    effort=call.effort,
                    task_type=task_type,
                )
            )
        return out

    def cost(self, table: PricingTable) -> tuple[float, int]:
        """API list-price cost and the number of responses on unpriced models."""
        total, unpriced = 0.0, 0
        for record in self.records("-"):
            value = price_step(record, table)
            if value is None:
                unpriced += 1
            else:
                total += value
        return total, unpriced

    def models(self) -> list[str]:
        counts = Counter(c.model for c in self.calls.values())
        return [m for m, _ in counts.most_common()]

    def main_model(self, table: PricingTable | None = None) -> str:
        """The model carrying the largest cost share (token share without prices),
        the same rule the report uses to attribute an attempt."""
        weights: Counter = Counter()
        for record in self.records("-"):
            value = price_step(record, table) if table else None
            weights[record.model] += value if value is not None else record.output_tokens
        return weights.most_common(1)[0][0] if weights else ""


def default_roots() -> list[tuple[str, Path]]:
    """Where Claude Code and Cowork keep transcripts on this computer."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [("code", (Path(config) if config else Path.home() / ".claude") / "projects")]
    if sys.platform == "win32":
        app = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
    elif sys.platform == "darwin":
        app = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        app = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"
    roots.append(("cowork", app / "local-agent-mode-sessions"))
    return roots


def roots_for_paths(paths: list[str]) -> list[tuple[str, Path]]:
    """Folders given on the command line. A Cowork sessions folder is
    recognised by its name; anything else is read as Claude Code transcripts."""
    return [
        ("cowork" if "local-agent-mode-sessions" in Path(p).parts else "code", Path(p)) for p in paths
    ]


def _cowork_hint(session_dir: Path) -> str:
    outputs = session_dir / "outputs"
    try:
        files = sorted(
            (f for f in outputs.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True
        )
    except OSError:
        return ""
    names = [f.name for f in files[:3]]
    return ("outputs: " + ", ".join(names))[:120] if names else ""


def discover_sessions(
    roots: list[tuple[str, Path]],
    *,
    since: date | None = None,
    include_titles: bool = True,
) -> tuple[dict[str, Session], Counter]:
    """Read every transcript under the roots. Returns the sessions that made
    at least one model call, keyed by session id, and a counter of what was read."""
    sessions: dict[str, Session] = {}
    owner: dict[tuple, str] = {}
    stats: Counter = Counter()
    since_ts = datetime(since.year, since.month, since.day).timestamp() if since else None

    for source, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            try:
                if since_ts is not None and path.stat().st_mtime < since_ts:
                    continue
            except OSError:
                continue
            group = None
            hint = ""
            if source == "cowork":
                parts = path.relative_to(root).parts
                folder = next((p for p in parts if p.startswith("local_")), None)
                if folder is None or ".claude" not in parts:
                    continue
                group = folder
                if include_titles and folder not in sessions:
                    hint = _cowork_hint(root.joinpath(*parts[: parts.index(folder) + 1]))
            stats["files"] += 1
            _read_transcript(path, source, group, hint, sessions, owner, stats, include_titles)

    found = {sid: s for sid, s in sessions.items() if s.calls}
    if since:
        found = {sid: s for sid, s in found.items() if s.ended[:10] >= since.isoformat()}
    return found, stats


def _read_transcript(path, source, group, hint, sessions, owner, stats, include_titles) -> None:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        stats["unreadable files"] += 1
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                stats["unreadable lines"] += 1
                continue
            if not isinstance(entry, dict):
                continue
            sid = group or entry.get("sessionId") or path.stem
            session = sessions.get(sid)
            if session is None:
                session = sessions[sid] = Session(sid, source, title=hint)
            if not session.entrypoint and isinstance(entry.get("entrypoint"), str):
                session.entrypoint = entry["entrypoint"]
            cwd = entry.get("cwd")
            if not session.project and isinstance(cwd, str) and cwd.strip("\\/"):
                session.project = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1]
            if isinstance(entry.get("version"), str):
                session.versions.add(entry["version"])
            if (
                include_titles
                and entry.get("type") == "custom-title"
                and isinstance(entry.get("customTitle"), str)
            ):
                session.title = entry["customTitle"].strip()[:120]

            message = entry.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            usage = message["usage"]
            model = message.get("model")
            if not isinstance(model, str) or not model or model.startswith("<"):
                stats["local messages skipped"] += 1
                continue
            key = (message.get("id") or entry.get("uuid") or line[:80], entry.get("requestId"))
            tools = [
                block.get("name", "")
                for block in message.get("content") or []
                if isinstance(block, dict) and block.get("type") in _TOOL_BLOCKS
            ]
            effort = entry.get("effort") if isinstance(entry.get("effort"), str) else None

            if key in owner:
                call = sessions[owner[key]].calls[key]
                if owner[key] == sid:
                    stats["repeated lines merged"] += 1
                else:
                    stats["responses already counted in another session"] += 1
                call.output_tokens = max(call.output_tokens, _int(usage.get("output_tokens")))
                call.input_tokens = max(call.input_tokens, _int(usage.get("input_tokens")))
                call.cache_read_tokens = max(call.cache_read_tokens, _int(usage.get("cache_read_input_tokens")))
                call.cache_write_tokens = max(
                    call.cache_write_tokens, _int(usage.get("cache_creation_input_tokens"))
                )
                call.effort = call.effort or effort
                call.timestamp = call.timestamp or _iso(entry.get("timestamp"))
            else:
                creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
                call = Call(
                    timestamp=_iso(entry.get("timestamp")),
                    model=model,
                    input_tokens=_int(usage.get("input_tokens")),
                    cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                    cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
                    cache_write_1h_tokens=_int(creation.get("ephemeral_1h_input_tokens")),
                    output_tokens=_int(usage.get("output_tokens")),
                    effort=effort,
                )
                session.calls[key] = call
                owner[key] = sid
                stats["responses"] += 1
                if usage.get("speed") == "fast":
                    stats["fast mode responses"] += 1
                if usage.get("service_tier") not in _STANDARD_TIERS:
                    stats["non-standard service tier responses"] += 1
                if usage.get("inference_geo") not in _STANDARD_GEO:
                    stats["regional inference responses"] += 1
            for name in tools:
                if name and name not in call.tool_names:
                    call.tool_names.append(name)


def write_sheet(
    path: str | Path,
    sessions: dict[str, Session],
    *,
    table: PricingTable | None = None,
    existing: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """Write the labelling sheet, newest session first. Rows already in an
    existing sheet keep what the user typed, including rows for sessions whose
    transcripts are gone. Returns (rows written, rows carrying user input)."""
    existing = existing or {}
    rows: list[dict] = []
    for session in sorted(sessions.values(), key=lambda s: s.started, reverse=True):
        cost = ""
        if table is not None:
            value, _ = session.cost(table)
            cost = f"{value:.2f}"
        row = {
            "session_id": session.session_id,
            "product": session.product,
            "started": _local(session.started),
            "ended": _local(session.ended),
            "project": session.project,
            "title": session.title,
            "main_model": session.main_model(table),
            "models": " ".join(session.models()),
            "calls": str(len(session.calls)),
            "api_cost_usd": cost,
        }
        previous = existing.get(session.session_id, {})
        for column in USER_COLUMNS:
            row[column] = previous.get(column, "")
        rows.append(row)
    seen = {r["session_id"] for r in rows}
    rows.extend(
        {c: old.get(c, "") for c in SHEET_COLUMNS} for sid, old in existing.items() if sid not in seen
    )
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write("sep=,\r\n")  # tells Excel the delimiter whatever the regional settings
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    labelled = sum(1 for r in rows if any(r.get(c) for c in USER_COLUMNS))
    return len(rows), labelled


def read_sheet(path: str | Path) -> list[dict]:
    """Read the sheet back, however Excel saved it: UTF-8 or the Windows code
    page, comma or semicolon, with or without the 'sep=' line."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].strip().lower().startswith("sep="):
        delimiter = lines[0].strip()[4:5] or ","
        lines = lines[1:]
    else:
        header = lines[0] if lines else ""
        delimiter = max((",", ";", "\t"), key=header.count)
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [
        {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        for row in reader
    ]
    if rows and "session_id" not in rows[0]:
        raise ValueError(f"{path}: no session_id column; is this a sheet from 'cpt sessions list'?")
    return rows


@dataclass
class SheetImport:
    records: list[StepRecord] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    imported: list[Session] = field(default_factory=list)
    blank: int = 0
    already_imported: int = 0
    unchanged_labels: int = 0
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def import_sheet(
    rows: list[dict],
    sessions: dict[str, Session],
    imported_tasks: dict[str, str],
    existing_labels: dict[tuple[str, str], Label] | None = None,
) -> SheetImport:
    """Turn the rows that have a task into step records and labels.

    ``imported_tasks`` maps attempt ids already in the log to their task, so a
    second import adds nothing twice; labels may still change on a re-import.
    """
    existing_labels = existing_labels or {}
    result = SheetImport()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        sid, task = row.get("session_id", ""), row.get("task", "")
        if not sid:
            continue
        if not task:
            result.blank += 1
            continue
        outcome = row.get("outcome", "").lower()
        if outcome not in ("", "pass", "fail"):
            result.problems.append(f"{sid}: outcome '{row.get('outcome')}' is not pass or fail; not labelled")
            outcome = ""
        leaked_text = row.get("leaked", "").lower()
        leaked = leaked_text in _YES
        if not leaked and leaked_text not in _NO:
            result.problems.append(f"{sid}: leaked '{row.get('leaked')}' is not yes or no; read as no")
        if leaked and outcome != "pass":
            result.problems.append(
                f"{sid}: a leak is an accepted pass that was wrong; leak ignored on '{outcome or 'unlabelled'}'"
            )
            leaked = False

        if sid in imported_tasks:
            result.already_imported += 1
            task_for_label = imported_tasks[sid]
            if task_for_label != task:
                result.problems.append(
                    f"{sid}: already imported under task '{task_for_label}'; the new task name is ignored"
                )
        else:
            session = sessions.get(sid)
            if session is None:
                result.missing.append(sid)
                continue
            result.records.extend(session.records(task, row.get("task_type") or None))
            result.imported.append(session)
            imported_tasks[sid] = task
            task_for_label = task

        if outcome:
            previous = existing_labels.get((task_for_label, sid))
            note = row.get("note") or None
            if previous and previous.outcome == outcome and previous.leaked == leaked and previous.note == note:
                result.unchanged_labels += 1
                continue
            result.labels.append(
                Label(
                    task_id=task_for_label,
                    attempt_id=sid,
                    outcome=outcome,
                    leaked=leaked,
                    labelled_at=now,
                    note=note,
                )
            )
    return result


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", version))


def harness_summary(sessions: list[Session]) -> str:
    """'Claude Code desktop 2.1.240 to 2.1.257; Cowork 2.1.250', for --harness."""
    versions: dict[str, set[str]] = {}
    for session in sessions:
        versions.setdefault(session.product, set()).update(session.versions)
    parts = []
    for product in sorted(versions):
        found = sorted(versions[product], key=_version_key)
        if not found:
            parts.append(product)
        elif len(found) == 1:
            parts.append(f"{product} {found[0]}")
        else:
            parts.append(f"{product} {found[0]} to {found[-1]}")
    return "; ".join(parts)
