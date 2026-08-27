"""Shared fixtures: a fake Anthropic API server for proxy tests."""

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


class _FakeAnthropicHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if request.get("stream"):
            payload = SSE_BODY.encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(NON_STREAMING_RESPONSE).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropicHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
