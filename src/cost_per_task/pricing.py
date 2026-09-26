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
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from .schema import StepRecord


class PricingError(ValueError):
    pass


_LATEST = "9999-12-31"  # pins a table to its newest rates

# What may follow a table key in a model id and still mean the same model: a
# release date, as Anthropic (-20250929) and OpenAI (-2026-04-01) write it,
# with anything after it. A version digit (-5 after claude-opus-5) does not.
_DATED_SUFFIX = re.compile(r"^-(\d{8}|\d{4}-\d{2}-\d{2})(-|$)")


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


@dataclass(frozen=True)
class DatedRates:
    """One model's rates over a stretch of time. ``effective`` is the day the
    rates apply from (the first snapshot's ``effective_from``, else its
    ``as_of``); ``as_of`` is the day they were last read and found unchanged."""

    effective: str
    as_of: str
    rates: ModelRates


def _check_date(value, what: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise PricingError(f"{what} must be a YYYY-MM-DD date, got {value!r}") from None


def _parse_models(raw: dict) -> dict[str, ModelRates]:
    models: dict[str, ModelRates] = {}
    for model_id, rates in raw.items():
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
    return models


def _compact(entries: list[DatedRates], model: str) -> list[DatedRates]:
    """Oldest first. A snapshot that repeats the previous rates confirms them:
    the earlier start stands and the later reading becomes their ``as_of``, the
    date a disclosure should show. Two rates for one day cannot both be right."""
    kept: list[DatedRates] = []
    for entry in sorted(entries, key=lambda e: (e.effective, e.as_of)):
        if kept and kept[-1].rates == entry.rates:
            kept[-1] = replace(kept[-1], as_of=max(kept[-1].as_of, entry.as_of))
            continue
        if kept and kept[-1].effective == entry.effective:
            raise PricingError(f"model '{model}' has two different prices for {entry.effective}")
        kept.append(entry)
    return kept


class PricingTable:
    """The current rates plus every earlier snapshot kept under ``history``.
    A call is priced with the rates in force on its day (``rates_for`` with
    ``at``), unless the table is pinned to one date with ``pin_to``."""

    def __init__(
        self,
        currency: str,
        as_of: str,
        models: dict[str, ModelRates],
        source: str | None = None,
        history: dict[str, list[DatedRates]] | None = None,
    ) -> None:
        self.currency = currency
        self.as_of = as_of
        self.source = source
        self.history = history or {m: [DatedRates(as_of, as_of, r)] for m, r in models.items()}
        # Latest known rates per model; a model a later snapshot dropped keeps its last ones.
        self.models = {model: entries[-1].rates for model, entries in self.history.items()}
        self.pinned: str | None = None
        self.usage: PriceUsage | None = None

    @classmethod
    def load(cls, path: str | Path) -> "PricingTable":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in ("currency", "as_of", "models"):
            if not data.get(key):
                raise PricingError(f"pricing table is missing required field '{key}'")
        snapshots = [data, *(data.get("history") or [])]
        entries: dict[str, list[DatedRates]] = defaultdict(list)
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not snapshot.get("as_of"):
                raise PricingError("every entry under 'history' needs an 'as_of' date")
            as_of = _check_date(snapshot["as_of"], "'as_of'")
            effective = as_of
            if snapshot.get("effective_from"):
                effective = _check_date(snapshot["effective_from"], "'effective_from'")
            for model, rates in _parse_models(snapshot.get("models") or {}).items():
                entries[model].append(DatedRates(effective, as_of, rates))
        history = {model: _compact(found, model) for model, found in entries.items()}
        return cls(data["currency"], data["as_of"], {}, data.get("source"), history)

    def pin_to(self, day: str) -> None:
        """Price every call as of one day, whatever its own date: ``latest``
        is today's rates, the way the table worked before snapshots."""
        self.pinned = _LATEST if day == "latest" else _check_date(day, "--prices-as-of")

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
        entries: dict[str, list[DatedRates]] = defaultdict(list)
        for table in tables:
            for model, rates in table.models.items():
                if model in models and models[model] != rates:
                    raise PricingError(f"model '{model}' is priced differently in two tables")
                models[model] = rates
                entries[model].extend(table.history[model])
        return cls(
            tables[0].currency,
            min(t.as_of for t in tables),
            {},
            " | ".join(t.source for t in tables if t.source) or None,
            {model: _compact(found, model) for model, found in entries.items()},
        )

    def _key_for(self, model: str) -> str | None:
        """Exact match first, then the longest key that the model id extends
        with a date, so claude-sonnet-5-20250929 and gpt-5.5-2026-04-01 find
        claude-sonnet-5 and gpt-5.5. A longer version id never falls back to a
        shorter model: claude-opus-5-5 is unpriced until the table names it,
        rather than silently taking claude-opus-5's rates. Gateway ids such as
        anthropic/claude-haiku-4.5 (OpenRouter) are tried without the vendor
        prefix and with dots turned into hyphens."""
        candidates = [model]
        if "/" in model:
            suffix = model.rsplit("/", 1)[1]
            candidates.extend([suffix, suffix.replace(".", "-")])
        for candidate in candidates:
            if candidate in self.models:
                return candidate
            best = None
            for key in self.models:
                if (
                    candidate.startswith(key)
                    and _DATED_SUFFIX.match(candidate[len(key) :])
                    and (best is None or len(key) > len(best))
                ):
                    best = key
            if best:
                return best
        return None

    def dated_rates_for(self, model: str, at: str | None = None) -> tuple[DatedRates, bool] | None:
        """The snapshot entry that prices ``model`` for a call made at ``at``
        (an ISO timestamp): the latest one in force that day. The flag is true
        when the call is older than the model's first snapshot, which then
        prices it for want of anything earlier. Without ``at``: latest rates."""
        key = self._key_for(model)
        if key is None:
            return None
        entries = self.history[key]
        day = self.pinned or (at[:10] if at else None)
        if day is None:
            return entries[-1], False
        in_force = [entry for entry in entries if entry.effective <= day]
        if in_force:
            return in_force[-1], False
        return entries[0], self.pinned is None

    def rates_for(self, model: str, at: str | None = None) -> ModelRates | None:
        found = self.dated_rates_for(model, at)
        return None if found is None else found[0].rates


@dataclass
class PriceUsage:
    """Which snapshots priced a set of calls, for the disclosure checklist."""

    snapshots: dict[str, int]  # snapshot as_of -> calls it priced
    predating: int  # calls older than their model's first snapshot
    pinned: str | None = None


def describe_usage(records: list[StepRecord], table: PricingTable) -> PriceUsage:
    snapshots: dict[str, int] = defaultdict(int)
    predating = 0
    for record in records:
        found = table.dated_rates_for(record.model, record.timestamp)
        if found is not None:
            snapshots[found[0].as_of] += 1
            predating += found[1]
    pinned = "latest" if table.pinned == _LATEST else table.pinned
    return PriceUsage(dict(sorted(snapshots.items())), predating, pinned)


def prices_line(table: PricingTable) -> str:
    """The prices entry of the disclosure: one date when one snapshot did all
    the pricing, otherwise each snapshot with the calls it priced."""
    usage = table.usage
    if usage is not None and usage.pinned:
        day = "the latest rates" if usage.pinned == "latest" else f"the rates of {usage.pinned}"
        return f"every call at {day} (--prices-as-of), table as of {table.as_of}"
    if usage is None or not usage.snapshots:
        return f"as of {table.as_of}"
    if len(usage.snapshots) == 1:
        return f"as of {next(iter(usage.snapshots))}"
    return "each call at the rates of its day; snapshots as of " + ", ".join(
        f"{day} ({calls} calls)" for day, calls in usage.snapshots.items()
    )


def price_breakdown(record: StepRecord, table: PricingTable) -> dict[str, float] | None:
    """Cost of one step per token class (input, cache_read, cache_write_5m,
    cache_write_1h, output, reasoning) in the table's currency, or None if the
    model is unpriced. The classes add up to ``price_step``."""
    rates = table.rates_for(record.model, record.timestamp)
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
    rates = table.rates_for(record.model, record.timestamp)
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
