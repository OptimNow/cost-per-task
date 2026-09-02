"""The cpt command line interface.

Commands:
  cpt serve    run the capture proxy in the foreground
  cpt run      run an agent command with the proxy set up and calls tagged
  cpt label    mark an attempt pass or fail (and optionally leaked), or import a CSV
  cpt report   cost per attempt and per solved task, per model and task type
  cpt compare  two-model comparison with the break-even cleanup cost K*
  cpt import   convert a Langfuse or LiteLLM usage export into a cpt log
  cpt prices   refresh a pricing table from the OptimNow AI Pricing Hub
  cpt mcp      serve the report over MCP (needs the [mcp] extra)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .analysis import load_analysis, pick_model
from .importers import import_langfuse, import_litellm
from .importers.common import ImportError_
from .labels import Label, LabelError, append_label, import_csv, load_labels
from .prices_hub import HUB_URL, PROVIDERS, build_table, diff_tables, fetch_hub, load_hub, write_table
from .pricing import PricingError
from .proxy import DEFAULT_UPSTREAMS, create_proxy
from .report import render_comparison, render_json, render_report
from .schema import JsonlWriter, read_jsonl

DEFAULT_LOG = "cpt-log.jsonl"
DEFAULT_LABELS = "cpt-labels.jsonl"


def _new_attempt_id() -> str:
    return datetime.now(timezone.utc).strftime("a%Y%m%dT%H%M%SZ")


def _add_proxy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--task-type", help="free-text task category, e.g. coding")
    parser.add_argument(
        "--anthropic-upstream",
        default=DEFAULT_UPSTREAMS["anthropic"],
        help="base URL: scheme, host and optional path prefix",
    )
    parser.add_argument(
        "--openai-upstream",
        default=DEFAULT_UPSTREAMS["openai"],
        help="base URL of OpenAI or any OpenAI-compatible gateway, e.g. https://openrouter.ai/api",
    )
    parser.add_argument(
        "--no-inject-usage",
        action="store_true",
        help="do not add stream_options.include_usage (OpenAI) or usage.include (OpenRouter) to requests",
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--labels", default=DEFAULT_LABELS)
    parser.add_argument(
        "--prices", action="append", required=True, help="pricing table JSON; repeat to merge vendors"
    )
    parser.add_argument("--cleanup-cost", type=float, help="K: cost of one leaked failure")
    parser.add_argument("--leak-rate", type=float, help="override L instead of using leak labels")
    parser.add_argument("--retry-cap", type=int, help="N for p_N (default: max attempts per task)")
    parser.add_argument("--k", type=int, help="k for pass^k (default: usual attempts per task)")
    parser.add_argument("--resamples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, help="bootstrap seed for reproducible intervals")
    parser.add_argument("--harness", help="harness name and version for the disclosure checklist")
    parser.add_argument("--task-type", help="only include attempts of this task type")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cpt", description="Measure the real cost per completed task of LLM agents."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the capture proxy in the foreground")
    serve.add_argument("--port", type=int, default=4000)
    serve.add_argument("--task-id", default=os.environ.get("CPT_TASK_ID", "unassigned"))
    serve.add_argument("--attempt-id", default=os.environ.get("CPT_ATTEMPT_ID"))
    _add_proxy_options(serve)

    run = sub.add_parser(
        "run", help="run a command through the proxy: cpt run --task-id T1 -- <command>"
    )
    run.add_argument("--task-id", required=True)
    run.add_argument("--attempt-id")
    _add_proxy_options(run)
    run.add_argument("cmd", nargs=argparse.REMAINDER)

    label = sub.add_parser("label", help="label an attempt pass or fail, or import a CSV")
    label.add_argument("outcome", nargs="?", choices=("pass", "fail"))
    label.add_argument("--task", help="task id (defaults to the latest task in the log)")
    label.add_argument("--attempt", help="attempt id (defaults to the latest attempt of the task)")
    label.add_argument("--leak", action="store_true", help="accepted output was actually wrong")
    label.add_argument("--note")
    label.add_argument("--import", dest="import_csv", metavar="CSV", help="bulk import labels")
    label.add_argument("--log", default=DEFAULT_LOG)
    label.add_argument("--labels", default=DEFAULT_LABELS)

    report = sub.add_parser("report", help="summarise a JSONL log")
    _add_analysis_options(report)
    report.add_argument(
        "--by-model-only", action="store_true", help="do not split groups by task type"
    )

    compare = sub.add_parser("compare", help="compare two models and print K*")
    compare.add_argument("model_a", help="the cheaper, leakier model (A)")
    compare.add_argument("model_b", help="the reliable model (B)")
    _add_analysis_options(compare)

    imp = sub.add_parser("import", help="convert a usage export into cpt step records")
    imp.add_argument("source", choices=("langfuse", "litellm"))
    imp.add_argument("file", help="export file: .csv, .json or .jsonl")
    imp.add_argument("--log", default=DEFAULT_LOG, help="cpt log to append to")
    imp.add_argument("--task-field", help="dotted field holding the task id")
    imp.add_argument("--attempt-field", help="dotted field holding the attempt id")
    imp.add_argument("--task-type")
    imp.add_argument("--dry-run", action="store_true", help="show what would be imported")

    prices = sub.add_parser("prices", help="pricing table maintenance")
    prices_sub = prices.add_subparsers(dest="prices_command", required=True)
    refresh = prices_sub.add_parser("refresh", help="diff or rewrite a table from the Pricing Hub")
    refresh.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    refresh.add_argument("--out", help="table path (default prices/<provider>.json)")
    refresh.add_argument("--url", default=HUB_URL)
    refresh.add_argument("--from-file", help="use a saved hub JSON instead of fetching")
    refresh.add_argument(
        "--map", action="append", default=[], metavar="HUB_ID=API_ID",
        help="override the model id mapping, e.g. anthropic/claude-haiku-4.5=claude-haiku-4-5",
    )
    refresh.add_argument("--write", action="store_true", help="write the table (default: diff only)")

    sub.add_parser("mcp", help="serve the report over MCP on stdio")

    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "run": _cmd_run,
        "label": _cmd_label,
        "report": _cmd_report,
        "compare": _cmd_compare,
        "import": _cmd_import,
        "prices": _cmd_prices,
        "mcp": _cmd_mcp,
    }
    return handlers[args.command](args)


def _upstreams(args: argparse.Namespace) -> dict[str, str]:
    return {"anthropic": args.anthropic_upstream, "openai": args.openai_upstream}


def _cmd_serve(args: argparse.Namespace) -> int:
    attempt_id = args.attempt_id or _new_attempt_id()
    server = create_proxy(
        upstreams=_upstreams(args),
        log_path=args.log,
        task_id=args.task_id,
        attempt_id=attempt_id,
        task_type=args.task_type,
        inject_usage=not args.no_inject_usage,
        port=args.port,
    )
    host, port = server.server_address[:2]
    print(f"cpt proxy on http://{host}:{port}")
    print(f"log: {args.log}  task: {args.task_id}  attempt: {attempt_id}")
    print(f"set ANTHROPIC_BASE_URL=http://{host}:{port} and OPENAI_BASE_URL=http://{host}:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("cpt run: no command given; usage: cpt run --task-id T1 -- <command>", file=sys.stderr)
        return 2
    # Resolve through PATH so .cmd and .bat shims work on Windows without a shell.
    resolved = shutil.which(cmd[0])
    if resolved:
        cmd[0] = resolved

    attempt_id = args.attempt_id or _new_attempt_id()
    server = create_proxy(
        upstreams=_upstreams(args),
        log_path=args.log,
        task_id=args.task_id,
        attempt_id=attempt_id,
        task_type=args.task_type,
        inject_usage=not args.no_inject_usage,
        port=0,
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    env["CPT_TASK_ID"] = args.task_id
    env["CPT_ATTEMPT_ID"] = attempt_id
    print(
        f"cpt: task {args.task_id} attempt {attempt_id}; "
        f"proxy on port {port}; logging to {args.log}"
    )
    try:
        return subprocess.run(cmd, env=env).returncode
    finally:
        server.shutdown()
        server.server_close()
        print(
            f"cpt: captured {server.captured_count} steps "
            f"(task {args.task_id}, attempt {attempt_id}) in {args.log}"
        )


def _latest_attempt(records, task_id: str | None) -> tuple[str, str] | None:
    candidates = [r for r in records if task_id is None or r.task_id == task_id]
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: (r.timestamp, r.attempt_id))
    return latest.task_id, latest.attempt_id


def _cmd_label(args: argparse.Namespace) -> int:
    if args.import_csv:
        try:
            count = import_csv(args.import_csv, args.labels)
        except (OSError, LabelError, KeyError) as exc:
            print(f"cpt label: import failed: {exc}", file=sys.stderr)
            return 1
        print(f"cpt: imported {count} labels into {args.labels}")
        return 0
    if not args.outcome:
        print("cpt label: give an outcome (pass or fail) or --import CSV", file=sys.stderr)
        return 2

    task_id, attempt_id = args.task, args.attempt
    if task_id is None or attempt_id is None:
        try:
            records = read_jsonl(args.log)
        except OSError as exc:
            print(f"cpt label: cannot read log to find the attempt: {exc}", file=sys.stderr)
            return 1
        found = _latest_attempt(records, task_id)
        if found is None:
            print("cpt label: no matching attempt in the log", file=sys.stderr)
            return 1
        task_id, attempt_id = found
    label = Label(
        task_id=task_id, attempt_id=attempt_id, outcome=args.outcome, leaked=args.leak, note=args.note
    )
    append_label(args.labels, label)
    flag = " (leaked)" if args.leak else ""
    print(f"cpt: labelled task {task_id} attempt {attempt_id} as {args.outcome}{flag}")
    return 0


def _analysis(args: argparse.Namespace, *, by_task_type: bool):
    try:
        return load_analysis(
            log=args.log,
            labels=args.labels,
            prices=args.prices,
            by_task_type=by_task_type,
            task_type=args.task_type,
            k=args.k,
            retry_cap=args.retry_cap,
            cleanup_cost=args.cleanup_cost,
            leak_rate=args.leak_rate,
            resamples=args.resamples,
            seed=args.seed,
        )
    except (OSError, PricingError) as exc:
        print(f"cpt: {exc}", file=sys.stderr)
        return None


def _cmd_report(args: argparse.Namespace) -> int:
    analysis = _analysis(args, by_task_type=not args.by_model_only)
    if analysis is None:
        return 1
    if args.json:
        print(render_json(analysis.attempts, analysis.summaries, analysis.table, harness=args.harness))
    else:
        print(render_report(analysis.attempts, analysis.summaries, analysis.table, harness=args.harness))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    analysis = _analysis(args, by_task_type=False)
    if analysis is None:
        return 1
    try:
        a = pick_model(analysis.summaries, args.model_a)
        b = pick_model(analysis.summaries, args.model_b)
    except LookupError as exc:
        print(f"cpt compare: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            render_json(
                analysis.attempts, [a, b], analysis.table, harness=args.harness, comparison=(a, b)
            )
        )
    else:
        print(render_comparison(a, b, analysis.table, harness=args.harness))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    importer = import_langfuse if args.source == "langfuse" else import_litellm
    options = {"task_type": args.task_type}
    if args.task_field:
        options["task_field"] = args.task_field
    if args.attempt_field:
        options["attempt_field"] = args.attempt_field
    try:
        result = importer(args.file, **options)
    except (OSError, ValueError, ImportError_) as exc:
        print(f"cpt import: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"cpt import: warning: {warning}", file=sys.stderr)
    attempts = {(r.task_id, r.attempt_id) for r in result.records}
    tasks = {r.task_id for r in result.records}
    if args.dry_run:
        print(
            f"cpt import: would append {len(result.records)} steps "
            f"({len(attempts)} attempts over {len(tasks)} tasks) to {args.log}"
        )
        return 0
    writer = JsonlWriter(args.log)
    for record in result.records:
        writer.append(record)
    print(
        f"cpt import: appended {len(result.records)} steps "
        f"({len(attempts)} attempts over {len(tasks)} tasks) to {args.log}"
    )
    return 0


def _cmd_prices(args: argparse.Namespace) -> int:
    overrides = {}
    for mapping in args.map:
        if "=" not in mapping:
            print(f"cpt prices: --map expects HUB_ID=API_ID, got '{mapping}'", file=sys.stderr)
            return 2
        hub_id, api_id = mapping.split("=", 1)
        overrides[hub_id.strip()] = api_id.strip()
    try:
        hub = load_hub(args.from_file) if args.from_file else fetch_hub(args.url)
    except (OSError, ValueError) as exc:
        print(f"cpt prices: cannot load the hub catalogue: {exc}", file=sys.stderr)
        return 1
    table, warnings = build_table(hub, args.provider, overrides)
    out = Path(args.out or f"prices/{args.provider}.json")
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None

    for warning in warnings:
        print(f"cpt prices: warning: {warning}", file=sys.stderr)
    changes = diff_tables(existing, table)
    print(f"hub catalogue {table['as_of']}: {len(table['models'])} {args.provider} models priced")
    if existing is None:
        print(f"{out} does not exist yet; all models are new")
    if changes:
        print("\n".join(changes))
    else:
        print(f"no changes against {out}")
    if args.write:
        write_table(table, out)
        print(f"wrote {out}")
    elif changes or existing is None:
        print("dry run; pass --write to update the table")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    return mcp_main()


if __name__ == "__main__":
    raise SystemExit(main())
