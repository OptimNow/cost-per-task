"""Pricing tables and cost computation.

Rates are stored per million tokens. Every table must carry a currency and
an ``as_of`` date: the DoiT disclosure checklist requires prices with dates,
so an undated table is rejected.

For providers that report reasoning tokens inside ``output_tokens`` (such
as Anthropic), ``reasoning_per_mtok`` is null and the output rate covers
both classes. A provider adapter that does split reasoning out must put
only the visible tokens in ``output_tokens`` so nothing is double counted.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .schema import StepRecord


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class ModelRates:
    input_per_mtok: float
    cache_read_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_write_1h_per_mtok: float
    output_per_mtok: float
    reasoning_per_mtok: float | None = None


_REQUIRED_RATES = (
    "input_per_mtok",
    "cache_read_per_mtok",
    "cache_write_5m_per_mtok",
    "cache_write_1h_per_mtok",
    "output_per_mtok",
)


class PricingTable:
    def __init__(
        self,
        currency: str,
        as_of: str,
        models: dict[str, ModelRates],
        source: str | None = None,
    ) -> None:
        self.currency = currency
        self.as_of = as_of
        self.models = models
        self.source = source

    @classmethod
    def load(cls, path: str | Path) -> "PricingTable":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in ("currency", "as_of", "models"):
            if not data.get(key):
                raise PricingError(f"pricing table is missing required field '{key}'")
        models: dict[str, ModelRates] = {}
        for model_id, rates in data["models"].items():
            values = {}
            for rate_key in _REQUIRED_RATES:
                value = rates.get(rate_key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise PricingError(
                        f"model '{model_id}': '{rate_key}' must be a number, got {value!r}"
                    )
                values[rate_key] = float(value)
            reasoning = rates.get("reasoning_per_mtok")
            if reasoning is not None and not isinstance(reasoning, (int, float)):
                raise PricingError(
                    f"model '{model_id}': 'reasoning_per_mtok' must be a number or null"
                )
            models[model_id] = ModelRates(
                reasoning_per_mtok=None if reasoning is None else float(reasoning), **values
            )
        return cls(data["currency"], data["as_of"], models, data.get("source"))

    def rates_for(self, model: str) -> ModelRates | None:
        """Exact match first, then the longest key the model id starts with,
        so a dated id like claude-sonnet-5-20250929 finds claude-sonnet-5."""
        if model in self.models:
            return self.models[model]
        best = None
        for key in self.models:
            if model.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        return self.models[best] if best else None


def price_step(record: StepRecord, table: PricingTable) -> float | None:
    """Cost of one step in the table's currency, or None if the model is unpriced."""
    rates = table.rates_for(record.model)
    if rates is None:
        return None
    write_1h = min(record.cache_write_1h_tokens, record.cache_write_tokens)
    write_5m = record.cache_write_tokens - write_1h
    cost = (
        record.input_tokens * rates.input_per_mtok
        + record.cache_read_tokens * rates.cache_read_per_mtok
        + write_5m * rates.cache_write_5m_per_mtok
        + write_1h * rates.cache_write_1h_per_mtok
        + record.output_tokens * rates.output_per_mtok
    )
    if record.reasoning_tokens and rates.reasoning_per_mtok is not None:
        cost += record.reasoning_tokens * rates.reasoning_per_mtok
    return cost / 1_000_000


def cost_per_attempt(
    records: list[StepRecord], table: PricingTable
) -> dict[tuple[str, str], float]:
    """Total priced cost per (task_id, attempt_id); unpriced steps are skipped."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for record in records:
        cost = price_step(record, table)
        if cost is not None:
            totals[(record.task_id, record.attempt_id)] += cost
    return dict(totals)
