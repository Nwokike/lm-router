"""Share this device's gateway with someone else, over a public tunnel.

The gateway itself listens on 127.0.0.1 and has **no authentication** — the
Kiri Router console literally tells users `api_key="any"`. Publishing it as-is
would make an open relay, so this module puts a small stdlib reverse proxy in
front of it:

* auth OFF (default) — requests pass straight through;
* auth ON — a generated `sk-lm-…` key is required as `Authorization: Bearer …`.

The tunnel uses the `ssh` binary that Windows, macOS and Linux all ship, so
this adds no dependency. Android has no ssh binary, so the feature is
desktop-only and says so rather than failing obscurely.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.logging import LOG

# localhost.run maps a remote port back to localhost:<local_port>.
TUNNEL_REMOTE_PORT = 80
DEFAULT_PROXY_PORT = 4096

_URL_RE = re.compile(r"https://[-a-zA-Z0-9]*\.lhr\.[a-z]+")


def generate_key() -> str:
    """A short, unguessable key for the share proxy."""
    return f"sk-lm-{secrets.token_urlsafe(24)}"


def is_ssh_available() -> bool:
    return shutil.which("ssh") is not None


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
        # constant-time-ish comparison; both sides are fixed length after parse
        return bool(token) and secrets.compare_digest(token, self.required_key)

    def _reject(self) -> None:
        body = json.dumps({"error": {"message": "Invalid or missing API key."}}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        # Auth gate FIRST: with a key set, an unauthenticated caller must never
        # reach the gateway.
        if not self._authorised():
            self._reject()
            return
        length = int(self.headers.get("content-length") or 0)
        payload = self.rfile.read(length) if length else None
        # target_base is 127.0.0.1:<gateway port>; the path is the caller's.
        url = f"{self.target_base}{self.path}"
        request = urllib.request.Request(url, data=payload, method=self.command)  # noqa: S310
        for header in ("Content-Type", "Accept", "Authorization"):
            value = self.headers.get(header)
            if value:
                request.add_header(header, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data = response.read()
                self.send_response(response.status)
                self.send_header(
                    "Content-Type", response.headers.get("Content-Type", "application/json")
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            LOG.warning("share proxy forward failed: %s", exc)
            body = json.dumps({"error": {"message": "Gateway unreachable."}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    do_GET = _forward
    do_POST = _forward
    do_DELETE = _forward


class ShareSession:
    """Owns the auth proxy and the ssh tunnel for one share."""

    def __init__(self, gateway_port: int, key: str = "") -> None:
        self.gateway_port = gateway_port
        self.key = key
        self.proxy_port = DEFAULT_PROXY_PORT
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ssh: subprocess.Popen | None = None
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

    # ── tunnel ─────────────────────────────────────────────────────────
    def start_tunnel(self) -> str:
        """Open a localhost.run tunnel. Returns the public URL, or ""."""
        if not is_ssh_available():
            LOG.warning("share: ssh is not installed on this device")
            return ""
        command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ServerAliveInterval=60",
            "-R",
            f"{TUNNEL_REMOTE_PORT}:localhost:{self.proxy_port}",
            "nokey@localhost.run",
        ]
        try:
            self._ssh = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            LOG.warning("share: could not start ssh: %s", exc)
            return ""

        # The remote URL is printed once the tunnel is up.
        for _ in range(40):
            line = self._read_line()
            if not line:
                if self._ssh.poll() is not None:
                    LOG.warning("share: ssh exited before assigning a URL")
                    return ""
                continue
            match = _URL_RE.search(line)
            if match:
                self.public_url = match.group(0)
                LOG.info("share tunnel live at %s", self.public_url)
                return self.public_url
        return ""

    def _read_line(self) -> str:
        if self._ssh is None or self._ssh.stdout is None:
            return ""
        try:
            return self._ssh.stdout.readline() or ""
        except OSError, ValueError:
            return ""

    # ── lifecycle ──────────────────────────────────────────────────────
    def stop(self) -> None:
        if self._ssh is not None:
            self._ssh.terminate()
            try:
                self._ssh.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._ssh.kill()
            self._ssh = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None
        self.public_url = ""
        LOG.info("share session stopped")

    @property
    def running(self) -> bool:
        return self._server is not None
