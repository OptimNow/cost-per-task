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
# What people type for an outcome, in English and French. Anything else is refused
# and reported, never guessed.
_PASS_WORDS = {"pass", "passed", "ok", "yes", "y", "success", "oui", "o", "reussi", "réussi", "succes", "succès"}
_FAIL_WORDS = {"fail", "failed", "ko", "no", "n", "failure", "non", "echec", "échec"}


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


def normalise_outcome(text: str | None) -> str | None:
    """``pass`` or ``fail`` for what a person typed (``OK``, ``ko``, ``oui``), ``""`` for
    nothing, None for a word that is neither."""
    word = (text or "").strip().lower()
    if not word:
        return ""
    if word in _PASS_WORDS:
        return "pass"
    if word in _FAIL_WORDS:
        return "fail"
    return None


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
    optional leaked (1/true/yes) and note. Returns the number imported. Every
    row is checked before the first one is written, so a bad row imports nothing."""
    labels: list[Label] = []
    with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"task_id", "attempt_id", "outcome"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise LabelError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        for number, row in enumerate(reader, start=2):  # row 1 is the header
            leaked = (row.get("leaked") or "").strip().lower() in _TRUE_VALUES
            outcome = normalise_outcome(row.get("outcome"))
            if not outcome:
                raise LabelError(
                    f"row {number}: outcome {row.get('outcome')!r} is not pass or fail; nothing imported"
                )
            labels.append(
                Label(
                    task_id=(row.get("task_id") or "").strip(),
                    attempt_id=(row.get("attempt_id") or "").strip(),
                    outcome=outcome,
                    leaked=leaked,
                    note=(row.get("note") or None),
                )
            )
    for label in labels:
        append_label(labels_path, label)
    return len(labels)
