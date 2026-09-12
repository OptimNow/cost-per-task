from __future__ import annotations

import re

from cost_per_task import cli
from cost_per_task.schema import StepRecord


def test_attempt_ids_are_unique_within_one_second():
    ids = {cli._new_attempt_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"a\d{8}T\d{6}Z-[0-9a-f]{4}", i) for i in ids)


def _rec(task: str, attempt: str) -> StepRecord:
    return StepRecord(task_id=task, attempt_id=attempt, step_id=1,
                      timestamp="2026-09-12T10:00:00+00:00", provider="anthropic", model="m")


def test_latest_attempt_follows_log_order_not_id_order():
    # Same-second timestamps and ids that sort the wrong way round.
    records = [_rec("t1", "a-ffff"), _rec("t2", "a-0000"), _rec("t1", "a-0001")]
    assert cli._latest_attempt(records, "t1") == ("t1", "a-0001")
    assert cli._latest_attempt(records, None) == ("t1", "a-0001")
    assert cli._latest_attempt(records, "t9") is None
