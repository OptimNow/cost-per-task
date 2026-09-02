"""One entry point from files on disk to summaries, shared by the CLI and
the MCP server."""

from __future__ import annotations

from dataclasses import dataclass

from .labels import load_labels
from .metrics import Attempt, GroupSummary, build_attempts, summarise
from .pricing import PricingTable
from .schema import read_jsonl


@dataclass
class Analysis:
    table: PricingTable
    attempts: list[Attempt]
    summaries: list[GroupSummary]


def load_analysis(
    *,
    log: str,
    labels: str,
    prices: str | list[str],
    by_task_type: bool = True,
    task_type: str | None = None,
    k: int | None = None,
    retry_cap: int | None = None,
    cleanup_cost: float | None = None,
    leak_rate: float | None = None,
    resamples: int = 10_000,
    seed: int | None = None,
) -> Analysis:
    """Raises OSError or PricingError when inputs cannot be read. ``prices``
    may be several tables (one per vendor) merged for mixed-vendor logs."""
    paths = [prices] if isinstance(prices, str) else list(prices)
    table = PricingTable.load_many(paths)
    records = read_jsonl(log)
    attempts = build_attempts(records, table, load_labels(labels))
    if task_type:
        attempts = [a for a in attempts if a.task_type == task_type]
    summaries = summarise(
        attempts,
        by_task_type=by_task_type,
        k=k,
        retry_cap=retry_cap,
        cleanup_cost=cleanup_cost,
        leak_rate=leak_rate,
        resamples=resamples,
        seed=seed,
    )
    return Analysis(table=table, attempts=attempts, summaries=summaries)


def pick_model(summaries: list[GroupSummary], wanted: str) -> GroupSummary:
    """Exact match first, then a unique prefix match (dated model ids)."""
    exact = [s for s in summaries if s.model == wanted]
    if len(exact) == 1:
        return exact[0]
    matches = [s for s in summaries if s.model.startswith(wanted)]
    if len(matches) != 1:
        names = ", ".join(s.model for s in summaries) or "none"
        raise LookupError(f"'{wanted}' matches {len(matches)} models; available: {names}")
    return matches[0]
