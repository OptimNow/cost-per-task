from __future__ import annotations

import json
import threading
import time
import urllib.request
from email.message import Message

from conftest import OPENAI_CHAT_SSE_WITH_USAGE, SSE_BODY

from cost_per_task.proxy import create_proxy, inject_include_usage, select_provider
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


def _start_proxy(log_path, **upstreams):
    server = create_proxy(
        upstreams=upstreams, log_path=log_path, task_id="T1", attempt_id="a1", task_type="coding"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(proxy_url: str, path: str, payload: dict, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(
        proxy_url + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return response.read()


ANTHROPIC_HEADERS = {"x-api-key": "sk-test-DUMMY-KEY", "anthropic-version": "2023-06-01"}
OPENAI_HEADERS = {"Authorization": "Bearer sk-test-DUMMY-KEY"}


def test_non_streaming_passthrough_and_log(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, anthropic=fake_upstream)
    try:
        body = json.loads(
            _post(proxy_url, "/v1/messages", {"model": "claude-sonnet-5", "messages": []}, ANTHROPIC_HEADERS)
        )
        assert body["usage"]["input_tokens"] == 1200

        record = _read_records(log, 1)[0]
        assert record.task_id == "T1"
        assert record.attempt_id == "a1"
        assert record.step_id == 1
        assert record.provider == "anthropic"
        assert record.model == "claude-sonnet-5-20250929"
        assert record.task_type == "coding"
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
    server, proxy_url = _start_proxy(log, anthropic=fake_upstream)
    try:
        body = _post(
            proxy_url,
            "/v1/messages",
            {"model": "claude-sonnet-5", "stream": True, "messages": []},
            ANTHROPIC_HEADERS,
        )
        assert body.decode() == SSE_BODY

        record = _read_records(log, 1)[0]
        assert record.model == "claude-sonnet-5-20250929"
        assert record.input_tokens == 800
        assert record.cache_read_tokens == 2000
        assert record.output_tokens == 180
        assert record.tool_names == ["bash"]
    finally:
        server.shutdown()
        server.server_close()


def test_no_secrets_or_content_in_log(fake_upstream, fake_openai_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, anthropic=fake_upstream, openai=fake_openai_upstream)
    try:
        prompt = [{"role": "user", "content": "CLIENT-SENSITIVE-PROMPT"}]
        _post(proxy_url, "/v1/messages", {"model": "claude-sonnet-5", "messages": prompt}, ANTHROPIC_HEADERS)
        _post(proxy_url, "/v1/chat/completions", {"model": "gpt-5.5", "messages": prompt}, OPENAI_HEADERS)
        _read_records(log, 2)
        raw = log.read_text(encoding="utf-8").lower()
        assert "sk-test-dummy-key" not in raw
        assert "client-sensitive-prompt" not in raw
        assert "secret-completion-text" not in raw
        assert "x-api-key" not in raw
        assert "authorization" not in raw
    finally:
        server.shutdown()
        server.server_close()


def test_step_ids_increment(fake_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, anthropic=fake_upstream)
    try:
        _post(proxy_url, "/v1/messages", {"model": "claude-sonnet-5", "messages": []}, ANTHROPIC_HEADERS)
        _post(proxy_url, "/v1/messages", {"model": "claude-sonnet-5", "messages": []}, ANTHROPIC_HEADERS)
        records = _read_records(log, 2)
        assert [r.step_id for r in records] == [1, 2]
    finally:
        server.shutdown()
        server.server_close()


def test_openai_chat_routing_and_reasoning_split(fake_upstream, fake_openai_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, anthropic=fake_upstream, openai=fake_openai_upstream)
    try:
        body = json.loads(
            _post(proxy_url, "/v1/chat/completions", {"model": "gpt-5.5", "messages": []}, OPENAI_HEADERS)
        )
        assert body["usage"]["prompt_tokens"] == 1000

        record = _read_records(log, 1)[0]
        assert record.provider == "openai"
        assert record.model == "gpt-5.5-2026-04-01"
        assert record.input_tokens == 600  # 1000 prompt minus 400 cached
        assert record.cache_read_tokens == 400
        assert record.cache_write_tokens == 0
        assert record.output_tokens == 100  # 300 completion minus 200 reasoning
        assert record.reasoning_tokens == 200
        assert record.tool_names == ["get_weather"]
    finally:
        server.shutdown()
        server.server_close()


def test_openai_stream_usage_is_injected(fake_openai_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, openai=fake_openai_upstream)
    try:
        body = _post(
            proxy_url,
            "/v1/chat/completions",
            {"model": "gpt-5.5", "stream": True, "messages": []},
            OPENAI_HEADERS,
        )
        # The fake only emits usage when include_usage was requested.
        assert body.decode() == OPENAI_CHAT_SSE_WITH_USAGE
        record = _read_records(log, 1)[0]
        assert record.input_tokens == 600
        assert record.cache_read_tokens == 100
        assert record.output_tokens == 50
        assert record.tool_names == ["bash"]
    finally:
        server.shutdown()
        server.server_close()


def test_openai_stream_without_injection_logs_nothing(fake_openai_upstream, tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    server = create_proxy(
        upstreams={"openai": fake_openai_upstream},
        log_path=log,
        task_id="T1",
        attempt_id="a1",
        inject_usage=False,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _post(proxy_url, "/v1/chat/completions", {"model": "gpt-5.5", "stream": True, "messages": []}, OPENAI_HEADERS)
        time.sleep(0.2)
        assert server.captured_count == 0
        assert not log.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_openai_responses_api_json_and_stream(fake_openai_upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, proxy_url = _start_proxy(log, openai=fake_openai_upstream)
    try:
        _post(proxy_url, "/v1/responses", {"model": "gpt-5.5", "input": "hi"}, OPENAI_HEADERS)
        _post(proxy_url, "/v1/responses", {"model": "gpt-5.5", "input": "hi", "stream": True}, OPENAI_HEADERS)
        first, second = _read_records(log, 2)
        assert (first.input_tokens, first.output_tokens, first.reasoning_tokens) == (500, 50, 30)
        assert first.tool_names == ["read_file"]
        assert (second.input_tokens, second.cache_read_tokens) == (600, 300)
        assert (second.output_tokens, second.reasoning_tokens) == (50, 70)
        assert second.tool_names == ["grep"]
    finally:
        server.shutdown()
        server.server_close()


def test_select_provider_by_path_then_headers():
    both = ["anthropic", "openai"]
    assert select_provider("/v1/messages?beta=true", Message(), both) == "anthropic"
    assert select_provider("/v1/chat/completions", Message(), both) == "openai"
    anthropic_headers = Message()
    anthropic_headers["x-api-key"] = "k"
    assert select_provider("/v1/models", anthropic_headers, both) == "anthropic"
    openai_headers = Message()
    openai_headers["Authorization"] = "Bearer k"
    assert select_provider("/v1/models", openai_headers, both) == "openai"
    assert select_provider("/v1/models", Message(), both) is None
    assert select_provider("/v1/chat/completions", Message(), ["anthropic"]) == "anthropic"


def test_inject_include_usage_only_touches_streaming_chat():
    streaming = json.dumps({"model": "m", "stream": True}).encode()
    injected = json.loads(inject_include_usage("/v1/chat/completions", streaming))
    assert injected["stream_options"] == {"include_usage": True}

    already = json.dumps({"stream": True, "stream_options": {"include_usage": True}}).encode()
    assert inject_include_usage("/v1/chat/completions", already) == already

    non_streaming = json.dumps({"model": "m"}).encode()
    assert inject_include_usage("/v1/chat/completions", non_streaming) == non_streaming
    assert inject_include_usage("/v1/responses", streaming) == streaming
    assert inject_include_usage("/v1/chat/completions", b"not json") == b"not json"
