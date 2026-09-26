from __future__ import annotations

import json

import pytest

from cost_per_task.pricing import (
    PricingError,
    PricingTable,
    cost_per_attempt,
    describe_usage,
    price_breakdown,
    price_step,
    speed_line,
)
from cost_per_task.schema import StepRecord

TABLE = {
    "currency": "USD",
    "as_of": "2026-08-26",
    "source": "test fixture, not real prices",
    "models": {
        "test-model": {
            "input_per_mtok": 3.0,
            "cache_read_per_mtok": 0.3,
            "cache_write_5m_per_mtok": 3.75,
            "cache_write_1h_per_mtok": 6.0,
            "output_per_mtok": 15.0,
            "reasoning_per_mtok": None,
        }
    },
}


def _table(tmp_path, data=TABLE) -> PricingTable:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return PricingTable.load(path)


def _record(**overrides) -> StepRecord:
    defaults = dict(
        task_id="T1",
        attempt_id="a1",
        step_id=1,
        timestamp="2026-08-26T10:00:00+00:00",
        provider="anthropic",
        model="test-model",
    )
    defaults.update(overrides)
    return StepRecord(**defaults)


def test_price_step_hand_computed(tmp_path):
    table = _table(tmp_path)
    record = _record(
        input_tokens=1_000_000,  # 3.00
        cache_read_tokens=1_000_000,  # 0.30
        cache_write_tokens=200_000,  # 100k at 5m + 100k at 1h
        cache_write_1h_tokens=100_000,  # 0.375 + 0.60
        output_tokens=100_000,  # 1.50
    )
    assert price_step(record, table) == pytest.approx(3.0 + 0.3 + 0.375 + 0.6 + 1.5)


def test_prefix_match_for_dated_model_ids(tmp_path):
    table = _table(tmp_path)
    record = _record(model="test-model-20260801", input_tokens=1_000_000)
    assert price_step(record, table) == pytest.approx(3.0)


def test_longer_version_id_never_matches_a_shorter_model(tmp_path):
    # Opus 5.5 must not be billed at Opus 5 rates when the table lacks it:
    # a silent 25% over-price on input and output, 2.5x on cache reads.
    data = json.loads(json.dumps(TABLE))
    data["models"] = {"claude-opus-5": TABLE["models"]["test-model"]}
    table = _table(tmp_path, data)
    assert table.rates_for("claude-opus-5") is not None
    assert table.rates_for("claude-opus-5-20260101") is not None
    assert table.rates_for("claude-opus-5-5") is None
    assert table.rates_for("claude-opus-5-5-20260925") is None
    assert table.rates_for("claude-opus-56") is None
    assert price_step(_record(model="claude-opus-5-5"), table) is None


def test_dated_suffix_forms_and_longest_key(tmp_path):
    data = json.loads(json.dumps(TABLE))
    rates = TABLE["models"]["test-model"]
    data["models"] = {"gpt-5.4": rates, "gpt-5.4-mini": {**rates, "output_per_mtok": 1.0}}
    table = _table(tmp_path, data)
    assert table.rates_for("gpt-5.4-2026-03-01").output_per_mtok == 15.0
    assert table.rates_for("gpt-5.4-mini-2026-03-01").output_per_mtok == 1.0
    assert table.rates_for("gpt-5.4-mini-2026-03-01-preview").output_per_mtok == 1.0
    assert table.rates_for("gpt-5.4-nano") is None
    assert table.rates_for("gpt-5.4-2026") is None


def test_shipped_table_prices_opus_5_5_at_its_own_rates():
    from cost_per_task.pricing import default_table_paths

    table = PricingTable.load_many(default_table_paths())
    rates = table.rates_for("claude-opus-5-5")
    assert rates is not None
    assert (
        rates.input_per_mtok,
        rates.cache_read_per_mtok,
        rates.cache_write_5m_per_mtok,
        rates.cache_write_1h_per_mtok,
        rates.output_per_mtok,
    ) == (4.0, 0.2, 5.0, 8.0, 20.0)
    assert rates != table.rates_for("claude-opus-5")
    assert table.rates_for("claude-fable-5-2") is None


def test_gateway_model_ids_match_without_vendor_prefix(tmp_path):
    data = json.loads(json.dumps(TABLE))
    data["models"]["claude-haiku-4-5"] = data["models"].pop("test-model")
    table = _table(tmp_path, data)
    assert table.rates_for("anthropic/claude-haiku-4.5") is not None
    assert table.rates_for("anthropic/claude-haiku-4-5-20251001") is not None
    assert table.rates_for("openai/gpt-5.5") is None


def test_load_many_merges_vendor_tables(tmp_path):
    anthropic = json.loads(json.dumps(TABLE))
    anthropic["models"] = {"claude-x": TABLE["models"]["test-model"]}
    anthropic["as_of"] = "2026-08-27"
    openai = json.loads(json.dumps(TABLE))
    openai["models"] = {"gpt-y": TABLE["models"]["test-model"]}
    openai["as_of"] = "2026-09-02"
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(anthropic), encoding="utf-8")
    b.write_text(json.dumps(openai), encoding="utf-8")
    merged = PricingTable.load_many([a, b])
    assert set(merged.models) == {"claude-x", "gpt-y"}
    assert merged.as_of == "2026-08-27"  # the oldest, never overstating freshness

    conflicting = json.loads(json.dumps(openai))
    conflicting["models"] = {"claude-x": {**TABLE["models"]["test-model"], "output_per_mtok": 99.0}}
    c = tmp_path / "c.json"
    c.write_text(json.dumps(conflicting), encoding="utf-8")
    with pytest.raises(PricingError):
        PricingTable.load_many([a, c])


def test_unpriced_model_returns_none(tmp_path):
    table = _table(tmp_path)
    assert price_step(_record(model="other-model"), table) is None


def test_missing_as_of_rejected(tmp_path):
    data = dict(TABLE)
    del data["as_of"]
    with pytest.raises(PricingError):
        _table(tmp_path, data)


def test_non_numeric_rate_rejected(tmp_path):
    data = json.loads(json.dumps(TABLE))
    data["models"]["test-model"]["input_per_mtok"] = "PLACEHOLDER"
    with pytest.raises(PricingError):
        _table(tmp_path, data)


def test_reasoning_tokens_priced_at_output_rate_by_default(tmp_path):
    table = _table(tmp_path)
    record = _record(output_tokens=100_000, reasoning_tokens=100_000)
    assert price_step(record, table) == pytest.approx(1.5 + 1.5)


def test_reasoning_tokens_use_explicit_rate_when_given(tmp_path):
    data = json.loads(json.dumps(TABLE))
    data["models"]["test-model"]["reasoning_per_mtok"] = 5.0
    table = _table(tmp_path, data)
    record = _record(reasoning_tokens=1_000_000)
    assert price_step(record, table) == pytest.approx(5.0)


def test_cost_per_attempt_groups_and_sums(tmp_path):
    table = _table(tmp_path)
    records = [
        _record(step_id=1, output_tokens=100_000),  # 1.50
        _record(step_id=2, input_tokens=1_000_000),  # 3.00
        _record(attempt_id="a2", step_id=1, input_tokens=2_000_000),  # 6.00
    ]
    totals = cost_per_attempt(records, table)
    assert totals[("T1", "a1")] == pytest.approx(4.5)
    assert totals[("T1", "a2")] == pytest.approx(6.0)


FAST = {
    "input_per_mtok": 6.0,
    "cache_read_per_mtok": 0.6,
    "cache_write_5m_per_mtok": 7.5,
    "cache_write_1h_per_mtok": 12.0,
    "output_per_mtok": 30.0,
    "reasoning_per_mtok": None,
}


def test_fast_mode_calls_take_the_fast_rates_when_the_table_has_them(tmp_path):
    data = json.loads(json.dumps(TABLE))
    data["models"]["test-model"]["fast"] = FAST
    table = _table(tmp_path, data)
    tokens = dict(input_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=200_000,
                  cache_write_1h_tokens=100_000, output_tokens=100_000)
    standard = price_step(_record(**tokens), table)
    fast = price_step(_record(speed="fast", **tokens), table)
    assert standard == pytest.approx(3.0 + 0.3 + 0.375 + 0.6 + 1.5)
    assert fast == pytest.approx(2 * standard)  # every class doubled in this fixture
    assert price_step(_record(speed="standard", **tokens), table) == standard
    breakdown = price_breakdown(_record(speed="fast", **tokens), table)
    assert sum(breakdown.values()) == pytest.approx(fast)
    assert breakdown["output"] == pytest.approx(3.0)
    assert table.rates_for("test-model").fast.output_per_mtok == 30.0


def test_fast_mode_calls_without_fast_rates_take_standard_rates_and_are_counted(tmp_path):
    table = _table(tmp_path)  # no fast block
    records = [
        _record(speed="fast", input_tokens=1_000_000),
        _record(step_id=2, speed="fast", input_tokens=1_000_000),
        _record(step_id=3, input_tokens=1_000_000),
    ]
    assert price_step(records[0], table) == pytest.approx(3.0)
    usage = describe_usage(records, table)
    assert usage.fast_calls == 0
    assert usage.fast_at_standard == {"test-model": 2}
    table.usage = usage
    assert speed_line(table) == (
        "2 fast mode calls priced at standard rates, no fast rates in the table for test-model (2)"
    )
    data = json.loads(json.dumps(TABLE))
    data["models"]["test-model"]["fast"] = FAST
    priced = _table(tmp_path, data)
    priced.usage = describe_usage(records, priced)
    assert (priced.usage.fast_calls, priced.usage.fast_at_standard) == (2, {})
    assert speed_line(priced) == "fast mode on 2 calls, priced at fast rates"
    priced.usage = describe_usage(records[2:], priced)
    assert speed_line(priced) == "no fast mode calls"


def test_fast_rates_are_validated_like_the_standard_ones(tmp_path):
    data = json.loads(json.dumps(TABLE))
    data["models"]["test-model"]["fast"] = {**FAST, "output_per_mtok": "40"}
    with pytest.raises(PricingError, match="fast.'output_per_mtok'"):
        _table(tmp_path, data)
    data["models"]["test-model"]["fast"] = 2.0
    with pytest.raises(PricingError, match="fast"):
        _table(tmp_path, data)


def test_shipped_table_prices_fast_mode_for_the_models_that_offer_it():
    from cost_per_task.pricing import default_table_paths

    table = PricingTable.load_many(default_table_paths())
    fast = table.rates_for("claude-opus-5-5").fast
    assert (fast.input_per_mtok, fast.cache_read_per_mtok, fast.output_per_mtok) == (8.0, 0.4, 40.0)
    assert (fast.cache_write_5m_per_mtok, fast.cache_write_1h_per_mtok) == (10.0, 16.0)
    for model in ("claude-opus-5", "claude-opus-4-8"):
        fast = table.rates_for(model).fast
        assert (fast.input_per_mtok, fast.cache_read_per_mtok, fast.output_per_mtok) == (10.0, 1.0, 50.0)
    for model in ("claude-fable-5-1", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-7"):
        assert table.rates_for(model).fast is None
    assert "fast mode" in table.source
