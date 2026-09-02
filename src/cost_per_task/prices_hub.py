"""Refresh pricing tables from the OptimNow AI Pricing Hub.

The hub's source of truth is ``GET https://optimtoken.optimnow.io/api/llm-models``,
a JSON catalogue with per-model input, output and cached-input prices per
million tokens plus a catalogue timestamp. It does not carry cache-write or
reasoning rates, so those come from documented per-provider rules below, and
``reasoning_per_mtok`` stays null (billed at the output rate).

Refreshing is a diff by default: nothing is written unless asked, so an
upstream anomaly never lands in the repository unseen. Models without a
cached-input price are skipped rather than guessed.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HUB_URL = "https://optimtoken.optimnow.io/api/llm-models"

_RATE_KEYS = (
    "input_per_mtok",
    "cache_read_per_mtok",
    "cache_write_5m_per_mtok",
    "cache_write_1h_per_mtok",
    "output_per_mtok",
    "reasoning_per_mtok",
)


@dataclass(frozen=True)
class ProviderRules:
    hub_provider: str
    id_prefix: str
    normalise: Callable[[str], str]
    cache_write_multipliers: Callable[[str], tuple[float, float]]
    source_note: str


def _openai_cache_write(model_id: str) -> tuple[float, float]:
    # No cache-write charge up to GPT-5.5; 1.25x input from GPT-5.6
    # (developers.openai.com prompt caching guide, checked 2026-09-02).
    match = re.match(r"gpt-(\d+)\.(\d+)", model_id)
    if match and (int(match.group(1)), int(match.group(2))) >= (5, 6):
        return 1.25, 1.25
    return 0.0, 0.0


PROVIDERS: dict[str, ProviderRules] = {
    "anthropic": ProviderRules(
        hub_provider="Anthropic",
        id_prefix="anthropic/",
        normalise=lambda suffix: suffix.replace(".", "-"),
        cache_write_multipliers=lambda _model: (1.25, 2.0),
        source_note=(
            "cache writes 1.25x (5m) and 2x (1h) of input per the platform.claude.com "
            "prompt caching pricing table (checked 2026-08-27)"
        ),
    ),
    "openai": ProviderRules(
        hub_provider="OpenAI",
        id_prefix="openai/",
        normalise=lambda suffix: suffix,
        cache_write_multipliers=_openai_cache_write,
        source_note=(
            "no cache-write charge up to GPT-5.5 and 1.25x input from GPT-5.6 per the "
            "developers.openai.com prompt caching guide (checked 2026-09-02); short-context "
            "tier only"
        ),
    ),
}


def fetch_hub(url: str = HUB_URL, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_hub(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_table(
    hub: dict, provider: str, overrides: dict[str, str] | None = None
) -> tuple[dict, list[str]]:
    """Turn the hub catalogue into a pricing table for one provider.

    Returns the table and a list of warnings (skipped or suspicious models).
    ``overrides`` maps a hub id (``anthropic/claude-haiku-4.5``) to the API
    model id to use instead of the normalised default.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider '{provider}'; choose from {', '.join(PROVIDERS)}")
    rules = PROVIDERS[provider]
    overrides = overrides or {}
    warnings: list[str] = []
    models: dict[str, dict] = {}

    for entry in hub.get("models", []):
        if entry.get("provider") != rules.hub_provider:
            continue
        hub_id = entry.get("openRouterId") or ""
        name = entry.get("model") or hub_id or "?"
        if not hub_id.startswith(rules.id_prefix):
            warnings.append(f"skipped {name}: hub id '{hub_id}' has no {rules.id_prefix} prefix")
            continue
        api_id = overrides.get(hub_id) or rules.normalise(hub_id[len(rules.id_prefix):])
        input_rate = entry.get("inputPricePer1M")
        output_rate = entry.get("outputPricePer1M")
        cached_rate = entry.get("cachedInputPricePer1M")
        if not (_is_number(input_rate) and _is_number(output_rate)):
            warnings.append(f"skipped {name}: no input/output price in the hub")
            continue
        if not _is_number(cached_rate):
            warnings.append(f"skipped {name}: hub has no cached-input price, not guessing")
            continue
        # Vendors price cache reads at 0.1x to 0.5x of input; anything outside
        # 0.05x to 1x is most likely an upstream feed error.
        if cached_rate > input_rate or (input_rate > 0 and cached_rate < 0.05 * input_rate):
            warnings.append(
                f"suspicious {name} ({api_id}): cache read {cached_rate} vs input {input_rate}; "
                "check against the vendor price list before writing"
            )
        write_5m, write_1h = rules.cache_write_multipliers(api_id)
        models[api_id] = {
            "input_per_mtok": float(input_rate),
            "cache_read_per_mtok": float(cached_rate),
            "cache_write_5m_per_mtok": round(float(input_rate) * write_5m, 6),
            "cache_write_1h_per_mtok": round(float(input_rate) * write_1h, 6),
            "output_per_mtok": float(output_rate),
            "reasoning_per_mtok": None,
        }

    timestamp = (hub.get("meta") or {}).get("timestamp") or ""
    table = {
        "currency": "USD",
        "as_of": timestamp[:10] if timestamp else date.today().isoformat(),
        "source": (
            f"OptimNow AI Pricing Hub ({HUB_URL}, catalogue timestamp {timestamp or 'unknown'}); "
            f"{rules.source_note}"
        ),
        "models": dict(sorted(models.items())),
    }
    return table, warnings


def diff_tables(old: dict | None, new: dict) -> list[str]:
    """Human-readable differences between the committed table and a new one."""
    lines: list[str] = []
    old_models = (old or {}).get("models", {})
    new_models = new.get("models", {})
    if old is not None and old.get("as_of") != new.get("as_of"):
        lines.append(f"as_of: {old.get('as_of')} -> {new.get('as_of')}")
    for model in sorted(set(new_models) - set(old_models)):
        rates = new_models[model]
        lines.append(
            f"+ {model}: input {rates['input_per_mtok']}, cache read {rates['cache_read_per_mtok']}, "
            f"output {rates['output_per_mtok']}"
        )
    for model in sorted(set(old_models) - set(new_models)):
        lines.append(f"- {model}: no longer in the hub (kept only if you skip --write)")
    for model in sorted(set(old_models) & set(new_models)):
        for key in _RATE_KEYS:
            before, after = old_models[model].get(key), new_models[model].get(key)
            if before != after:
                lines.append(f"~ {model}.{key}: {before} -> {after}")
    return lines


def write_table(table: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
