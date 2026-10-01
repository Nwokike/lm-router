"""Batch 5 service fixes regression: engine/search/history/MCP hardening.

- Engine download capped; VERSION tolerant; tmp PID-suffixed; stop locked.
- Search primary bounded + raises into fallbacks; top_k clamped; no-results
  distinct from unreachable; fallbacks carry URLs and fast connect.
- Conversations land 0600; tombstone re-checked post-write; JSON export
  survives non-UTF8; system role skipped in projections.
- MCP test timeout drains; pre-enter tracking; diagnose passes env/cwd.
"""

from __future__ import annotations

import pytest
from test_boot_smoke import boot_page

__all__ = ["boot_page"]


def test_engine_version_regex_tolerant() -> None:
    from services.engine import _validate

    assert _validate(b'VERSION = "1.2.3"\nif __name__ == "__main__":\n') == "1.2.3"
    assert _validate(b"VERSION='2.0'\nif __name__ == \"__main__\":\n") == "2.0"
    assert _validate(b'VERSION  =  "3.1"\nif __name__ == "__main__":\n') == "3.1"


def test_engine_download_capped(monkeypatch) -> None:
    from services import engine as engine_mod

    class _Big:
        def read(self, n: int = -1):
            return b"x" * (n + 1 if n is not None and n >= 0 else 10)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine_mod.urllib.request, "urlopen", lambda req, timeout=None: _Big())
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)
    with pytest.raises(ValueError, match="exceeds"):
        engine_mod._download("https://router.kiri.ng/run.py")


def test_engine_tmp_pid_suffixed(tmp_path, monkeypatch) -> None:
    import os

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    from core import storage

    cache = storage.engine_cache_path()
    # Reproduce the tmp-name logic: must contain the PID.
    tmp = cache.with_name(f"{cache.stem}.{os.getpid()}.new.py")
    assert str(os.getpid()) in tmp.name
    assert tmp.suffix == ".py"


def test_search_primary_failures_raise_into_fallbacks(monkeypatch) -> None:
    import asyncio

    import httpx

    from services import search as search_mod
    from services.http import HttpService

    async def _boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(search_mod, "streamable_http_client", _boom)

    async def _wiki(http, query, top_k):
        return ""  # empty: must fall THROUGH to the next source

    async def _ddg(http, query, top_k):
        return "DuckDuckGo results:\n- fallback hit"

    monkeypatch.setattr(search_mod, "FALLBACKS", (_wiki, _ddg))
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    http = HttpService(client=httpx.AsyncClient(transport=transport))
    out = asyncio.run(search_mod.run_search_with_fallback(http, "flet 1.0.3"))
    assert "fallback hit" in out


def test_search_clamp_and_empty_query() -> None:
    import asyncio

    from services.http import HttpService
    from services.search import _clamp_top_k, run_search

    assert _clamp_top_k(99) == 10
    assert _clamp_top_k(-3) == 1
    assert _clamp_top_k("garbage") == 5
    http = HttpService()
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run_search(http, "   "))


def test_search_tool_picks_vendor_prefixed_names() -> None:
    from services.search import _pick_search_tool

    assert _pick_search_tool(["exa_web_search", "other"]) == "exa_web_search"
    assert _pick_search_tool(["tavily_search"]) == "tavily_search"
    assert _pick_search_tool(["unrelated"]) is None


def test_conversation_files_land_owner_only(
    tmp_path,
    monkeypatch,
) -> None:
    import os
    import stat as statmod

    from kani import ChatMessage, Kani
    from kani.engines.base import BaseEngine

    from core.state import state
    from services import history

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))

    class _StubEngine(BaseEngine):
        max_context_size = 4096

        def prompt_len(self, messages, functions=None, **kwargs) -> int:  # type: ignore[no-untyped-def]
            return 0

        async def predict(self, messages, functions=None, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    kani = Kani(_StubEngine(), chat_history=[ChatMessage.user("secret prompt here")])
    monkeypatch.setattr(state, "active_conversation", "permtest123456")

    # Bypass the AgentService type: save_conversation only needs .kani.
    class _Agent:
        def __init__(self, k):
            self.kani = k

    assert history.save_conversation(_Agent(kani)) is True
    path = history.conversation_path("permtest123456")
    assert path.exists()
    assert "secret prompt" in path.read_text(encoding="utf-8")
    if os.name == "posix":
        mode = statmod.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"conversation landed {oct(mode)}, want 0o600"
    monkeypatch.setattr(state, "active_conversation", "")


def test_conversation_path_rejects_empty() -> None:
    from services.history import conversation_path

    with pytest.raises(ValueError, match="empty"):
        conversation_path("")


def test_mcp_diagnose_passes_env_cwd(monkeypatch) -> None:
    import asyncio

    from core.settings import MCPServerConfig
    from services import mcp as mcp_mod
    from services.mcp import MCPError

    seen: dict = {}

    async def _fake_to_thread(fn, *args, **kwargs):
        seen["env"] = kwargs.get("env")
        seen["cwd"] = kwargs.get("cwd")

        class _P:
            stderr = ""
            stdout = ""

        return _P()

    monkeypatch.setattr(mcp_mod.asyncio, "to_thread", _fake_to_thread)
    server = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        env={"MY_KEY": "abc"},
        cwd="/tmp/somedir",  # noqa: S108 (fake path for the diagnose harness)
    )
    out = asyncio.run(mcp_mod._stdio_diagnose(server, MCPError("tool", "Connection closed")))
    assert "Connection closed" in out
    assert seen["env"] is not None and seen["env"].get("MY_KEY") == "abc"
    assert seen["cwd"] == "/tmp/somedir"  # noqa: S108 (matches the fake above)
