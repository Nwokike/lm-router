"""Share proxy: optional bearer-key auth in front of the local gateway.

The gateway has no authentication of its own (the router console says
`api_key="any"`), so publishing it without a key would be an open relay. The
proxy passes through when no key is set and enforces the key when it is.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.share import ShareSession, generate_key, is_ssh_available


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


@pytest.fixture
def gateway():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield port
    server.shutdown()


def _probe(session: ShareSession, auth: str | None = None) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{session.proxy_port}/v1/models")
    if auth:
        request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _free_proxy_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_generated_keys_are_prefixed_and_long():
    key = generate_key()
    assert key.startswith("sk-lm-")
    assert len(key) > 30
    assert key != generate_key()


def test_no_key_passes_everything_through(gateway) -> None:
    session = ShareSession(gateway, key="")
    session.proxy_port = _free_proxy_port()
    assert session.start_proxy()
    try:
        assert _probe(session) == 200
    finally:
        session.stop()


def test_key_rejects_missing_and_wrong_credentials(gateway) -> None:
    session = ShareSession(gateway, key="sk-lm-thekey")
    session.proxy_port = _free_proxy_port()
    assert session.start_proxy()
    try:
        assert _probe(session) == 401
        assert _probe(session, "Bearer sk-lm-wrong") == 401
        assert _probe(session, "sk-lm-thekey") == 401  # not a Bearer header
        assert _probe(session, "Bearer sk-lm-thekey") == 200
    finally:
        session.stop()


def test_tunnel_reports_absence_of_ssh_instead_of_hanging(monkeypatch, gateway) -> None:
    """A device without ssh must say so, not block forever."""
    session = ShareSession(gateway)
    session.proxy_port = _free_proxy_port()
    monkeypatch.setattr("services.share.is_ssh_available", lambda: False)
    assert session.start_tunnel() == ""


def test_tunnel_availability_is_a_plain_path_check() -> None:
    assert isinstance(is_ssh_available(), bool)
