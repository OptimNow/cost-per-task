"""Synthetic dataset where 1 input token costs 1 currency unit, so attempt
costs can be read straight off the records."""

from __future__ import annotations

import json

import pytest

from cost_per_task.labels import Label
from cost_per_task.metrics import break_even_cleanup_cost, build_attempts, summarise
from cost_per_task.pricing import PricingTable, describe_usage
from cost_per_task.report import render_comparison, render_report
from cost_per_task.schema import StepRecord

UNIT_RATES = {
    "input_per_mtok": 1_000_000.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 0.0,
}


@pytest.fixture
def table(tmp_path) -> PricingTable:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "currency": "USD",
                "as_of": "2026-09-01",
                "models": {"model-a": UNIT_RATES, "model-b": UNIT_RATES},
            }
        ),
        encoding="utf-8",
    )
    return PricingTable.load(path)


def _step(task, attempt, minute, model, cost, step_id=1, task_type="coding"):
    return StepRecord(
        task_id=task,
        attempt_id=attempt,
        step_id=step_id,
        timestamp=f"2026-09-01T10:{minute:02d}:00+00:00",
        provider="test",
        model=model,
        input_tokens=cost,
        task_type=task_type,
    )


def _dataset():
    # model-a: four tasks, two attempts each. Costs and outcomes:
    # t1: 1 pass, 1 pass | t2: 2 fail, 2 pass (leaked) | t3: 1 pass, 3 pass | t4: 2 fail, 2 fail
    records = [
        _step("t1", "a1", 0, "model-a", 1), _step("t1", "a2", 1, "model-a", 1),
        _step("t2", "a1", 2, "model-a", 2), _step("t2", "a2", 3, "model-a", 2),
        _step("t3", "a1", 4, "model-a", 1), _step("t3", "a2", 5, "model-a", 3),
        _step("t4", "a1", 6, "model-a", 2), _step("t4", "a2", 7, "model-a", 2),
        # model-b: two tasks, one attempt each, both pass
        _step("t5", "b1", 8, "model-b", 4), _step("t6", "b1", 9, "model-b", 6),
    ]
    outcomes = {
        ("t1", "a1"): ("pass", False), ("t1", "a2"): ("pass", False),
        ("t2", "a1"): ("fail", False), ("t2", "a2"): ("pass", True),
        ("t3", "a1"): ("pass", False), ("t3", "a2"): ("pass", False),
        ("t4", "a1"): ("fail", False), ("t4", "a2"): ("fail", False),
        ("t5", "b1"): ("pass", False), ("t6", "b1"): ("pass", False),
    }
    labels = {
        key: Label(key[0], key[1], outcome, leaked=leaked) for key, (outcome, leaked) in outcomes.items()
    }
    return records, labels


def test_group_summary_matches_hand_computation(table):
    records, labels = _dataset()
    attempts = build_attempts(records, table, labels)
    assert len(attempts) == 10

    summaries = summarise(attempts, cleanup_cost=10.0, resamples=300, seed=3)
    a = next(s for s in summaries if s.model == "model-a")
    b = next(s for s in summaries if s.model == "model-b")

    assert a.task_type == "coding"
    assert (a.attempts, a.tasks, a.labelled, a.successes, a.failures, a.leaks) == (8, 4, 8, 5, 3, 1)
    assert a.mean_cost == pytest.approx(1.75)
    assert a.p90_cost == pytest.approx(2.3)
    assert a.success_rate == pytest.approx(0.625)
    assert a.success_interval[0] < 0.625 < a.success_interval[1]
    assert a.cpt_solved == pytest.approx(2.8)
    assert a.cpt_solved_interval is not None
    assert a.cost_per_task_attempted == pytest.approx(3.5)
    assert a.retry_cap == 2
    assert a.capped_success == pytest.approx(1 - 0.375**2)
    assert a.k == 2
    assert a.pass_k == pytest.approx(0.5)
    assert a.leak_rate == pytest.approx(0.2)
    assert a.cpt_risk == pytest.approx(2.8 + 0.2 * 10.0)
    assert a.cache_hit_rate == 0.0

    assert b.cpt_solved == pytest.approx(5.0)
    assert b.leak_rate == 0.0
    assert b.pass_k is None  # one attempt per task, no consistency measure
    assert break_even_cleanup_cost(a, b) == pytest.approx(11.0)


def test_leak_rate_override_and_unlabelled(table):
    records, labels = _dataset()
    attempts = build_attempts(records, table, {})
    summary = summarise(attempts, resamples=10)[0]
    assert summary.success_rate is None
    assert summary.cpt_solved is None
    assert summary.mean_cost > 0

    attempts = build_attempts(records, table, labels)
    a = next(s for s in summarise(attempts, leak_rate=0.05, cleanup_cost=100.0, resamples=10) if s.model == "model-a")
    assert a.leak_rate == 0.05
    assert a.cpt_risk == pytest.approx(2.8 + 5.0)


def test_primary_model_is_the_largest_cost_share(table):
    records = [
        _step("t1", "a1", 0, "model-a", 1, step_id=1),
        _step("t1", "a1", 0, "model-b", 9, step_id=2),
    ]
    attempts = build_attempts(records, table, {})
    assert attempts[0].model == "model-b"
    assert attempts[0].models == {"model-a", "model-b"}
    assert attempts[0].cost == pytest.approx(10.0)


def test_reported_cost_is_reconciled_against_table_price(table):
    records = [_step("t1", "a1", 0, "model-a", 2), _step("t2", "a1", 1, "model-a", 4)]
    records[0].reported_cost = 2.5  # OpenRouter charged more than the list price
    attempts = build_attempts(records, table, {})
    assert attempts[0].reported_cost == 2.5
    assert attempts[1].reported_cost is None
    summary = summarise(attempts, resamples=10)[0]
    assert summary.reported_cost_attempts == 1
    assert summary.reported_cost_total == pytest.approx(2.5)
    assert summary.table_cost_for_reported == pytest.approx(2.0)
    text = render_report(attempts, [summary], table)
    assert "provider-reported cost: 2.5000 USD over 1 attempts" in text
    assert "(+25.0%)" in text
    assert "reconciliation:" in text


def test_step_outcome_label_used_when_no_label_file(table):
    record = _step("t1", "a1", 0, "model-a", 1)
    record.outcome_label = "pass"
    attempts = build_attempts([record], table, {})
    assert attempts[0].outcome == "pass"


def test_reports_render_and_carry_the_checklist(table):
    records, labels = _dataset()
    attempts = build_attempts(records, table, labels)
    summaries = summarise(attempts, cleanup_cost=10.0, resamples=50, seed=1)
    table.usage = describe_usage(records, table)  # as every command does before rendering
    text = render_report(attempts, summaries, table, harness="test-harness 1.0")
    for expected in (
        "CPT_solved = E[C] / p: 2.8000 USD",
        "CPT_risk = CPT_solved + L x K: 4.8000 USD",
        "pass^2: 0.500",
        "disclosure checklist",
        "harness: test-harness 1.0",
        "model versions: model-a, model-b",
        "cache hit rate: model-a [coding] 0.0%; model-b [coding] 0.0%",
        "speed: no fast mode calls",
    ):
        assert expected in text
    assert "labelled attempts" not in text  # everything is labelled: one mean, no scope note

    a, b = summarise(attempts, by_task_type=False, cleanup_cost=10.0, resamples=50, seed=1)
    comparison = render_comparison(a, b, table)
    assert "K* = 11.0000 USD" in comparison
    assert "below K*, model-a (cheaper, leakier) wins" in comparison


def test_checklist_cache_hit_rate_falls_back_to_na(table):
    # A group whose attempts carried no prompt tokens has no cache hit rate.
    record = _step("t1", "a1", 0, "model-a", 0)
    record.output_tokens = 5
    attempts = build_attempts([record], table, {})
    text = render_report(attempts, summarise(attempts, resamples=10), table)
    assert "  cache hit rate: n/a" in text
    assert "  cache hit rate: " not in text.splitlines()


def test_labelled_mean_is_shown_when_some_attempts_are_unlabelled(table):
    # Costs 1, 1 and 10 with the expensive attempt unlabelled: the mean over all
    # attempts is 4, while CPT_solved divides the labelled mean, 1, by p = 1.
    records = [
        _step("t1", "a1", 0, "model-a", 1),
        _step("t2", "a1", 1, "model-a", 1),
        _step("t3", "a1", 2, "model-a", 10),
    ]
    labels = {
        ("t1", "a1"): Label("t1", "a1", "pass", leaked=False),
        ("t2", "a1"): Label("t2", "a1", "pass", leaked=False),
    }
    attempts = build_attempts(records, table, labels)
    summary = summarise(attempts, resamples=10, seed=1)[0]
    assert summary.mean_cost == pytest.approx(4.0)
    assert summary.labelled_mean_cost == pytest.approx(1.0)
    assert summary.cpt_solved == pytest.approx(1.0)
    text = render_report(attempts, [summary], table)
    assert "attempt cost C: mean 4.0000 USD (labelled attempts 1.0000 USD), P90" in text
    assert "CPT_solved = E[C] / p: 1.0000 USD over labelled attempts (bootstrap 95%" in text

    # Once every attempt is labelled the two means coincide and the note goes.
    labels[("t3", "a1")] = Label("t3", "a1", "fail", leaked=False)
    attempts = build_attempts(records, table, labels)
    summary = summarise(attempts, resamples=10, seed=1)[0]
    assert summary.labelled_mean_cost == pytest.approx(summary.mean_cost)
    assert summary.cpt_solved == pytest.approx(6.0)
    text = render_report(attempts, [summary], table)
    assert "labelled attempts" not in text

    # Nothing labelled: no labelled mean at all.
    summary = summarise(build_attempts(records, table, {}), resamples=10)[0]
    assert summary.labelled_mean_cost is None
