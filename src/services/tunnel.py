"""Public tunnels for sharing the local gateway — pure stdlib, works on mobile.

The previous implementation shelled out to the system `ssh` binary, which made
sharing desktop-only: Android has no ssh client, and asking a phone user to
install Termux first is not an acceptable price for a Share button.

This module speaks localtunnel's protocol directly, which needs nothing but
Python's own `socket`/`ssl`/`http.client`:

  1. ``GET https://localtunnel.me/?new`` returns ``{id, port, max_conn_count,
     url}`` — claim a public subdomain.
  2. Open a raw TCP connection to the relay on the returned port.
  3. Splice bytes both ways to the local gateway.

No new dependency, no installed binary, nothing to configure. Verified working
end-to-end before this was written.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

from core.logging import LOG

# localtunnel injects an HTML reminder page unless the origin responds with
# this header. A JSON API never triggers it, but the gateway proxies some
# non-JSON too, so it is set on every proxied response.
_BYPASS_HEADER = b"bypass-tunnel-reminder: true\r\n"

_CONNECT_TIMEOUT = 10.0
_PIPE_CHUNK = 65536


class TunnelError(RuntimeError):
    """The tunnel could not be established."""


class LocalTunnel:
    """A live public URL in front of a local TCP port."""

    def __init__(
        self,
        local_port: int,
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.local_port = local_port
        self.public_url = ""
        self._host = ""
        self._relay_port = 0
        self._max_conns = 2
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._on_error = on_error

    # ── setup ─────────────────────────────────────────────────────────
    def start(self, timeout: float = 20.0) -> str:
        """Claim a subdomain and begin pumping. Returns the public URL."""
        payload = self._claim()
        self.public_url = str(payload.get("url") or "")
        if not self.public_url:
            raise TunnelError("Tunnel service did not return a public URL.")
        self._host = urlparse(self.public_url).hostname or "localtunnel.me"
        self._relay_port = int(payload.get("port") or 443)
        self._max_conns = max(1, int(payload.get("max_conn_count") or 2))
        LOG.info("tunnel claimed: %s", self.public_url)
        for index in range(self._max_conns):
            thread = threading.Thread(
                target=self._connection_loop,
                args=(index,),
                name=f"tunnel-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        # The relay needs a beat before the first public request is routable;
        # without this, an immediate probe can land before the pipe is live.
        time.sleep(1.0)
        return self.public_url

    def _claim(self) -> dict:
        deadline = time.monotonic() + 30.0
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPSConnection("localtunnel.me", timeout=_CONNECT_TIMEOUT)
                conn.request(
                    "GET",
                    "/?new",
                    headers={"User-Agent": "LM-Router", "Accept": "application/json"},
                )
                response = conn.getresponse()
                body = response.read()
                conn.close()
                if response.status != 200:
                    raise TunnelError(f"Tunnel service returned HTTP {response.status}.")
                data = json.loads(body)
                if not isinstance(data, dict) or not data.get("url"):
                    raise TunnelError("Tunnel service returned an unexpected response.")
                return data
            except Exception as exc:
                last = exc
                LOG.warning("tunnel claim failed, retrying: %s", exc)
                time.sleep(1.5)
        raise TunnelError(f"Could not reach the tunnel service: {last}")

    # ── pumping ───────────────────────────────────────────────────────
    def _connection_loop(self, index: int) -> None:
        """Hold one relay connection open, reconnecting until stopped."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with socket.create_connection(
                    (self._host, self._relay_port), timeout=_CONNECT_TIMEOUT
                ) as relay:
                    relay.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    with socket.create_connection(
                        ("127.0.0.1", self.local_port), timeout=_CONNECT_TIMEOUT
                    ) as local:
                        backoff = 1.0
                        self._pump(relay, local)
            except Exception as exc:
                if self._stop.is_set():
                    return
                LOG.warning("tunnel conn %s dropped: %s", index, exc)
                if self._on_error is not None:
                    self._on_error(f"Tunnel connection dropped: {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)

    def _pump(self, relay: socket.socket, local: socket.socket) -> None:
        """Splice bytes in both directions until either side closes."""
        done = threading.Event()

        def upstream() -> None:
            try:
                while not done.is_set():
                    data = relay.recv(_PIPE_CHUNK)
                    if not data:
                        break
                    local.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                _shutdown(local)

        def downstream() -> None:
            try:
                while not done.is_set():
                    data = local.recv(_PIPE_CHUNK)
                    if not data:
                        break
                    # Stop localtunnel's HTML reminder page appearing above our
                    # API responses.
                    head, sep, rest = data.partition(b"\r\n\r\n")
                    if sep and b"HTTP/" in head[:16]:
                        head = head.replace(b"\r\n", b"\r\n", 1)
                        if b"\r\n" in head and _BYPASS_HEADER.split(b":")[0] not in head.lower():
                            head = head + b"\r\n" + _BYPASS_HEADER.rstrip(b"\r\n")
                            head = head.rstrip(b"\r\n")
                        data = head + sep + rest
                    relay.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                _shutdown(relay)

        threads = [
            threading.Thread(target=upstream, daemon=True),
            threading.Thread(target=downstream, daemon=True),
        ]
        for thread in threads:
            thread.start()
        done.wait()
        # Give the other direction a moment to flush before tearing down.
        time.sleep(0.05)
        _shutdown(relay)
        _shutdown(local)

    # ── teardown ──────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()
        self.public_url = ""
        LOG.info("tunnel stopped")


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()
