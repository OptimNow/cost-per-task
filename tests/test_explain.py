"""cpt explain: one attempt's cost taken apart by token class, model and step."""

from __future__ import annotations

import json

import pytest

from cost_per_task import cli
from cost_per_task.labels import Label, append_label
from cost_per_task.metrics import explain_attempt
from cost_per_task.pricing import PricingTable, price_breakdown, price_step
from cost_per_task.report import render_explain
from cost_per_task.schema import JsonlWriter, StepRecord

RATES = {
    "input_per_mtok": 3.0,
    "cache_read_per_mtok": 0.3,
    "cache_write_5m_per_mtok": 3.75,
    "cache_write_1h_per_mtok": 6.0,
    "output_per_mtok": 15.0,
    "reasoning_per_mtok": None,
}


@pytest.fixture
def table(tmp_path) -> PricingTable:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {"currency": "USD", "as_of": "2026-09-01", "models": {"model-a": RATES, "model-b": RATES}}
        ),
        encoding="utf-8",
    )
    return PricingTable.load(path)


def _records() -> list[StepRecord]:
    """Two steps on two models: 13,125 and 4,050 millionths of a dollar."""
    return [
        StepRecord(
            task_id="t1", attempt_id="a1", step_id=1, timestamp="2026-09-01T10:00:00+00:00",
            provider="test", model="model-a", input_tokens=1000, cache_write_tokens=2000,
            cache_write_1h_tokens=500, output_tokens=100, tool_call_count=2,
            tool_names=["Read", "Edit"], effort="high", task_type="coding",
        ),
        StepRecord(
            task_id="t1", attempt_id="a1", step_id=2, timestamp="2026-09-01T10:01:00+00:00",
            provider="test", model="model-b", cache_read_tokens=10_000, output_tokens=50,
            reasoning_tokens=20, task_type="coding",
        ),
    ]


def test_breakdown_adds_up_to_the_step_price(table):
    for record in _records():
        breakdown = price_breakdown(record, table)
        assert sum(breakdown.values()) == pytest.approx(price_step(record, table))
    first = price_breakdown(_records()[0], table)
    assert first == pytest.approx(
        {"input": 0.003, "cache_read": 0.0, "cache_write_5m": 0.005625,
         "cache_write_1h": 0.003, "output": 0.0015, "reasoning": 0.0}
    )
    unpriced = StepRecord(task_id="t", attempt_id="a", step_id=1, timestamp="", provider="x", model="?")
    assert price_breakdown(unpriced, table) is None


def test_explanation_by_class_model_and_step(table):
    labels = {("t1", "a1"): Label("t1", "a1", "pass", leaked=True)}
    explanation = explain_attempt(_records(), table, labels)
    a = explanation.attempt
    assert a.cost == pytest.approx(0.017175) and a.model == "model-a" and a.leaked

    assert [c.key for c in explanation.classes] == [
        "input", "cache_read", "cache_write_5m", "cache_write_1h", "output", "reasoning",
    ]
    by_class = {c.key: c for c in explanation.classes}
    assert (by_class["cache_write_1h"].tokens, by_class["cache_write_1h"].cost) == (500, pytest.approx(0.003))
    assert (by_class["reasoning"].tokens, by_class["reasoning"].cost) == (20, pytest.approx(0.0003))
    assert (by_class["output"].tokens, by_class["output"].cost) == (150, pytest.approx(0.00225))
    assert sum(c.cost for c in explanation.classes) == pytest.approx(a.cost)

    assert [(m, n) for m, n, _ in explanation.by_model] == [("model-a", 1), ("model-b", 1)]
    assert explanation.by_model[0][2] == pytest.approx(0.013125)
    assert explanation.largest_prompt.step_id == 2
    assert explanation.steps[0].tool_names == ["Read", "Edit"]
    assert explanation.steps[1].output_tokens == 70  # output and reasoning together

    text = render_explain(explanation, table)
    assert "attempt a1" in text and "outcome pass+leak" in text and "effort high" in text
    assert "model model-a (also model-b)" in text
    assert "by token class" in text and "cache write 1h" in text and "reasoning" in text
    assert "by model" in text and "most expensive steps (2 of 2)" in text
    assert "largest prompt: 10,000 tokens at step 2" in text
    assert "most expensive steps" not in render_explain(explanation, table, top_steps=0)


def test_classes_without_tokens_are_left_out_except_the_common_four(table):
    explanation = explain_attempt(_records()[:1], table, {})
    assert [c.key for c in explanation.classes] == [
        "input", "cache_read", "cache_write_5m", "cache_write_1h", "output",
    ]
    assert "by model" not in render_explain(explanation, table)


def test_records_of_several_attempts_are_refused(table):
    other = StepRecord(task_id="t1", attempt_id="a2", step_id=1, timestamp="", provider="x", model="model-a")
    with pytest.raises(ValueError):
        explain_attempt([*_records(), other], table, {})


def test_cli_explain_picks_and_matches_attempts(tmp_path, table, capsys):
    log = tmp_path / "log.jsonl"
    writer = JsonlWriter(log)
    for record in _records():
        writer.append(record)
    writer.append(
        StepRecord(
            task_id="t2", attempt_id="a2", step_id=1, timestamp="2026-09-01T11:00:00+00:00",
            provider="test", model="model-a", input_tokens=10,
        )
    )
    labels = tmp_path / "labels.jsonl"
    append_label(labels, Label("t1", "a1", "fail"))
    prices = str(tmp_path / "prices.json")
    base = ["explain", "--log", str(log), "--labels", str(labels), "--prices", prices]

    assert cli.main(base) == 0  # the latest attempt in the log
    assert "attempt a2" in capsys.readouterr().out
    assert cli.main([*base, "--task", "t1"]) == 0
    out = capsys.readouterr().out
    assert "attempt a1" in out and "outcome fail" in out and "task t1 [coding]" in out
    assert cli.main([*base, "--attempt", "a"]) == 1
    assert "matches 2 attempts: a1, a2" in capsys.readouterr().err
    assert cli.main([*base, "--attempt", "a1"]) == 0
    assert "attempt a1" in capsys.readouterr().out
    assert cli.main([*base, "--attempt", "zz"]) == 1
    assert "matches 0 attempts" in capsys.readouterr().err
    assert cli.main(["explain", "--log", str(tmp_path / "missing.jsonl"), "--prices", prices]) == 1


def test_fast_mode_steps_are_priced_at_fast_rates_and_marked(tmp_path):
    fast_rates = {k: (v * 2 if isinstance(v, float) else v) for k, v in RATES.items()}
    path = tmp_path / "fast-prices.json"
    path.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01",
                    "models": {"model-a": {**RATES, "fast": fast_rates}, "model-b": RATES}}),
        encoding="utf-8",
    )
    fast_table = PricingTable.load(path)
    records = _records()
    records[0].speed = "fast"  # model-a, has fast rates: doubled
    records[1].speed = "fast"  # model-b, none: standard rates, counted
    assert price_step(records[0], fast_table) == pytest.approx(2 * 13_125 / 1_000_000)
    assert price_step(records[1], fast_table) == pytest.approx(4_050 / 1_000_000)
    from cost_per_task.pricing import describe_usage

    fast_table.usage = describe_usage(records, fast_table)
    explanation = explain_attempt(records, fast_table, {})
    text = render_explain(explanation, fast_table)
    assert "fast mode on 2 steps" in text
    assert "warning: fast mode on 1 calls, priced at fast rates; 1 fast mode calls priced at standard rates, no fast rates in the table for model-b (1)" in text
    assert "model-a fast" in text and "model-b fast" in text
