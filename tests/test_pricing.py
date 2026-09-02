from __future__ import annotations

import json

import pytest

from cost_per_task.pricing import PricingError, PricingTable, cost_per_attempt, price_step
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
