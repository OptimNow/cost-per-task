"""Pricing tables and cost computation.

Rates are stored per million tokens. Every table must carry a currency and
an ``as_of`` date: the DoiT disclosure checklist requires prices with dates,
so an undated table is rejected.

Reasoning tokens are priced at ``reasoning_per_mtok`` when a table gives
one, otherwise at the output rate, which is how both Anthropic and OpenAI
bill today. Anthropic does not report reasoning separately (it is inside
``output_tokens``, so ``reasoning_tokens`` is null); the OpenAI adapter
splits reasoning out of the output count so nothing is double counted.
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

    @classmethod
    def load_many(cls, paths: list[str | Path]) -> "PricingTable":
        """Merge several tables (one per provider, say) for logs that mix
        vendors, as OpenRouter traffic does. Currencies must agree, a model
        priced differently in two tables is an error, and the merged
        ``as_of`` is the oldest so the disclosure never overstates freshness."""
        tables = [cls.load(path) for path in paths]
        if not tables:
            raise PricingError("no pricing table found; pass --prices PATH")
        if len(tables) == 1:
            return tables[0]
        currencies = {t.currency for t in tables}
        if len(currencies) > 1:
            raise PricingError(f"pricing tables use different currencies: {sorted(currencies)}")
        models: dict[str, ModelRates] = {}
        for table in tables:
            for model, rates in table.models.items():
                if model in models and models[model] != rates:
                    raise PricingError(f"model '{model}' is priced differently in two tables")
                models[model] = rates
        return cls(
            tables[0].currency,
            min(t.as_of for t in tables),
            models,
            " | ".join(t.source for t in tables if t.source) or None,
        )

    def rates_for(self, model: str) -> ModelRates | None:
        """Exact match first, then the longest key the model id starts with,
        so a dated id like claude-sonnet-5-20250929 finds claude-sonnet-5.
        Gateway ids such as anthropic/claude-haiku-4.5 (OpenRouter) are tried
        without the vendor prefix and with dots turned into hyphens."""
        candidates = [model]
        if "/" in model:
            suffix = model.rsplit("/", 1)[1]
            candidates.extend([suffix, suffix.replace(".", "-")])
        for candidate in candidates:
            if candidate in self.models:
                return self.models[candidate]
            best = None
            for key in self.models:
                if candidate.startswith(key) and (best is None or len(key) > len(best)):
                    best = key
            if best:
                return self.models[best]
        return None


def price_breakdown(record: StepRecord, table: PricingTable) -> dict[str, float] | None:
    """Cost of one step per token class (input, cache_read, cache_write_5m,
    cache_write_1h, output, reasoning) in the table's currency, or None if the
    model is unpriced. The classes add up to ``price_step``."""
    rates = table.rates_for(record.model)
    if rates is None:
        return None
    write_1h = min(record.cache_write_1h_tokens, record.cache_write_tokens)
    write_5m = record.cache_write_tokens - write_1h
    reasoning_rate = (
        rates.output_per_mtok if rates.reasoning_per_mtok is None else rates.reasoning_per_mtok
    )
    per_mtok = {
        "input": record.input_tokens * rates.input_per_mtok,
        "cache_read": record.cache_read_tokens * rates.cache_read_per_mtok,
        "cache_write_5m": write_5m * rates.cache_write_5m_per_mtok,
        "cache_write_1h": write_1h * rates.cache_write_1h_per_mtok,
        "output": record.output_tokens * rates.output_per_mtok,
        "reasoning": (record.reasoning_tokens or 0) * reasoning_rate,
    }
    return {key: value / 1_000_000 for key, value in per_mtok.items()}


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
    if record.reasoning_tokens:
        reasoning_rate = (
            rates.output_per_mtok
            if rates.reasoning_per_mtok is None
            else rates.reasoning_per_mtok
        )
        cost += record.reasoning_tokens * reasoning_rate
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


def default_table_paths(names: tuple[str, ...] = ("anthropic.json", "openai.json")) -> list[str]:
    """The dated price tables shipped with cost-per-task: ``prices/<name>`` in
    the current folder, else inside the installed package, else in the
    repository an editable install points at. Missing tables are skipped."""
    here = Path(__file__).resolve()
    found = []
    for name in names:
        for candidate in (Path("prices") / name, here.parent / "prices" / name, here.parents[2] / "prices" / name):
            if candidate.exists():
                found.append(str(candidate))
                break
    return found
