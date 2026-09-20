"""A default task id from what the environment already knows.

Labelling by hand is the bottleneck of a weekly measurement, and an empty task
column measures nothing. The guess comes from structure only (git branch, pull
request number, working folder, day, session id), never from prompts, answers or
session titles. It may split one job over two task ids, which a person fixes by
giving them one name; it never merges two sessions on a hunch, since a false
merge would pass unrelated work off as retries of one task. It is always marked
as a guess (``task_source``), so a report can say how many of its tasks a person
stated and how many were inferred. Outcomes are never guessed: pass or fail
stays a human call.
"""

from __future__ import annotations

import re
import subprocess

# Branches that say nothing about the task at hand.
DEFAULT_BRANCHES = frozenset({"", "HEAD", "main", "master", "develop", "development", "trunk"})

# An issue number opens the branch name or one of its segments: 123-fix-login,
# fix/123-login, feature/issue-45. A version (release/0.6.0) or a date
# (2026-09-notes) is not an issue.
_ISSUE = re.compile(r"(?:^|/)(?:issue[-_/]?|gh[-_]?|#)?(\d{1,6})(?![.\d])(?!-\d)(?=[-_/]|$)", re.IGNORECASE)

MANUAL = "manual"


def issue_in_branch(branch: str) -> str | None:
    match = _ISSUE.search(branch or "")
    return match.group(1) if match else None


def suggest_task(
    *,
    branch: str = "",
    pr_number: int | str | None = None,
    project: str = "",
    day: str = "",
    session: str = "",
) -> tuple[str, str] | None:
    """(task id, source) from the strongest signal available, or None. Sources
    in order: ``issue`` (number in the branch name), ``pr``, ``branch``, then
    ``session`` (the session on its own: project, day and the start of its
    id). The project name prefixes the id so that two repositories never share
    a task."""
    prefix = f"{project}/" if project else ""
    branch = (branch or "").strip()
    issue = issue_in_branch(branch)
    if issue:
        return f"{prefix}issue-{issue}", "issue"
    if pr_number not in (None, ""):
        return f"{prefix}pr-{pr_number}", "pr"
    if branch not in DEFAULT_BRANCHES:
        return f"{prefix}{branch}", "branch"
    if project and day and session:
        return f"{project}/{day}-{session[:8]}", "session"
    return None


def source_of(task: str, suggestion: tuple[str, str] | None) -> str:
    """How a task id came about: the suggestion's source when the id is still
    the suggested one, ``manual`` when a person typed something else."""
    if suggestion is not None and task == suggestion[0]:
        return suggestion[1]
    return MANUAL


def project_name(cwd: str) -> str:
    """The working folder's name; for a Claude Code worktree
    (``<repo>/.claude/worktrees/<name>``), the repository's."""
    parts = [p for p in re.split(r"[\\/]", cwd or "") if p]
    for index in range(len(parts) - 2):
        if parts[index + 1] == ".claude" and parts[index + 2] == "worktrees":
            return parts[index]
    return parts[-1] if parts else ""


def current_branch(cwd: str | None = None) -> str:
    """The checked-out git branch, or '' outside a repository or without git."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""
