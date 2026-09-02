"""The cpt command line interface.

Commands:
  cpt serve    run the capture proxy in the foreground
  cpt run      run an agent command with the proxy set up and calls tagged
  cpt label    mark an attempt pass or fail (and optionally leaked), or import a CSV
  cpt report   cost per attempt and per solved task, per model and task type
  cpt compare  two-model comparison with the break-even cleanup cost K*
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone

from .labels import Label, LabelError, append_label, import_csv, load_labels
from .metrics import build_attempts, summarise
from .pricing import PricingError, PricingTable
from .proxy import DEFAULT_UPSTREAMS, create_proxy
from .report import render_comparison, render_report
from .schema import read_jsonl

DEFAULT_LOG = "cpt-log.jsonl"
DEFAULT_LABELS = "cpt-labels.jsonl"


def _new_attempt_id() -> str:
    return datetime.now(timezone.utc).strftime("a%Y%m%dT%H%M%SZ")


def _add_proxy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--task-type", help="free-text task category, e.g. coding")
    parser.add_argument(
        "--anthropic-upstream", default=DEFAULT_UPSTREAMS["anthropic"], help="scheme and host only"
    )
    parser.add_argument(
        "--openai-upstream", default=DEFAULT_UPSTREAMS["openai"], help="scheme and host only"
    )
    parser.add_argument(
        "--no-inject-usage",
        action="store_true",
        help="do not add stream_options.include_usage to streaming OpenAI chat requests",
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--labels", default=DEFAULT_LABELS)
    parser.add_argument("--prices", required=True, help="pricing table JSON")
    parser.add_argument("--cleanup-cost", type=float, help="K: cost of one leaked failure")
    parser.add_argument("--leak-rate", type=float, help="override L instead of using leak labels")
    parser.add_argument("--retry-cap", type=int, help="N for p_N (default: max attempts per task)")
    parser.add_argument("--k", type=int, help="k for pass^k (default: usual attempts per task)")
    parser.add_argument("--resamples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, help="bootstrap seed for reproducible intervals")
    parser.add_argument("--harness", help="harness name and version for the disclosure checklist")
    parser.add_argument("--task-type", help="only include attempts of this task type")


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

    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "run": _cmd_run,
        "label": _cmd_label,
        "report": _cmd_report,
        "compare": _cmd_compare,
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


def _load_analysis(args: argparse.Namespace, *, by_task_type: bool):
    try:
        table = PricingTable.load(args.prices)
    except (OSError, PricingError) as exc:
        print(f"cpt: cannot load prices: {exc}", file=sys.stderr)
        return None
    try:
        records = read_jsonl(args.log)
    except OSError as exc:
        print(f"cpt: cannot read log: {exc}", file=sys.stderr)
        return None
    labels = load_labels(args.labels)
    attempts = build_attempts(records, table, labels)
    if args.task_type:
        attempts = [a for a in attempts if a.task_type == args.task_type]
    summaries = summarise(
        attempts,
        by_task_type=by_task_type,
        k=args.k,
        retry_cap=args.retry_cap,
        cleanup_cost=args.cleanup_cost,
        leak_rate=args.leak_rate,
        resamples=args.resamples,
        seed=args.seed,
    )
    return table, attempts, summaries


def _cmd_report(args: argparse.Namespace) -> int:
    loaded = _load_analysis(args, by_task_type=not args.by_model_only)
    if loaded is None:
        return 1
    table, attempts, summaries = loaded
    print(render_report(attempts, summaries, table, harness=args.harness))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    loaded = _load_analysis(args, by_task_type=False)
    if loaded is None:
        return 1
    table, _attempts, summaries = loaded
    picked = []
    for wanted in (args.model_a, args.model_b):
        matches = [s for s in summaries if s.model == wanted or s.model.startswith(wanted)]
        if len(matches) != 1:
            names = ", ".join(s.model for s in summaries) or "none"
            print(
                f"cpt compare: '{wanted}' matches {len(matches)} models; available: {names}",
                file=sys.stderr,
            )
            return 1
        picked.append(matches[0])
    print(render_comparison(picked[0], picked[1], table, harness=args.harness))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
