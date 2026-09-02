"""JSON output and the MCP tool functions, exercised through the CLI and the
shared analysis loader on a small synthetic log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_per_task import cli, mcp_server
from cost_per_task.labels import Label, append_label
from cost_per_task.schema import JsonlWriter, StepRecord

UNIT_RATES = {
    "input_per_mtok": 1_000_000.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 0.0,
}


@pytest.fixture
def workspace(tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01", "models": {"model-a": UNIT_RATES, "model-b": UNIT_RATES}}),
        encoding="utf-8",
    )
    log = tmp_path / "log.jsonl"
    writer = JsonlWriter(log)
    rows = [
        ("t1", "a1", "model-a", 1), ("t1", "a2", "model-a", 3),
        ("t2", "a1", "model-a", 2), ("t3", "b1", "model-b", 5), ("t4", "b1", "model-b", 5),
    ]
    for i, (task, attempt, model, cost) in enumerate(rows):
        writer.append(
            StepRecord(task_id=task, attempt_id=attempt, step_id=1, timestamp=f"2026-09-01T10:0{i}:00+00:00",
                       provider="test", model=model, input_tokens=cost)
        )
    labels = tmp_path / "labels.jsonl"
    for task, attempt, outcome, leaked in (
        ("t1", "a1", "fail", False), ("t1", "a2", "pass", False), ("t2", "a1", "pass", True),
        ("t3", "b1", "pass", False), ("t4", "b1", "pass", False),
    ):
        append_label(labels, Label(task, attempt, outcome, leaked=leaked))
    return {"log": str(log), "labels": str(labels), "prices": str(prices)}


def test_report_json_output(workspace, capsys):
    code = cli.main(["report", "--log", workspace["log"], "--labels", workspace["labels"],
                     "--prices", workspace["prices"], "--json", "--cleanup-cost", "10", "--resamples", "50", "--seed", "1"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prices"]["as_of"] == "2026-09-01"
    assert payload["attempts"] == 5
    a = next(g for g in payload["groups"] if g["model"] == "model-a")
    # model-a: costs 1, 3, 2 -> mean 2; passes 2 of 3 -> CPT 3; leaks 1 of 2 -> L 0.5
    assert a["mean_cost"] == pytest.approx(2.0)
    assert a["cpt_solved"] == pytest.approx(3.0)
    assert a["leak_rate"] == pytest.approx(0.5)
    assert a["cpt_risk"] == pytest.approx(8.0)
    assert isinstance(a["success_interval"], list)


def test_compare_json_output(workspace, capsys):
    code = cli.main(["compare", "model-a", "model-b", "--log", workspace["log"], "--labels", workspace["labels"],
                     "--prices", workspace["prices"], "--json", "--cleanup-cost", "10", "--resamples", "50"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # K* = (5 - 3) / (0.5 - 0) = 4
    assert payload["comparison"]["break_even_cleanup_cost"] == pytest.approx(4.0)


def test_compare_rejects_ambiguous_model(workspace, capsys):
    code = cli.main(["compare", "model", "model-b", "--log", workspace["log"], "--labels", workspace["labels"],
                     "--prices", workspace["prices"], "--resamples", "10"])
    assert code == 1
    assert "matches 2 models" in capsys.readouterr().err


def test_mcp_tool_functions(workspace):
    report = mcp_server.tool_report(log=workspace["log"], prices=workspace["prices"], labels=workspace["labels"],
                                    cleanup_cost=10.0, resamples=20, seed=1)
    assert {g["model"] for g in report["groups"]} == {"model-a", "model-b"}

    compare = mcp_server.tool_compare("model-a", "model-b", log=workspace["log"], prices=workspace["prices"],
                                      labels=workspace["labels"], cleanup_cost=10.0, resamples=20)
    assert compare["break_even_cleanup_cost"] == pytest.approx(4.0)

    denominator = mcp_server.tool_risk_denominator("model-a", 10.0, log=workspace["log"],
                                                   prices=workspace["prices"], labels=workspace["labels"])
    assert denominator["cpt_risk"] == pytest.approx(8.0)
    assert denominator["prices_as_of"] == "2026-09-01"
    assert denominator["success_interval_95"][0] < 2 / 3 < denominator["success_interval_95"][1]


def test_build_server_registers_tools_when_sdk_present():
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    assert hasattr(server, "run")


def test_import_and_prices_cli(tmp_path, capsys):
    export = tmp_path / "spend.jsonl"
    export.write_text(json.dumps({"request_id": "r1", "model": "gpt-5.4", "prompt_tokens": 10, "completion_tokens": 2,
                                  "startTime": "2026-09-01T10:00:00Z", "metadata": {"task_id": "t1"}, "session_id": "s1"}) + "\n",
                      encoding="utf-8")
    log = tmp_path / "log.jsonl"
    assert cli.main(["import", "litellm", str(export), "--log", str(log), "--dry-run"]) == 0
    assert not log.exists()
    assert cli.main(["import", "litellm", str(export), "--log", str(log)]) == 0
    assert "appended 1 steps" in capsys.readouterr().out
    assert log.exists()

    hub = str(Path(__file__).parent / "fixtures" / "hub_sample.json")
    out = tmp_path / "anthropic.json"
    assert cli.main(["prices", "refresh", "--provider", "anthropic", "--from-file", hub, "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "does not exist yet" in captured.out
    assert "dry run" in captured.out
    assert "suspicious Claude Fable 5.1" in captured.err
    assert not out.exists()
    assert cli.main(["prices", "refresh", "--provider", "anthropic", "--from-file", hub, "--out", str(out), "--write"]) == 0
    assert out.exists()
    assert cli.main(["prices", "refresh", "--provider", "anthropic", "--from-file", hub, "--out", str(out)]) == 0
    assert "no changes" in capsys.readouterr().out
