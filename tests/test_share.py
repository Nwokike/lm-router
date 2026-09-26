"""Share proxy: optional bearer-key auth in front of the local gateway.

The gateway has no authentication of its own (the router console says
`api_key="any"`), so publishing it without a key would be an open relay. The
proxy passes through when no key is set and enforces the key when it is —
and it STREAMS, so `stream=true` replies reach the client incrementally.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.share import ShareSession, generate_key


class _FakeGateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "auto"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def gateway():
    server, port = _serve(_FakeGateway)
    yield port
    server.shutdown()


def _make_echo(received: list) -> type:
    """A gateway that records what the proxy forwarded and answers 200."""

    class _Echo(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def _respond(self):
            length = int(self.headers.get("content-length") or 0)
            received.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": self.rfile.read(length) if length else b"",
                },
            )
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond
        do_PUT = _respond
        do_PATCH = _respond
        do_DELETE = _respond
        do_OPTIONS = _respond
        do_HEAD = _respond

    return _Echo


@pytest.fixture
def echo_gateway():
    received: list = []
    server, port = _serve(_make_echo(received))
    yield port, received
    server.shutdown()


def _make_slow_stream() -> type:
    class _SlowStream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for payload in (b"data: alpha\n\n", b"data: beta\n\n"):
                self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
                self.wfile.flush()
                time.sleep(0.6)
            self.wfile.write(b"0\r\n\r\n")

    return _SlowStream


def _start(session: ShareSession) -> ShareSession:
    session.proxy_port = 0  # ephemeral: never collide with a parallel test
    assert session.start_proxy()
    return session


def _probe(session: ShareSession, auth: str | None = None, path: str = "/v1/models") -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{session.proxy_port}{path}")
    if auth:
        request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_generated_keys_are_prefixed_and_long():
    key = generate_key()
    assert key.startswith("sk-lm-")
    assert len(key) > 30
    assert key != generate_key()


def test_no_key_passes_everything_through(gateway) -> None:
    session = _start(ShareSession(gateway, key=""))
    try:
        assert _probe(session) == 200
    finally:
        session.stop()


def test_key_rejects_missing_and_wrong_credentials(gateway) -> None:
    session = _start(ShareSession(gateway, key="sk-lm-thekey"))
    try:
        assert _probe(session) == 401
        assert _probe(session, "Bearer sk-lm-wrong") == 401
        assert _probe(session, "sk-lm-thekey") == 401  # not a Bearer header
        assert _probe(session, "Bearer sk-lm-thekey") == 200
    finally:
        session.stop()


def test_non_ascii_bearer_gets_a_clean_401(gateway) -> None:
    """compare_digest raises TypeError on non-ASCII str — that used to drop
    the connection with a traceback instead of answering."""
    session = _start(ShareSession(gateway, key="sk-lm-thekey"))
    try:
        assert _probe(session, "Bearer ünïcode") == 401
    finally:
        session.stop()


def test_response_streams_progressively(gateway) -> None:
    """First SSE chunk must arrive BEFORE the upstream finishes — the old
    proxy buffered response.read() and delivered the whole stream at once."""
    server, port = _serve(_make_slow_stream())
    session = _start(ShareSession(port, key=""))
    try:
        started = time.monotonic()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{session.proxy_port}/v1/chat/completions",
            timeout=10,
        ) as response:
            first = response.read1(65536)
            elapsed = time.monotonic() - started
            assert b"data: alpha" in first, first
            assert b"data: beta" not in first, "first chunk must not contain later data"
            assert elapsed < 0.5, f"first chunk took {elapsed:.2f}s — buffered, not streamed"
            second = response.read1(65536)
            assert b"data: beta" in second, second
    finally:
        session.stop()
        server.shutdown()


def test_chunked_request_body_arrives_intact(echo_gateway) -> None:
    """Clients may frame bodies with Transfer-Encoding: chunked — reading
    only content-length used to forward an empty POST."""
    port, received = echo_gateway
    session = _start(ShareSession(port, key=""))
    try:
        raw = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: share\r\n"
            b"Connection: close\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"6\r\nhello \r\n"
            b"5\r\nworld\r\n"
            b"0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", session.proxy_port), timeout=5) as sock:
            sock.sendall(raw)
            response = sock.makefile("rb").read()
        assert b"200 OK" in response
        assert received and received[0]["body"] == b"hello world", received
    finally:
        session.stop()


def test_every_standard_method_reaches_the_gateway(echo_gateway) -> None:
    port, received = echo_gateway
    session = _start(ShareSession(port, key=""))
    try:
        for method in ("PUT", "PATCH", "OPTIONS"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{session.proxy_port}/v1/files",
                data=b"payload" if method == "PUT" else None,
                method=method,
            )
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                assert response.status == 200, method
        assert [entry["method"] for entry in received] == ["PUT", "PATCH", "OPTIONS"]
    finally:
        session.stop()


def test_share_key_auth_strips_authorization_upstream(echo_gateway) -> None:
    port, received = echo_gateway
    session = _start(ShareSession(port, key="sk-lm-thekey"))
    try:
        assert _probe(session, "Bearer sk-lm-thekey") == 200
        assert received[0]["authorization"] is None, "share key must not leak to the gateway"
    finally:
        session.stop()


def test_auth_off_forwards_a_clients_own_gateway_key(echo_gateway) -> None:
    port, received = echo_gateway
    session = _start(ShareSession(port, key=""))
    try:
        assert _probe(session, "Bearer client-key") == 200
        assert received[0]["authorization"] == "Bearer client-key"
    finally:
        session.stop()
