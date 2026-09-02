"""Local reverse proxy that captures token usage from LLM API responses.

The agent is pointed at this server via ANTHROPIC_BASE_URL and
OPENAI_BASE_URL. One port serves every configured provider: requests are
routed by path (``/v1/messages`` is Anthropic, ``/v1/chat/completions`` and
``/v1/responses`` are OpenAI), falling back to the auth header style. Each
request is forwarded to the real API and the response returned unchanged;
only usage metadata is written to the JSONL log.

Two deliberate changes to requests, both switchable off: streaming OpenAI
Chat Completions calls get ``stream_options.include_usage`` set, since
without it the stream never reports usage; and calls bound for OpenRouter
get ``usage: {include: true}`` so OpenRouter returns native token counts and
the cost it charged. Any OpenAI-compatible gateway can be the ``openai``
upstream; the upstream URL may carry a path prefix (``https://openrouter.ai/api``).

Security invariants:
- request and response bodies are never logged
- headers, including API keys, are never logged
- the log holds only token counts, model, provider, ids, latency and tool names
"""

from __future__ import annotations

import http.client
import itertools
import json
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from .providers.anthropic import AnthropicAdapter
from .providers.base import ParsedUsage, ProviderAdapter
from .providers.openai import OpenAIAdapter
from .schema import JsonlWriter, StepRecord

ADAPTERS: dict[str, ProviderAdapter] = {
    "anthropic": AnthropicAdapter(),
    "openai": OpenAIAdapter(),
}
DEFAULT_UPSTREAMS = {name: adapter.default_upstream for name, adapter in ADAPTERS.items()}

_ANTHROPIC_PATHS = ("/v1/messages",)
_OPENAI_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/completions", "/v1/embeddings")
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


def select_provider(path: str, headers, available: Iterable[str]) -> str | None:
    """Pick the provider for a request; a lone configured provider gets
    everything, so single-provider setups need no routing at all."""
    available = list(available)
    bare = path.split("?", 1)[0]
    if bare.startswith(_ANTHROPIC_PATHS):
        name = "anthropic"
    elif bare.startswith(_OPENAI_PATHS):
        name = "openai"
    elif "anthropic-version" in headers or "x-api-key" in headers:
        name = "anthropic"
    elif "authorization" in headers:
        name = "openai"
    else:
        name = None
    if name in available:
        return name
    if len(available) == 1:
        return available[0]
    return None


def inject_include_usage(path: str, body: bytes | None) -> bytes | None:
    """Add stream_options.include_usage to a streaming Chat Completions request."""
    if not body or not path.split("?", 1)[0].startswith("/v1/chat/completions"):
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or not data.get("stream"):
        return body
    options = data.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage"):
        return body
    data["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
    return json.dumps(data).encode("utf-8")


def inject_openrouter_usage(path: str, body: bytes | None) -> bytes | None:
    """Ask OpenRouter for usage accounting (native token counts plus the
    charged cost) on a Chat Completions request that does not already."""
    if not body or not path.split("?", 1)[0].endswith("/chat/completions"):
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or "usage" in data:
        return body
    data["usage"] = {"include": True}
    return json.dumps(data).encode("utf-8")


def provider_label(adapter_name: str, upstream: SplitResult) -> str:
    """The adapter's name when talking to the vendor itself, otherwise the
    gateway host (``openrouter.ai``), so the log says who billed the call."""
    default_host = urlsplit(ADAPTERS[adapter_name].default_upstream).netloc
    host = upstream.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    # Loopback upstreams are local stand-ins for the vendor (tests, mocks).
    if upstream.netloc == default_host or host in ("127.0.0.1", "localhost", "::1"):
        return adapter_name
    return host[4:] if host.startswith("api.") else host


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstreams: dict[str, str],
        log_path: str | Path,
        task_id: str,
        attempt_id: str,
        task_type: str | None = None,
        inject_usage: bool = True,
        openrouter_usage: bool | None = None,
    ) -> None:
        super().__init__(address, ProxyHandler)
        unknown = set(upstreams) - set(ADAPTERS)
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(sorted(unknown))}")
        self.routes: dict[str, SplitResult] = {
            name: urlsplit(url) for name, url in upstreams.items()
        }
        self.provider_names = {
            name: provider_label(name, split) for name, split in self.routes.items()
        }
        self.writer = JsonlWriter(log_path)
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.task_type = task_type
        self.inject_usage = inject_usage
        # OpenRouter usage accounting: auto-detected from the upstream host
        # unless forced, since OpenAI itself rejects the extra parameter.
        if openrouter_usage is None:
            openai_route = self.routes.get("openai")
            openrouter_usage = bool(openai_route and "openrouter" in openai_route.netloc)
        self.openrouter_usage = openrouter_usage
        self.captured_count = 0
        self._step_counter = itertools.count(1)

    def next_step_id(self) -> int:
        return next(self._step_counter)

    def handle_error(self, request, client_address) -> None:
        # A client dropping its connection (common when an agent aborts or
        # retries) is not worth a traceback on the console.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def log_message(self, format: str, *args) -> None:
        # The default access log prints request lines to stderr; keep quiet.
        pass

    def _handle(self) -> None:
        provider = select_provider(self.path, self.headers, self.server.routes)
        if provider is None:
            self.send_error(404, "cpt: no upstream configured for this request")
            return
        adapter = ADAPTERS[provider]
        upstream_url = self.server.routes[provider]

        length_header = self.headers.get("Content-Length")
        if length_header:
            body = self.rfile.read(int(length_header))
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self._read_chunked_body()
        else:
            body = None
        if provider == "openai" and self.server.inject_usage:
            body = inject_include_usage(self.path, body)
            if self.server.openrouter_usage:
                body = inject_openrouter_usage(self.path, body)

        headers = {}
        for name, value in self.headers.items():
            lname = name.lower()
            # Content-Length is re-derived from the forwarded body.
            if lname in _HOP_BY_HOP or lname in ("accept-encoding", "content-length"):
                continue
            headers[name] = value
        # Identity encoding so the usage block can be parsed in clear text.
        headers["Accept-Encoding"] = "identity"
        headers["Host"] = upstream_url.netloc

        connection_cls = (
            http.client.HTTPSConnection
            if (upstream_url.scheme or "https") == "https"
            else http.client.HTTPConnection
        )
        upstream = connection_cls(upstream_url.netloc, timeout=600)
        upstream_path = upstream_url.path.rstrip("/") + self.path
        provider_name = self.server.provider_names[provider]
        started = time.monotonic()
        try:
            upstream.request(self.command, upstream_path, body=body, headers=headers)
            response = upstream.getresponse()
            content_type = response.getheader("Content-Type", "") or ""
            if "text/event-stream" in content_type:
                self._relay_stream(response, started, adapter, provider_name)
            else:
                self._relay_buffered(response, started, adapter, provider_name)
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

    def _relay_buffered(
        self,
        response: http.client.HTTPResponse,
        started: float,
        adapter: ProviderAdapter,
        provider_name: str,
    ) -> None:
        payload = response.read()
        latency_ms = int((time.monotonic() - started) * 1000)
        # Log before relaying so the record is on disk by the time the
        # client sees the response.
        if response.status >= 400:
            self._note_upstream_error(response.status)
        else:
            usage = adapter.parse_json_body(payload)
            if usage:
                self._log(provider_name, usage, latency_ms)
        self._send_response_headers(response, content_length=len(payload))
        self.wfile.write(payload)

    def _relay_stream(
        self,
        response: http.client.HTTPResponse,
        started: float,
        adapter: ProviderAdapter,
        provider_name: str,
    ) -> None:
        self._send_response_headers(response, close=True)
        collector = adapter.sse_collector()
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
            self._log(provider_name, usage, latency_ms)
        else:
            print(
                f"cpt: stream for {self.command} {self.path} ended without a usage "
                "block; not logged",
                file=sys.stderr,
            )

    def _log(self, provider: str, usage: ParsedUsage, latency_ms: int) -> None:
        record = StepRecord(
            task_id=self.server.task_id,
            attempt_id=self.server.attempt_id,
            step_id=self.server.next_step_id(),
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            provider=provider,
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
            task_type=self.server.task_type,
            reported_cost=usage.reported_cost,
        )
        self.server.writer.append(record)
        self.server.captured_count += 1


def create_proxy(
    *,
    log_path: str | Path,
    task_id: str,
    attempt_id: str,
    upstreams: dict[str, str] | None = None,
    task_type: str | None = None,
    inject_usage: bool = True,
    openrouter_usage: bool | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ProxyServer:
    """Build a proxy server; port 0 picks a free ephemeral port. ``upstreams``
    maps provider name to base URL (scheme, host and an optional path prefix
    such as ``https://openrouter.ai/api``) and defaults to the vendors' own
    APIs for every known provider."""
    return ProxyServer(
        (host, port),
        dict(DEFAULT_UPSTREAMS) if upstreams is None else upstreams,
        log_path,
        task_id,
        attempt_id,
        task_type=task_type,
        inject_usage=inject_usage,
        openrouter_usage=openrouter_usage,
    )
