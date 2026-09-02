from __future__ import annotations

import json

from cost_per_task.importers import import_langfuse, import_litellm, read_rows

LANGFUSE_ROWS = [
    {
        "id": "obs1", "traceId": "trace-A", "sessionId": "task-1", "type": "GENERATION",
        "model": "claude-sonnet-5-20250929",
        "startTime": "2026-09-01T10:00:00.000Z", "endTime": "2026-09-01T10:00:02.500Z",
        "usageDetails": {"input": 1000, "output": 200, "cache_read_input_tokens": 4000,
                         "cache_creation_input_tokens": 300, "total": 5500},
        "metadata": {"prompt": "CLIENT-SENSITIVE-PROMPT"},
    },
    {
        "id": "obs2", "traceId": "trace-A", "sessionId": "task-1", "type": "GENERATION",
        "model": "gpt-5.5-2026-04-01",
        "startTime": "2026-09-01T10:00:03.000Z", "latency": 1.2,
        "usageDetails": {"input": 800, "output": 100, "input_cached_tokens": 300},
    },
    {"id": "span1", "traceId": "trace-A", "sessionId": "task-1", "type": "SPAN",
     "startTime": "2026-09-01T10:00:00.000Z"},
    {
        "id": "obs3", "traceId": "trace-B", "sessionId": "task-1", "type": "GENERATION",
        "model": "claude-sonnet-5-20250929", "startTime": "2026-09-01T11:00:00.000Z",
        "usage": {"input": 10, "output": 5},
    },
    {"id": "obs4", "traceId": "trace-C", "type": "GENERATION", "model": "claude-sonnet-5",
     "startTime": "2026-09-01T12:00:00.000Z", "usageDetails": {"input": 1, "output": 1}},
]


def test_langfuse_import_groups_and_classifies_tokens(tmp_path):
    path = tmp_path / "langfuse.json"
    path.write_text(json.dumps(LANGFUSE_ROWS), encoding="utf-8")
    result = import_langfuse(path)

    assert len(result.records) == 3
    first, second, third = result.records
    assert (first.task_id, first.attempt_id, first.step_id) == ("task-1", "trace-A", 1)
    assert first.provider == "anthropic"
    assert (first.input_tokens, first.cache_read_tokens, first.cache_write_tokens) == (1000, 4000, 300)
    assert first.output_tokens == 200
    assert first.latency_ms == 2500
    assert first.timestamp == "2026-09-01T10:00:00+00:00"

    assert (second.step_id, second.provider) == (2, "openai")
    # OpenAI-style export: cached tokens were inside the input total
    assert (second.input_tokens, second.cache_read_tokens) == (500, 300)
    assert second.latency_ms == 1200

    assert (third.attempt_id, third.input_tokens, third.output_tokens) == ("trace-B", 10, 5)
    # the span had no usage and obs4 had no session
    assert result.skipped == 2
    assert any("sessionId" in w for w in result.warnings)
    assert "CLIENT-SENSITIVE-PROMPT" not in json.dumps([r.to_json() for r in result.records])


def test_langfuse_csv_with_json_cells(tmp_path):
    path = tmp_path / "langfuse.csv"
    path.write_text(
        "id,traceId,sessionId,type,model,startTime,usageDetails\n"
        'obs1,trace-A,task-9,GENERATION,claude-haiku-4-5,2026-09-01T10:00:00Z,"{""input"": 50, ""output"": 7}"\n',
        encoding="utf-8",
    )
    result = import_langfuse(path)
    assert len(result.records) == 1
    assert result.records[0].task_id == "task-9"
    assert (result.records[0].input_tokens, result.records[0].output_tokens) == (50, 7)


LITELLM_ROWS = [
    {
        "request_id": "req-1", "call_type": "acompletion", "model": "gpt-5.4-2026-02-01",
        "custom_llm_provider": "openai", "prompt_tokens": 1200, "completion_tokens": 300,
        "total_tokens": 1500, "spend": 0.0075, "startTime": "2026-09-01T10:00:00Z",
        "endTime": "2026-09-01T10:00:04Z", "request_duration_ms": 4000,
        "metadata": {"task_id": "issue-7", "user_api_key_alias": "dev"},
        "session_id": "sess-1", "messages": [{"role": "user", "content": "CLIENT-SENSITIVE-PROMPT"}],
    },
    {
        "request_id": "req-2", "model": "gpt-5.4-2026-02-01", "prompt_tokens": 100,
        "completion_tokens": 20, "startTime": "2026-09-01T10:00:05Z", "endTime": "2026-09-01T10:00:06Z",
        "metadata": "{\"task_id\": \"issue-7\"}", "session_id": "sess-1",
    },
    {"request_id": "req-3", "model": "gpt-5.4", "prompt_tokens": 5, "completion_tokens": 1,
     "metadata": {}, "session_id": "sess-2"},
]


def test_litellm_import_defaults(tmp_path):
    path = tmp_path / "spend.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in LITELLM_ROWS) + "\n", encoding="utf-8")
    result = import_litellm(path)
    assert len(result.records) == 2
    first, second = result.records
    assert (first.task_id, first.attempt_id, first.step_id) == ("issue-7", "sess-1", 1)
    assert first.provider == "openai"
    assert (first.input_tokens, first.output_tokens, first.latency_ms) == (1200, 300, 4000)
    assert second.step_id == 2
    assert second.latency_ms == 1000  # from start/end when no duration column
    assert result.skipped == 1  # req-3 has no task id in metadata
    assert "CLIENT-SENSITIVE-PROMPT" not in json.dumps([r.to_json() for r in result.records])


def test_litellm_custom_fields(tmp_path):
    path = tmp_path / "spend.json"
    path.write_text(json.dumps({"data": LITELLM_ROWS}), encoding="utf-8")
    result = import_litellm(path, task_field="session_id", attempt_field="request_id", task_type="support")
    assert len(result.records) == 3
    assert {r.task_id for r in result.records} == {"sess-1", "sess-2"}
    assert all(r.task_type == "support" for r in result.records)


def test_read_rows_formats(tmp_path):
    (tmp_path / "a.jsonl").write_text('{"x": 1}\n\n{"x": 2}\n', encoding="utf-8")
    (tmp_path / "b.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    assert read_rows(tmp_path / "a.jsonl") == [{"x": 1}, {"x": 2}]
    assert read_rows(tmp_path / "b.csv") == [{"x": "1", "y": "2"}]
