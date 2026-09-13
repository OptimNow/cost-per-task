"""MCP server exposing the cost-per-task report to AI assistants and to the
OptimNow AI ROI calculator, which can use CPT_risk as its denominator.

Needs the optional extra: ``pip install "cost-per-task[mcp]"``. The tool
functions are plain Python so they can be tested without the SDK.
"""

from __future__ import annotations

import sys

from .analysis import load_analysis, pick_model
from .metrics import break_even_cleanup_cost
from .pricing import default_table_paths
from .report import summary_to_dict, table_to_dict

DEFAULT_LOG = "cpt-log.jsonl"
DEFAULT_LABELS = "cpt-labels.jsonl"
DEFAULT_PRICES = ""  # empty: the Anthropic and OpenAI tables shipped with the tool; or comma-separated paths


def _price_paths(prices: str) -> list[str]:
    return [p.strip() for p in prices.split(",") if p.strip()] or default_table_paths()


def tool_report(
    log: str = DEFAULT_LOG,
    prices: str = DEFAULT_PRICES,
    labels: str = DEFAULT_LABELS,
    cleanup_cost: float | None = None,
    leak_rate: float | None = None,
    task_type: str | None = None,
    by_task_type: bool = True,
    resamples: int = 10_000,
    seed: int | None = None,
) -> dict:
    """Cost per attempt and per solved task for every model (and task type)
    in a cpt log: mean and P90 attempt cost, success rate with Wilson interval,
    CPT_solved with bootstrap interval, pass^k, leak rate and CPT_risk when a
    cleanup cost is given. Prices come from the named table and are dated."""
    analysis = load_analysis(
        log=log,
        labels=labels,
        prices=_price_paths(prices),
        by_task_type=by_task_type,
        task_type=task_type,
        cleanup_cost=cleanup_cost,
        leak_rate=leak_rate,
        resamples=resamples,
        seed=seed,
    )
    return {
        "prices": table_to_dict(analysis.table),
        "attempts": len(analysis.attempts),
        "groups": [summary_to_dict(s) for s in analysis.summaries],
    }


def tool_compare(
    model_a: str,
    model_b: str,
    log: str = DEFAULT_LOG,
    prices: str = DEFAULT_PRICES,
    labels: str = DEFAULT_LABELS,
    cleanup_cost: float | None = None,
    task_type: str | None = None,
    resamples: int = 10_000,
    seed: int | None = None,
) -> dict:
    """Compare two models on cost per solved task and leak rate and return the
    break-even cleanup cost K* = (CPT_B - CPT_A) / (L_A - L_B): below K* the
    cheaper, leakier model A wins on risk-adjusted cost; above it, model B wins."""
    analysis = load_analysis(
        log=log,
        labels=labels,
        prices=_price_paths(prices),
        by_task_type=False,
        task_type=task_type,
        cleanup_cost=cleanup_cost,
        resamples=resamples,
        seed=seed,
    )
    a = pick_model(analysis.summaries, model_a)
    b = pick_model(analysis.summaries, model_b)
    return {
        "prices": table_to_dict(analysis.table),
        "model_a": summary_to_dict(a),
        "model_b": summary_to_dict(b),
        "break_even_cleanup_cost": break_even_cleanup_cost(a, b),
    }


def tool_risk_denominator(
    model: str,
    cleanup_cost: float,
    log: str = DEFAULT_LOG,
    prices: str = DEFAULT_PRICES,
    labels: str = DEFAULT_LABELS,
    leak_rate: float | None = None,
    task_type: str | None = None,
    seed: int | None = None,
) -> dict:
    """Risk-adjusted cost per solved task for one model, for use as the cost
    denominator in an ROI calculation: CPT_risk = CPT_solved + L x K, with the
    sample size, intervals and price date needed to disclose it."""
    analysis = load_analysis(
        log=log,
        labels=labels,
        prices=_price_paths(prices),
        by_task_type=False,
        task_type=task_type,
        cleanup_cost=cleanup_cost,
        leak_rate=leak_rate,
        seed=seed,
    )
    s = pick_model(analysis.summaries, model)
    return {
        "model": s.model,
        "currency": analysis.table.currency,
        "prices_as_of": analysis.table.as_of,
        "attempts": s.attempts,
        "tasks": s.tasks,
        "labelled": s.labelled,
        "success_rate": s.success_rate,
        "success_interval_95": list(s.success_interval) if s.success_interval else None,
        "cpt_solved": s.cpt_solved,
        "cpt_solved_interval_95": list(s.cpt_solved_interval) if s.cpt_solved_interval else None,
        "leak_rate": s.leak_rate,
        "cleanup_cost": s.cleanup_cost,
        "cpt_risk": s.cpt_risk,
    }


def build_server():
    try:
        from mcp.server import MCPServer as Server  # mcp 2.x
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as exc:
            raise SystemExit(
                'cpt mcp needs the optional extra: pip install "cost-per-task[mcp]"'
            ) from exc
    server = Server("cost-per-task")
    server.tool(name="cpt_report")(tool_report)
    server.tool(name="cpt_compare")(tool_compare)
    server.tool(name="cpt_risk_denominator")(tool_risk_denominator)
    return server


def main() -> int:
    print("cpt mcp: serving cost-per-task tools over stdio", file=sys.stderr)
    build_server().run(transport="stdio")
    return 0
