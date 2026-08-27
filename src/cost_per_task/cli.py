"""The cpt command line interface.

Commands:
  cpt serve   run the capture proxy in the foreground
  cpt run     run an agent command with the proxy set up and calls tagged
  cpt report  summarise a JSONL log with a pricing table
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone

from .pricing import PricingError, PricingTable
from .proxy import create_proxy
from .report import render_report
from .schema import read_jsonl

DEFAULT_LOG = "cpt-log.jsonl"
DEFAULT_UPSTREAM = "https://api.anthropic.com"


def _new_attempt_id() -> str:
    return datetime.now(timezone.utc).strftime("a%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cpt", description="Measure the real cost per completed task of LLM agents."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the capture proxy in the foreground")
    serve.add_argument("--port", type=int, default=4000)
    serve.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    serve.add_argument("--log", default=DEFAULT_LOG)
    serve.add_argument("--task-id", default=os.environ.get("CPT_TASK_ID", "unassigned"))
    serve.add_argument("--attempt-id", default=os.environ.get("CPT_ATTEMPT_ID"))

    run = sub.add_parser(
        "run", help="run a command through the proxy: cpt run --task-id T1 -- <command>"
    )
    run.add_argument("--task-id", required=True)
    run.add_argument("--attempt-id")
    run.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    run.add_argument("--log", default=DEFAULT_LOG)
    run.add_argument("cmd", nargs=argparse.REMAINDER)

    report = sub.add_parser("report", help="summarise a JSONL log")
    report.add_argument("--log", default=DEFAULT_LOG)
    report.add_argument("--prices", required=True)

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "run":
        return _cmd_run(args)
    return _cmd_report(args)


def _cmd_serve(args: argparse.Namespace) -> int:
    attempt_id = args.attempt_id or _new_attempt_id()
    server = create_proxy(
        upstream=args.upstream,
        log_path=args.log,
        task_id=args.task_id,
        attempt_id=attempt_id,
        port=args.port,
    )
    host, port = server.server_address[:2]
    print(f"cpt proxy on http://{host}:{port} -> {args.upstream}")
    print(f"log: {args.log}  task: {args.task_id}  attempt: {attempt_id}")
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
        upstream=args.upstream,
        log_path=args.log,
        task_id=args.task_id,
        attempt_id=attempt_id,
        port=0,
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
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


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        table = PricingTable.load(args.prices)
    except (OSError, PricingError) as exc:
        print(f"cpt report: cannot load prices: {exc}", file=sys.stderr)
        return 1
    try:
        records = read_jsonl(args.log)
    except OSError as exc:
        print(f"cpt report: cannot read log: {exc}", file=sys.stderr)
        return 1
    print(render_report(records, table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
