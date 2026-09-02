"""Outcome labels for attempts, kept apart from the usage log.

The usage log is append-only and never rewritten. Labels live in their own
JSONL file keyed by (task_id, attempt_id); re-labelling appends a new line
and the latest line wins. A leak is an attempt that was accepted as a pass
but later found to be wrong (the L term in CPT_risk).
"""

from __future__ import annotations

import csv
import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES = ("pass", "fail")
_TRUE_VALUES = {"1", "true", "yes", "y"}


class LabelError(ValueError):
    pass


@dataclass
class Label:
    task_id: str
    attempt_id: str
    outcome: str  # pass | fail
    leaked: bool = False
    labelled_at: str = ""
    note: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise LabelError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")
        if not self.labelled_at:
            self.labelled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_label(path: str | Path, label: Label) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dataclasses.asdict(label)) + "\n")


def load_labels(path: str | Path) -> dict[tuple[str, str], Label]:
    """Latest label per (task_id, attempt_id); an absent file means no labels."""
    labels: dict[tuple[str, str], Label] = {}
    file = Path(path)
    if not file.exists():
        return labels
    known = {f.name for f in dataclasses.fields(Label)}
    with file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            data = {k: v for k, v in json.loads(line).items() if k in known}
            label = Label(**data)
            labels[(label.task_id, label.attempt_id)] = label
    return labels


def import_csv(csv_path: str | Path, labels_path: str | Path) -> int:
    """Append labels from a CSV with columns task_id, attempt_id, outcome and
    optional leaked (1/true/yes) and note. Returns the number imported."""
    count = 0
    with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"task_id", "attempt_id", "outcome"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise LabelError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            leaked = (row.get("leaked") or "").strip().lower() in _TRUE_VALUES
            label = Label(
                task_id=row["task_id"].strip(),
                attempt_id=row["attempt_id"].strip(),
                outcome=row["outcome"].strip().lower(),
                leaked=leaked,
                note=(row.get("note") or None),
            )
            append_label(labels_path, label)
            count += 1
    return count
