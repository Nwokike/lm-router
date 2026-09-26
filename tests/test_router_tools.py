"""Live router chat tools: status, catalog order/vocabulary, single probe."""

from __future__ import annotations

import json

import httpx
import pytest

from services import router_tools
from services.http import HttpService


def _http(handler) -> HttpService:
    return HttpService(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.anyio
async def test_gateway_status_reports_live_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/health"):
            return httpx.Response(
                200,
                json={"status": "ok", "adapter": "kiri-router", "uptime_sec": 5412},
            )
        return httpx.Response(
            200,
            json={
                "models": {"total": 38, "active": 26, "degraded": 7, "failed": 5},
                "sources": {"responsive": 4, "total": 4},
                "last_discovery_sec": 42,
            },
        )

    out = await router_tools.gateway_status(_http(handler))
    assert "running" in out and "uptime" in out and "1h 30m" in out, out
    assert "38 total" in out and "26 active" in out, out
    assert "capped or slow" in out and "5 failed" in out, out
    assert "4/4 responsive" in out and "42s ago" in out, out
    # Only counts: never a model id or source name (invariant 13).
    assert "auto" not in out


@pytest.mark.anyio
async def test_gateway_status_offline_is_actionable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    out = await router_tools.gateway_status(_http(handler))
    assert "Start gateway" in out, out


@pytest.mark.anyio
async def test_list_models_keeps_server_order_and_translates_status() -> None:
    rows = [
        {"id": "auto", "status": "active"},
        {"id": "alpha", "status": "untested", "latency_ms": None},
        {"id": "beta", "status": "slow", "latency_ms": 9000},
        {"id": "gamma", "status": "failed"},
        {"id": "ok", "status": "active", "latency_ms": 120},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": rows})

    out = await router_tools.list_models(_http(handler))
    lines = out.splitlines()
    assert lines[0].startswith("5 models (server order)"), lines[0]
    assert lines[1] == "auto: active", lines[1]
    assert lines[2] == "alpha: rate limited", "untested renders as rate limited"
    assert lines[3] == "beta: slow, 9000ms", lines[3]
    assert lines[4] == "gamma: failed", lines[4]
    assert lines[5] == "ok: active, 120ms", lines[5]

    # Filter matches the DISPLAY word…
    out = await router_tools.list_models(_http(handler), "rate limited")
    assert "alpha" in out and "beta" not in out and "gamma" not in out, out
    # …and the raw word.
    out = await router_tools.list_models(_http(handler), "untested")
    assert "alpha" in out and "gamma" not in out, out
    # No match: honest empty.
    out = await router_tools.list_models(_http(handler), "xyz")
    assert out == "No models matched that filter.", out


@pytest.mark.anyio
async def test_probe_model_reports_verdicts(monkeypatch) -> None:
    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("services.model_bench.asyncio.sleep", _fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "alpha", "endpoint_type": "chat.completion"}]},
            )
        body = json.loads(request.content or b"{}")
        if body.get("model") == "alpha-limited":
            return httpx.Response(429, json={"error": "rate limited"})
        if body.get("model") not in ("alpha", "alpha-limited"):
            # A genuinely bad id fails at the gateway, honestly.
            return httpx.Response(404, json={"error": "unknown model"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    service = _http(handler)

    out = await router_tools.probe_model(service, "alpha")
    assert "OK in" in out and out.startswith("alpha:"), out

    out = await router_tools.probe_model(service, "alpha-limited")
    assert "rate limited" in out, out

    out = await router_tools.probe_model(service, "nope")
    assert out.startswith("nope:") and "failed" in out, out
