"""The self-contained labelling page (cpt sessions page).

A browser is not available to the test suite, so two things are checked here:
the guarantees that can be read off the page itself (nothing loaded, nothing
connected, nothing run but the module's own script, text never parsed as
markup), and the contract with the importer, on a CSV recorded from the real
page in Chromium (tests/fixtures/page_export.b64, synthetic sessions with
hostile titles, saved on 2026-09-20)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from cost_per_task import cli
from cost_per_task.importers.claude_sessions import SHEET_COLUMNS, read_sheet
from cost_per_task.labels import load_labels
from cost_per_task.review_page import BODY, SCRIPT, STYLE, content_security_policy, render_page
from cost_per_task.schema import read_jsonl

INJECTION = '</script><script>alert("title injection")</script><img src=x onerror=alert(1)>'
FORMULA = '=HYPERLINK("http://attacker.example","open me")'
RATES = {
    "input_per_mtok": 0.0,
    "cache_read_per_mtok": 0.0,
    "cache_write_5m_per_mtok": 0.0,
    "cache_write_1h_per_mtok": 0.0,
    "output_per_mtok": 1_000_000.0,
}


def _call(sid, mid, ts, *, branch, pr=None, cwd="C:\\work\\shop"):
    entry = {
        "type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": "r" + mid,
        "entrypoint": "claude-desktop", "cwd": cwd, "version": "2.1.257", "gitBranch": branch,
        "message": {"id": mid, "model": "claude-fable-5-1", "role": "assistant", "content": [],
                    "usage": {"input_tokens": 100, "output_tokens": 10}},
    }
    if pr is not None:
        entry["prNumber"] = pr
    return entry


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The sessions the recorded export was made from: same ids, titles and branches."""
    root = tmp_path / "claude" / "projects" / "shop"
    root.mkdir(parents=True)
    sessions = {
        "aaaaaaaa-1111": ("Login fix, first try", [
            _call("aaaaaaaa-1111", "a1", "2026-09-15T08:00:00Z", branch="fix/123-login"),
            _call("aaaaaaaa-1111", "a2", "2026-09-15T08:41:00Z", branch="fix/123-login")]),
        "bbbbbbbb-2222": ("Login fix, retry", [
            _call("bbbbbbbb-2222", "b1", "2026-09-16T09:00:00Z", branch="fix/123-login")]),
        "cccccccc-3333": ("Quick path for the guide", [
            _call("cccccccc-3333", "c1", "2026-09-17T10:00:00Z", branch="docs/guide", pr=14)]),
        "dddddddd-4444": (INJECTION, [
            _call("dddddddd-4444", "d1", "2026-09-18T11:00:00Z", branch="main", cwd="C:\\work\\lab-notes")]),
        "eeeeeeee-5555": (FORMULA, [
            _call("eeeeeeee-5555", "e1", "2026-09-19T12:00:00Z", branch="main", cwd="C:\\work\\lab-notes")]),
    }
    for sid, (title, lines) in sessions.items():
        entries = [{"type": "custom-title", "customTitle": title, "sessionId": sid}, *lines]
        (root / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"currency": "USD", "as_of": "2026-09-01", "models": {"claude-fable-5-1": RATES}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path, ["--path", str(tmp_path / "claude" / "projects"), "--prices", str(prices)]


def _data(html: str) -> dict:
    block = re.search(r'<script type="application/json" id="cpt-data">(.*?)</script>', html, re.S)
    return json.loads(block.group(1))


def _sha256(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


# --- what can be read off the page ----------------------------------------------


def test_policy_blocks_every_load_and_connection_and_pins_the_script():
    policy = content_security_policy()
    assert policy.startswith("default-src 'none';")
    assert f"script-src '{_sha256(SCRIPT)}'" in policy and f"style-src '{_sha256(STYLE)}'" in policy
    assert "base-uri 'none'" in policy and "form-action 'none'" in policy
    for loosening in ("unsafe-inline", "unsafe-eval", "http", "data:", "blob:", "*", "connect-src"):
        assert loosening not in policy

    html = render_page([])
    head = html[: html.index("<style>")]
    assert f'content="{policy}"' in head  # the policy comes before anything it governs
    # Exactly what the hashes cover sits between the tags.
    assert f"<style>{STYLE}</style>" in html and f"<script>{SCRIPT}</script>" in html


def test_template_loads_nothing_and_has_no_way_out():
    for part in (STYLE, BODY, SCRIPT):
        assert "http://" not in part and "https://" not in part and "//" not in part.replace("// ", "")
    assert not re.search(r"\b(src|href|action|srcset|poster|background)\s*=", BODY)
    assert not re.search(r"\bon[a-z]+\s*=", BODY) and "style=" not in BODY  # no inline handlers or styles
    assert "url(" not in STYLE and "@import" not in STYLE
    for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(",
                 "localStorage", "sessionStorage", "indexedDB", "document.cookie", "postMessage",
                 "eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert call not in SCRIPT, call


def test_page_is_plain_ascii_whatever_the_titles_hold():
    html = render_page([{"session_id": "s1", "title": "r\u00e9sum\u00e9 \u2028 \U0001f9ea", "status": "new"}])
    assert html.isascii()
    # The byte order mark of the CSV is written by the script as an escape, not as the character.
    assert "\\ufeff" in SCRIPT and "\ufeff" not in SCRIPT
    assert _data(html)["rows"][0]["title"] == "r\u00e9sum\u00e9 \u2028 \U0001f9ea"


def test_a_title_cannot_break_out_of_the_data_block():
    html = render_page([{"session_id": "s1", "title": INJECTION + "<!--", "status": "new"}])
    assert html.count("<script") == 2 and html.count("</script>") == 2  # the data block and the script
    assert "<img" not in html and "<!--" not in html  # no tag and no comment opens inside the data
    assert "\\u003c/script\\u003e" in html and "\\u003cimg" in html
    assert _data(html)["rows"][0]["title"] == INJECTION + "<!--"


def test_page_data_matches_the_sheet_columns():
    html = render_page(
        [{"session_id": "s1", "task": "shop/pr-9", "status": "new"}],
        suggestions={"s1": ("shop/pr-9", "pr")}, currency="USD", generated="2026-09-20 12:00",
    )
    data = _data(html)
    assert data["columns"] == SHEET_COLUMNS
    row = data["rows"][0]
    assert set(SHEET_COLUMNS) <= set(row) and (row["suggested_task"], row["suggested_source"]) == ("shop/pr-9", "pr")
    assert "ok" in data["pass_words"] and "ko" in data["fail_words"] and "oui" in data["yes_words"]


# --- the command ---------------------------------------------------------------


def test_page_command_writes_one_file_and_carries_entries_over(lab, capsys):
    tmp_path, scan = lab
    assert cli.main(["sessions", "page", *scan]) == 0
    out = capsys.readouterr().out
    assert "wrote sessions.html: 5 sessions, 0 with entries carried over" in out
    assert "treat sessions.html as private" in out
    raw = (tmp_path / "sessions.html").read_bytes()
    assert b"\r" not in raw  # written as bytes: the hashed script is the script on disk
    html = raw.decode("ascii")
    rows = {r["session_id"][:4]: r for r in _data(html)["rows"]}
    assert rows["cccc"]["task"] == "shop/pr-14" and rows["cccc"]["suggested_source"] == "pr"
    assert rows["dddd"]["title"] == INJECTION and rows["aaaa"]["minutes"] == "41"
    assert [p.name for p in tmp_path.iterdir() if p.suffix in (".html", ".csv")] == ["sessions.html"]

    # An outcome typed in the spreadsheet shows up in the page.
    assert cli.main(["sessions", "list", *scan]) == 0
    sheet = tmp_path / "sessions.csv"
    sheet.write_text(sheet.read_text(encoding="utf-8-sig").replace("shop/pr-14,pr,,", "shop/pr-14,pr,,ok"),
                     encoding="utf-8-sig")
    capsys.readouterr()
    assert cli.main(["sessions", "page", *scan]) == 0
    assert "1 with entries carried over" in capsys.readouterr().out
    rows = {r["session_id"][:4]: r for r in _data((tmp_path / "sessions.html").read_text())["rows"]}
    assert (rows["cccc"]["outcome"], rows["cccc"]["status"]) == ("ok", "labelled")


def test_page_command_honours_the_privacy_options(lab, capsys):
    tmp_path, scan = lab
    assert cli.main(["sessions", "page", "--no-titles", "--no-infer", "--out", "bare.html", *scan]) == 0
    html = (tmp_path / "bare.html").read_text()
    rows = _data(html)["rows"]
    assert all(r["title"] == "" and r["branch"] == "" and r["task"] == "" and r["suggested_task"] == "" for r in rows)
    assert "title injection" not in html and "fix/123-login" not in html


# --- the contract with the importer ---------------------------------------------


def test_csv_recorded_from_the_real_page_imports(lab, capsys):
    """In the browser: first try failed, the retry passed and leaked, the two lab-notes
    sessions were selected through the project filter and given one task (a formula,
    to make the point) and a pass in bulk; the guide session was left alone."""
    tmp_path, scan = lab
    recorded = Path(__file__).parent / "fixtures" / "page_export.b64"
    (tmp_path / "sessions.csv").write_bytes(base64.b64decode(recorded.read_text().strip()))

    rows = {r["session_id"][:4]: r for r in read_sheet(tmp_path / "sessions.csv")}
    assert list(next(iter(rows.values()))) == SHEET_COLUMNS
    assert rows["eeee"]["title"] == FORMULA and rows["dddd"]["title"] == INJECTION  # guard off again
    assert rows["eeee"]["task"] == "=cmd|' /C calc'!A0" and rows["eeee"]["task_source"] == "manual"
    assert (rows["bbbb"]["outcome"], rows["bbbb"]["leaked"], rows["bbbb"]["status"]) == ("pass", "yes", "labelled")
    assert (rows["cccc"]["outcome"], rows["cccc"]["status"]) == ("", "new")

    assert cli.main(["sessions", "import", *scan[:2]]) == 0
    out = capsys.readouterr().out
    assert "imported 4 sessions" in out and "left out: 1 sessions with a suggested task" in out
    tasks = {r.attempt_id[:4]: (r.task_id, r.task_source) for r in read_jsonl(tmp_path / "cpt-log.jsonl")}
    assert tasks == {
        "aaaa": ("shop/issue-123", "issue"), "bbbb": ("shop/issue-123", "issue"),
        "dddd": ("=cmd|' /C calc'!A0", "manual"), "eeee": ("=cmd|' /C calc'!A0", "manual"),
    }
    labels = {k[1][:4]: (v.outcome, v.leaked) for k, v in load_labels(tmp_path / "cpt-labels.jsonl").items()}
    assert labels == {"aaaa": ("fail", False), "bbbb": ("pass", True), "dddd": ("pass", False), "eeee": ("pass", False)}
