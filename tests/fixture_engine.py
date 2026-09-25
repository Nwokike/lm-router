"""Minimal stand-in for the Kiri Router engine, used by the engine tests.

The real gateway is fetched live from router.kiri.ng and is deliberately NOT
vendored in this repo (it changes constantly). These tests still need a real,
bindable HTTP server that speaks the contract `EngineService` depends on:

  * ``acquire_server(port) -> (server | None, port)``
  * ``discover(force=..., quiet=...)``
  * ``GET /health``          -> {"adapter": "kiri-router", "version": ...}
  * ``GET /account-limits``  -> per-model rate hints
  * ``GET /v1/models``       -> OpenAI-shaped catalog incl. the ``auto`` model
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "9.9.9"
START_TIME = time.time()

_MODELS = [
    {
        "id": "auto",
        "object": "model",
        "created": int(START_TIME),
        "owned_by": "kiri",
        "endpoint_type": "chat.completion",
        "is_free": True,
        "status": "active",
        "latency_ms": None,
        "rate_hint": {
            "tier": "variable",
            "approx_per_hour": None,
            "label": "Free tier, rotates across available models",
        },
    },
    {
        "id": "test-model-free",
        "object": "model",
        "created": int(START_TIME),
        "owned_by": "kiri",
        "endpoint_type": "chat.completion",
        "is_free": True,
        "status": "active",
        "latency_ms": 120,
        "rate_hint": {"tier": "generous", "approx_per_hour": 200, "label": "Free tier"},
    },
]


def discover(force: bool = False, quiet: bool = True) -> dict:
    """The real engine probes upstream here; the fixture is always warm."""
    return {m["id"]: m for m in _MODELS}


def listed_models(refresh: bool = False) -> list[dict]:
    return [dict(m) for m in _MODELS]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(
                {
                    "adapter": "kiri-router",
                    "version": VERSION,
                    "uptime_sec": int(time.time() - START_TIME),
                },
            )
        elif path == "/account-limits":
            models = [m for m in _MODELS if m["id"] != "auto"]
            self._json(
                {
                    "adapter_version": VERSION,
                    "uptime_seconds": int(time.time() - START_TIME),
                    "total_models_available": len(models),
                    "free_models_available": len(models),
                    "free_models": [
                        {
                            "id": m["id"],
                            "endpoint": m["endpoint_type"],
                            "status": m["status"],
                            "latency_ms": m["latency_ms"],
                            "rate_hint": m.get("rate_hint"),
                        }
                        for m in models
                    ],
                    "zero_auth_supported": True,
                },
            )
        elif path in ("/v1/models", "/models"):
            self._json({"object": "list", "data": listed_models()})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args) -> None:  # silence stderr in tests
        return


class _Server(ThreadingHTTPServer):
    # One owner per port: the app relies on a second bind FAILING so it can
    # adopt the running gateway instead of fighting it for the port. (The real
    # router probes by connecting for the same reason — on Windows a bind can
    # "succeed" on an occupied port.)
    allow_reuse_address = False
    daemon_threads = True


def acquire_server(want_port: int):
    """Bind want_port, or report the existing owner when it is taken."""
    try:
        server = _Server(("127.0.0.1", want_port), _Handler)
    except OSError:
        return None, want_port
    return server, server.server_address[1]


if __name__ == "__main__":  # pragma: no cover - mirrors the real engine guard
    srv, port = acquire_server(8082)
    srv.serve_forever()
