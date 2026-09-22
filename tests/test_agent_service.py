"""Agent service: streamed turns over a portal, error mapping, model switching."""

import threading
import time

import httpx
import openai
from kani import ChatMessage

from core.settings import AppSettings
from core.state import state
from services.agent import AgentService


def _stream_body(*chunks: str, usage: dict | None = None) -> bytes:
    lines = []
    for chunk in chunks:
        payload = {"choices": [{"delta": {"content": chunk}}]}
        lines.append("data: " + __import__("json").dumps(payload))
    if usage is not None:
        lines.append("data: " + __import__("json").dumps({"usage": usage}))
    lines.append("data: [DONE]")
    # SSE events are separated by a blank line; the SDK decoder waits for \n\n.
    return "".join(line + "\n\n" for line in lines).encode()


def _agent(monkeypatch, handler) -> AgentService:
    client = openai.AsyncOpenAI(
        base_url="http://gateway.test/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from kani.engines.openai import OpenAIEngine

    def factory(settings: AppSettings, model: str) -> OpenAIEngine:
        return OpenAIEngine(
            client=client,
            model=model,
            api_type="chat_completions",
            max_context_size=4096,
        )

    return AgentService(AppSettings(), engine_factory=factory)


def _boot(monkeypatch, handler) -> AgentService:
    monkeypatch.setattr(state, "gateway_running", True)
    monkeypatch.setattr(state, "gateway_base_url", "http://gateway.test/v1")
    monkeypatch.setattr(state, "model", "mimo-v2.6-flash-free")
    agent = _agent(monkeypatch, handler)
    agent.start()
    agent.ensure_kani("mimo-v2.6-flash-free", "be brief")
    return agent


def test_streamed_turn_delivers_deltas_and_usage(monkeypatch) -> None:
    seen: dict = {"chunks": [], "usage": None, "errors": []}
    done = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.read())
        assert body["stream"] is True
        assert body["model"] == "mimo-v2.6-flash-free"
        return httpx.Response(
            200,
            content=_stream_body(
                "Hel",
                "lo",
                usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            ),
            headers={"content-type": "text/event-stream"},
        )

    agent = _boot(monkeypatch, handler)
    try:
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: seen["chunks"].append(c),
            on_done=lambda msg, usage: (seen.update(usage=usage), done.set()),
            on_error=lambda kind, text: seen["errors"].append((kind, text)),
        )
        assert started is True
        assert done.wait(10), "turn never finished"
        assert seen["errors"] == []
        assert "".join(seen["chunks"]) == "Hello"
        assert seen["usage"]["total_tokens"] == 9
        time.sleep(0.05)
        assert agent.busy is False
    finally:
        agent.stop()


def test_rate_limit_maps_to_friendly_error(monkeypatch) -> None:
    seen: dict = {"errors": []}
    failed = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded. Please try again later."}},
        )

    agent = _boot(monkeypatch, handler)
    try:
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: None,
            on_done=lambda msg, usage: None,
            on_error=lambda kind, text: (seen["errors"].append((kind, text)), failed.set()),
        )
        assert started is True
        assert failed.wait(10)
        assert seen["errors"][0][0] == "rate_limited"
        time.sleep(0.05)
        assert agent.busy is False
    finally:
        agent.stop()


def test_offline_gateway_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(state, "gateway_running", False)
    agent = AgentService(AppSettings())
    agent.start()
    try:
        errors: list = []
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: None,
            on_done=lambda msg, usage: None,
            on_error=lambda kind, text: errors.append((kind, text)),
        )
        assert started is False
        assert errors and errors[0][0] == "offline"
        assert agent.busy is False
    finally:
        agent.stop()


def test_busy_and_stop_are_safe(monkeypatch) -> None:
    agent = _boot(monkeypatch, lambda request: httpx.Response(200, json={}))
    try:
        agent._current = object()  # simulate an in-flight turn
        calls: list = []
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: calls.append("d"),
            on_done=lambda msg, usage: calls.append("done"),
            on_error=lambda kind, text: calls.append("e"),
        )
        assert started is False
        assert calls == []  # silent rejection while busy

        cancelled: dict = {"done": False}

        class _Fut:
            def cancel(self) -> None:
                cancelled["done"] = True

        agent._current = _Fut()
        agent.stop_turn()
        assert cancelled["done"] is True
        assert agent.busy is False
        agent.stop_turn()  # second stop is a no-op
    finally:
        agent._current = None
        agent.stop()


def test_ensure_kani_rebuilds_on_model_change(monkeypatch) -> None:
    agent = _boot(monkeypatch, lambda request: httpx.Response(200, json={}))
    try:
        first = agent.kani
        assert first is not None
        first.chat_history.append(ChatMessage.user("keep me"))
        other = agent.ensure_kani("nemotron-3.5-lightning-free", "be brief")
        assert other is not first
        assert len(other.chat_history) == 1
        assert other.chat_history[0].text == "keep me"
        again = agent.ensure_kani("nemotron-3.5-lightning-free", "be brief")
        assert again is other  # same model+system+base returns cached instance
    finally:
        agent.stop()
