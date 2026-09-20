"""Label sessions one by one in the terminal (``cpt sessions label``).

The spreadsheet round trip suits a one-off study. A weekly habit needs less:
the sessions since last time, shown one at a time, one letter each. Every
answer becomes a sheet row and goes through ``import_sheet``, the path the
spreadsheet takes, so both ways of labelling write the same records. Titles
are shown on screen to jog the memory and are written nowhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .importers.claude_sessions import Session, _local
from .labels import normalise_outcome
from .pricing import PricingTable
from .taskid import source_of

KEYS = "p pass | f fail | l leak | s skip | t task | m same task as previous | y type | n note | q quit"
_WORDS = {"pass": "p", "fail": "f", "leak": "l", "skip": "s", "task": "t", "same": "m", "type": "y",
          "note": "n", "quit": "q", "exit": "q"}


@dataclass
class Answer:
    session: Session
    row: dict  # a labelling sheet row: session_id, task, task_type, outcome, leaked, note


def _card(index: int, total: int, session: Session, table: PricingTable | None, task: str,
          source: str, task_type: str, note: str) -> list[str]:
    cost = f"{session.cost(table)[0]:.2f} {table.currency}" if table is not None else ""
    head = [
        f"[{index}/{total}]", session.project or session.product, _local(session.started),
        f"{session.minutes} min", f"{len(session.calls)} calls", cost, session.main_model(table),
    ]
    lines = ["", "  ".join(part for part in head if part)]
    lines.append(f"       task: {task or '(none yet: press t)'}" + (f" ({source})" if task else ""))
    if session.branch:
        lines.append(f"       branch: {session.branch}")
    if session.title:
        lines.append(f"       title: {session.title}")
    if task_type or note:
        lines.append(f"       type: {task_type or '-'}   note: {note or '-'}")
    return lines


def review(
    sessions: list[Session],
    *,
    table: PricingTable | None = None,
    fixed_tasks: dict[str, str] | None = None,
    infer: bool = True,
    ask: Callable[[str], str] | None = None,
    out: Callable[[str], None] = print,
) -> Iterator[Answer]:
    """Show each session and yield an Answer as soon as it gets an outcome, so the caller can
    save it before the next question. ``fixed_tasks`` maps sessions already in the log to their
    task, which can no longer change. Ends on q, on end of input or on Ctrl-C."""
    fixed_tasks = fixed_tasks or {}
    ask = ask or input  # looked up now, so that a test or a wrapper can replace input()
    previous_task = ""
    out(f"keys: {KEYS}")
    for index, session in enumerate(sessions, start=1):
        sid = session.session_id
        suggestion = session.suggested_task()
        task = fixed_tasks.get(sid) or (suggestion[0] if infer and suggestion else "")
        task_type = note = ""
        while True:
            source = "in the log" if sid in fixed_tasks else source_of(task, suggestion)
            for line in _card(index, len(sessions), session, table, task, source, task_type, note):
                out(line)
            try:
                typed = ask("       > ").strip().lower()
                key = _WORDS.get(typed, typed)
                # One letter is a key (n is note, y is type); a word may be an outcome (ok, ko, oui).
                word = normalise_outcome(typed) if len(typed) > 1 else None
                outcome = {"p": "pass", "l": "pass", "f": "fail"}.get(key) or word
                if outcome:
                    if not task:
                        out("       give the task a name first (t)")
                        continue
                    yield Answer(session, {
                        "session_id": sid, "task": task, "task_type": task_type, "outcome": outcome,
                        "leaked": "yes" if key == "l" else "", "note": note,
                    })
                    previous_task = task
                    break
                if key == "s":
                    break
                if key == "q":
                    return
                if key in ("t", "m") and sid in fixed_tasks:
                    out("       this session is already in the log under that task; the name stays")
                elif key == "t":
                    task = ask("       task name: ").strip() or task
                elif key == "m":
                    if previous_task:
                        task = previous_task
                    else:
                        out("       no task labelled yet in this run")
                elif key == "y":
                    task_type = ask("       task type (coding, writing, analysis ...): ").strip()
                elif key == "n":
                    note = ask("       note: ").strip()
                else:
                    out(f"       keys: {KEYS}")
            except (EOFError, KeyboardInterrupt):
                out("")
                return
