"""Plain-text report and two-model comparison.

Every report ends with the disclosure checklist from the DoiT framework, so
the numbers travel with the facts needed to interpret them.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable

from .metrics import Attempt, GroupSummary, break_even_cleanup_cost
from .pricing import PricingTable


def summary_to_dict(summary: GroupSummary) -> dict:
    data = dataclasses.asdict(summary)
    for key in ("models_seen", "efforts_seen"):
        data[key] = sorted(data[key])
    for key in ("success_interval", "cpt_solved_interval"):
        if data[key] is not None:
            data[key] = list(data[key])
    return data


def table_to_dict(table: PricingTable) -> dict:
    return {"currency": table.currency, "as_of": table.as_of, "source": table.source}


def render_json(
    attempts: list[Attempt],
    summaries: list[GroupSummary],
    table: PricingTable,
    *,
    harness: str | None = None,
    comparison: tuple[GroupSummary, GroupSummary] | None = None,
) -> str:
    payload: dict = {
        "prices": table_to_dict(table),
        "harness": harness,
        "attempts": len(attempts),
        "groups": [summary_to_dict(s) for s in summaries],
    }
    if comparison is not None:
        a, b = comparison
        payload["comparison"] = {
            "model_a": a.model,
            "model_b": b.model,
            "break_even_cleanup_cost": break_even_cleanup_cost(a, b),
        }
    return json.dumps(payload, indent=2)


def _money(value: float | None, currency: str) -> str:
    return "n/a" if value is None else f"{value:.4f} {currency}"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _interval(interval: tuple[float, float] | None, fmt) -> str:
    return "" if interval is None else f" ({fmt(interval[0])} to {fmt(interval[1])})"


def _group_name(summary: GroupSummary) -> str:
    return summary.model if summary.task_type is None else f"{summary.model} [{summary.task_type}]"


def _attempt_table(attempts: list[Attempt], currency: str) -> list[str]:
    header = (
        f"{'task':<12} {'attempt':<26} {'model':<28} {'type':<10} {'steps':>5} "
        f"{'cost':>10} {'outcome':<9}"
    )
    lines = [header, "-" * len(header)]
    for a in attempts:
        outcome = a.outcome or "-"
        if a.leaked:
            outcome += "+leak"
        lines.append(
            f"{a.task_id[:12]:<12} {a.attempt_id[:26]:<26} {a.model[:28]:<28} "
            f"{(a.task_type or '-')[:10]:<10} {a.steps:>5} {a.cost:>10.4f} {outcome:<9}"
        )
    return lines


def _reconciliation(s: GroupSummary, currency: str) -> str:
    table = s.table_cost_for_reported or 0.0
    reported = s.reported_cost_total or 0.0
    if table > 0:
        delta = f"{100 * (reported - table) / table:+.1f}%"
    else:
        delta = "n/a"
    return (
        f"provider-reported cost: {_money(reported, currency)} over {s.reported_cost_attempts} "
        f"attempts; table price for the same attempts {_money(table, currency)} ({delta})"
    )


def _group_section(s: GroupSummary, currency: str) -> list[str]:
    lines = [f"group: {_group_name(s)}"]
    lines.append(
        f"  attempts {s.attempts} over {s.tasks} tasks; labelled {s.labelled} "
        f"(pass {s.successes}, fail {s.failures}, leaked {s.leaks})"
    )
    # E[C] in CPT_solved is the mean over labelled attempts only. When some
    # attempts are unlabelled it differs in scope from the mean over all
    # attempts, so print it too and say so on the CPT_solved line.
    partly_labelled = 0 < s.labelled < s.attempts
    mean = _money(s.mean_cost, currency)
    if partly_labelled and s.labelled_mean_cost is not None:
        mean += f" (labelled attempts {_money(s.labelled_mean_cost, currency)})"
    lines.append(
        f"  attempt cost C: mean {mean}, P90 {_money(s.p90_cost, currency)}, "
        f"total {_money(s.total_cost, currency)}"
    )
    if s.success_rate is None:
        lines.append("  success rate p: n/a (no labelled attempts; run cpt label)")
    else:
        lines.append(
            f"  success rate p: {_rate(s.success_rate)}"
            f"{_interval(s.success_interval, _rate).replace('(', '(Wilson 95%: ', 1)}"
        )
    if s.cpt_solved is None:
        lines.append("  CPT_solved: n/a (needs at least one labelled pass)")
    else:
        ci = ""
        if s.cpt_solved_interval:
            lo, hi = s.cpt_solved_interval
            ci = f" (bootstrap 95%: {lo:.4f} to {hi:.4f}, {s.resamples} resamples)"
        scope = " over labelled attempts" if partly_labelled else ""
        lines.append(f"  CPT_solved = E[C] / p: {_money(s.cpt_solved, currency)}{scope}{ci}")
    lines.append(
        f"  cost per task attempted (failures included): "
        f"{_money(s.cost_per_task_attempted, currency)}"
    )
    if s.capped_success is not None:
        lines.append(f"  capped retries N={s.retry_cap}: p_N {_rate(s.capped_success)}")
    if s.pass_k is not None:
        lines.append(f"  pass^{s.k}: {_rate(s.pass_k)}")
    if s.leak_rate is not None:
        lines.append(f"  leak rate L: {_rate(s.leak_rate)}")
    if s.cpt_risk is not None:
        lines.append(
            f"  CPT_risk = CPT_solved + L x K: {_money(s.cpt_risk, currency)} "
            f"(K = {_money(s.cleanup_cost, currency)})"
        )
    if s.cache_hit_rate is not None:
        lines.append(f"  cache hit rate: {100 * s.cache_hit_rate:.1f}%")
    if s.reported_cost_total is not None:
        lines.append("  " + _reconciliation(s, currency))
    if s.unpriced_steps:
        lines.append(f"  warning: {s.unpriced_steps} steps had no price and count as zero cost")
    return lines


def _checklist(
    summaries: Iterable[GroupSummary],
    table: PricingTable,
    *,
    harness: str | None,
    break_even: tuple[str, str, float | None] | None = None,
) -> list[str]:
    summaries = list(summaries)
    models = sorted(set().union(*(s.models_seen for s in summaries))) if summaries else []
    efforts = sorted(set().union(*(s.efforts_seen for s in summaries))) if summaries else []
    lines = ["disclosure checklist"]
    lines.append(f"  model versions: {', '.join(models) or 'none'}")
    lines.append(
        f"  prices: as of {table.as_of} in {table.currency}"
        + (f"; source: {table.source}" if table.source else "")
    )
    lines.append(f"  harness: {harness or 'not stated (pass --harness)'}")
    cache_rates = "; ".join(
        f"{_group_name(s)} {100 * s.cache_hit_rate:.1f}%"
        for s in summaries
        if s.cache_hit_rate is not None
    )
    lines.append(f"  cache hit rate: {cache_rates or 'n/a'}")
    lines.append(f"  effort settings: {', '.join(efforts) or 'not recorded'}")
    lines.append(
        "  sample size: "
        + "; ".join(
            f"{_group_name(s)} n={s.attempts} attempts over {s.tasks} tasks"
            + (f", k={s.k}" if s.k else "")
            for s in summaries
        )
    )
    lines.append(
        "  intervals: Wilson score (z=1.96) on p; percentile bootstrap on CPT_solved "
        + (
            f"({summaries[0].resamples} task resamples"
            + (f", seed {summaries[0].seed}" if summaries and summaries[0].seed is not None else "")
            + ")"
            if summaries
            else ""
        )
    )
    lines.append(
        "  leak rate: "
        + (
            "; ".join(
                f"{_group_name(s)} L={_rate(s.leak_rate)}"
                for s in summaries
                if s.leak_rate is not None
            )
            or "not measured (label leaks with cpt label --leak)"
        )
    )
    reconciled = [s for s in summaries if s.reported_cost_total is not None]
    if reconciled:
        lines.append(
            "  reconciliation: "
            + "; ".join(f"{_group_name(s)} {_reconciliation(s, table.currency)}" for s in reconciled)
        )
    k_values = {s.cleanup_cost for s in summaries if s.cleanup_cost is not None}
    lines.append(
        "  cleanup cost K assumed: "
        + (", ".join(_money(k, table.currency) for k in sorted(k_values)) or "not supplied")
    )
    if break_even is not None:
        a, b, k_star = break_even
        lines.append(
            f"  break-even K* ({a} vs {b}): "
            + (_money(k_star, table.currency) if k_star is not None else "undefined")
        )
    return lines


def render_report(
    attempts: list[Attempt],
    summaries: list[GroupSummary],
    table: PricingTable,
    *,
    harness: str | None = None,
) -> str:
    if not attempts:
        return "no records in log"
    lines: list[str] = [f"prices: {table.as_of} ({table.currency})", ""]
    lines.extend(_attempt_table(attempts, table.currency))
    lines.append("")
    for summary in summaries:
        lines.extend(_group_section(summary, table.currency))
        lines.append("")
    unlabelled = sum(1 for a in attempts if a.outcome is None)
    if unlabelled:
        lines.append(
            f"note: {unlabelled} of {len(attempts)} attempts are unlabelled; "
            "label them with cpt label to compute cost per solved task"
        )
        lines.append("")
    lines.extend(_checklist(summaries, table, harness=harness))
    return "\n".join(lines)


def render_comparison(
    a: GroupSummary,
    b: GroupSummary,
    table: PricingTable,
    *,
    harness: str | None = None,
) -> str:
    currency = table.currency
    k_star = break_even_cleanup_cost(a, b)
    lines: list[str] = [f"prices: {table.as_of} ({currency})", ""]
    for s in (a, b):
        lines.extend(_group_section(s, currency))
        lines.append("")
    lines.append(f"break-even cleanup cost K* = (CPT_B - CPT_A) / (L_A - L_B)")
    if k_star is None:
        lines.append(
            "  undefined: both models need CPT_solved and a leak rate, and the leak rates must differ"
        )
    else:
        cheaper = a if (a.cpt_solved or 0) <= (b.cpt_solved or 0) else b
        leakier = a if (a.leak_rate or 0) >= (b.leak_rate or 0) else b
        lines.append(f"  K* = {_money(k_star, currency)}")
        if k_star < 0:
            lines.append(
                f"  {cheaper.model} is both cheaper per solved task and no leakier; "
                "it wins at any cleanup cost"
            )
        else:
            other = b if leakier is a else a
            lines.append(
                f"  below K*, {leakier.model} (cheaper, leakier) wins on CPT_risk; "
                f"above K*, {other.model} wins"
            )
    lines.append("")
    lines.extend(_checklist([a, b], table, harness=harness, break_even=(a.model, b.model, k_star)))
    return "\n".join(lines)
