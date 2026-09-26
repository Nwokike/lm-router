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
_MAX_HEAD_BYTES = 65536


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
        # Live relay sockets, tracked so stop() can unblock a recv/connect
        # immediately instead of waiting out the 10s socket timeout.
        self._socks: set[socket.socket] = set()
        self._socks_lock = threading.Lock()
        # Debounce: a flapping relay rewrote the red share banner every
        # backoff tick (15s, forever). Report on change, else at most 60s.
        self._last_error_key = ""
        self._last_error_at = 0.0

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
                with contextlib.closing(conn):
                    conn.request(
                        "GET",
                        "/?new",
                        headers={"User-Agent": "LM-Router", "Accept": "application/json"},
                    )
                    response = conn.getresponse()
                    body = response.read()
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
                # socket context managers close both ends on exit; tracking
                # exists so stop() can SHUTDOWN a parked recv immediately.
                # Nested try/finally: 'local' must never be referenced when
                # its connect raised (the old layout logged a NameError as a
                # dropped connection and leaked the relay from the set).
                with socket.create_connection(
                    (self._host, self._relay_port), timeout=_CONNECT_TIMEOUT
                ) as relay:
                    relay.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    self._track(relay)
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", self.local_port), timeout=_CONNECT_TIMEOUT
                        ) as local:
                            self._track(local)
                            try:
                                backoff = 1.0
                                self._pump(relay, local)
                            finally:
                                self._untrack(local)
                    finally:
                        self._untrack(relay)
            except Exception as exc:
                if self._stop.is_set():
                    return
                LOG.warning("tunnel conn %s dropped: %s", index, exc)
                if self._on_error is not None:
                    now = time.monotonic()
                    key = str(exc)
                    if key != self._last_error_key or now - self._last_error_at >= 60.0:
                        self._last_error_key = key
                        self._last_error_at = now
                        self._on_error(f"Tunnel connection dropped: {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)

    def _track(self, sock: socket.socket) -> None:
        with self._socks_lock:
            self._socks.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._socks_lock:
            self._socks.discard(sock)

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

        pending = b""
        head_done = False

        def downstream() -> None:
            nonlocal pending, head_done
            try:
                while not done.is_set():
                    data = local.recv(_PIPE_CHUNK)
                    if not data:
                        break
                    if head_done:
                        relay.sendall(data)
                        continue
                    # Buffer until the header terminator arrives: the old
                    # single-recv partition missed any response whose headers
                    # spanned two TCP chunks, and the bypass header was never
                    # injected (the reminder page came back).
                    pending += data
                    head, sep, rest = pending.partition(b"\r\n\r\n")
                    if not sep:
                        if len(pending) <= _MAX_HEAD_BYTES:
                            continue
                        # Pathological (no header end): pass through as-is.
                        relay.sendall(pending)
                        pending = b""
                        head_done = True
                        continue
                    relay.sendall(_augment_head(head, sep) + rest)
                    pending = b""
                    head_done = True
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
        # Unblock every parked recv/connect NOW — with only the event set, a
        # live pump kept splicing for up to the 10s socket timeout after a
        # stop->start cycle, and old loops could feed the new session.
        with self._socks_lock:
            socks = list(self._socks)
            self._socks.clear()
        for sock in socks:
            _shutdown(sock)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        self.public_url = ""
        LOG.info("tunnel stopped")


def _augment_head(head: bytes, sep: bytes) -> bytes:
    """Insert localtunnel's bypass header before the blank line (once).

    Pure so it is testable: non-HTTP heads and heads that already carry the
    header pass through byte-identical.
    """
    if b"HTTP/" not in head[:16] or b"bypass-tunnel-reminder" in head.lower():
        return head + sep
    return head + b"\r\n" + _BYPASS_HEADER.rstrip(b"\r\n") + sep


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
