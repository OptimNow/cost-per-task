"""Usage extraction for the OpenAI Chat Completions and Responses APIs.

Chat Completions reports ``usage.prompt_tokens`` and ``usage.completion_tokens``
with ``prompt_tokens_details.cached_tokens`` and
``completion_tokens_details.reasoning_tokens``. The Responses API reports
``usage.input_tokens`` and ``usage.output_tokens`` with
``input_tokens_details.cached_tokens`` and ``output_tokens_details.reasoning_tokens``.

OpenAI counts cached tokens inside the prompt total and reasoning tokens
inside the completion total. Both are subtracted here so every token lands in
exactly one class. Cache writes are free up to GPT-5.5; from GPT-5.6 they are
billed at 1.25x input and reported as ``*_tokens_details.cache_write_tokens``,
which is read here when present and treated as a subset of the prompt total
(medium confidence on the subset semantics; verify against a live response).

Streaming Chat Completions carry usage only when the request sets
``stream_options.include_usage``; the proxy injects that by default.

OpenAI-compatible gateways reuse this adapter. OpenRouter adds ``usage.cost``
(the USD amount it actually charged) when usage accounting is enabled; it is
kept as ``reported_cost`` so the report can reconcile it with the table price.
"""

from __future__ import annotations

import json

from .base import ParsedUsage


def _usage_from_dict(usage: dict) -> ParsedUsage:
    parsed = ParsedUsage()
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens")) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens")) or 0)
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = (
        usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    )
    cached = int(prompt_details.get("cached_tokens") or 0)
    cache_write = int(prompt_details.get("cache_write_tokens") or 0)
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    parsed.input_tokens = max(prompt - cached - cache_write, 0)
    parsed.cache_read_tokens = cached
    parsed.cache_write_tokens = cache_write
    parsed.output_tokens = max(completion - reasoning, 0)
    parsed.reasoning_tokens = reasoning
    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        parsed.reported_cost = float(cost)
    return parsed


def _chat_tool_names(choices: list) -> list[str]:
    names = []
    for choice in choices or []:
        message = (choice or {}).get("message") or {}
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            if function.get("name"):
                names.append(function["name"])
    return names


def _response_tool_names(output: list) -> list[str]:
    return [
        item["name"]
        for item in output or []
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name")
    ]


class OpenAIAdapter:
    name = "openai"
    default_upstream = "https://api.openai.com"

    def parse_json_body(self, payload: bytes) -> ParsedUsage | None:
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
            return None
        parsed = _usage_from_dict(data["usage"])
        parsed.model = data.get("model", "")
        if data.get("object") == "response" or "output" in data:
            parsed.tool_names = _response_tool_names(data.get("output") or [])
        else:
            parsed.tool_names = _chat_tool_names(data.get("choices") or [])
        return parsed

    def sse_collector(self) -> "_OpenAISseCollector":
        return _OpenAISseCollector()


class _OpenAISseCollector:
    """Handles both stream shapes: Chat Completions chunks (usage arrives in
    the final chunk before ``[DONE]``) and Responses API events (usage
    arrives inside ``response.completed``)."""

    def __init__(self) -> None:
        self._usage: ParsedUsage | None = None
        self._model = ""
        self._tool_names: list[str] = []
        self._data_lines: list[str] = []

    def feed_line(self, line: str) -> None:
        if line.startswith("data:"):
            self._data_lines.append(line[5:].strip())
        elif line == "" and self._data_lines:
            raw = "\n".join(self._data_lines)
            self._data_lines = []
            self._handle_event(raw)

    def _handle_event(self, raw: str) -> None:
        if raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("object") == "chat.completion.chunk":
            self._model = event.get("model") or self._model
            for choice in event.get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    function = (call or {}).get("function") or {}
                    if function.get("name"):
                        self._tool_names.append(function["name"])
            if isinstance(event.get("usage"), dict):
                self._usage = _usage_from_dict(event["usage"])
        elif event.get("type") in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            if isinstance(response.get("usage"), dict):
                self._usage = _usage_from_dict(response["usage"])
            self._model = response.get("model") or self._model
            self._tool_names = _response_tool_names(response.get("output") or [])

    def result(self) -> ParsedUsage | None:
        if self._usage is None:
            return None
        self._usage.model = self._model
        self._usage.tool_names = list(self._tool_names)
        return self._usage
