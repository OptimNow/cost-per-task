"""Usage extraction for the Anthropic Messages API.

Non-streaming responses carry a single top-level ``usage`` block. Streaming
responses split it: input and cache counts arrive in the ``message_start``
event, the final output count in the last ``message_delta`` event.

Anthropic reports extended-thinking tokens inside ``output_tokens`` with no
separate reasoning count, so ``reasoning_tokens`` stays None here.
"""

from __future__ import annotations

import json

from .base import ParsedUsage

_TOOL_BLOCK_TYPES = ("tool_use", "server_tool_use")


def _usage_from_dict(usage: dict) -> ParsedUsage:
    parsed = ParsedUsage()
    parsed.input_tokens = int(usage.get("input_tokens") or 0)
    parsed.output_tokens = int(usage.get("output_tokens") or 0)
    parsed.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    parsed.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    detail = usage.get("cache_creation")
    if isinstance(detail, dict):
        parsed.cache_write_1h_tokens = int(detail.get("ephemeral_1h_input_tokens") or 0)
    return parsed


class AnthropicAdapter:
    name = "anthropic"
    default_upstream = "https://api.anthropic.com"

    def parse_json_body(self, payload: bytes) -> ParsedUsage | None:
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict) or "usage" not in data:
            return None
        parsed = _usage_from_dict(data.get("usage") or {})
        parsed.model = data.get("model", "")
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") in _TOOL_BLOCK_TYPES:
                parsed.tool_names.append(block.get("name", ""))
        return parsed

    def sse_collector(self) -> "_AnthropicSseCollector":
        return _AnthropicSseCollector()


class _AnthropicSseCollector:
    def __init__(self) -> None:
        self._usage: ParsedUsage | None = None
        self._data_lines: list[str] = []

    def feed_line(self, line: str) -> None:
        if line.startswith("data:"):
            self._data_lines.append(line[5:].strip())
        elif line == "" and self._data_lines:
            raw = "\n".join(self._data_lines)
            self._data_lines = []
            self._handle_event(raw)

    def _handle_event(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self._usage = _usage_from_dict(message.get("usage") or {})
            self._usage.model = message.get("model", "")
        elif etype == "content_block_start" and self._usage is not None:
            block = event.get("content_block") or {}
            if block.get("type") in _TOOL_BLOCK_TYPES:
                self._usage.tool_names.append(block.get("name", ""))
        elif etype == "message_delta" and self._usage is not None:
            usage = event.get("usage") or {}
            if "output_tokens" in usage:
                self._usage.output_tokens = int(usage["output_tokens"] or 0)

    def result(self) -> ParsedUsage | None:
        return self._usage
