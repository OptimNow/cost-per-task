"""Dated price snapshots: a call keeps the rates of its day, whatever the
table says today. Rates here are test values, not real prices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_per_task import cli
from cost_per_task.prices_hub import with_history
from cost_per_task.pricing import (
    PricingError,
    PricingTable,
    describe_usage,
    price_step,
    prices_line,
)
from cost_per_task.schema import StepRecord


def _rates(input_rate: float) -> dict:
    return {
        "input_per_mtok": input_rate,
        "cache_read_per_mtok": 0.0,
        "cache_write_5m_per_mtok": 0.0,
        "cache_write_1h_per_mtok": 0.0,
        "output_per_mtok": 0.0,
        "reasoning_per_mtok": None,
    }


def _snapshot(as_of: str, **models: float) -> dict:
    return {
        "currency": "USD",
        "as_of": as_of,
        "source": "test fixture, not real prices",
        "models": {name.replace("_", "-"): _rates(rate) for name, rate in models.items()},
    }


# Current rates on top, two older snapshots under history; new-model only
# appears in the latest one.
TABLE = {
    **_snapshot("2026-09-10", test_model=1.0, new_model=7.0),
    "history": [
        _snapshot("2026-08-01", test_model=2.0),
        _snapshot("2026-06-01", test_model=4.0),
    ],
}


def _load(tmp_path, data=TABLE, name="prices.json") -> PricingTable:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return PricingTable.load(path)


def _call(day: str, model: str = "test-model") -> StepRecord:
    return StepRecord(
        task_id="T1",
        attempt_id="a1",
        step_id=1,
        timestamp=f"{day}T10:00:00+00:00",
        provider="test",
        model=model,
        input_tokens=1_000_000,
    )


def test_each_call_is_priced_at_the_rates_of_its_day(tmp_path):
    table = _load(tmp_path)
    assert price_step(_call("2026-06-15"), table) == pytest.approx(4.0)
    assert price_step(_call("2026-08-01"), table) == pytest.approx(2.0)  # the day it starts
    assert price_step(_call("2026-09-09"), table) == pytest.approx(2.0)
    assert price_step(_call("2026-09-20"), table) == pytest.approx(1.0)
    # Without a date (a lookup, not a call): the latest rates.
    assert table.rates_for("test-model").input_per_mtok == 1.0
    assert table.as_of == "2026-09-10"


def test_a_call_older_than_every_snapshot_takes_the_oldest_and_is_counted(tmp_path):
    table = _load(tmp_path)
    assert price_step(_call("2026-01-05"), table) == pytest.approx(4.0)
    # new-model was first read on 2026-09-10; a call from August still gets a price.
    assert price_step(_call("2026-08-20", "new-model"), table) == pytest.approx(7.0)
    usage = describe_usage(
        [_call("2026-01-05"), _call("2026-08-20", "new-model"), _call("2026-09-20")], table
    )
    assert usage.predating == 2
    assert usage.snapshots == {"2026-06-01": 1, "2026-09-10": 2}


def test_a_table_without_history_prices_as_before(tmp_path):
    table = _load(tmp_path, _snapshot("2026-09-10", test_model=1.0))
    assert price_step(_call("2026-09-20"), table) == pytest.approx(1.0)
    assert price_step(_call("2026-03-01"), table) == pytest.approx(1.0)
    table.usage = describe_usage([_call("2026-09-20")], table)
    assert prices_line(table) == "as of 2026-09-10"


def test_effective_from_wins_over_as_of(tmp_path):
    data = {**_snapshot("2026-09-10", test_model=1.0), "effective_from": "2026-09-01",
            "history": [_snapshot("2026-08-01", test_model=2.0)]}
    table = _load(tmp_path, data)
    assert price_step(_call("2026-09-05"), table) == pytest.approx(1.0)
    assert price_step(_call("2026-08-31"), table) == pytest.approx(2.0)


def test_unchanged_rates_keep_their_earliest_date(tmp_path):
    data = {**_snapshot("2026-09-10", test_model=2.0),
            "history": [_snapshot("2026-08-01", test_model=2.0)]}
    table = _load(tmp_path, data)
    usage = describe_usage([_call("2026-08-15")], table)
    assert usage.predating == 0  # known from 2026-08-01
    assert usage.snapshots == {"2026-09-10": 1}  # and shown with the day they were last confirmed


def test_pinning_prices_every_call_at_one_day(tmp_path):
    table = _load(tmp_path)
    table.pin_to("2026-08-15")
    assert price_step(_call("2026-06-15"), table) == pytest.approx(2.0)
    assert price_step(_call("2026-09-20"), table) == pytest.approx(2.0)
    table.pin_to("latest")
    assert price_step(_call("2026-06-15"), table) == pytest.approx(1.0)
    table.usage = describe_usage([_call("2026-06-15")], table)
    assert table.usage.predating == 0
    assert "latest rates" in prices_line(table)
    with pytest.raises(PricingError):
        table.pin_to("last week")


def test_bad_history_is_rejected(tmp_path):
    undated = {**_snapshot("2026-09-10", test_model=1.0), "history": [{"models": {}}]}
    with pytest.raises(PricingError, match="as_of"):
        _load(tmp_path, undated)
    clash = {**_snapshot("2026-09-10", test_model=1.0),
             "history": [_snapshot("2026-09-10", test_model=2.0)]}
    with pytest.raises(PricingError, match="two different prices"):
        _load(tmp_path, clash)


def test_load_many_keeps_each_vendor_history(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(TABLE), encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text(json.dumps(_snapshot("2026-07-01", other_model=9.0)), encoding="utf-8")
    table = PricingTable.load_many([a, b])
    assert table.as_of == "2026-07-01"
    assert price_step(_call("2026-06-15"), table) == pytest.approx(4.0)
    assert price_step(_call("2026-09-20", "other-model"), table) == pytest.approx(9.0)


def test_with_history_keeps_the_previous_snapshot():
    old = _snapshot("2026-08-01", test_model=2.0)
    new = _snapshot("2026-09-10", test_model=1.0)
    merged = with_history(old, new)
    assert merged["as_of"] == "2026-09-10"
    assert merged["history"] == [old]
    # A second change stacks, newest first, and history never nests.
    newer = _snapshot("2026-10-01", test_model=0.5)
    again = with_history(merged, newer)
    assert [h["as_of"] for h in again["history"]] == ["2026-09-10", "2026-08-01"]
    assert all("history" not in h for h in again["history"])
    assert with_history(None, new) == new


def test_with_history_on_unchanged_rates_keeps_the_known_from_date():
    old = _snapshot("2026-08-01", test_model=2.0)
    merged = with_history(old, _snapshot("2026-09-10", test_model=2.0))
    assert merged["as_of"] == "2026-09-10"
    assert merged["effective_from"] == "2026-08-01"
    assert "history" not in merged
    # And once more: the first date still stands.
    again = with_history(merged, _snapshot("2026-10-01", test_model=2.0))
    assert again["effective_from"] == "2026-08-01"


def test_refresh_write_then_report_prices_by_day(tmp_path, capsys):
    """End to end: a table written before a price change keeps pricing the
    calls made before it, and --prices-as-of re-prices them on purpose."""
    hub = json.loads((Path(__file__).parent / "fixtures" / "hub_sample.json").read_text(encoding="utf-8"))
    out = tmp_path / "anthropic.json"
    hub_file = tmp_path / "hub.json"

    hub_file.write_text(json.dumps(hub), encoding="utf-8")
    args = ["prices", "refresh", "--provider", "anthropic", "--from-file", str(hub_file), "--out", str(out), "--write"]
    assert cli.main(args) == 0
    first = json.loads(out.read_text(encoding="utf-8"))
    model, before = next(iter(first["models"].items()))

    # The hub, a month later, with that model's input price halved.
    hub["meta"]["timestamp"] = "2026-10-02T07:00:00.000Z"
    for entry in hub["models"]:
        if entry.get("inputPricePer1M") == before["input_per_mtok"] and entry.get("provider") == "Anthropic":
            entry["inputPricePer1M"] = before["input_per_mtok"] / 2
    hub_file.write_text(json.dumps(hub), encoding="utf-8")
    assert cli.main(args) == 0
    assert "1 earlier snapshots kept under history" in capsys.readouterr().out
    second = json.loads(out.read_text(encoding="utf-8"))
    assert second["as_of"] == "2026-10-02"
    assert second["history"][0]["as_of"] == first["as_of"]

    log = tmp_path / "log.jsonl"
    early = _call("2026-09-05", model)
    late = _call("2026-10-05", model)
    late.attempt_id = "a2"
    log.write_text(early.to_json() + "\n" + late.to_json() + "\n", encoding="utf-8")
    labels = tmp_path / "labels.jsonl"
    labels.write_text("", encoding="utf-8")
    base = ["report", "--log", str(log), "--labels", str(labels), "--prices", str(out), "--json", "--resamples", "10"]

    assert cli.main(base) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prices"]["snapshots_used"] == {first["as_of"]: 1, "2026-10-02": 1}
    total = sum(g["total_cost"] for g in payload["groups"])
    assert total == pytest.approx(before["input_per_mtok"] * 1.5)

    assert cli.main(base + ["--prices-as-of", "latest"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sum(g["total_cost"] for g in payload["groups"]) == pytest.approx(before["input_per_mtok"])
    assert payload["prices"]["pinned_to"] == "latest"


def test_refresh_carries_hand_entered_fast_rates_forward():
    from cost_per_task.prices_hub import carry_fast_rates

    fast = {**_rates(4.0), "output_per_mtok": 20.0}
    old = _snapshot("2026-09-01", test_model=2.0, other_model=1.0)
    old["models"]["test-model"]["fast"] = fast
    old["models"]["other-model"]["fast"] = fast
    # The hub never serves fast rates: a fresh build has none.
    new = _snapshot("2026-09-10", test_model=2.0, other_model=1.5)
    notes = carry_fast_rates(old, new)
    assert new["models"]["test-model"]["fast"] == fast  # standard rates unchanged: kept
    assert "fast" not in new["models"]["other-model"]  # standard rates moved: dropped, not guessed
    assert notes == [
        "! other-model: fast mode rates dropped because its standard rates changed; "
        "read them again from the vendor price list before writing",
        "= test-model: fast mode rates kept from the previous table",
    ]
    assert carry_fast_rates(None, new) == []
    # Kept rates count as unchanged, so no new snapshot is stacked for them alone.
    same = _snapshot("2026-09-10", test_model=2.0)
    previous = _snapshot("2026-09-01", test_model=2.0)
    previous["models"]["test-model"]["fast"] = fast
    carry_fast_rates(previous, same)
    merged = with_history(previous, same)
    assert merged["effective_from"] == "2026-09-01" and "history" not in merged
    assert merged["models"]["test-model"]["fast"] == fast
