"""Task ids inferred from the environment: git branch, issue number, pull
request. The guess is marked as one all the way to the report, and an outcome
is never guessed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cost_per_task import cli
from cost_per_task.importers.claude_sessions import discover_sessions, import_sheet, read_sheet, write_sheet
from cost_per_task.labels import load_labels
from cost_per_task.schema import read_jsonl
from cost_per_task.taskid import issue_in_branch, project_name, source_of, suggest_task

RATES = {
    "input_per_mtok": 0.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 1_000_000.0,
}


@pytest.mark.parametrize(
    "branch, issue",
    [
        ("fix/123-login", "123"),
        ("123-fix", "123"),
        ("feature/issue-45-export", "45"),
        ("gh-77", "77"),
        ("release/0.6.0", None),
        ("release-0.6.0", None),
        ("2026-09-notes", None),
        ("claude/methodology-feedback-80db53", None),
        ("v2/thing", None),
        ("", None),
    ],
)
def test_issue_number_in_branch(branch, issue):
    assert issue_in_branch(branch) == issue


def test_suggestion_takes_the_strongest_signal():
    assert suggest_task(branch="fix/123-login", pr_number=9, project="shop") == ("shop/issue-123", "issue")
    assert suggest_task(branch="docs/quick-path", pr_number=9, project="shop") == ("shop/pr-9", "pr")
    assert suggest_task(branch="docs/quick-path", project="shop") == ("shop/docs/quick-path", "branch")
    assert suggest_task(branch="main", project="shop", day="2026-09-10") == ("shop/2026-09-10", "date")
    assert suggest_task(branch="main") is None
    assert source_of("shop/pr-9", ("shop/pr-9", "pr")) == "pr"
    assert source_of("checkout rewrite", ("shop/pr-9", "pr")) == "manual"
    assert source_of("anything", None) == "manual"


def test_project_name_sees_through_a_worktree():
    assert project_name("C:\\Users\\j\\GitHub\\shop\\.claude\\worktrees\\brave-otter-1a2b") == "shop"
    assert project_name("/home/j/shop/") == "shop"
    assert project_name("") == ""


def _assistant(sid, mid, ts, *, branch, pr=None, cwd="C:\\work\\shop"):
    entry = {
        "type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": "r" + mid,
        "entrypoint": "cli", "cwd": cwd, "version": "2.1.257", "gitBranch": branch,
        "message": {"id": mid, "model": "claude-fable-5", "role": "assistant",
                    "content": [{"type": "text", "text": "SECRET-ANSWER"}],
                    "usage": {"input_tokens": 1, "output_tokens": 10}},
    }
    if pr is not None:
        entry["prNumber"] = pr
    return entry


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "claude" / "projects" / "shop"
    root.mkdir(parents=True)
    sessions = {
        "A": [_assistant("A", "a1", "2026-09-10T08:00:00Z", branch="fix/123-login")],
        "B": [_assistant("B", "b1", "2026-09-11T08:00:00Z", branch="fix/123-login")],
        "C": [_assistant("C", "c1", "2026-09-12T08:00:00Z", branch="docs/guide", pr=9),
              {"type": "custom-title", "customTitle": "SECRET-TITLE", "sessionId": "C"}],
        "D": [_assistant("D", "d1", "2026-09-13T08:00:00Z", branch="main")],
    }
    for sid, lines in sessions.items():
        (root / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01", "models": {"claude-fable-5": RATES}}),
        encoding="utf-8",
    )
    return tmp_path, [("code", tmp_path / "claude" / "projects")], prices


def test_sheet_is_prefilled_and_keeps_what_the_user_typed(workspace):
    tmp_path, roots, _ = workspace
    sessions, _ = discover_sessions(roots)
    sheet = tmp_path / "sheet.csv"
    write_sheet(sheet, sessions)
    rows = {r["session_id"]: r for r in read_sheet(sheet)}
    assert (rows["A"]["task"], rows["A"]["task_source"]) == ("shop/issue-123", "issue")
    assert rows["A"]["task"] == rows["B"]["task"]  # two attempts at one task
    assert (rows["C"]["task"], rows["C"]["task_source"], rows["C"]["branch"]) == ("shop/pr-9", "pr", "docs/guide")
    assert rows["D"]["task_source"] == "date"
    assert all(r["outcome"] == "" for r in rows.values())  # never guessed

    # The user renames one task; a refresh keeps it and marks it as theirs.
    rows["C"]["task"] = "guide rewrite"
    write_sheet(sheet, sessions, existing=rows)
    again = {r["session_id"]: r for r in read_sheet(sheet)}
    assert (again["C"]["task"], again["C"]["task_source"]) == ("guide rewrite", "manual")
    assert again["A"]["task_source"] == "issue"

    write_sheet(tmp_path / "bare.csv", sessions, infer=False)
    assert all(r["task"] == "" for r in read_sheet(tmp_path / "bare.csv"))


def test_a_suggested_task_needs_an_outcome_to_be_imported(workspace):
    tmp_path, roots, _ = workspace
    sessions, _ = discover_sessions(roots)
    sheet = tmp_path / "sheet.csv"
    write_sheet(sheet, sessions)
    rows = read_sheet(sheet)
    for row in rows:
        if row["session_id"] in ("A", "B"):
            row["outcome"] = "pass" if row["session_id"] == "B" else "fail"
        if row["session_id"] == "D":
            row["task"] = "typed by hand"  # no outcome, yet stated: imports as before

    result = import_sheet(rows, sessions, {})
    assert sorted(s.session_id for s in result.imported) == ["A", "B", "D"]
    assert result.inferred_only == 1  # C: suggested, never looked at
    sources = {r.attempt_id: r.task_source for r in result.records}
    assert sources == {"A": "issue", "B": "issue", "D": "manual"}
    assert {(l.attempt_id, l.outcome) for l in result.labels} == {("A", "fail"), ("B", "pass")}

    everything = import_sheet(rows, sessions, {}, include_unlabelled=True)
    assert len(everything.imported) == 4


def test_cli_flow_reports_how_tasks_were_identified(workspace, capsys, monkeypatch):
    tmp_path, _, prices = workspace
    monkeypatch.chdir(tmp_path)
    scan = ["--path", str(tmp_path / "claude" / "projects")]
    assert cli.main(["sessions", "list", "--prices", str(prices), *scan]) == 0
    assert "pre-filled" in capsys.readouterr().out
    sheet = tmp_path / "sessions.csv"
    text = sheet.read_text(encoding="utf-8-sig")
    assert text.count("shop/issue-123,issue,,") == 2
    text = text.replace("shop/issue-123,issue,,", "shop/issue-123,issue,,pass")
    sheet.write_text(text, encoding="utf-8-sig")

    assert cli.main(["sessions", "import", *scan]) == 0
    out = capsys.readouterr().out
    assert "imported 2 sessions" in out
    assert "left out: 2 sessions with a suggested task and no outcome" in out
    log = (tmp_path / "cpt-log.jsonl").read_text(encoding="utf-8")
    assert "SECRET-TITLE" not in log and "SECRET-ANSWER" not in log

    assert cli.main(["report", "--prices", str(prices), "--resamples", "10", "--seed", "1"]) == 0
    assert "task identity: 0 tasks stated by hand, 1 inferred (issue 1)" in capsys.readouterr().out


def test_report_on_a_log_without_task_source_says_stated_by_hand(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({
        "task_id": "T1", "attempt_id": "a1", "step_id": 1, "timestamp": "2026-09-10T08:00:00+00:00",
        "provider": "anthropic", "model": "claude-fable-5", "output_tokens": 5,
    }) + "\n", encoding="utf-8")
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01", "models": {"claude-fable-5": RATES}}),
        encoding="utf-8",
    )
    labels = tmp_path / "labels.jsonl"
    labels.write_text("", encoding="utf-8")
    assert cli.main(["report", "--log", str(log), "--labels", str(labels), "--prices", str(prices),
                     "--resamples", "10"]) == 0
    assert "task identity: 1 tasks, all stated by hand" in capsys.readouterr().out


def test_run_infers_the_task_and_labels_from_the_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "current_branch", lambda: "fix/123-login")
    log, labels = tmp_path / "log.jsonl", tmp_path / "labels.jsonl"
    # The command makes no model call, so nothing is captured and nothing is labelled.
    code = cli.main(["run", "--log", str(log), "--labels", str(labels), "--label-from-exit",
                     "--", sys.executable, "-c", "raise SystemExit(3)"])
    out = capsys.readouterr().out
    assert code == 3
    assert f"task id inferred from the git issue: {tmp_path.name}/issue-123" in out
    assert not labels.exists()

    monkeypatch.setattr(cli, "current_branch", lambda: "main")
    assert cli.main(["run", "--log", str(log), "--", sys.executable, "-c", "pass"]) == 2
    assert "pass --task-id" in capsys.readouterr().err


def test_run_labels_a_captured_attempt_from_the_exit_code(fake_upstream, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    log, labels = tmp_path / "log.jsonl", tmp_path / "labels.jsonl"
    client = (
        "import json, os, sys, urllib.request\n"
        "req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',\n"
        "    data=json.dumps({'model': 'claude-sonnet-5', 'messages': []}).encode(),\n"
        "    headers={'Content-Type': 'application/json', 'x-api-key': 'sk-test-DUMMY-KEY'}, method='POST')\n"
        "urllib.request.urlopen(req).read()\n"
        "sys.exit(1)\n"
    )
    code = cli.main(["run", "--task-id", "T9", "--attempt-id", "a9", "--log", str(log), "--labels", str(labels),
                     "--anthropic-upstream", fake_upstream, "--label-from-exit", "--", sys.executable, "-c", client])
    assert code == 1
    assert read_jsonl(log)[0].task_source is None  # stated by hand
    label = load_labels(labels)[("T9", "a9")]
    assert (label.outcome, label.note) == ("fail", "from exit code 1")
