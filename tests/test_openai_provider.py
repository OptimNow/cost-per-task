from __future__ import annotations

import json

from conftest import (
    OPENAI_CHAT_RESPONSE,
    OPENAI_CHAT_SSE_NO_USAGE,
    OPENAI_CHAT_SSE_WITH_USAGE,
    OPENAI_RESPONSES_RESPONSE,
    OPENAI_RESPONSES_SSE,
)

from cost_per_task.providers.openai import OpenAIAdapter


def _collect(body: str):
    collector = OpenAIAdapter().sse_collector()
    for line in body.split("\n"):
        collector.feed_line(line)
    return collector.result()


def test_chat_completion_json_splits_cached_and_reasoning():
    usage = OpenAIAdapter().parse_json_body(json.dumps(OPENAI_CHAT_RESPONSE).encode())
    assert usage is not None
    assert usage.model == "gpt-5.5-2026-04-01"
    assert usage.input_tokens == 600
    assert usage.cache_read_tokens == 400
    assert usage.cache_write_tokens == 0
    assert usage.output_tokens == 100
    assert usage.reasoning_tokens == 200
    assert usage.tool_names == ["get_weather"]


def test_responses_api_json():
    usage = OpenAIAdapter().parse_json_body(json.dumps(OPENAI_RESPONSES_RESPONSE).encode())
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (500, 50, 30)
    assert usage.tool_names == ["read_file"]


def test_cache_write_tokens_are_read_when_reported():
    payload = {
        "object": "response",
        "model": "gpt-5.6",
        "output": [],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    usage = OpenAIAdapter().parse_json_body(json.dumps(payload).encode())
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (500, 200, 300)


def test_json_without_usage_is_ignored():
    assert OpenAIAdapter().parse_json_body(b'{"object":"list","data":[]}') is None
    assert OpenAIAdapter().parse_json_body(b"garbage") is None


def test_chat_stream_with_usage():
    usage = _collect(OPENAI_CHAT_SSE_WITH_USAGE)
    assert usage is not None
    assert usage.model == "gpt-5.5-2026-04-01"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (600, 100, 50)
    assert usage.tool_names == ["bash"]


def test_chat_stream_without_usage_yields_nothing():
    assert _collect(OPENAI_CHAT_SSE_NO_USAGE) is None


def test_responses_stream():
    usage = _collect(OPENAI_RESPONSES_SSE)
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens) == (600, 300)
    assert (usage.output_tokens, usage.reasoning_tokens) == (50, 70)
    assert usage.tool_names == ["grep"]
