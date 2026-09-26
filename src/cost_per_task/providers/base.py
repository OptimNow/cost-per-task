"""Provider adapter interface.

An adapter knows how to read the usage metadata out of one provider's API
responses, in both plain JSON and server-sent-event (streaming) form. It
never inspects or retains prompt or completion content beyond tool names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ParsedUsage:
    """Token counts and metadata extracted from one API response."""

    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    reasoning_tokens: int | None = None
    output_tokens: int = 0
    tool_names: list[str] = field(default_factory=list)
    reported_cost: float | None = None
    speed: str | None = None  # Anthropic usage.speed: fast or standard


class SseCollector(Protocol):
    def feed_line(self, line: str) -> None: ...

    def result(self) -> ParsedUsage | None: ...


class ProviderAdapter(Protocol):
    name: str
    default_upstream: str

    def parse_json_body(self, payload: bytes) -> ParsedUsage | None: ...

    def sse_collector(self) -> SseCollector: ...
