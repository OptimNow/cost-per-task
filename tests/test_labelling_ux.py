"""The weekly labelling habit: the terminal labeller, --new, the sheet's
status and order, forgiving outcome words, and the table of tasks."""

from __future__ import annotations

import csv
import io
import json

import pytest

from cost_per_task import cli
from cost_per_task.importers.claude_sessions import (
    SHEET_COLUMNS,
    discover_sessions,
    import_sheet,
    read_sheet,
    select_new,
    write_sheet,
)
from cost_per_task.labels import LabelError, import_csv, load_labels, normalise_outcome
from cost_per_task.mcp_server import tool_report
from cost_per_task.schema import read_jsonl

# Output tokens cost 1 USD each, everything else is free: a session of 10 tokens costs 10 USD.
RATES = {
    "input_per_mtok": 0.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 1_000_000.0,
}


def _call(sid, mid, ts, *, branch, pr=None, out=10):
    entry = {
        "type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": "r" + mid,
        "entrypoint": "cli", "cwd": "C:\\work\\shop", "version": "2.1.257", "gitBranch": branch,
        "message": {"id": mid, "model": "claude-fable-5", "role": "assistant",
                    "content": [{"type": "text", "text": "SECRET-ANSWER"}],
                    "usage": {"input_tokens": 1, "output_tokens": out}},
    }
    if pr is not None:
        entry["prNumber"] = pr
    return entry


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """Four sessions on four days: A and B on one issue branch, C on a pull request with a
    title, D on main. A ran for 25 minutes over two calls."""
    root = tmp_path / "claude" / "projects" / "shop"
    root.mkdir(parents=True)
    sessions = {
        "A": [_call("A", "a1", "2026-09-10T08:00:00Z", branch="fix/123-login", out=4),
              _call("A", "a2", "2026-09-10T08:25:30Z", branch="fix/123-login", out=6)],
        "B": [_call("B", "b1", "2026-09-11T08:00:00Z", branch="fix/123-login")],
        "C": [_call("C", "c1", "2026-09-12T08:00:00Z", branch="docs/guide", pr=9),
              {"type": "custom-title", "customTitle": "SECRET-TITLE", "sessionId": "C"}],
        "D": [_call("D", "d1", "2026-09-13T08:00:00Z", branch="main")],
    }
    for sid, lines in sessions.items():
        (root / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01", "models": {"claude-fable-5": RATES}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    scan = ["--path", str(tmp_path / "claude" / "projects"), "--prices", str(prices)]
    return tmp_path, scan


def _type(monkeypatch, *lines):
    """Feed the labeller what a person would type; the input ends after the last line."""
    answers = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


# --- forgiving outcome words ---------------------------------------------------


@pytest.mark.parametrize("typed, outcome", [
    ("pass", "pass"), ("PASS ", "pass"), ("ok", "pass"), ("Oui", "pass"), ("yes", "pass"), ("réussi", "pass"),
    ("fail", "fail"), ("KO", "fail"), ("non", "fail"), ("failed", "fail"), ("échec", "fail"),
    ("", ""), (None, ""), ("maybe", None), ("x", None), ("1", None),
])
def test_outcome_words(typed, outcome):
    assert normalise_outcome(typed) == outcome


def test_sheet_import_understands_ok_and_ko_and_names_the_row(shop):
    tmp_path, _ = shop
    sessions, _ = discover_sessions([("code", tmp_path / "claude" / "projects")])
    rows = [
        {"session_id": "A", "task": "t", "outcome": "OK"},
        {"session_id": "B", "task": "t", "outcome": "ko"},
        {"session_id": "C", "task": "t", "outcome": "maybe"},
    ]
    result = import_sheet(rows, sessions, {})
    assert [(l.attempt_id, l.outcome) for l in result.labels] == [("A", "pass"), ("B", "fail")]
    assert len(result.problems) == 1
    assert result.problems[0].startswith("row 4 (C): outcome 'maybe' is not pass or fail")


def test_label_csv_import_is_all_or_nothing(tmp_path):
    source = tmp_path / "labels.csv"
    source.write_text("task_id,attempt_id,outcome\nT1,a1,ok\nT1,a2,perhaps\n", encoding="utf-8")
    target = tmp_path / "labels.jsonl"
    with pytest.raises(LabelError, match="row 3"):
        import_csv(source, target)
    assert not target.exists()
    source.write_text("task_id,attempt_id,outcome\nT1,a1,ok\nT1,a2,KO\n", encoding="utf-8")
    assert import_csv(source, target) == 2
    assert {l.outcome for l in load_labels(target).values()} == {"pass", "fail"}


# --- the sheet: order, status, minutes, --min-calls, --new ---------------------


def test_sheet_puts_what_is_left_to_do_first(shop):
    tmp_path, _ = shop
    sessions, _ = discover_sessions([("code", tmp_path / "claude" / "projects")])
    sheet = tmp_path / "sessions.csv"
    write_sheet(sheet, sessions)

    header = next(csv.reader(io.StringIO(sheet.read_text(encoding="utf-8-sig").split("\n", 1)[1])))
    assert header == SHEET_COLUMNS
    assert header[0] == "status" and header[-1] == "session_id"
    assert header.index("title") < header.index("task") < header.index("outcome") < header.index("models")

    rows = read_sheet(sheet)
    assert [r["session_id"] for r in rows] == ["D", "C", "B", "A"]  # all new: newest first
    assert {r["status"] for r in rows} == {"new"}
    assert {r["session_id"]: r["minutes"] for r in rows} == {"A": "25", "B": "0", "C": "0", "D": "0"}

    # The user gives B an outcome; A is imported by another route; a refresh re-sorts.
    existing = {r["session_id"]: r for r in rows}
    existing["B"]["outcome"] = "pass"
    write_sheet(sheet, sessions, existing=existing, imported={"A"})
    again = read_sheet(sheet)
    assert [(r["session_id"], r["status"]) for r in again] == [
        ("D", "open"), ("C", "open"), ("B", "labelled"), ("A", "imported"),
    ]


def test_select_new_is_what_happened_after_the_last_session_in_the_log(shop):
    tmp_path, _ = shop
    sessions, _ = discover_sessions([("code", tmp_path / "claude" / "projects")])
    fresh, cutoff = select_new(sessions, set())
    assert sorted(fresh) == ["A", "B", "C", "D"] and cutoff == ""
    fresh, cutoff = select_new(sessions, {"B"})
    assert sorted(fresh) == ["C", "D"]  # A is older than B: last week's business
    assert cutoff.startswith("2026-09-11T08:00:00")


def test_list_new_writes_a_short_sheet_and_keeps_pending_entries(shop, capsys):
    tmp_path, scan = shop
    assert cli.main(["sessions", "list", *scan]) == 0
    assert "4 sessions are not in the log yet" in capsys.readouterr().out
    sheet = tmp_path / "sessions.csv"

    # The user judges B and leaves a note on A without an outcome; B gets imported.
    text = sheet.read_text(encoding="utf-8-sig")
    rows = read_sheet(sheet)
    for row in rows:
        if row["session_id"] == "B":
            row["outcome"] = "ok"
        if row["session_id"] == "A":
            row["note"] = "to check with the team"
    sessions, _ = discover_sessions([("code", tmp_path / "claude" / "projects")])
    write_sheet(sheet, sessions, existing={r["session_id"]: r for r in rows})
    assert cli.main(["sessions", "import", *scan[:2]]) == 0
    capsys.readouterr()

    assert cli.main(["sessions", "list", "--new", *scan]) == 0
    out = capsys.readouterr().out
    assert "2 new sessions since" in out
    assert "--new: 3 rows of the previous sheet were left out" in out
    short = {r["session_id"]: r for r in read_sheet(sheet)}
    assert sorted(short) == ["A", "C", "D"]  # C and D are new; A is older but carries a note
    assert short["A"]["note"] == "to check with the team" and short["A"]["status"] == "open"
    assert text  # the first sheet existed


def test_min_calls_leaves_tiny_sessions_out(shop, capsys):
    tmp_path, scan = shop
    assert cli.main(["sessions", "list", "--min-calls", "2", *scan]) == 0
    assert [r["session_id"] for r in read_sheet(tmp_path / "sessions.csv")] == ["A"]


# --- the terminal labeller -----------------------------------------------------


def test_labeller_end_to_end(shop, monkeypatch, capsys):
    tmp_path, scan = shop
    _type(
        monkeypatch,
        "t", "login fix", "y", "coding", "ko",   # A: renamed, typed, failed (a word works as well as a key)
        "m", "p",                                # B: same task as the previous one, passed
        "n", "needed a redo later", "l",         # C: a note, then a leak
        "?", "s",                                # D: help, then skipped
    )
    assert cli.main(["sessions", "label", *scan]) == 0
    captured = capsys.readouterr()
    assert "4 sessions without an outcome, oldest first" in captured.out
    assert "[1/4]  shop" in captured.out and "25 min" in captured.out and "10.00 USD" in captured.out
    assert "task: shop/issue-123 (issue)" in captured.out
    assert "task: login fix (manual)" in captured.out
    assert "title: SECRET-TITLE" in captured.out  # on screen only
    assert "labelled 3 of 4 sessions (pass 2, fail 1, leaked 1); 4 model calls added" in captured.out
    assert "python -m cost_per_task.cli report" in captured.out

    records = read_jsonl(tmp_path / "cpt-log.jsonl")
    assert {(r.attempt_id, r.task_id, r.task_source, r.task_type) for r in records} == {
        ("A", "login fix", "manual", "coding"),
        ("B", "login fix", "manual", None),
        ("C", "shop/pr-9", "pr", None),
    }
    labels = load_labels(tmp_path / "cpt-labels.jsonl")
    assert labels[("login fix", "A")].outcome == "fail"
    assert labels[("login fix", "B")].outcome == "pass"
    leak = labels[("shop/pr-9", "C")]
    assert (leak.outcome, leak.leaked, leak.note) == ("pass", True, "needed a redo later")
    for file in ("cpt-log.jsonl", "cpt-labels.jsonl"):
        text = (tmp_path / file).read_text(encoding="utf-8")
        assert "SECRET-TITLE" not in text and "SECRET-ANSWER" not in text
    assert not (tmp_path / "sessions.csv").exists()  # no spreadsheet involved

    # A second run only has D left; quitting writes nothing; titles can be hidden.
    _type(monkeypatch, "q")
    assert cli.main(["sessions", "label", "--no-titles", *scan]) == 0
    out = capsys.readouterr().out
    assert "1 sessions without an outcome" in out and "SECRET-TITLE" not in out
    assert "labelled 0 of 1 sessions, so nothing was written" in out
    assert len(read_jsonl(tmp_path / "cpt-log.jsonl")) == 4

    # D is newer than everything in the log, so --new still offers it; input ending is a quit.
    _type(monkeypatch)
    assert cli.main(["sessions", "label", "--new", *scan]) == 0
    assert "1 new sessions since" in capsys.readouterr().out


def test_labeller_new_skips_what_is_older_than_the_log(shop, monkeypatch, capsys):
    tmp_path, scan = shop
    _type(monkeypatch, "s", "s", "s", "pass")  # only D, the latest, gets an outcome
    assert cli.main(["sessions", "label", *scan]) == 0
    capsys.readouterr()
    _type(monkeypatch)
    assert cli.main(["sessions", "label", "--new", *scan]) == 0
    assert "nothing to label among the new sessions" in capsys.readouterr().out


def test_labeller_needs_a_task_name_when_nothing_is_suggested(shop, monkeypatch, capsys):
    tmp_path, scan = shop
    _type(monkeypatch, "p", "t", "release prep", "p", "q")
    assert cli.main(["sessions", "label", "--no-infer", *scan]) == 0
    out = capsys.readouterr().out
    assert "give the task a name first (t)" in out
    assert [(r.task_id, r.task_source) for r in read_jsonl(tmp_path / "cpt-log.jsonl")][0] == (
        "release prep", "manual")


# --- the table of tasks --------------------------------------------------------


def test_task_table_in_the_report_and_on_its_own(shop, monkeypatch, capsys):
    tmp_path, scan = shop
    _type(monkeypatch, "t", "login fix", "f", "m", "p", "l", "s")
    assert cli.main(["sessions", "label", *scan]) == 0
    capsys.readouterr()
    prices = scan[-1]

    assert cli.main(["tasks", "--prices", prices]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "tasks, most expensive first" in out
    row = next(line for line in lines if line.startswith("login fix"))
    # two attempts, one pass, one fail, no leak, none open, 20 USD in all, 20 to the first pass
    assert row.split()[2:] == ["2", "1", "1", "0", "0", "20.0000", "20.0000", "manual"]
    assert lines.index(row) < lines.index(next(line for line in lines if line.startswith("shop/pr-9")))
    assert "2 tasks: 2 solved, 0 failed so far, 0 waiting for an outcome" in out

    assert cli.main(["report", "--prices", prices, "--resamples", "10", "--seed", "1"]) == 0
    report = capsys.readouterr().out
    assert report.index("tasks, most expensive first") < report.index("group:")

    assert cli.main(["tasks", "--prices", prices, "--json"]) == 0
    tasks = {t["task_id"]: t for t in json.loads(capsys.readouterr().out)["tasks"]}
    assert tasks["login fix"]["cost_to_first_pass"] == pytest.approx(20.0)
    assert tasks["shop/pr-9"]["leaks"] == 1 and tasks["shop/pr-9"]["source"] == "pr"


def test_mcp_tools_still_return_no_task_ids(shop, monkeypatch, capsys):
    """SECURITY.md promises aggregates only over MCP: the task table stays out of it."""
    tmp_path, scan = shop
    _type(monkeypatch, "p", "p", "p", "p")
    assert cli.main(["sessions", "label", *scan]) == 0
    result = tool_report(log=str(tmp_path / "cpt-log.jsonl"), labels=str(tmp_path / "cpt-labels.jsonl"),
                         prices=scan[-1], resamples=10)
    assert "tasks" not in result
    assert "shop/issue-123" not in json.dumps(result)


# --- a first run in an empty folder ----------------------------------------------


@pytest.mark.parametrize("command", [["tasks"], ["report"], ["compare", "a", "b"], ["explain"], ["label", "pass"]])
def test_commands_say_how_a_log_comes_to_be_when_there_is_none(tmp_path, monkeypatch, capsys, command):
    """Seen in a first test of the labeller: every session skipped, then cpt tasks answered
    with the operating system's Errno 2."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(command) == 1
    err = capsys.readouterr().err
    assert "there is no usage log yet: cpt-log.jsonl does not exist in this folder" in err
    assert "cpt sessions label" in err and "--log PATH" in err
    assert "Errno" not in err
