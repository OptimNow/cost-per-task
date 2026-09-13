"""The shipped price tables apply when no --prices is given, from any folder."""

from __future__ import annotations

from pathlib import Path

from cost_per_task import cli, mcp_server
from cost_per_task.pricing import default_table_paths
from cost_per_task.schema import JsonlWriter, StepRecord


def _log_one_call(path: Path) -> None:
    JsonlWriter(path).append(
        StepRecord(
            task_id="t1",
            attempt_id="a1",
            step_id=1,
            timestamp="2026-09-13T10:00:00+00:00",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=100,
        )
    )


def test_both_shipped_tables_are_found():
    assert [Path(p).name for p in default_table_paths()] == ["anthropic.json", "openai.json"]


def test_found_from_another_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no prices/ folder here
    paths = default_table_paths()
    assert len(paths) == 2 and all(Path(p).is_file() for p in paths)


def test_report_without_prices_uses_shipped_tables(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _log_one_call(tmp_path / "log.jsonl")
    code = cli.main(["report", "--log", "log.jsonl", "--labels", "labels.jsonl", "--resamples", "20"])
    assert code == 0
    out = capsys.readouterr().out
    assert "prices: as of" in out
    assert "claude-haiku-4-5-20251001" in out
    assert "no price" not in out


def test_mcp_default_prices_are_the_shipped_tables():
    assert mcp_server._price_paths("") == default_table_paths()
    assert mcp_server._price_paths("a.json, b.json") == ["a.json", "b.json"]
