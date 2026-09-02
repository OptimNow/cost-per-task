from __future__ import annotations

import pytest

from cost_per_task.labels import Label, LabelError, append_label, import_csv, load_labels


def test_latest_label_wins(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, Label("T1", "a1", "fail"))
    append_label(path, Label("T1", "a1", "pass", leaked=True))
    append_label(path, Label("T1", "a2", "pass"))
    labels = load_labels(path)
    assert labels[("T1", "a1")].outcome == "pass"
    assert labels[("T1", "a1")].leaked is True
    assert labels[("T1", "a2")].leaked is False
    assert labels[("T1", "a1")].labelled_at


def test_missing_file_means_no_labels(tmp_path):
    assert load_labels(tmp_path / "absent.jsonl") == {}


def test_invalid_outcome_rejected():
    with pytest.raises(LabelError):
        Label("T1", "a1", "maybe")


def test_csv_import(tmp_path):
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "task_id,attempt_id,outcome,leaked,note\n"
        "T1,a1,pass,,\n"
        "T1,a2,PASS,yes,accepted but wrong\n"
        "T2,a1,fail,0,\n",
        encoding="utf-8",
    )
    labels_path = tmp_path / "labels.jsonl"
    assert import_csv(csv_path, labels_path) == 3
    labels = load_labels(labels_path)
    assert labels[("T1", "a2")].outcome == "pass"
    assert labels[("T1", "a2")].leaked is True
    assert labels[("T1", "a2")].note == "accepted but wrong"
    assert labels[("T2", "a1")].outcome == "fail"


def test_csv_import_requires_columns(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("task_id,outcome\nT1,pass\n", encoding="utf-8")
    with pytest.raises(LabelError):
        import_csv(csv_path, tmp_path / "labels.jsonl")
