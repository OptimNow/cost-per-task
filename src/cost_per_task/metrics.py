"""From step records to attempts to the cost-per-task estimators.

An attempt's cost is the sum of its priced steps (C_attempt). Its primary
model is the one carrying the largest share of that cost, which matters for
agents that mix a main model with cheaper side calls. Attempts are grouped by
primary model (and task type when requested) and each group gets:

- mean and P90 of C_attempt over all attempts, and the mean over labelled attempts
- success rate p over labelled attempts, with a Wilson interval
- CPT_solved = E[C_attempt] / p, both taken over labelled attempts so the
  estimator stays coherent, with a task-level bootstrap interval
- cost per task attempted = total cost / distinct tasks (failures included)
- capped-retry success p_N and pass^k consistency
- leak rate L = leaked passes / passes, and CPT_risk = CPT_solved + L x K
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .labels import Label
from .pricing import PricingTable, price_breakdown, price_step
from .schema import StepRecord
from .stats import bootstrap_interval, capped_retry_success, pass_k, percentile, wilson_interval


@dataclass
class Attempt:
    task_id: str
    attempt_id: str
    model: str
    task_type: str | None
    cost: float
    steps: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_calls: int
    first_timestamp: str
    unpriced_steps: int
    reported_cost: float | None = None  # sum of provider-reported step costs, if any step had one
    models: set[str] = field(default_factory=set)
    efforts: set[str] = field(default_factory=set)
    outcome: str | None = None  # pass | fail | None when unlabelled
    leaked: bool = False
    task_source: str = "manual"  # or the signal the task id was inferred from

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @property
    def cache_hit_rate(self) -> float | None:
        prompt_total = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return self.cache_read_tokens / prompt_total if prompt_total else None


def build_attempts(
    records: list[StepRecord],
    table: PricingTable,
    labels: dict[tuple[str, str], Label],
) -> list[Attempt]:
    grouped: dict[tuple[str, str], list[StepRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.task_id, record.attempt_id)].append(record)

    attempts: list[Attempt] = []
    for (task_id, attempt_id), steps in grouped.items():
        steps.sort(key=lambda s: (s.timestamp, s.step_id))
        cost_by_model: dict[str, float] = defaultdict(float)
        tokens_by_model: dict[str, int] = defaultdict(int)
        unpriced = 0
        for step in steps:
            step_cost = price_step(step, table)
            if step_cost is None:
                unpriced += 1
            else:
                cost_by_model[step.model] += step_cost
            tokens_by_model[step.model] += (
                step.input_tokens
                + step.cache_read_tokens
                + step.cache_write_tokens
                + step.output_tokens
                + (step.reasoning_tokens or 0)
            )
        weights = cost_by_model if any(cost_by_model.values()) else tokens_by_model
        primary = max(weights, key=lambda m: weights[m])
        reported = [s.reported_cost for s in steps if s.reported_cost is not None]

        label = labels.get((task_id, attempt_id))
        if label is not None:
            outcome, leaked = label.outcome, label.leaked
        else:
            step_labels = [s.outcome_label for s in steps if s.outcome_label in ("pass", "fail")]
            outcome, leaked = (step_labels[-1] if step_labels else None), False

        attempts.append(
            Attempt(
                task_id=task_id,
                attempt_id=attempt_id,
                model=primary,
                task_type=next((s.task_type for s in steps if s.task_type), None),
                cost=sum(cost_by_model.values()),
                steps=len(steps),
                input_tokens=sum(s.input_tokens for s in steps),
                cache_read_tokens=sum(s.cache_read_tokens for s in steps),
                cache_write_tokens=sum(s.cache_write_tokens for s in steps),
                output_tokens=sum(s.output_tokens for s in steps),
                reasoning_tokens=sum(s.reasoning_tokens or 0 for s in steps),
                tool_calls=sum(s.tool_call_count for s in steps),
                first_timestamp=steps[0].timestamp,
                unpriced_steps=unpriced,
                reported_cost=sum(reported) if reported else None,
                models={s.model for s in steps},
                efforts={s.effort for s in steps if s.effort},
                outcome=outcome,
                leaked=leaked,
                task_source=next((s.task_source for s in steps if s.task_source), None) or "manual",
            )
        )
    attempts.sort(key=lambda a: (a.task_id, a.first_timestamp, a.attempt_id))
    return attempts


@dataclass
class GroupSummary:
    model: str
    task_type: str | None
    attempts: int
    tasks: int
    labelled: int
    successes: int
    failures: int
    leaks: int
    total_cost: float
    mean_cost: float
    p90_cost: float
    cost_per_task_attempted: float
    success_rate: float | None
    success_interval: tuple[float, float] | None
    cpt_solved: float | None
    cpt_solved_interval: tuple[float, float] | None
    retry_cap: int | None
    capped_success: float | None
    k: int | None
    pass_k: float | None
    leak_rate: float | None
    cleanup_cost: float | None
    cpt_risk: float | None
    cache_hit_rate: float | None
    models_seen: set[str]
    efforts_seen: set[str]
    unpriced_steps: int
    resamples: int
    seed: int | None
    # Reconciliation against what the provider said it charged (OpenRouter),
    # over the attempts that reported a cost.
    reported_cost_attempts: int = 0
    reported_cost_total: float | None = None
    table_cost_for_reported: float | None = None
    # Mean C_attempt over labelled attempts only: the E[C] that CPT_solved
    # divides by p. None when nothing is labelled.
    labelled_mean_cost: float | None = None
    # Tasks per origin of their id: 'manual' (stated by a person) or the signal it was
    # inferred from (issue, pr, branch, session).
    task_sources: dict[str, int] = field(default_factory=dict)


def _cpt_solved(attempts: list[Attempt]) -> float | None:
    labelled = [a for a in attempts if a.outcome is not None]
    passes = sum(1 for a in labelled if a.passed)
    if not labelled or passes == 0:
        return None
    mean_cost = sum(a.cost for a in labelled) / len(labelled)
    return mean_cost / (passes / len(labelled))


def summarise(
    attempts: list[Attempt],
    *,
    by_task_type: bool = True,
    k: int | None = None,
    retry_cap: int | None = None,
    cleanup_cost: float | None = None,
    leak_rate: float | None = None,
    resamples: int = 10_000,
    seed: int | None = None,
) -> list[GroupSummary]:
    groups: dict[tuple[str, str | None], list[Attempt]] = defaultdict(list)
    for attempt in attempts:
        key = (attempt.model, attempt.task_type if by_task_type else None)
        groups[key].append(attempt)

    summaries = []
    for (model, task_type), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        summaries.append(
            _summarise_group(
                model,
                task_type,
                members,
                k=k,
                retry_cap=retry_cap,
                cleanup_cost=cleanup_cost,
                leak_rate_override=leak_rate,
                resamples=resamples,
                seed=seed,
            )
        )
    return summaries


def _summarise_group(
    model: str,
    task_type: str | None,
    members: list[Attempt],
    *,
    k: int | None,
    retry_cap: int | None,
    cleanup_cost: float | None,
    leak_rate_override: float | None,
    resamples: int,
    seed: int | None,
) -> GroupSummary:
    costs = [a.cost for a in members]
    tasks = {a.task_id for a in members}
    labelled = [a for a in members if a.outcome is not None]
    successes = sum(1 for a in labelled if a.passed)
    failures = len(labelled) - successes
    leaks = sum(1 for a in labelled if a.passed and a.leaked)

    success_rate = successes / len(labelled) if labelled else None
    success_interval = wilson_interval(successes, len(labelled)) if labelled else None
    labelled_mean_cost = sum(a.cost for a in labelled) / len(labelled) if labelled else None

    cpt_solved = _cpt_solved(members)
    cpt_interval = None
    if cpt_solved is not None:
        by_task: dict[str, list[Attempt]] = defaultdict(list)
        for attempt in labelled:
            by_task[attempt.task_id].append(attempt)
        cpt_interval = bootstrap_interval(
            list(by_task.values()), _cpt_solved, resamples=resamples, seed=seed
        )

    attempts_per_task = Counter(a.task_id for a in members)
    cap = retry_cap or max(attempts_per_task.values())
    capped = capped_retry_success(success_rate, cap) if success_rate is not None else None

    ordered_outcomes: dict[str, list[bool]] = defaultdict(list)
    for attempt in sorted(labelled, key=lambda a: (a.first_timestamp, a.attempt_id)):
        ordered_outcomes[attempt.task_id].append(attempt.passed)
    if k is None:
        counts = [len(v) for v in ordered_outcomes.values() if len(v) >= 2]
        k_used = Counter(counts).most_common(1)[0][0] if counts else None
    else:
        k_used = k
    pass_k_value = pass_k(ordered_outcomes, k_used) if k_used and ordered_outcomes else None

    if leak_rate_override is not None:
        leak_rate = leak_rate_override
    elif successes:
        leak_rate = leaks / successes
    else:
        leak_rate = None
    cpt_risk = None
    if cpt_solved is not None and cleanup_cost is not None and leak_rate is not None:
        cpt_risk = cpt_solved + leak_rate * cleanup_cost

    prompt_total = sum(a.input_tokens + a.cache_read_tokens + a.cache_write_tokens for a in members)
    cache_hit_rate = (
        sum(a.cache_read_tokens for a in members) / prompt_total if prompt_total else None
    )
    with_reported = [a for a in members if a.reported_cost is not None]

    return GroupSummary(
        model=model,
        task_type=task_type,
        attempts=len(members),
        tasks=len(tasks),
        labelled=len(labelled),
        successes=successes,
        failures=failures,
        leaks=leaks,
        total_cost=sum(costs),
        mean_cost=sum(costs) / len(costs),
        p90_cost=percentile(costs, 90),
        cost_per_task_attempted=sum(costs) / len(tasks),
        success_rate=success_rate,
        success_interval=success_interval,
        cpt_solved=cpt_solved,
        cpt_solved_interval=cpt_interval,
        retry_cap=cap,
        capped_success=capped,
        k=k_used,
        pass_k=pass_k_value,
        leak_rate=leak_rate,
        cleanup_cost=cleanup_cost,
        cpt_risk=cpt_risk,
        cache_hit_rate=cache_hit_rate,
        models_seen=set().union(*(a.models for a in members)),
        efforts_seen=set().union(*(a.efforts for a in members)),
        unpriced_steps=sum(a.unpriced_steps for a in members),
        resamples=resamples,
        seed=seed,
        reported_cost_attempts=len(with_reported),
        reported_cost_total=(
            sum(a.reported_cost for a in with_reported) if with_reported else None
        ),
        table_cost_for_reported=sum(a.cost for a in with_reported) if with_reported else None,
        labelled_mean_cost=labelled_mean_cost,
        task_sources=dict(Counter({a.task_id: a.task_source for a in members}.values())),
    )


def break_even_cleanup_cost(a: GroupSummary, b: GroupSummary) -> float | None:
    """K* = (CPT_B - CPT_A) / (L_A - L_B): the cleanup cost at which the two
    models have equal CPT_risk. None if either CPT or leak rate is missing,
    or the leak rates are equal."""
    if a.cpt_solved is None or b.cpt_solved is None:
        return None
    if a.leak_rate is None or b.leak_rate is None or a.leak_rate == b.leak_rate:
        return None
    return (b.cpt_solved - a.cpt_solved) / (a.leak_rate - b.leak_rate)


CLASS_LABELS = (
    ("input", "input"),
    ("cache_read", "cache read"),
    ("cache_write_5m", "cache write 5m"),
    ("cache_write_1h", "cache write 1h"),
    ("output", "output"),
    ("reasoning", "reasoning"),
)
_ALWAYS_SHOWN = ("input", "cache_read", "cache_write_5m", "output")


@dataclass
class ClassCost:
    key: str
    label: str
    tokens: int
    cost: float


@dataclass
class StepCost:
    step_id: int
    timestamp: str
    model: str
    prompt_tokens: int  # input, cache reads and cache writes together
    output_tokens: int  # output and reasoning together
    cost: float | None  # None when the model is unpriced
    tool_names: list[str]
    effort: str | None


@dataclass
class Explanation:
    """One attempt's cost taken apart for ``cpt explain``: by token class, by
    model and by step."""

    attempt: Attempt
    classes: list[ClassCost]
    by_model: list[tuple[str, int, float]]  # model, steps, cost; largest cost first
    steps: list[StepCost]  # in call order
    largest_prompt: StepCost | None


def explain_attempt(
    records: list[StepRecord],
    table: PricingTable,
    labels: dict[tuple[str, str], Label],
) -> Explanation:
    """Take one attempt's cost apart. ``records`` must all belong to the same
    (task_id, attempt_id). A token class is listed when it has tokens; the
    four common ones always are."""
    attempts = build_attempts(records, table, labels)
    if len(attempts) != 1:
        raise ValueError(f"expected the records of one attempt, got {len(attempts)}")
    tokens: Counter = Counter()
    costs: Counter = Counter()
    model_steps: Counter = Counter()
    model_cost: Counter = Counter()
    steps: list[StepCost] = []
    for step in sorted(records, key=lambda s: (s.timestamp, s.step_id)):
        write_1h = min(step.cache_write_1h_tokens, step.cache_write_tokens)
        tokens.update(
            {
                "input": step.input_tokens,
                "cache_read": step.cache_read_tokens,
                "cache_write_5m": step.cache_write_tokens - write_1h,
                "cache_write_1h": write_1h,
                "output": step.output_tokens,
                "reasoning": step.reasoning_tokens or 0,
            }
        )
        breakdown = price_breakdown(step, table)
        if breakdown is not None:
            costs.update(breakdown)
        cost = price_step(step, table)
        model_steps[step.model] += 1
        model_cost[step.model] += cost or 0.0
        steps.append(
            StepCost(
                step_id=step.step_id,
                timestamp=step.timestamp,
                model=step.model,
                prompt_tokens=step.input_tokens + step.cache_read_tokens + step.cache_write_tokens,
                output_tokens=step.output_tokens + (step.reasoning_tokens or 0),
                cost=cost,
                tool_names=list(step.tool_names),
                effort=step.effort,
            )
        )
    classes = [
        ClassCost(key, label, tokens[key], costs[key])
        for key, label in CLASS_LABELS
        if tokens[key] or key in _ALWAYS_SHOWN
    ]
    by_model = sorted(
        ((model, model_steps[model], model_cost[model]) for model in model_steps),
        key=lambda item: (-item[2], -item[1], item[0]),
    )
    return Explanation(
        attempt=attempts[0],
        classes=classes,
        by_model=by_model,
        steps=steps,
        largest_prompt=max(steps, key=lambda s: s.prompt_tokens, default=None),
    )
