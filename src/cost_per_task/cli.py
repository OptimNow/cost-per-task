"""The cpt command line interface.

Commands:
  cpt serve    run the capture proxy in the foreground
  cpt run      run an agent command with the proxy set up and calls tagged
  cpt label    mark an attempt pass or fail (and optionally leaked), or import a CSV
  cpt report   cost per attempt and per solved task, per model and task type
  cpt explain  where one attempt's cost went: by token class, model and step
  cpt compare  two-model comparison with the break-even cleanup cost K*
  cpt import   convert a Langfuse or LiteLLM usage export into a cpt log
  cpt prices   refresh a pricing table from the OptimNow AI Pricing Hub
  cpt sessions list, import and summarise Claude Code and Cowork sessions from their transcripts
  cpt mcp      serve the report over MCP (needs the [mcp] extra)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__
from .analysis import load_analysis, pick_model
from .importers import import_langfuse, import_litellm
from .importers.claude_sessions import (
    WARNING_TEXT,
    default_roots,
    discover_sessions,
    harness_summary,
    import_sheet,
    read_sheet,
    render_sessions_summary,
    roots_for_paths,
    summarise_sessions,
    write_sheet,
)
from .importers.common import ImportError_
from .labels import Label, LabelError, append_label, import_csv, load_labels
from .metrics import explain_attempt
from .prices_hub import (
    HUB_URL,
    PROVIDERS,
    build_table,
    diff_tables,
    fetch_hub,
    load_hub,
    with_history,
    write_table,
)
from .pricing import PricingError, PricingTable, default_table_paths, describe_usage, prices_line
from .proxy import DEFAULT_UPSTREAMS, create_proxy
from .report import render_comparison, render_explain, render_json, render_report
from .schema import JsonlWriter, read_jsonl
from .taskid import current_branch, project_name, suggest_task

DEFAULT_LOG = "cpt-log.jsonl"
DEFAULT_LABELS = "cpt-labels.jsonl"
DEFAULT_SHEET = "sessions.csv"
# Where cpt sessions import wrote in 0.5.0; still used when present and the
# default files are absent, so an earlier log is never split in two.
LEGACY_SESSIONS_LOG = "sessions-log.jsonl"
LEGACY_SESSIONS_LABELS = "sessions-labels.jsonl"
DEFAULT_PRICES = "prices/anthropic.json"


def _new_attempt_id() -> str:
    # Timestamp for readability, random suffix so two attempts started in the
    # same second never share an id (which would merge them into one attempt).
    stamp = datetime.now(timezone.utc).strftime("a%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


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


def _add_prices_as_of(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prices-as-of",
        metavar="DATE",
        help="price every call at the rates of this day (YYYY-MM-DD) or at the latest ones (latest); "
        "default: each call at the rates in force on its own day",
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--labels", default=DEFAULT_LABELS)
    parser.add_argument(
        "--prices",
        action="append",
        help="pricing table JSON; repeat to merge vendors (default: the Anthropic and OpenAI tables shipped with the tool)",
    )
    _add_prices_as_of(parser)
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
    parser.add_argument("--version", action="version", version=f"cpt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the capture proxy in the foreground")
    serve.add_argument("--port", type=int, default=4000)
    serve.add_argument("--task-id", default=os.environ.get("CPT_TASK_ID", "unassigned"))
    serve.add_argument("--attempt-id", default=os.environ.get("CPT_ATTEMPT_ID"))
    _add_proxy_options(serve)

    run = sub.add_parser(
        "run", help="run a command through the proxy: cpt run --task-id T1 -- <command>"
    )
    run.add_argument(
        "--task-id",
        help="task id (default: inferred from the git branch: issue number, else branch name)",
    )
    run.add_argument("--attempt-id")
    run.add_argument("--labels", default=DEFAULT_LABELS, help="labels file for --label-from-exit")
    run.add_argument(
        "--label-from-exit",
        action="store_true",
        help="label the attempt from the command's exit code: 0 is pass, anything else fail. "
        "Only for a command that checks the result itself (a script ending with the tests); "
        "an agent CLI exits 0 whether or not it solved the task",
    )
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

    explain = sub.add_parser(
        "explain", help="where one attempt's cost went: by token class, model and step"
    )
    explain.add_argument(
        "--attempt", help="attempt id, or a unique prefix of it (default: the latest attempt)"
    )
    explain.add_argument("--task", help="task id, to pick its latest attempt")
    explain.add_argument("--log", default=DEFAULT_LOG)
    explain.add_argument("--labels", default=DEFAULT_LABELS)
    explain.add_argument(
        "--prices",
        action="append",
        help="pricing table JSON; repeat to merge vendors (default: the shipped tables)",
    )
    _add_prices_as_of(explain)
    explain.add_argument(
        "--top", type=int, default=10, help="how many of the most expensive steps to list (0: none)"
    )

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

    sessions = sub.add_parser(
        "sessions", help="measure Claude Code and Cowork sessions from their transcripts"
    )
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    s_list = sessions_sub.add_parser("list", help="write a labelling sheet of your sessions")
    s_list.add_argument("--out", default=DEFAULT_SHEET, help="sheet to write or refresh")
    s_list.add_argument("--since", help="only sessions active on or after this date, YYYY-MM-DD")
    s_list.add_argument("--prices", action="append", help="pricing table(s) for the cost column")
    _add_prices_as_of(s_list)
    s_list.add_argument("--no-titles", action="store_true", help="leave session titles out of the sheet")
    s_list.add_argument(
        "--no-infer",
        action="store_true",
        help="leave the task column empty instead of suggesting one from the branch or pull request",
    )
    s_import = sessions_sub.add_parser("import", help="import the sessions you gave a task, with labels")
    s_import.add_argument("sheet", nargs="?", default=DEFAULT_SHEET)
    s_import.add_argument("--log", help=f"log to append to (default {DEFAULT_LOG}, as cpt run)")
    s_import.add_argument("--labels", help=f"labels file (default {DEFAULT_LABELS}, as cpt label)")
    s_import.add_argument(
        "--include-unlabelled",
        action="store_true",
        help="also import sessions whose task is still the suggested one and that have no outcome",
    )
    s_summary = sessions_sub.add_parser(
        "summary", help="totals by product, model and period at API list prices, no labels needed"
    )
    s_summary.add_argument("--since", help="only sessions active on or after this date, YYYY-MM-DD")
    s_summary.add_argument(
        "--by", choices=("day", "week", "month"), default="month", help="period of the time table"
    )
    s_summary.add_argument(
        "--subscription",
        type=float,
        metavar="PRICE",
        help="monthly subscription price, to show the list-price cost as a multiple of it",
    )
    s_summary.add_argument(
        "--top", type=int, default=5, help="how many of the most expensive sessions to list (0: none)"
    )
    s_summary.add_argument("--prices", action="append", help="pricing table(s) for the cost")
    _add_prices_as_of(s_summary)
    for sp in (s_list, s_import, s_summary):
        sp.add_argument("--source", choices=("all", "code", "cowork"), default="all",
                        help="Claude Code transcripts, Cowork sessions, or both")
        sp.add_argument("--path", action="append", default=[],
                        help="scan this folder instead of the default locations (repeatable)")

    sub.add_parser("mcp", help="serve the report over MCP on stdio")

    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "run": _cmd_run,
        "label": _cmd_label,
        "report": _cmd_report,
        "compare": _cmd_compare,
        "explain": _cmd_explain,
        "import": _cmd_import,
        "sessions": _cmd_sessions,
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

    task_source = None
    if not args.task_id:
        guess = suggest_task(branch=current_branch(), project=project_name(os.getcwd()))
        if guess is None:
            print(
                "cpt run: no --task-id, and no git branch to infer one from (not a repository, "
                "or a default branch); pass --task-id T1",
                file=sys.stderr,
            )
            return 2
        args.task_id, task_source = guess
        print(f"cpt: task id inferred from the git {task_source}: {args.task_id}")

    attempt_id = args.attempt_id or _new_attempt_id()
    server = create_proxy(
        upstreams=_upstreams(args),
        log_path=args.log,
        task_id=args.task_id,
        attempt_id=attempt_id,
        task_type=args.task_type,
        task_source=task_source,
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
    code = None
    try:
        code = subprocess.run(cmd, env=env).returncode
        return code
    finally:
        server.shutdown()
        server.server_close()
        print(
            f"cpt: captured {server.captured_count} steps "
            f"(task {args.task_id}, attempt {attempt_id}) in {args.log}"
        )
        if args.label_from_exit and code is not None and server.captured_count:
            outcome = "pass" if code == 0 else "fail"
            append_label(
                args.labels,
                Label(task_id=args.task_id, attempt_id=attempt_id, outcome=outcome,
                      note=f"from exit code {code}"),
            )
            print(f"cpt: labelled {outcome} from exit code {code} in {args.labels}")


def _latest_attempt(records, task_id: str | None) -> tuple[str, str] | None:
    # The log is append-only, so the last matching line is the latest attempt;
    # timestamps have one-second resolution and cannot break ties.
    for record in reversed(records):
        if task_id is None or record.task_id == task_id:
            return record.task_id, record.attempt_id
    return None


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


def _load_table(paths: list[str], args: argparse.Namespace) -> PricingTable:
    table = PricingTable.load_many(paths)
    if args.prices_as_of:
        table.pin_to(args.prices_as_of)
    return table


def _analysis(args: argparse.Namespace, *, by_task_type: bool):
    try:
        return load_analysis(
            log=args.log,
            labels=args.labels,
            prices=args.prices or default_table_paths(),
            prices_as_of=args.prices_as_of,
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


def _cmd_explain(args: argparse.Namespace) -> int:
    try:
        records = read_jsonl(args.log)
        table = _load_table(args.prices or default_table_paths(), args)
    except (OSError, PricingError) as exc:
        print(f"cpt explain: {exc}", file=sys.stderr)
        return 1
    in_task = [r for r in records if args.task is None or r.task_id == args.task]
    if args.attempt:
        ids = sorted({r.attempt_id for r in in_task})
        matches = [i for i in ids if i == args.attempt] or [i for i in ids if i.startswith(args.attempt)]
        if len(matches) != 1:
            shown = ", ".join(matches[:5]) + (" ..." if len(matches) > 5 else "")
            print(
                f"cpt explain: '{args.attempt}' matches {len(matches)} attempts"
                + (f": {shown}" if matches else ""),
                file=sys.stderr,
            )
            return 1
        steps = [r for r in in_task if r.attempt_id == matches[0]]
    else:
        found = _latest_attempt(records, args.task)
        if found is None:
            print("cpt explain: no matching attempt in the log", file=sys.stderr)
            return 1
        task_id, attempt_id = found
        steps = [r for r in records if r.task_id == task_id and r.attempt_id == attempt_id]
    table.usage = describe_usage(steps, table)
    try:
        explanation = explain_attempt(steps, table, load_labels(args.labels))
    except ValueError:
        print(
            f"cpt explain: attempt {steps[0].attempt_id} appears under several tasks; add --task",
            file=sys.stderr,
        )
        return 1
    print(render_explain(explanation, table, top_steps=args.top))
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
        merged = with_history(existing, table)
        write_table(merged, out)
        kept = len(merged.get("history", []))
        print(f"wrote {out}" + (f"; {kept} earlier snapshots kept under history" if kept else ""))
    elif changes or existing is None:
        print("dry run; pass --write to update the table")
    return 0


def _default_price_paths() -> list[str]:
    """The shipped Anthropic table, for pricing Claude Code and Cowork sessions."""
    return default_table_paths(("anthropic.json",))


def _session_roots(args: argparse.Namespace):
    if args.path:
        return roots_for_paths(args.path)
    roots = default_roots()
    return roots if args.source == "all" else [r for r in roots if r[0] == args.source]


def _print_session_warnings(stats) -> None:
    for key, text in WARNING_TEXT.items():
        if stats.get(key):
            print(f"cpt sessions: warning: {stats[key]} {text}", file=sys.stderr)


def _cmd_sessions(args: argparse.Namespace) -> int:
    if args.sessions_command == "list":
        return _cmd_sessions_list(args)
    if args.sessions_command == "summary":
        return _cmd_sessions_summary(args)
    return _cmd_sessions_import(args)


def _print_scan_header(sessions, stats, roots, since) -> None:
    scanned = [str(root) for _, root in roots if root.is_dir()]
    active = f" active on or after {since}" if since else ""
    print(
        f"cpt sessions: {len(sessions)} sessions with model calls{active}, "
        f"read from {stats['files']} transcripts in:"
    )
    for folder in scanned or ["(none of the folders exist; use --path)"]:
        print(f"  {folder}")


def _cmd_sessions_list(args: argparse.Namespace) -> int:
    try:
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        print(f"cpt sessions: --since expects YYYY-MM-DD, got '{args.since}'", file=sys.stderr)
        return 2
    roots = _session_roots(args)
    sessions, stats = discover_sessions(roots, since=since, include_titles=not args.no_titles)
    price_paths = args.prices or _default_price_paths()
    table = None
    if price_paths:
        try:
            table = _load_table(price_paths, args)
        except (OSError, PricingError) as exc:
            print(f"cpt sessions: cannot load prices: {exc}", file=sys.stderr)
            return 1
    out = Path(args.out)
    try:
        existing = {r["session_id"]: r for r in read_sheet(out)} if out.exists() else {}
        written, kept = write_sheet(
            out, sessions, table=table, existing=existing, infer=not args.no_infer
        )
    except PermissionError:
        print(f"cpt sessions: cannot write {out}; close it in Excel and run again", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"cpt sessions: {exc}", file=sys.stderr)
        return 1

    _print_scan_header(sessions, stats, roots, since)
    by_product: dict[str, int] = {}
    for session in sessions.values():
        by_product[session.product] = by_product.get(session.product, 0) + 1
    for product, count in sorted(by_product.items()):
        print(f"  {product}: {count}")
    if table is not None:
        table.usage = describe_usage([r for s in sessions.values() for r in s.records("-")], table)
        total = sum(s.cost(table)[0] for s in sessions.values())
        unpriced = sorted({m for s in sessions.values() for m in s.models() if table.rates_for(m) is None})
        print(f"cost column: {total:.2f} {table.currency} in total at API list prices, {prices_line(table)}.")
        print("  On a subscription you are not billed per token: this is a shadow cost.")
        if unpriced:
            print(f"  No price for {', '.join(unpriced)}; those calls count as zero.")
    else:
        print("cost column left empty: no pricing table found (pass --prices).")
    _print_session_warnings(stats)
    print(f"wrote {out}: {written} rows, {kept} with your entries kept.")
    if not args.no_infer:
        print("The task column is pre-filled from the issue number, pull request or git branch of each")
        print("session (task_source says which); change any that is wrong, sessions that did the same")
        print("job share a task. A suggested task is only imported once you give it an outcome.")
    print("Next: open it in Excel, fill task (same name for sessions that did the same job), task_type,")
    print("outcome (pass or fail) and leaked (yes or no) for the sessions to measure, save, then run:")
    print(f"  python -m cost_per_task.cli sessions import {out}")
    return 0


def _cmd_sessions_import(args: argparse.Namespace) -> int:
    try:
        rows = read_sheet(args.sheet)
    except (OSError, ValueError) as exc:
        print(f"cpt sessions: cannot read the sheet: {exc}", file=sys.stderr)
        return 1
    log = _sessions_file(args.log, DEFAULT_LOG, LEGACY_SESSIONS_LOG)
    labels_path = _sessions_file(args.labels, DEFAULT_LABELS, LEGACY_SESSIONS_LABELS)
    sessions, stats = discover_sessions(_session_roots(args), include_titles=False)
    imported_tasks: dict[str, str] = {}
    if Path(log).exists():
        for record in read_jsonl(log):
            imported_tasks.setdefault(record.attempt_id, record.task_id)
    existing_labels = load_labels(labels_path)
    result = import_sheet(
        rows, sessions, imported_tasks, existing_labels, include_unlabelled=args.include_unlabelled
    )

    writer = JsonlWriter(log)
    for record in result.records:
        writer.append(record)
    for label in result.labels:
        append_label(labels_path, label)

    print(
        f"cpt sessions: imported {len(result.imported)} sessions "
        f"({len(result.records)} model calls) into {log}; "
        f"{len(result.labels)} labels written to {labels_path}"
    )
    print(
        f"  skipped: no task {result.blank}, already in the log: {result.already_imported}, "
        f"labels unchanged {result.unchanged_labels}"
    )
    if result.inferred_only:
        print(
            f"  left out: {result.inferred_only} sessions with a suggested task and no outcome "
            "(fill outcome, or pass --include-unlabelled to measure their cost anyway)"
        )
    if result.missing:
        print(
            f"  {len(result.missing)} sessions in the sheet have no transcript any more: "
            + ", ".join(result.missing[:5])
            + (" ..." if len(result.missing) > 5 else "")
        )
    for problem in result.problems:
        print(f"cpt sessions: warning: {problem}", file=sys.stderr)
    _print_session_warnings(stats)
    measured = [sessions[sid] for sid in imported_tasks if sid in sessions]
    harness = harness_summary(measured) or "Claude Code"
    options = (f" --log {log}" if log != DEFAULT_LOG else "") + (
        f" --labels {labels_path}" if labels_path != DEFAULT_LABELS else ""
    )
    print("Report:")
    print(f'  python -m cost_per_task.cli report{options} --seed 1 --harness "{harness}"')
    return 0


def _sessions_file(given: str | None, default: str, legacy: str) -> str:
    """The log or labels file for cpt sessions import: what was asked for,
    else the default shared with cpt run, else the 0.5.0 name if only that
    file exists."""
    if given:
        return given
    if not Path(default).exists() and Path(legacy).exists():
        print(
            f"cpt sessions: using {legacy} from an earlier version; rename it to {default} "
            "to share the default files with the other commands",
            file=sys.stderr,
        )
        return legacy
    return default


def _cmd_sessions_summary(args: argparse.Namespace) -> int:
    try:
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        print(f"cpt sessions: --since expects YYYY-MM-DD, got '{args.since}'", file=sys.stderr)
        return 2
    try:
        table = _load_table(args.prices or _default_price_paths(), args)
    except (OSError, PricingError) as exc:
        print(f"cpt sessions: cannot load prices: {exc}", file=sys.stderr)
        return 1
    roots = _session_roots(args)
    sessions, stats = discover_sessions(roots, since=since, include_titles=False)
    _print_scan_header(sessions, stats, roots, since)
    if not sessions:
        _print_session_warnings(stats)
        return 1
    summary = summarise_sessions(sessions, table, period=args.by, top=args.top)
    print()
    print(render_sessions_summary(summary, table, subscription=args.subscription))
    _print_session_warnings(stats)
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    return mcp_main()


if __name__ == "__main__":
    raise SystemExit(main())
