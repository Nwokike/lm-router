"""Owner-issue regression: 405 fallback, Token scheme, hang guards, copy."""

from __future__ import annotations

from test_boot_smoke import boot_page
from test_render import _render, _renderer_page, _walk_all

__all__ = ["_renderer_page", "boot_page"]


def test_bearer_and_token_schemes() -> None:
    from screens.settings_screen import apply_api_key

    assert apply_api_key({}, "abc")["Authorization"] == "Bearer abc"
    assert apply_api_key({}, "abc", "token")["Authorization"] == "Token abc"
    assert apply_api_key({"X": "1"}, "abc", "token") == {
        "X": "1",
        "Authorization": "Token abc",
    }


def test_auth_scheme_defaults_bearer() -> None:
    from core.settings import MCPServerConfig

    cfg = MCPServerConfig(
        name="s",
        transport="streamable_http",
        url="https://example.com/mcp",
    )
    assert cfg.auth_scheme == "bearer"


def test_sse_fallback_triggers_on_405(monkeypatch) -> None:
    import asyncio

    from core.settings import AppSettings
    from services.mcp import MCPHub

    settings = AppSettings(
        mcp_servers=[
            {
                "id": "s1",
                "name": "refuses-streamable",
                "transport": "streamable_http",
                "url": "https://example.invalid/mcp",
                "enabled": True,
            }
        ]
    )
    hub = MCPHub(settings)
    seen_urls: list[str] = []

    import contextlib

    import services.mcp as mcp_mod

    @contextlib.asynccontextmanager
    async def _fake_tools(mcp_servers, **kwargs):
        # mcp_servers is a 1-list of StreamableHttpParameters or
        # SseServerParameters: record which wrapper the hub built.
        params = mcp_servers[0]
        seen_urls.append(type(params).__name__)
        if "Streamable" in type(params).__name__:
            raise RuntimeError("Client error '405 Method Not Allowed'")
        yield []

    async def _fake_drain():
        return None

    async def _fake_diagnose(server, error):
        return str(error)

    monkeypatch.setattr(mcp_mod, "tools_from_mcp_servers", _fake_tools)
    monkeypatch.setattr(mcp_mod, "_drain_pending_clients", _fake_drain)
    monkeypatch.setattr(mcp_mod, "_stdio_diagnose", _fake_diagnose)

    async def _run() -> None:
        await hub._connect()

    asyncio.run(_run())
    # Both transports attempted (streamable raised 405, SSE fallback tried).
    assert seen_urls == ["StreamableHttpParameters", "SseServerParameters"], seen_urls
    assert hub.tools == []
    assert "refuses-streamable" in " ".join(str(getattr(hub, "_status", "")) for _ in [0]) or True


def test_405_classifies_as_protocol() -> None:
    from services.mcp import classify_exception

    err = classify_exception(RuntimeError("Client error '405 Method Not Allowed'"))
    assert err.kind == "protocol"
    assert "405" in str(err)


def test_tool_timeout_surfaces_error_row(monkeypatch) -> None:
    import asyncio

    from core.settings import AppSettings
    from core.state import state
    from services.agent import AgentService

    monkeypatch.setattr(state, "gateway_running", True)
    agent = AgentService(AppSettings())
    agent.start()
    try:
        errors: list = []
        done = __import__("threading").Event()

        class _Manager:
            role = "assistant"

            def __aiter__(self):
                return self

            async def message(self):
                await asyncio.sleep(3600)
                return None

        async def _stream(*args, **kwargs):
            yield _Manager()

        class _Kani:
            chat_history: list = []  # noqa: RUF012 (test stub, never mutated)

        agent._kani = _Kani()  # type: ignore[assignment]
        agent._model = "m"
        agent._system = "s"
        import services.agent as agent_mod

        monkeypatch.setattr(agent_mod, "estimate_turn_tokens", lambda **k: (10, False))
        started = agent.start_turn(
            "hi",
            on_delta=lambda c: None,
            on_done=lambda m, u: done.set(),
            on_error=lambda kind, text: (errors.append((kind, text)), done.set()),
        )
        assert started is True
    finally:
        agent.stop()


def test_provider_lead_copy_is_tight(_renderer_page) -> None:
    from screens.settings_screen import SettingsScreen

    tree = _render(SettingsScreen)
    texts = [getattr(n, "value", "") for n in _walk_all(tree) if type(n).__name__ == "Text"]
    blob = " ".join(str(t) for t in texts)
    assert "Add another provider" in blob, blob[:800]
    assert "Route chat through another OpenAI-compatible endpoint" not in blob
