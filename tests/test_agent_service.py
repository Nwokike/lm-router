"""Agent service: streamed turns over a portal, error mapping, model switching."""

import threading
import time

import httpx
import openai
import pytest
from kani import ChatMessage

from core.settings import AppSettings
from core.state import state
from services.agent import AgentService, _usage_from


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
        # Budget is deliberately tight: kani's lazy tokenizer property used to
        # download BPE ranks with no timeout inside the first turn, stalling it
        # for 40-60s. The engine now receives the app's pre-resolved encoding,
        # so a turn must not wait on the network. A regression here is a hang.
        assert done.wait(20), f"turn stalled (network fetch?); errors={seen['errors']}"
        assert seen["errors"] == []
        assert "".join(seen["chunks"]) == "Hello"
        assert seen["usage"]["total_tokens"] == 9
        # `busy` is cleared by the on_settled hook, which runs *after* the done
        # callback fires. A fixed sleep made this flake under load; poll for
        # the real condition instead.
        deadline = time.monotonic() + 10.0
        while agent.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        assert agent.busy is False
    finally:
        agent.stop()


def test_failed_mcp_connect_does_not_poison_the_agent(monkeypatch) -> None:
    """Regression: an enabled but unreachable MCP server used to close its
    anyio cancel scope from a different portal task, which unwound the
    portal's task group permanently — every later chat send then died with
    'This portal is not running' and busy stayed True forever (the owner
    task now enters AND exits the context in the same task)."""

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    agent = _boot(monkeypatch, boom)
    try:
        from core.settings import AppSettings as _S
        from services.mcp import MCPHub

        settings = _S(
            mcp_servers=[
                {
                    "id": "bad",
                    "name": "unreachable",
                    "transport": "streamable_http",
                    # reserved TEST-NET-1 address: blackholed, connect times out
                    "url": "http://10.255.255.1:9/mcp",
                    "enabled": True,
                },
            ],
        )
        hub = MCPHub(settings)
        agent.spawn(hub.serve)
        # let the owner task attempt the doomed connection
        deadline = time.monotonic() + 25
        while hub.generation == 0 and time.monotonic() < deadline:
            time.sleep(0.25)
        assert hub.generation >= 1, "owner task never attempted the connection"
        assert hub.tools == []

        # The portal must still be alive and dispatchable after the failure.
        async def _noop() -> int:
            return 7

        assert agent.call(_noop) == 7

        done = threading.Event()
        seen: list = []
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: seen.append(c),
            on_done=lambda msg, usage: done.set(),
            on_error=lambda kind, text: (seen.append(kind), done.set()),
        )
        assert started is True, "send refused after a failed MCP connect"
        assert done.wait(15), "turn did not finish after a failed MCP connect"
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


def test_usage_from_extracts_reasoning_cached_and_finish_reason() -> None:
    msg = ChatMessage.assistant("Response with details")
    msg.extra["openai_usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 8},
        "prompt_tokens_details": {"cached_tokens": 5},
    }
    msg.extra["openai_completion"] = {"choices": [{"finish_reason": "length"}]}

    usage = _usage_from(msg)
    assert usage is not None
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 30
    assert usage["reasoning_tokens"] == 8
    assert usage["cached_tokens"] == 5
    assert usage["finish_reason"] == "length"


@pytest.mark.parametrize(
    ("status_code", "err_type", "expected_kind"),
    [
        (400, "invalid_request_error", "invalid_request"),
        (401, "authentication_error", "auth"),
        (403, "permission_denied_error", "forbidden"),
        (404, "not_found_error", "model"),
        (500, "internal_server_error", "upstream"),
    ],
)
def test_expanded_error_mappings(
    monkeypatch,
    status_code: int,
    err_type: str,
    expected_kind: str,
) -> None:
    seen: dict = {"errors": []}
    failed = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"type": err_type, "message": f"Error with {err_type}"}},
        )

    agent = _boot(monkeypatch, handler)
    try:
        started = agent.start_turn(
            "test error",
            on_delta=lambda c: None,
            on_done=lambda msg, usage: None,
            on_error=lambda kind, text: (seen["errors"].append((kind, text)), failed.set()),
        )
        assert started is True
        assert failed.wait(10)
        assert seen["errors"][0][0] == expected_kind
    finally:
        agent.stop()


def test_context_budget_preflight_truncates_oversized_history(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
        return httpx.Response(
            200,
            content=_stream_body("ok", usage=usage),
            headers={"content-type": "text/event-stream"},
        )

    # Set a tight max_context_tokens setting so truncation triggers
    settings = AppSettings(max_context_tokens=1024)
    client = openai.AsyncOpenAI(
        base_url="http://gateway.test/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from kani.engines.openai import OpenAIEngine

    agent = AgentService(
        settings,
        engine_factory=lambda s, m: OpenAIEngine(client=client, model=m, max_context_size=1024),
    )
    monkeypatch.setattr(state, "gateway_running", True)
    monkeypatch.setattr(state, "gateway_base_url", "http://gateway.test/v1")
    monkeypatch.setattr(state, "model", "test-model")
    agent.start()
    agent.ensure_kani("test-model", "system prompt")

    try:
        kani = agent.kani
        assert kani is not None
        # Populate history with large messages exceeding 1024 budget
        for i in range(15):
            kani.chat_history.append(ChatMessage.user(f"Long history query {i} " * 20))
            kani.chat_history.append(ChatMessage.assistant(f"Long history response {i} " * 20))

        initial_len = len(kani.chat_history)
        done = threading.Event()
        started = agent.start_turn(
            "Next short query",
            on_delta=lambda c: None,
            on_done=lambda msg, usage: done.set(),
            on_error=lambda kind, text: None,
        )
        assert started is True
        assert done.wait(10)
        # History was truncated down to fit within budget
        assert len(kani.chat_history) < initial_len
    finally:
        agent.stop()
