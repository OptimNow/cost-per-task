from __future__ import annotations

import json

from cost_per_task.schema import JsonlWriter, StepRecord, read_jsonl


def _record() -> StepRecord:
    return StepRecord(
        task_id="T1",
        attempt_id="a1",
        step_id=1,
        timestamp="2026-08-26T10:00:00+00:00",
        provider="anthropic",
        model="test-model",
        input_tokens=100,
        output_tokens=50,
        tool_names=["bash"],
        tool_call_count=1,
    )


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "log.jsonl"
    writer = JsonlWriter(path)
    writer.append(_record())
    writer.append(_record())
    records = read_jsonl(path)
    assert len(records) == 2
    assert records[0] == _record()


def test_unknown_fields_are_ignored_on_read(tmp_path):
    path = tmp_path / "log.jsonl"
    data = json.loads(_record().to_json())
    data["added_in_a_future_version"] = 42
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    assert read_jsonl(path)[0] == _record()
