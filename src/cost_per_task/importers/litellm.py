"""LiteLLM spend log importer.

Reads rows shaped like the ``LiteLLM_SpendLogs`` table or the ``/spend/logs``
API response: ``request_id``, ``model``, ``custom_llm_provider``,
``prompt_tokens``, ``completion_tokens``, ``startTime``, ``endTime``,
``request_duration_ms``, ``metadata`` (JSON), ``session_id``.

Spend logs do not break out cached or reasoning tokens, so ``prompt_tokens``
lands entirely in ``input_tokens``. If your deployment uses prompt caching
the import overstates cost; the report's disclosure checklist will show a
0% cache hit rate so the reader knows.

Defaults read the task id from ``metadata.task_id`` and use ``session_id``
as the attempt. Built from the published Prisma schema, not a live export:
verify against your own export and override the fields if needed.
"""

from __future__ import annotations

from pathlib import Path

from .common import (
    ImportResult,
    ParsedCall,
    infer_provider,
    parse_time,
    read_rows,
    rows_to_records,
    to_int,
)

DEFAULT_TASK_FIELD = "metadata.task_id"
DEFAULT_ATTEMPT_FIELD = "session_id"


def parse_litellm_row(row: dict) -> ParsedCall | None:
    model = row.get("model") or row.get("model_group")
    if not model:
        return None
    prompt = to_int(row.get("prompt_tokens"))
    completion = to_int(row.get("completion_tokens"))
    if not prompt and not completion:
        return None

    start = parse_time(row.get("startTime") or row.get("start_time"))
    end = parse_time(row.get("endTime") or row.get("end_time"))
    if row.get("request_duration_ms") not in (None, ""):
        latency_ms = to_int(row["request_duration_ms"])
    elif start and end:
        latency_ms = int((end - start).total_seconds() * 1000)
    else:
        latency_ms = 0

    provider = row.get("custom_llm_provider") or infer_provider(str(model))
    return ParsedCall(
        model=str(model),
        provider=str(provider),
        timestamp=(start.isoformat(timespec="seconds") if start else ""),
        input_tokens=prompt,
        output_tokens=completion,
        reasoning_tokens=None,
        latency_ms=max(latency_ms, 0),
    )


def import_litellm(
    path: str | Path,
    *,
    task_field: str = DEFAULT_TASK_FIELD,
    attempt_field: str = DEFAULT_ATTEMPT_FIELD,
    task_type: str | None = None,
) -> ImportResult:
    rows = read_rows(path)
    return rows_to_records(
        rows,
        parse_litellm_row,
        task_field=task_field,
        attempt_field=attempt_field,
        task_type=task_type,
    )
