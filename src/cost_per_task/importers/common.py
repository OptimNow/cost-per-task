"""Shared plumbing for importers: reading export files, dotted field lookup,
and turning per-call rows into StepRecords grouped by task and attempt."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..schema import StepRecord


class ImportError_(ValueError):
    pass


@dataclass
class ParsedCall:
    """One API call as an importer understood it, before task grouping."""

    model: str
    provider: str
    timestamp: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    latency_ms: int = 0
    tool_names: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    records: list[StepRecord]
    skipped: int
    warnings: list[str]


def read_rows(path: str | Path) -> list[dict]:
    """Read a CSV, JSON array, JSON object holding an array, or JSONL file."""
    file = Path(path)
    suffix = file.suffix.lower()
    text = file.read_text(encoding="utf-8")
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    raise ImportError_(f"{file}: expected a list of rows, or an object containing one")


def get_field(row: dict, dotted: str):
    """Look up ``a.b.c`` in a row; nested values stored as JSON strings (as CSV
    exports do) are decoded on the way."""
    current = row
    for part in dotted.split("."):
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except ValueError:
                return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def to_int(value) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def infer_provider(model: str) -> str:
    lowered = (model or "").lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if lowered.startswith("grok"):
        return "xai"
    if lowered.startswith("gemini"):
        return "google"
    return "unknown"


def rows_to_records(
    rows: list[dict],
    parse: Callable[[dict], ParsedCall | None],
    *,
    task_field: str,
    attempt_field: str,
    task_type: str | None,
) -> ImportResult:
    """Group parsed calls by (task, attempt), order them in time and number
    the steps. Rows without a model, usage, task or attempt are skipped."""
    grouped: dict[tuple[str, str], list[ParsedCall]] = defaultdict(list)
    skipped = 0
    missing_ids = 0
    for row in rows:
        task_id = get_field(row, task_field)
        attempt_id = get_field(row, attempt_field)
        call = parse(row)
        if call is None:
            skipped += 1
            continue
        if task_id in (None, "") or attempt_id in (None, ""):
            missing_ids += 1
            continue
        grouped[(str(task_id), str(attempt_id))].append(call)

    records: list[StepRecord] = []
    for (task_id, attempt_id), calls in grouped.items():
        calls.sort(key=lambda c: c.timestamp)
        for step_id, call in enumerate(calls, start=1):
            records.append(
                StepRecord(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    timestamp=call.timestamp,
                    provider=call.provider,
                    model=call.model,
                    input_tokens=call.input_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_write_tokens=call.cache_write_tokens,
                    reasoning_tokens=call.reasoning_tokens,
                    output_tokens=call.output_tokens,
                    tool_call_count=len(call.tool_names),
                    tool_names=call.tool_names,
                    latency_ms=call.latency_ms,
                    task_type=task_type,
                )
            )
    records.sort(key=lambda r: (r.task_id, r.attempt_id, r.step_id))

    warnings = []
    if skipped:
        warnings.append(f"{skipped} rows had no model or usage and were skipped")
    if missing_ids:
        warnings.append(
            f"{missing_ids} rows had no '{task_field}' or '{attempt_field}' value and were "
            "skipped; use --task-field / --attempt-field to point at the right columns"
        )
    return ImportResult(records=records, skipped=skipped + missing_ids, warnings=warnings)
