"""Langfuse observation export importer.

Reads generation rows as exported from Langfuse (CSV or JSON): ``id``,
``traceId``, ``type``, ``model``, ``startTime``, ``endTime``, ``latency``,
``usageDetails`` and ``metadata``. ``usageDetails`` holds provider-native
keys, so cache traffic is matched by pattern (``cache_read``, ``cached``,
``cache_creation``, ``cache_write``).

Reasoning tokens are left inside the output total rather than split out:
both Anthropic and OpenAI bill them at the output rate, and the export does
not say whether a reasoning key is a subset of the output count. The total
cost is right either way.

Defaults treat a Langfuse session as the task and a trace as the attempt.
Built from the documented schema, not a live export: verify field names
against your own export and override with --task-field / --attempt-field.
"""

from __future__ import annotations

from pathlib import Path

from .common import (
    ImportResult,
    ParsedCall,
    as_dict,
    infer_provider,
    parse_time,
    read_rows,
    rows_to_records,
    to_int,
)

DEFAULT_TASK_FIELD = "sessionId"
DEFAULT_ATTEMPT_FIELD = "traceId"

_INPUT_KEYS = ("input", "inputTokens", "input_tokens", "promptTokens", "prompt_tokens")
_OUTPUT_KEYS = ("output", "outputTokens", "output_tokens", "completionTokens", "completion_tokens")


def _sum_matching(details: dict, *needles: str, exclude: tuple[str, ...] = ()) -> int:
    total = 0
    for key, value in details.items():
        lowered = key.lower()
        if any(n in lowered for n in needles) and not any(e in lowered for e in exclude):
            total += to_int(value)
    return total


def parse_langfuse_row(row: dict) -> ParsedCall | None:
    observation_type = str(row.get("type") or "GENERATION").upper()
    if observation_type not in ("GENERATION", "LLM"):
        return None
    model = row.get("model")
    if not model:
        return None
    details = as_dict(row.get("usageDetails")) or as_dict(row.get("usage"))
    if not details:
        return None

    input_total = next((to_int(details[k]) for k in _INPUT_KEYS if k in details), 0)
    output_total = next((to_int(details[k]) for k in _OUTPUT_KEYS if k in details), 0)
    cache_read = _sum_matching(details, "cache_read", "cached", "cache_hit", exclude=("write", "creation"))
    cache_write = _sum_matching(details, "cache_creation", "cache_write")
    # Anthropic-style exports report cache reads alongside a fresh input
    # count; OpenAI-style exports fold cached tokens into the input total.
    if cache_read and "cached" in " ".join(details).lower() and cache_read <= input_total:
        input_total -= cache_read

    start = parse_time(row.get("startTime"))
    end = parse_time(row.get("endTime"))
    if row.get("latency") not in (None, ""):
        latency_ms = int(float(row["latency"]) * 1000)
    elif start and end:
        latency_ms = int((end - start).total_seconds() * 1000)
    else:
        latency_ms = 0

    return ParsedCall(
        model=str(model),
        provider=infer_provider(str(model)),
        timestamp=(start.isoformat(timespec="seconds") if start else ""),
        input_tokens=max(input_total, 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_total,
        reasoning_tokens=None,
        latency_ms=max(latency_ms, 0),
    )


def import_langfuse(
    path: str | Path,
    *,
    task_field: str = DEFAULT_TASK_FIELD,
    attempt_field: str = DEFAULT_ATTEMPT_FIELD,
    task_type: str | None = None,
) -> ImportResult:
    rows = read_rows(path)
    return rows_to_records(
        rows,
        parse_langfuse_row,
        task_field=task_field,
        attempt_field=attempt_field,
        task_type=task_type,
    )
