"""Fresh-boot fixes: seeding on every load path, session retry, tier order."""

from __future__ import annotations


def test_seed_survives_missing_file(tmp_path, monkeypatch) -> None:
    """The owner's second run: storage file deleted -> load() early-returns.
    Builtins must still seed (they did not — zero servers, no MCP, no log)."""
    from core import storage
    from core.settings import AppSettings

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    assert not storage.settings_path().exists(), "precondition: fresh store"
    settings = AppSettings.load()
    builtin = [s for s in settings.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin} == {
        "builtin-exa",
        "builtin-parallel",
    }, f"builtins not seeded on missing file: {settings.mcp_servers}"


def test_seed_survives_corrupt_file(tmp_path, monkeypatch) -> None:
    from core import storage
    from core.settings import AppSettings

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    storage.settings_path().write_text("{corrupt", encoding="utf-8")
    settings = AppSettings.load()
    builtin = [s for s in settings.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin} == {
        "builtin-exa",
        "builtin-parallel",
    }, "builtins not seeded on corrupt file"


def test_seed_survives_non_dict_file(tmp_path, monkeypatch) -> None:
    from core import storage
    from core.settings import AppSettings

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    storage.settings_path().write_text("[1,2,3]", encoding="utf-8")
    settings = AppSettings.load()
    builtin = [s for s in settings.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin} == {
        "builtin-exa",
        "builtin-parallel",
    }, "builtins not seeded on non-dict file"


def test_session_terminated_retries_once(monkeypatch) -> None:
    import asyncio

    from core.settings import AppSettings
    from services.mcp import MCPHub

    settings = AppSettings(
        mcp_servers=[
            {
                "id": "s1",
                "name": "flaky",
                "transport": "streamable_http",
                "url": "https://flaky.test/mcp/",
                "enabled": True,
            }
        ]
    )
    hub = MCPHub(settings)
    attempts: list[str] = []
    import contextlib

    import services.mcp as mcp_mod

    @contextlib.asynccontextmanager
    async def _fake_tools(mcp_servers, **kwargs):
        params = mcp_servers[0]
        attempts.append(type(params).__name__)
        if len(attempts) == 1:
            raise RuntimeError("Session terminated")
        yield []

    async def _fake_drain():
        return None

    monkeypatch.setattr(mcp_mod, "tools_from_mcp_servers", _fake_tools)
    monkeypatch.setattr(mcp_mod, "_drain_pending_clients", _fake_drain)

    async def _run():
        await hub._connect()

    asyncio.run(_run())
    assert attempts == ["StreamableHttpParameters", "StreamableHttpParameters"], attempts
    assert hub.tools == []


def test_chat_models_tier_order_not_alphabetical() -> None:
    from core.catalog import chat_models

    catalog = [
        {
            "id": "aaa-slow-minimal",
            "endpoint_type": "chat.completion",
            "status": "active",
            "rate_hint": {"tier": "minimal"},
        },
        {
            "id": "zzz-fast-high",
            "endpoint_type": "chat.completion",
            "status": "active",
            "latency_ms": 120,
            "rate_hint": {"tier": "high"},
        },
        {
            "id": "mmm-medium",
            "endpoint_type": "chat.completion",
            "status": "active",
            "latency_ms": 100,
            "rate_hint": {"tier": "medium"},
        },
    ]
    ids = [m["id"] for m in chat_models(catalog)]
    assert ids[0] == "zzz-fast-high", f"high tier must lead, got {ids}"
    assert ids[1] == "mmm-medium"
    assert ids[2] == "aaa-slow-minimal"
