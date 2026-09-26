"""Model test bench: verdicts, endpoint payloads, the 429 retry, sweeps."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from services import model_bench
from services.http import HttpService


def _service(handler) -> HttpService:
    return HttpService(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("services.model_bench.asyncio.sleep", _fake_sleep)
    return recorded


def test_payloads_follow_the_row_endpoint() -> None:
    path, body = model_bench.build_payload("m1", "chat.completion")
    assert path == "/chat/completions"
    assert body["messages"][0]["content"] == model_bench.PROMPT
    assert body["stream"] is False
    assert body["max_tokens"] == 16

    path, body = model_bench.build_payload("m2", "response")
    assert path == "/responses"
    assert body["input"] == model_bench.PROMPT

    path, body = model_bench.build_payload("m3", "systemone")
    assert path == "/systemone"
    assert body["questions"]["test"]["instructions"] == model_bench.PROMPT

    path, _ = model_bench.build_payload("m4", "")
    assert path == "/chat/completions", "unknown endpoints fall back to chat"


def test_classify_matrix_matches_the_reference() -> None:
    assert model_bench.classify(200, "OK") == "OK"
    assert model_bench.classify(200, "   ") == "EMPTY"
    assert model_bench.classify(429, "") == "RATE"
    assert model_bench.classify(500, "boom") == "FAIL"
    assert model_bench.classify(None, "", transport_error=True) == "FAIL"


def test_text_of_extraction_order() -> None:
    assert model_bench.text_of({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert model_bench.text_of({"output_text": "yo"}) == "yo"
    assert model_bench.text_of({"output_text": ["a", "b"]}) == "ab"
    assert (
        model_bench.text_of({"output": [{"type": "t", "text": "x"}, {"content": [{"text": "y"}]}]})
        == "xy"
    )
    # Unknown-but-real JSON bodies surface as a snippet (the reference's
    # final fallback — the Jev/SystemOne fix); a genuinely empty body is not
    # content, and a non-dict never is.
    assert model_bench.text_of({"weird": 1}) == '{"weird":1}'
    assert model_bench.text_of({}) == ""
    assert model_bench.text_of("not a dict") == ""


@pytest.mark.anyio
async def test_ok_and_empty_verdicts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("model") == "m-empty":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    service = _service(handler)
    ok = await model_bench.test_model(service, "http://gw/v1", "m-ok", "chat.completion")
    assert ok["verdict"] == "OK"
    assert isinstance(ok["ms"], int) and ok["ms"] >= 0
    assert ok["id"] == "m-ok"

    empty = await model_bench.test_model(service, "http://gw/v1", "m-empty", "chat.completion")
    assert empty["verdict"] == "EMPTY", "200 with nothing usable is EMPTY, not OK"


@pytest.mark.anyio
async def test_rate_limit_retries_once_after_four_seconds(sleeps) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    service = _service(handler)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "OK"
    assert calls["n"] == 2, "exactly one retry"
    assert sleeps == [model_bench.RATE_RETRY_DELAY]


@pytest.mark.anyio
async def test_double_rate_limit_is_rate_and_500_is_fail(sleeps) -> None:
    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    service = _service(always_429)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "RATE"
    assert sleeps == [model_bench.RATE_RETRY_DELAY], "only ONE retry even on 429"

    sleeps.clear()

    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    service = _service(always_500)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "FAIL"
    assert sleeps == [], "non-429 statuses are never retried by the bench"


@pytest.mark.anyio
async def test_transport_error_fails_without_retry(sleeps) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    service = _service(handler)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "FAIL"
    assert result["snippet"], "the transport failure must be visible"
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.anyio
async def test_retest_is_serial_progressive_and_stoppable(sleeps) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append(body["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    service = _service(handler)
    rows = [{"id": f"m{i}", "endpoint_type": "chat.completion"} for i in range(4)]

    progress: list[tuple] = []
    results = await model_bench.retest(
        service,
        "http://gw/v1",
        rows,
        on_row=lambda r, done, total: progress.append((r["id"], done, total)),
    )
    assert [r["id"] for r in results] == [r["id"] for r in rows], "serial, in order"
    assert seen == ["m0", "m1", "m2", "m3"], "strictly serial (no concurrency)"
    assert progress[0] == ("m0", 1, 4)
    assert progress[-1] == ("m3", 4, 4)

    # Pre-set stop event: nothing runs.
    stopped = threading.Event()
    stopped.set()
    results = await model_bench.retest(service, "http://gw/v1", rows, stop_event=stopped)
    assert results == []

    # Stop mid-sweep from inside the progress callback.
    stop = threading.Event()

    def _stop_after_two(r, done, total):
        if done == 2:
            stop.set()

    seen.clear()
    results = await model_bench.retest(
        service,
        "http://gw/v1",
        rows,
        on_row=_stop_after_two,
        stop_event=stop,
    )
    assert len(results) == 2, "Stop takes effect after the current probe"


@pytest.mark.anyio
async def test_systemone_shaped_replies_read_as_ok() -> None:
    """The Jev regression: a healthy SystemOne reply matches NONE of the
    chat/responses shapes, so without the reference's JSON fallback it read
    as EMPTY and the row showed 'failed' on a perfect 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(
                200, json={"data": [{"id": "jev-1", "endpoint_type": "systemone"}]}
            )
        return httpx.Response(
            200,
            json={"state": "ok", "questions": {"test": {"answer": "OK"}}},
        )

    service = _service(handler)
    result = await model_bench.test_model(service, "http://gw/v1", "jev-1", "systemone")
    assert result["verdict"] == "OK", result
    assert "questions" in result["snippet"], "the JSON snippet must surface"


@pytest.mark.anyio
async def test_error_body_on_200_is_a_failure() -> None:
    """A 200 carrying {'error': ...} is not a success — the reference would
    stringify it into OK; we report it honestly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "model not found"}})

    service = _service(handler)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "FAIL", result
    assert "model not found" in result["snippet"], result


@pytest.mark.anyio
async def test_known_shape_with_empty_content_stays_empty() -> None:
    """The JSON fallback must not hijack the EMPTY verdict for chat replies
    whose content is present but blank."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    service = _service(handler)
    result = await model_bench.test_model(service, "http://gw/v1", "m", "chat.completion")
    assert result["verdict"] == "EMPTY", result
