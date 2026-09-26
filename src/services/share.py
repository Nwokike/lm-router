"""Share this device's gateway with someone else, over a public tunnel.

The gateway itself listens on 127.0.0.1 and has **no authentication** — the
Kiri Router console literally tells users `api_key="any"`. Publishing it as-is
would make an open relay, so this module puts a small stdlib reverse proxy in
front of it:

* auth OFF (default) — requests pass straight through;
* auth ON — a generated `sk-lm-…` key is required as `Authorization: Bearer …`.

The proxy STREAMS: request bodies (Content-Length or chunked) and response
bodies are relayed chunk-by-chunk as they arrive, so `stream=true`
completions reach the client incrementally. The public URL itself comes from
`tunnel.py` (localtunnel's protocol, pure stdlib — works on Android); this
module only owns the proxy.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.logging import LOG

DEFAULT_PROXY_PORT = 4096

# Hop-by-hop headers (RFC 9110): relaying them lies to the other side about
# how THIS connection is framed.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    },
)
_CHUNK = 8 * 1024


def generate_key() -> str:
    """A short, unguessable key for the share proxy."""
    return f"sk-lm-{secrets.token_urlsafe(24)}"


class _ProxyHandler(BaseHTTPRequestHandler):
    """Forwards to the local gateway, optionally requiring a bearer key."""

    protocol_version = "HTTP/1.1"
    target_base: str = ""
    required_key: str = ""
    verbose: bool = False

    def log_message(self, *args) -> None:  # quiet; our ring logger has the detail
        return

    def _authorised(self) -> bool:
        if not self.required_key:
            return True
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token:
            return False
        # Bytes: compare_digest raises TypeError on non-ASCII str, which used
        # to drop the connection with a traceback instead of answering 401.
        return secrets.compare_digest(token.encode("utf-8"), self.required_key.encode("utf-8"))

    def _reject(self) -> None:
        body = json.dumps({"error": {"message": "Invalid or missing API key."}}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_bad_body(self) -> None:
        body = json.dumps({"error": {"message": "Malformed request body."}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        """Request body, handling both framings clients actually use.

        OpenAI SDKs and curl send Content-Length; large or streaming uploads
        may arrive chunked — reading only content-length used to forward an
        empty body for the latter.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line.split(b";", 1)[0], 16)
                if size == 0:
                    while self.rfile.readline().strip():  # trailers
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # chunk CRLF
            return b"".join(chunks) or None
        length = int(self.headers.get("content-length") or 0)
        if length:
            return self.rfile.read(length)
        return None

    def _relay(self, response) -> None:
        """Stream the upstream response to the client as it arrives."""
        content_length = response.headers.get("Content-Length")
        self.send_response(response.status)
        for name, value in response.headers.items():
            lower = name.lower()
            if lower in _HOP_BY_HOP or lower in ("content-length", "transfer-encoding"):
                continue
            self.send_header(name, value)
        if self.command == "HEAD":
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            self.end_headers()
            return
        try:
            if content_length is not None and content_length.isdigit():
                self.send_header("Content-Length", content_length)
                self.end_headers()
                remaining = int(content_length)
                while remaining > 0:
                    # read1: at most one underlying read — read() would block
                    # until the full buffer fills and defeat streaming.
                    chunk = response.read1(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                return
            # Unknown length (SSE / chunked upstream): urllib already
            # de-chunks, so re-frame as chunked and ship each piece now.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = response.read1(_CHUNK)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Headers are already out: a vanished client (or a gateway that
            # stopped mid-stream) can only be logged, never answered.
            LOG.info("share proxy stream ended early: %s", exc)
            self.close_connection = True

    def _forward(self) -> None:
        # Auth gate FIRST: with a key set, an unauthenticated caller must never
        # reach the gateway.
        if not self._authorised():
            self._reject()
            return
        try:
            payload = self._read_body()
        except (ValueError, OSError) as exc:
            LOG.info("share proxy bad request body: %s", exc)
            self._reject_bad_body()
            return
        # target_base is 127.0.0.1:<gateway port>; the path is the caller's.
        url = f"{self.target_base}{self.path}"
        request = urllib.request.Request(url, data=payload, method=self.command)  # noqa: S310
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in _HOP_BY_HOP:
                continue
            if lower in ("content-length", "transfer-encoding"):
                continue  # urllib re-frames the body we already read
            if lower == "authorization" and self.required_key:
                # The inbound bearer is OUR share key; never leak it upstream.
                # (Auth OFF keeps a client's own gateway key in place.)
                continue
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                self._relay(response)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            for name, value in exc.headers.items():
                if name.lower() in _HOP_BY_HOP or name.lower() in (
                    "content-length",
                    "transfer-encoding",
                ):
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception as exc:
            LOG.warning("share proxy forward failed: %s", exc)
            with contextlib.suppress(OSError):
                body = json.dumps({"error": {"message": "Gateway unreachable."}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward


class ShareSession:
    """Owns the auth proxy for one share (the tunnel lives in tunnel.py)."""

    def __init__(self, gateway_port: int, key: str = "") -> None:
        self.gateway_port = gateway_port
        self.key = key
        self.proxy_port = DEFAULT_PROXY_PORT
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.public_url = ""

    # ── proxy ──────────────────────────────────────────────────────────
    def start_proxy(self) -> bool:
        if self._server is not None:
            return True
        handler = type(
            "_BoundHandler",
            (_ProxyHandler,),
            {
                "target_base": f"http://127.0.0.1:{self.gateway_port}",
                "required_key": self.key,
            },
        )
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.proxy_port), handler)
        except OSError as exc:
            LOG.warning("share proxy could not bind port %s: %s", self.proxy_port, exc)
            return False
        server.daemon_threads = True
        self._server = server
        self.proxy_port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever, name="share-proxy", daemon=True
        )
        self._thread.start()
        LOG.info("share proxy listening on 127.0.0.1:%s (auth=%s)", self.proxy_port, bool(self.key))
        return True

    # ── lifecycle ──────────────────────────────────────────────────────
    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None
        self.public_url = ""
        LOG.info("share session stopped")
