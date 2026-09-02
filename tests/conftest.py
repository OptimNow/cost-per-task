"""Shared fixtures: fake Anthropic and OpenAI API servers for proxy tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

NON_STREAMING_RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "model": "claude-sonnet-5-20250929",
    "content": [
        {"type": "text", "text": "SECRET-COMPLETION-TEXT"},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}},
    ],
    "stop_reason": "tool_use",
    "usage": {
        "input_tokens": 1200,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 4500,
        "output_tokens": 250,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 300,
            "ephemeral_1h_input_tokens": 0,
        },
    },
}

SSE_BODY = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_02",'
    '"model":"claude-sonnet-5-20250929","usage":{"input_tokens":800,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":2000,'
    '"output_tokens":1}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"tool_use","id":"tu_2","name":"bash","input":{}}}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
    '"usage":{"output_tokens":180}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
)

OPENAI_CHAT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-5.5-2026-04-01",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "SECRET-COMPLETION-TEXT",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": 400},
        "completion_tokens_details": {"reasoning_tokens": 200},
    },
}

OPENAI_RESPONSES_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "model": "gpt-5.5-2026-04-01",
    "output": [{"type": "function_call", "name": "read_file", "arguments": "{}"}],
    "usage": {
        "input_tokens": 500,
        "output_tokens": 80,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 30},
    },
}

_CHUNK_1 = (
    '{"object":"chat.completion.chunk","model":"gpt-5.5-2026-04-01","choices":[{"index":0,'
    '"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_2","type":"function",'
    '"function":{"name":"bash","arguments":""}}]}}],"usage":null}'
)
_CHUNK_2 = (
    '{"object":"chat.completion.chunk","model":"gpt-5.5-2026-04-01","choices":[{"index":0,'
    '"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}],"usage":null}'
)
_CHUNK_USAGE = (
    '{"object":"chat.completion.chunk","model":"gpt-5.5-2026-04-01","choices":[],'
    '"usage":{"prompt_tokens":700,"completion_tokens":50,'
    '"prompt_tokens_details":{"cached_tokens":100},'
    '"completion_tokens_details":{"reasoning_tokens":0}}}'
)
OPENAI_CHAT_SSE_WITH_USAGE = (
    f"data: {_CHUNK_1}\n\ndata: {_CHUNK_2}\n\ndata: {_CHUNK_USAGE}\n\ndata: [DONE]\n\n"
)
OPENAI_CHAT_SSE_NO_USAGE = f"data: {_CHUNK_1}\n\ndata: {_CHUNK_2}\n\ndata: [DONE]\n\n"

OPENAI_RESPONSES_SSE = (
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_2"}}\n\n'
    "event: response.completed\n"
    'data: {"type":"response.completed","response":{"id":"resp_2","object":"response",'
    '"model":"gpt-5.5-2026-04-01","output":[{"type":"function_call","name":"grep"}],'
    '"usage":{"input_tokens":900,"output_tokens":120,'
    '"input_tokens_details":{"cached_tokens":300},'
    '"output_tokens_details":{"reasoning_tokens":70}}}}\n\n'
)


def _send(handler: BaseHTTPRequestHandler, payload: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class _FakeAnthropicHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if request.get("stream"):
            _send(self, SSE_BODY.encode(), "text/event-stream")
        else:
            _send(self, json.dumps(NON_STREAMING_RESPONSE).encode(), "application/json")


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """Mimics OpenAI: a streamed chat completion carries usage only when the
    request asked for it via stream_options.include_usage."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.split("?")[0]
        if path == "/v1/responses":
            if request.get("stream"):
                _send(self, OPENAI_RESPONSES_SSE.encode(), "text/event-stream")
            else:
                _send(self, json.dumps(OPENAI_RESPONSES_RESPONSE).encode(), "application/json")
        elif request.get("stream"):
            include_usage = (request.get("stream_options") or {}).get("include_usage")
            body = OPENAI_CHAT_SSE_WITH_USAGE if include_usage else OPENAI_CHAT_SSE_NO_USAGE
            _send(self, body.encode(), "text/event-stream")
        else:
            _send(self, json.dumps(OPENAI_CHAT_RESPONSE).encode(), "application/json")


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def fake_upstream():
    server = _serve(_FakeAnthropicHandler)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def fake_openai_upstream():
    server = _serve(_FakeOpenAIHandler)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
