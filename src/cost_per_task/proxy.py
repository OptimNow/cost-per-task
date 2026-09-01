"""Local reverse proxy that captures token usage from LLM API responses.

The agent is pointed at this server via ANTHROPIC_BASE_URL (or the
equivalent variable for another provider). Every request is forwarded to
the real API and the response is returned unchanged; only usage metadata
is written to the JSONL log.

Security invariants:
- request and response bodies are never logged
- headers, including API keys, are never logged
- the log holds only token counts, model, provider, ids, latency and tool names
"""

from __future__ import annotations

import http.client
import itertools
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .providers.anthropic import AnthropicAdapter
from .providers.base import ParsedUsage, ProviderAdapter
from .schema import JsonlWriter, StepRecord

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: str,
        log_path: str | Path,
        task_id: str,
        attempt_id: str,
        adapter: ProviderAdapter | None = None,
    ) -> None:
        super().__init__(address, ProxyHandler)
        split = urlsplit(upstream)
        self.upstream_scheme = split.scheme or "https"
        self.upstream_host = split.netloc
        self.adapter: ProviderAdapter = adapter or AnthropicAdapter()
        self.writer = JsonlWriter(log_path)
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.captured_count = 0
        self._step_counter = itertools.count(1)

    def next_step_id(self) -> int:
        return next(self._step_counter)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def log_message(self, format: str, *args) -> None:
        # The default access log prints request lines to stderr; keep quiet.
        pass

    def _handle(self) -> None:
        length_header = self.headers.get("Content-Length")
        if length_header:
            body = self.rfile.read(int(length_header))
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self._read_chunked_body()
        else:
            body = None

        headers = {}
        for name, value in self.headers.items():
            if name.lower() in _HOP_BY_HOP or name.lower() == "accept-encoding":
                continue
            headers[name] = value
        # Identity encoding so the usage block can be parsed in clear text.
        headers["Accept-Encoding"] = "identity"
        headers["Host"] = self.server.upstream_host

        connection_cls = (
            http.client.HTTPSConnection
            if self.server.upstream_scheme == "https"
            else http.client.HTTPConnection
        )
        upstream = connection_cls(self.server.upstream_host, timeout=600)
        started = time.monotonic()
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            content_type = response.getheader("Content-Type", "") or ""
            if "text/event-stream" in content_type:
                self._relay_stream(response, started)
            else:
                self._relay_buffered(response, started)
        except OSError as exc:
            self.send_error(502, f"upstream unreachable: {exc.__class__.__name__}")
        finally:
            upstream.close()

    do_POST = _handle
    do_GET = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def _read_chunked_body(self) -> bytes:
        # The forwarded request carries a plain Content-Length body, so the
        # chunked framing is decoded here rather than passed through.
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                while self.rfile.readline().strip():
                    pass
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def _note_upstream_error(self, status: int) -> None:
        print(
            f"cpt: upstream returned HTTP {status} for {self.command} {self.path}; "
            "not logged",
            file=sys.stderr,
        )

    def _send_response_headers(
        self,
        response: http.client.HTTPResponse,
        content_length: int | None = None,
        close: bool = False,
    ) -> None:
        self.send_response(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() in _HOP_BY_HOP or name.lower() == "content-length":
                continue
            self.send_header(name, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def _relay_buffered(self, response: http.client.HTTPResponse, started: float) -> None:
        payload = response.read()
        latency_ms = int((time.monotonic() - started) * 1000)
        self._send_response_headers(response, content_length=len(payload))
        self.wfile.write(payload)
        if response.status >= 400:
            self._note_upstream_error(response.status)
        else:
            usage = self.server.adapter.parse_json_body(payload)
            if usage:
                self._log(usage, latency_ms)

    def _relay_stream(self, response: http.client.HTTPResponse, started: float) -> None:
        self._send_response_headers(response, close=True)
        collector = self.server.adapter.sse_collector()
        pending = b""
        while True:
            # read1 returns as soon as bytes arrive, so events reach the
            # client without waiting for the full response
            chunk = response.read1(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                collector.feed_line(line.rstrip(b"\r").decode("utf-8", "replace"))
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status >= 400:
            self._note_upstream_error(response.status)
            return
        usage = collector.result()
        if usage:
            self._log(usage, latency_ms)

    def _log(self, usage: ParsedUsage, latency_ms: int) -> None:
        record = StepRecord(
            task_id=self.server.task_id,
            attempt_id=self.server.attempt_id,
            step_id=self.server.next_step_id(),
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            provider=self.server.adapter.name,
            model=usage.model,
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_write_1h_tokens=usage.cache_write_1h_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            output_tokens=usage.output_tokens,
            tool_call_count=len(usage.tool_names),
            tool_names=usage.tool_names,
            latency_ms=latency_ms,
        )
        self.server.writer.append(record)
        self.server.captured_count += 1


def create_proxy(
    *,
    upstream: str,
    log_path: str | Path,
    task_id: str,
    attempt_id: str,
    host: str = "127.0.0.1",
    port: int = 0,
    adapter: ProviderAdapter | None = None,
) -> ProxyServer:
    """Build a proxy server; port 0 picks a free ephemeral port."""
    return ProxyServer((host, port), upstream, log_path, task_id, attempt_id, adapter)
