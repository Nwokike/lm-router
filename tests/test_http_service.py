"""HttpService retry semantics: idempotent retries, POST protection, house backoff.

The module shipped untested — which is how a 3-of-12 transport-error catch
and an off-by-one backoff schedule (the final 1.0s step was dead code) both
survived to production.
"""

from __future__ import annotations

import httpx
import pytest

from services.http import BACKOFF, HttpService


def _service(handler) -> HttpService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpService(client=client)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record (and skip) every backoff sleep inside services.http."""
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("services.http.asyncio.sleep", _fake_sleep)
    return recorded


@pytest.mark.anyio
async def test_get_retries_transport_errors_then_succeeds(sleeps) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"ok": True})

    service = _service(handler)
    try:
        response = await service.get("http://gateway.test/v1/models")
        assert response.status_code == 200
        assert calls["n"] == 3
        assert sleeps == [BACKOFF[0], BACKOFF[1]], sleeps
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_exhausted_get_returns_final_response_with_full_backoff(sleeps) -> None:
    """The documented schedule is (0.2, 0.5, 1.0): the old loop never
    applied the final step (off-by-one guard)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "busy"})

    service = _service(handler)
    try:
        response = await service.get("http://gateway.test/v1/models")
        assert response.status_code == 503
        assert sleeps == list(BACKOFF), sleeps
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_post_is_never_replayed(sleeps) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    service = _service(handler)
    try:
        with pytest.raises(httpx.ConnectError):
            await service.post("http://gateway.test/v1/chat", json={})
        assert calls["n"] == 1, "a POST must never be replayed"
        assert sleeps == [], "no retry attempts means no backoff"
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_every_transport_error_class_is_retried(sleeps) -> None:
    """RemoteProtocolError escaped the old 3-type catch with ZERO retries —
    half-dead sockets are exactly the mobile-transient failure."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("incomplete", request=request)
        return httpx.Response(200, json={"ok": True})

    service = _service(handler)
    try:
        response = await service.get("http://gateway.test/v1/models")
        assert response.status_code == 200
        assert calls["n"] == 2
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_client_follows_redirects() -> None:
    service = HttpService()
    try:
        assert service.client.follow_redirects is True, (
            "Wikipedia/DDG fallbacks die on a 301 without this"
        )
        await service.aclose()
    finally:
        pass
