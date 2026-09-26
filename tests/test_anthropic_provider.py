"""Usage extraction for the Anthropic Messages API, JSON and streaming."""

from __future__ import annotations

import json

from conftest import NON_STREAMING_RESPONSE, SSE_BODY

from cost_per_task.providers.anthropic import AnthropicAdapter

# A streamed response that ran a server-side tool (web search). Anthropic
# documents the counts in message_delta as cumulative, and here they exceed
# the message_start values once the search results were added to the input.
SSE_SERVER_TOOL_BODY = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_03",'
    '"model":"claude-sonnet-5-20250929","usage":{"input_tokens":1000,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    '"output_tokens":1}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"server_tool_use","id":"srvtoolu_1","name":"web_search","input":{}}}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"input_tokens":9000,"cache_creation_input_tokens":200,'
    '"cache_read_input_tokens":3000,"output_tokens":510,'
    '"cache_creation":{"ephemeral_5m_input_tokens":150,"ephemeral_1h_input_tokens":50},'
    '"server_tool_use":{"web_search_requests":1}}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
)


def _collect(body: str):
    collector = AnthropicAdapter().sse_collector()
    for line in body.split("\n"):
        collector.feed_line(line)
    return collector.result()


def test_json_body():
    usage = AnthropicAdapter().parse_json_body(json.dumps(NON_STREAMING_RESPONSE).encode())
    assert usage is not None
    assert usage.model == "claude-sonnet-5-20250929"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (1200, 4500, 300)
    assert usage.cache_write_1h_tokens == 0
    assert usage.output_tokens == 250
    assert usage.reasoning_tokens is None
    assert usage.tool_names == ["read_file"]


def test_stream_with_output_only_delta_keeps_message_start_counts():
    usage = _collect(SSE_BODY)
    assert usage is not None
    assert usage.model == "claude-sonnet-5-20250929"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (800, 2000, 0)
    assert usage.output_tokens == 180
    assert usage.tool_names == ["bash"]


def test_stream_delta_counts_are_cumulative_after_server_tools():
    usage = _collect(SSE_SERVER_TOOL_BODY)
    assert usage is not None
    assert usage.input_tokens == 9000
    assert usage.cache_write_tokens == 200
    assert usage.cache_write_1h_tokens == 50
    assert usage.cache_read_tokens == 3000
    assert usage.output_tokens == 510
    assert usage.tool_names == ["web_search"]


def test_stream_delta_never_lowers_a_count():
    body = SSE_SERVER_TOOL_BODY.replace('"input_tokens":9000', '"input_tokens":400').replace(
        '"output_tokens":510', '"output_tokens":null'
    )
    usage = _collect(body)
    assert usage is not None
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 1


def test_speed_is_read_from_the_usage_block():
    fast = json.loads(json.dumps(NON_STREAMING_RESPONSE))
    fast["usage"]["speed"] = "fast"
    assert AnthropicAdapter().parse_json_body(json.dumps(fast).encode()).speed == "fast"
    assert AnthropicAdapter().parse_json_body(json.dumps(NON_STREAMING_RESPONSE).encode()).speed is None
    # A stream names it in message_start (SSE_BODY) or in the final delta.
    assert _collect(SSE_BODY).speed == "fast"
    assert _collect(SSE_SERVER_TOOL_BODY).speed is None
    delta_only = SSE_SERVER_TOOL_BODY.replace('"server_tool_use"', '"speed":"standard","server_tool_use"')
    assert _collect(delta_only).speed == "standard"
    odd = json.loads(json.dumps(NON_STREAMING_RESPONSE))
    odd["usage"]["speed"] = "x" * 100
    assert len(AnthropicAdapter().parse_json_body(json.dumps(odd).encode()).speed) == 20
