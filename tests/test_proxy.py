from __future__ import annotations

import json
import threading
import time
import urllib.request

from conftest import SSE_BODY

from cost_per_task.proxy import create_proxy
from cost_per_task.schema import read_jsonl


def _read_records(log, count, timeout=5.0):
    """The streaming path logs after the response is fully relayed, so a
    just-finished client may be ahead of the writer; poll briefly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists():
            records = read_jsonl(log)
            if len(records) >= count:
                return records
        time.sleep(0.02)
    raise AssertionError(f"expected {count} records in {log}")


def _start_proxy(upstream: str, log_path):
    server = create_proxy(
        upstream=upstream, log_path=log_path, task_id="T1", attempt_id="a1"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(proxy_url: str, payload: dict) -> bytes:
    request = urllib.request.Request(
        proxy_url + "/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "sk-test-DUMMY-KEY"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return response.read()


def test_non_streaming_passthrough_and_log(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(fake_upstream, log)
    try:
        body = json.loads(_post(proxy_url, {"model": "claude-sonnet-5", "messages": []}))
        assert body["usage"]["input_tokens"] == 1200

        records = _read_records(log, 1)
        record = records[0]
        assert record.task_id == "T1"
        assert record.attempt_id == "a1"
        assert record.step_id == 1
        assert record.provider == "anthropic"
        assert record.model == "claude-sonnet-5-20250929"
        assert record.input_tokens == 1200
        assert record.cache_read_tokens == 4500
        assert record.cache_write_tokens == 300
        assert record.cache_write_1h_tokens == 0
        assert record.output_tokens == 250
        assert record.reasoning_tokens is None
        assert record.tool_names == ["read_file"]
        assert record.tool_call_count == 1
        assert record.outcome_label == "pending"
    finally:
        server.shutdown()
        server.server_close()


def test_streaming_passthrough_and_log(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(fake_upstream, log)
    try:
        body = _post(proxy_url, {"model": "claude-sonnet-5", "stream": True, "messages": []})
        assert body.decode() == SSE_BODY

        records = _read_records(log, 1)
        record = records[0]
        assert record.model == "claude-sonnet-5-20250929"
        assert record.input_tokens == 800
        assert record.cache_read_tokens == 2000
        assert record.output_tokens == 180
        assert record.tool_names == ["bash"]
    finally:
        server.shutdown()
        server.server_close()


def test_no_secrets_or_content_in_log(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(fake_upstream, log)
    try:
        _post(
            proxy_url,
            {
                "model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": "CLIENT-SENSITIVE-PROMPT"}],
            },
        )
        _read_records(log, 1)
        raw = log.read_text(encoding="utf-8")
        assert "sk-test-DUMMY-KEY" not in raw
        assert "CLIENT-SENSITIVE-PROMPT" not in raw
        assert "SECRET-COMPLETION-TEXT" not in raw
        assert "x-api-key" not in raw.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_step_ids_increment(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(fake_upstream, log)
    try:
        _post(proxy_url, {"model": "claude-sonnet-5", "messages": []})
        _post(proxy_url, {"model": "claude-sonnet-5", "messages": []})
        records = _read_records(log, 2)
        assert [r.step_id for r in records] == [1, 2]
    finally:
        server.shutdown()
        server.server_close()
