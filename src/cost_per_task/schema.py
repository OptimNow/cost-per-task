"""Step-level log records and JSONL persistence.

Field names follow the OpenTelemetry GenAI semantic conventions where an
equivalent exists (gen_ai.request.model -> model, gen_ai.provider.name ->
provider). Token counts always come from the provider API response, never
from a local tokenizer.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class StepRecord:
    """One logged API call (a step) inside one attempt at one task."""

    task_id: str
    attempt_id: str
    step_id: int
    timestamp: str  # ISO 8601, UTC
    provider: str
    model: str
    input_tokens: int = 0  # fresh, uncached input (x in the DoiT model)
    cache_read_tokens: int = 0  # r
    cache_write_tokens: int = 0  # w, total across TTL tiers
    cache_write_1h_tokens: int = 0  # subset of cache_write_tokens billed at the 1h rate
    reasoning_tokens: int | None = None  # t; None when the provider does not report it separately
    output_tokens: int = 0  # y; includes reasoning tokens for providers that do not split them out
    tool_call_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    latency_ms: int = 0
    effort: str | None = None
    # The speed the provider reports it served the call at (Anthropic ``usage.speed``:
    # ``fast`` or ``standard``); None when it says nothing. Fast mode is priced from the
    # model's ``fast`` rates when the table has them.
    speed: str | None = None
    task_type: str | None = None
    # How the task id came about: None or 'manual' when a person stated it, else the
    # signal it was inferred from (issue, pr, branch, session).
    task_source: str | None = None
    reported_cost: float | None = None  # cost the provider itself reported (OpenRouter usage.cost)
    outcome_label: str = "pending"  # pass | fail | pending; the labels file wins at report time
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "StepRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class JsonlWriter:
    """Append-only, thread-safe JSONL writer used by the proxy."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: StepRecord) -> None:
        line = record.to_json() + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def read_jsonl(path: str | Path) -> list[StepRecord]:
    records = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(StepRecord.from_dict(json.loads(line)))
    return records
