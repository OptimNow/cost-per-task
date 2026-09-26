"""The Claude Code and Cowork transcript importer, on synthetic transcripts
laid out exactly like the real folders."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cost_per_task import cli
from cost_per_task.importers.claude_sessions import (
    _period_key,
    discover_sessions,
    harness_summary,
    import_sheet,
    read_sheet,
    render_sessions_summary,
    roots_for_paths,
    summarise_sessions,
    write_sheet,
)
from cost_per_task.pricing import PricingTable
from cost_per_task.schema import read_jsonl

SECRETS = ("CLIENT-SECRET-PROMPT", "SECRET-ANSWER", "TOOL-INPUT-SECRET", "Fix invoice bug")
MODELS = ("claude-fable-5", "claude-fable-5-1", "claude-haiku-4-5", "claude-sonnet-5")

# Output tokens cost 1 USD each, everything else is free: costs read as token counts.
RATES = {
    "input_per_mtok": 0.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 1_000_000.0,
}


def _assistant(sid, mid, rid, model, out, ts, *, tools=(), speed="standard", entry="claude-desktop",
               version="2.1.257", effort="xhigh", cwd="C:\\work\\proj", sidechain=False):
    content = [{"type": "tool_use", "name": t, "input": {"cmd": "TOOL-INPUT-SECRET"}} for t in tools]
    content = content or [{"type": "text", "text": "SECRET-ANSWER"}]
    return {
        "type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": rid, "entrypoint": entry,
        "cwd": cwd, "version": version, "effort": effort, "isSidechain": sidechain,
        "message": {
            "id": mid, "model": model, "role": "assistant", "content": content,
            "usage": {
                "input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
                "cache_creation": {"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 0},
                "speed": speed, "service_tier": "standard", "inference_geo": "not_available",
            },
        },
    }


def _write(path: Path, lines) -> None:
    """Strings are written as raw lines, anything else as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    path.write_text(text + "\n", encoding="utf-8")


def _touch(path: Path, day: str) -> None:
    """Set the file's last-modified time to local noon on that day."""
    stamp = datetime.fromisoformat(f"{day}T12:00:00").timestamp()
    os.utime(path, (stamp, stamp))


def _prices(path: Path) -> Path:
    path.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-13", "models": {m: RATES for m in MODELS}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def roots(tmp_path):
    code = tmp_path / "claude" / "projects"
    _write(code / "proj" / "S1.jsonl", [
        {"type": "user", "sessionId": "S1", "timestamp": "2026-09-10T08:00:00.000Z",
         "message": {"role": "user", "content": "CLIENT-SECRET-PROMPT"}},
        {"type": "custom-title", "customTitle": "Fix invoice bug", "sessionId": "S1"},
        # one response written on two lines: the output count grows, tools differ per line
        _assistant("S1", "m1", "r1", "claude-fable-5", 5, "2026-09-10T08:00:05.000Z", tools=["Read"]),
        _assistant("S1", "m1", "r1", "claude-fable-5", 120, "2026-09-10T08:00:06.000Z", tools=["Edit"]),
        _assistant("S1", "m2", "r2", "claude-fable-5-1", 50, "2026-09-10T08:01:00.000Z", speed="fast"),
        {"type": "assistant", "sessionId": "S1", "timestamp": "2026-09-10T08:02:00.000Z",
         "message": {"id": "x", "model": "<synthetic>", "usage": {"output_tokens": 0}}},
        "not json at all",
    ])
    _write(code / "proj" / "S1" / "subagents" / "agent-a1.jsonl", [
        _assistant("S1", "m3", "r3", "claude-fable-5", 30, "2026-09-10T08:03:00.000Z", sidechain=True),
    ])
    _write(code / "proj" / "S2.jsonl", [
        _assistant("S2", "m4", "r4", "claude-haiku-4-5", 10, "2026-08-01T09:00:00.000Z",
                   entry="cli", version="2.1.200"),
        # history copied from S1 on resume: must not be counted twice
        _assistant("S2", "m1", "r1", "claude-fable-5", 120, "2026-08-01T09:00:01.000Z", entry="cli"),
    ])
    cowork = tmp_path / "Claude" / "local-agent-mode-sessions"
    folder = cowork / "acct" / "org" / "local_abc"
    _write(folder / ".claude" / "projects" / "p" / "T1.jsonl", [
        _assistant("T1", "c1", "q1", "claude-sonnet-5", 40, "2026-09-11T10:00:00.000Z",
                   entry="local-agent", cwd="/sessions/x/outputs"),
    ])
    _write(folder / ".claude" / "projects" / "p" / "T2.jsonl", [
        _assistant("T2", "c2", "q2", "claude-sonnet-5", 60, "2026-09-11T10:05:00.000Z", entry="local-agent"),
    ])
    _write(folder / "audit.jsonl", [{"message": {"model": "claude-sonnet-5", "usage": {"output_tokens": 999}}}])
    (folder / "outputs").mkdir(parents=True)
    (folder / "outputs" / "report.docx").write_text("x", encoding="utf-8")
    return roots_for_paths([str(code), str(cowork)])


@pytest.fixture
def table(tmp_path):
    return PricingTable.load(_prices(tmp_path / "prices.json"))


def test_discovery_dedupes_folds_subagents_and_groups_cowork(roots, table):
    sessions, stats = discover_sessions(roots)
    assert set(sessions) == {"S1", "S2", "local_abc"}
    s1 = sessions["S1"]
    assert s1.product == "Claude Code desktop"
    assert s1.project == "proj"
    assert s1.title == "Fix invoice bug"
    assert len(s1.calls) == 3  # m1 once, m2, and m3 from the sub-agent
    m1 = max(s1.calls.values(), key=lambda c: c.output_tokens)
    assert m1.output_tokens == 120 and m1.tool_names == ["Read", "Edit"] and m1.effort == "xhigh"
    assert s1.cost(table) == (200.0, 0)
    assert s1.main_model(table) == "claude-fable-5"
    assert sessions["S2"].product == "Claude Code CLI"
    assert len(sessions["S2"].calls) == 1  # the copied m1 stays with S1
    cowork = sessions["local_abc"]
    assert cowork.product == "Cowork" and len(cowork.calls) == 2
    assert cowork.title == "outputs: report.docx"
    assert cowork.cost(table) == (100.0, 0)  # audit.jsonl ignored
    assert stats["fast mode responses"] == 1
    fast_call = next(c for c in s1.calls.values() if c.model == "claude-fable-5-1")
    assert fast_call.speed == "fast" and m1.speed == "standard"
    assert [r.speed for r in s1.records("-") if r.model == "claude-fable-5-1"] == ["fast"]
    assert stats["unreadable lines"] == 1
    assert stats["local messages skipped"] == 1
    assert stats["responses already counted in another session"] == 1


def test_titles_can_be_left_out_and_since_filters(roots):
    sessions, _ = discover_sessions(roots, include_titles=False, since=date(2026, 9, 1))
    assert set(sessions) == {"S1", "local_abc"}
    assert all(s.title == "" for s in sessions.values())


def test_since_keeps_whole_sessions_whatever_the_file_dates(tmp_path, table, capsys):
    """The case seen on 14 September 2026: a session started on the 12th, still
    active on the 14th, whose sub-agent transcripts were last written on the
    12th. With --since, each session listed has the calls the import finds."""
    code = tmp_path / "claude" / "projects"
    main = code / "proj" / "L1.jsonl"
    _write(main, [
        _assistant("L1", "l1", "r1", "claude-sonnet-5", 10, "2026-09-12T09:00:00.000Z"),
        _assistant("L1", "l2", "r2", "claude-sonnet-5", 10, "2026-09-14T12:00:00.000Z"),
    ])
    agents = [code / "proj" / "L1" / "subagents" / f"agent-a{n}.jsonl" for n in (1, 2)]
    for n, agent in enumerate(agents, start=1):
        _write(agent, [_assistant("L1", f"s{n}", f"q{n}", "claude-fable-5-1", 100,
                                  f"2026-09-12T10:0{n}:00.000Z", sidechain=True)])
    # B0 resumed A0: the copied response h1 counts in A0, where it is found first
    _write(code / "proj" / "A0.jsonl", [
        _assistant("A0", "h1", "k1", "claude-fable-5", 40, "2026-09-12T08:00:00.000Z"),
    ])
    _write(code / "proj" / "B0.jsonl", [
        _assistant("B0", "h1", "k1", "claude-fable-5", 40, "2026-09-12T08:00:00.000Z"),
        _assistant("B0", "h2", "k2", "claude-fable-5", 5, "2026-09-14T12:30:00.000Z"),
    ])
    cowork = tmp_path / "Claude" / "local-agent-mode-sessions"
    folder = cowork / "acct" / "org" / "local_c" / ".claude" / "projects" / "p"
    _write(folder / "T1.jsonl", [
        _assistant("T1", "c1", "j1", "claude-sonnet-5", 60, "2026-09-12T11:00:00.000Z", entry="local-agent"),
    ])
    _write(folder / "T2.jsonl", [
        _assistant("T2", "c2", "j2", "claude-sonnet-5", 6, "2026-09-14T13:00:00.000Z", entry="local-agent"),
    ])
    for path in (*agents, code / "proj" / "A0.jsonl", folder / "T1.jsonl"):
        _touch(path, "2026-09-12")
    for path in (main, code / "proj" / "B0.jsonl", folder / "T2.jsonl"):
        _touch(path, "2026-09-14")
    roots = roots_for_paths([str(code), str(cowork)])

    everything, _ = discover_sessions(roots)
    listed, _ = discover_sessions(roots, since=date(2026, 9, 14))
    assert set(listed) == {"L1", "B0", "local_c"}
    for sid, session in listed.items():
        assert session.calls == everything[sid].calls, sid
    assert len(listed["L1"].calls) == 4 and listed["L1"].cost(table) == (220.0, 0)
    assert listed["L1"].main_model(table) == "claude-fable-5-1"
    assert len(listed["B0"].calls) == 1 and len(listed["local_c"].calls) == 2

    sheet = tmp_path / "s.csv"
    prices = _prices(tmp_path / "prices.json")
    args = ["sessions", "list", "--since", "2026-09-14", "--out", str(sheet), "--prices", str(prices),
            "--path", str(code), "--path", str(cowork)]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "3 sessions with model calls active on or after 2026-09-14, read from 7 transcripts" in out
    row = next(r for r in read_sheet(sheet) if r["session_id"] == "L1")
    assert (row["calls"], row["api_cost_usd"], row["main_model"]) == ("4", "220.00", "claude-fable-5-1")


@pytest.fixture
def east_of_utc(monkeypatch):
    """Local time two hours ahead of UTC, as in Paris in summer. Windows has no
    time.tzset: there the machine's own time zone applies."""
    if not hasattr(time, "tzset"):
        yield
        return
    monkeypatch.setenv("TZ", "CEST-2")  # POSIX sign convention: two hours east of UTC
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_since_is_a_local_date(tmp_path, east_of_utc):
    """--since counts days in local time, as the sheet shows them: a session
    ending half an hour after local midnight on the date is kept, one ending
    half an hour before it is not."""
    midnight = datetime(2026, 9, 14).astimezone()
    code = tmp_path / "projects"
    for sid, minutes in (("AFTER", 30), ("BEFORE", -30)):
        end = (midnight + timedelta(minutes=minutes)).astimezone(timezone.utc)
        stamp = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _write(code / "p" / f"{sid}.jsonl", [_assistant(sid, sid, sid, "claude-sonnet-5", 5, stamp)])
    sessions, _ = discover_sessions(roots_for_paths([str(code)]), since=date(2026, 9, 14))
    assert set(sessions) == {"AFTER"}


def test_sheet_round_trip_with_excel_semicolons_and_merge(roots, table, tmp_path):
    sessions, _ = discover_sessions(roots)
    sheet = tmp_path / "sessions.csv"
    assert write_sheet(sheet, sessions, table=table) == (3, 0)
    rows = read_sheet(sheet)
    assert [r["session_id"] for r in rows] == ["local_abc", "S1", "S2"]
    assert rows[1]["api_cost_usd"] == "200.00" and rows[1]["main_model"] == "claude-fable-5"

    # What French Excel saves: semicolons, the Windows code page, no sep line.
    header = ";".join(rows[0].keys())
    body = []
    for r in rows:
        if r["session_id"] == "S1":
            r = {**r, "task": "facture", "note": "déjà vu"}
        body.append(";".join(r.values()))
    sheet.write_bytes(("\r\n".join([header] + body) + "\r\n").encode("cp1252"))
    edited = read_sheet(sheet)
    assert next(r for r in edited if r["session_id"] == "S1")["note"] == "déjà vu"

    # Listing again keeps what was typed.
    existing = {r["session_id"]: r for r in edited}
    assert write_sheet(sheet, sessions, table=table, existing=existing) == (3, 1)
    assert next(r for r in read_sheet(sheet) if r["session_id"] == "S1")["task"] == "facture"


def test_import_writes_records_and_labels_once(roots):
    sessions, _ = discover_sessions(roots)
    rows = [
        {"session_id": "S1", "task": "invoice-fix", "task_type": "coding", "outcome": "pass",
         "leaked": "oui", "note": ""},
        {"session_id": "local_abc", "task": "report", "task_type": "writing", "outcome": "FAIL",
         "leaked": "", "note": "late"},
        {"session_id": "S2", "task": "", "outcome": "pass"},
        {"session_id": "gone", "task": "old", "outcome": "pass"},
        {"session_id": "S1", "task": "x", "outcome": "fail", "leaked": "yes"},
    ]
    imported: dict[str, str] = {}
    result = import_sheet(rows, sessions, imported)
    assert len(result.records) == 5  # 3 calls for S1, 2 for the Cowork session
    assert {r.task_type for r in result.records} == {"coding", "writing"}
    firsts = [(l.task_id, l.outcome, l.leaked) for l in result.labels][:2]
    assert firsts == [("invoice-fix", "pass", True), ("report", "fail", False)]
    assert result.blank == 1 and result.missing == ["gone"]
    assert any("already imported under task 'invoice-fix'" in p for p in result.problems)
    assert any("leak ignored" in p for p in result.problems)
    text = json.dumps([r.to_json() for r in result.records])
    for secret in SECRETS:
        assert secret not in text

    previous = {(l.task_id, l.attempt_id): l for l in result.labels[:2]}
    again = import_sheet(rows[:2], sessions, imported, previous)
    assert again.records == [] and again.already_imported == 2 and again.unchanged_labels == 2


def test_harness_summary(roots):
    sessions, _ = discover_sessions(roots)
    assert harness_summary(list(sessions.values())) == (
        "Claude Code CLI 2.1.200 to 2.1.257; Claude Code desktop 2.1.257; Cowork 2.1.257"
    )


def test_cli_list_import_report(roots, tmp_path, capsys):
    code_root, cowork_root = (str(p) for _, p in roots)
    prices = _prices(tmp_path / "prices.json")
    sheet, log, labels = tmp_path / "s.csv", tmp_path / "log.jsonl", tmp_path / "labels.jsonl"
    paths = ["--path", code_root, "--path", cowork_root]

    assert cli.main(["sessions", "list", "--out", str(sheet), "--prices", str(prices), *paths]) == 0
    captured = capsys.readouterr()
    assert "3 sessions" in captured.out and "shadow cost" in captured.out
    assert "fast mode" in captured.err

    rows = read_sheet(sheet)
    for r in rows:
        if r["session_id"] == "S1":
            r.update(task="invoice-fix", task_type="coding", outcome="pass")
        if r["session_id"] == "local_abc":
            r.update(task="report", task_type="coding", outcome="fail")
    write_sheet(sheet, {}, existing={r["session_id"]: r for r in rows})

    args = ["sessions", "import", str(sheet), "--log", str(log), "--labels", str(labels), *paths]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "imported 2 sessions" in out and "--harness" in out
    assert len(read_jsonl(log)) == 5
    assert cli.main(args) == 0
    assert "already in the log: 2" in capsys.readouterr().out
    assert len(read_jsonl(log)) == 5
    raw = log.read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in raw

    report_args = ["report", "--log", str(log), "--labels", str(labels), "--prices", str(prices),
                   "--resamples", "50"]
    assert cli.main(report_args) == 0
    report = capsys.readouterr().out
    assert "pass 1, fail 0" in report and "pass 0, fail 1" in report
    assert "effort settings: xhigh" in report


def test_empty_inference_geo_is_not_flagged(tmp_path):
    line = _assistant("S9", "g1", "q9", "claude-opus-4-7", 5, "2026-05-01T10:00:00.000Z")
    line["message"]["usage"]["inference_geo"] = ""
    _write(tmp_path / "projects" / "p" / "S9.jsonl", [line])
    _, stats = discover_sessions(roots_for_paths([str(tmp_path / "projects")]))
    assert stats["regional inference responses"] == 0


def test_summary_adds_up_by_product_model_and_period(roots, table):
    sessions, _ = discover_sessions(roots, include_titles=False)
    summary = summarise_sessions(sessions, table, period="month", top=2)
    assert (summary.sessions, summary.calls, summary.cost, summary.unpriced_calls) == (3, 6, 310.0, 0)
    assert summary.tokens == {"input": 60, "cache_read": 6000, "cache_write": 1200, "output": 310}
    assert summary.by_product == [
        ("Claude Code desktop", 1, 3, 200.0), ("Cowork", 1, 2, 100.0), ("Claude Code CLI", 1, 1, 10.0),
    ]
    assert summary.by_model[0] == ("claude-fable-5", 1, 2, 150.0)
    assert [m for m, *_ in summary.by_model] == [
        "claude-fable-5", "claude-sonnet-5", "claude-fable-5-1", "claude-haiku-4-5",
    ]
    assert summary.by_period == [("2026-08", 1, 1, 10.0), ("2026-09", 2, 5, 300.0)]
    assert [(s.session_id, cost) for s, cost in summary.top] == [("S1", 200.0), ("local_abc", 100.0)]

    assert _period_key("2026-09-10 08:00", "week") == "2026-W37"
    assert _period_key("2026-09-10 08:00", "day") == "2026-09-10"
    by_day = summarise_sessions(sessions, table, period="day", top=0)
    assert [p for p, *_ in by_day.by_period] == ["2026-08-01", "2026-09-10", "2026-09-11"]
    assert by_day.top == []
    with pytest.raises(ValueError):
        summarise_sessions(sessions, table, period="year")

    text = render_sessions_summary(summary, table, subscription=100.0)
    assert "3 sessions, 6 model calls" in text and "310.00 USD" in text and "shadow cost" in text
    assert "cache read 6,000 (82.6% of prompt tokens)" in text
    assert "Claude Code desktop" in text and "64.5%" in text
    assert "3.0x" in text and "0.1x" in text  # September and August over a 100 USD subscription
    assert "most expensive sessions (2)" in text and "S1" in text and "local_abc" in text
    weekly = summarise_sessions(sessions, table, period="week")
    assert "use --by month" in render_sessions_summary(weekly, table, subscription=100.0)
    assert "x subscription" not in render_sessions_summary(summary, table)


def test_cli_summary(roots, tmp_path, capsys):
    code_root, cowork_root = (str(p) for _, p in roots)
    prices = _prices(tmp_path / "prices.json")
    args = ["sessions", "summary", "--prices", str(prices), "--by", "day", "--top", "1",
            "--since", "2026-09-01", "--path", code_root, "--path", cowork_root]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "2 sessions with model calls active on or after 2026-09-01" in captured.out
    assert "by day" in captured.out and "2026-09-10" in captured.out
    assert "most expensive sessions (1)" in captured.out
    assert "fast mode" in captured.err
    nowhere = ["sessions", "summary", "--prices", str(prices), "--path", str(tmp_path / "nowhere")]
    assert cli.main(nowhere) == 1


def test_import_uses_the_shared_default_files_and_the_old_ones_if_present(
    roots, tmp_path, monkeypatch, capsys
):
    code_root, cowork_root = (str(p) for _, p in roots)
    paths = ["--path", code_root, "--path", cowork_root]
    sheet = tmp_path / "s.csv"
    row = {"session_id": "S1", "task": "invoice-fix", "outcome": "pass"}
    write_sheet(sheet, {}, existing={"S1": row})

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert cli.main(["sessions", "import", str(sheet), *paths]) == 0
    out = capsys.readouterr().out
    assert (fresh / "cpt-log.jsonl").exists() and (fresh / "cpt-labels.jsonl").exists()
    assert "report --seed 1" in out and "--log" not in out

    old = tmp_path / "old"
    old.mkdir()
    (old / "sessions-log.jsonl").touch()
    (old / "sessions-labels.jsonl").touch()
    monkeypatch.chdir(old)
    assert cli.main(["sessions", "import", str(sheet), *paths]) == 0
    captured = capsys.readouterr()
    assert "using sessions-log.jsonl from an earlier version" in captured.err
    assert "--log sessions-log.jsonl --labels sessions-labels.jsonl" in captured.out
    assert len(read_jsonl(old / "sessions-log.jsonl")) == 3
    assert not (old / "cpt-log.jsonl").exists()


def test_fast_mode_responses_are_priced_at_fast_rates_when_the_table_has_them(roots, tmp_path, capsys):
    sessions, _ = discover_sessions(roots)
    s1 = sessions["S1"]
    standard = PricingTable.load(_prices(tmp_path / "standard.json"))
    assert s1.cost(standard) == (200.0, 0)  # 120 + 50 + 30 output tokens at 1 USD each
    summary = summarise_sessions(sessions, standard)
    text = render_sessions_summary(summary, standard)
    assert "speed: 1 fast mode calls priced at standard rates, no fast rates in the table for claude-fable-5-1 (1)" in text

    data = json.loads(_prices(tmp_path / "fast.json").read_text(encoding="utf-8"))
    data["models"]["claude-fable-5-1"]["fast"] = {**RATES, "output_per_mtok": 2_000_000.0}
    path = tmp_path / "fast.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    fast = PricingTable.load(path)
    assert s1.cost(fast) == (250.0, 0)  # the 50 fast output tokens now cost 2 USD each
    text = render_sessions_summary(summarise_sessions(sessions, fast), fast)
    assert "speed: fast mode on 1 calls, priced at fast rates" in text
