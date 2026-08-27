"""Plain-text summary of a JSONL log.

Phase 1 scope: per-attempt token totals and cost, plus per-model totals.
Success rates, intervals, CPT_solved and CPT_risk arrive with the
labelling CLI in Phase 2.
"""

from __future__ import annotations

from collections import defaultdict

from .pricing import PricingTable, price_step
from .schema import StepRecord


def render_report(records: list[StepRecord], table: PricingTable) -> str:
    if not records:
        return "no records in log"

    lines: list[str] = []
    lines.append(f"prices: {table.as_of} ({table.currency})")
    if table.source:
        lines.append(f"price source: {table.source}")
    lines.append("")

    by_attempt: dict[tuple[str, str], list[StepRecord]] = defaultdict(list)
    for record in records:
        by_attempt[(record.task_id, record.attempt_id)].append(record)

    header = (
        f"{'task':<12} {'attempt':<18} {'steps':>5} {'input':>9} {'cache_r':>9} "
        f"{'cache_w':>9} {'output':>9} {'tools':>5} {'cost':>10}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    unpriced: dict[str, int] = defaultdict(int)
    grand_total = 0.0
    model_totals: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))

    for (task_id, attempt_id), steps in sorted(by_attempt.items()):
        cost = 0.0
        for step in steps:
            step_cost = price_step(step, table)
            if step_cost is None:
                unpriced[step.model] += 1
            else:
                cost += step_cost
                count, total = model_totals[step.model]
                model_totals[step.model] = (count + 1, total + step_cost)
        grand_total += cost
        lines.append(
            f"{task_id:<12} {attempt_id:<18} {len(steps):>5} "
            f"{sum(s.input_tokens for s in steps):>9} "
            f"{sum(s.cache_read_tokens for s in steps):>9} "
            f"{sum(s.cache_write_tokens for s in steps):>9} "
            f"{sum(s.output_tokens for s in steps):>9} "
            f"{sum(s.tool_call_count for s in steps):>5} "
            f"{cost:>10.4f}"
        )

    lines.append("")
    lines.append("per model:")
    for model, (count, total) in sorted(model_totals.items()):
        lines.append(f"  {model}: {count} steps, {total:.4f} {table.currency}")
    lines.append(f"total priced cost: {grand_total:.4f} {table.currency}")

    for model, count in sorted(unpriced.items()):
        lines.append(f"warning: no price for model '{model}', {count} steps unpriced")

    pending = sum(1 for r in records if r.outcome_label == "pending")
    if pending:
        lines.append(
            f"note: {pending} of {len(records)} steps have outcome 'pending'; "
            "label attempts to unlock cost per solved task (Phase 2)"
        )
    return "\n".join(lines)
