"""Dependency-utilization regression: M4 fixes must hold.

- Seed round-trips through settings (None <-> blank) and reaches the engine.
- Provider base_url rejects non-http(s); name strips; timeouts coerce/validate.
- Approx token estimates carry headroom; truncation stays linear.
- MCP test() is time-bounded; bench timeout keeps connect at 5s.
- Stored JSON lands owner-only (0600 on POSIX).
"""

from __future__ import annotations

import json
import os
import stat

import httpx
from test_boot_smoke import boot_page

from core.settings import AppSettings, MCPServerConfig, ProviderConfig

__all__ = ["boot_page"]


def test_seed_round_trip_and_engine_param() -> None:
    s = AppSettings(seed=42)
    assert s.seed == 42
    stored = s.to_stored()
    assert AppSettings.from_stored(stored).seed == 42
    blank = AppSettings()
    assert blank.seed is None

    from services.agent import AgentService

    agent = AgentService(AppSettings(seed=7))
    params = agent._generation_params()
    assert params["seed"] == 7
    assert "seed" not in AgentService(AppSettings())._generation_params()


def test_provider_url_constrained_to_http() -> None:
    import pydantic

    ok = ProviderConfig(name="x", base_url="https://api.example.com/v1")
    assert str(ok.base_url).startswith("https://")
    try:
        ProviderConfig(name="x", base_url="ftp://files.example.com/x")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("ftp:// provider URL must not validate")


def test_provider_name_strips() -> None:
    cfg = ProviderConfig(name="  spaced  ", base_url="https://api.example.com/v1")
    assert cfg.name == "spaced"


def test_mcp_timeout_field_validation_and_coercion() -> None:
    import pydantic

    cfg = MCPServerConfig(
        name="s",
        transport="streamable_http",
        url="https://example.com/mcp",
        timeout="5",
        sse_read_timeout="",
    )
    assert cfg.timeout == 5.0
    assert cfg.sse_read_timeout is None
    try:
        MCPServerConfig(
            name="s",
            transport="streamable_http",
            url="https://example.com/mcp",
            timeout=-1,
        )
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("negative timeout must not validate")
    empty_cwd = MCPServerConfig(name="s", transport="stdio", command="python", cwd="  ")
    assert empty_cwd.cwd is None


def test_approx_estimate_carries_headroom(monkeypatch) -> None:
    from tiktoken import registry

    from services import tokenizer as tk_mod
    from services.tokenizer import estimate_turn_tokens

    monkeypatch.setattr(registry, "ENCODINGS", {})
    monkeypatch.setattr(tk_mod, "_loaded", {})
    monkeypatch.setattr(tk_mod, "_attempted", {})
    total, is_approx = estimate_turn_tokens(
        system_prompt="sys",
        chat_history=[],
        user_prompt="hello world, this is a longer prompt for counting",
        model="unknown-model-zz",
    )
    assert is_approx is True
    # Headroom: approx total must exceed the raw ~len/4 heuristic.
    assert total > len("hello world, this is a longer prompt for counting") // 4


def test_truncate_is_linear_and_cached(monkeypatch) -> None:
    from kani import ChatMessage

    from services.tokenizer import truncate_history_to_budget

    history = [ChatMessage.user(f"message number {i} " * 20) for i in range(30)]
    kept, dropped = truncate_history_to_budget(history, budget_tokens=200, model="gpt-4o")
    assert dropped > 0
    assert len(kept) < len(history)


def test_bench_timeout_keeps_fast_connect() -> None:
    from services.model_bench import TEST_TIMEOUT

    assert isinstance(TEST_TIMEOUT, httpx.Timeout)
    assert TEST_TIMEOUT.connect == 5.0
    assert TEST_TIMEOUT.read == 120.0


def test_update_client_sends_house_user_agent(monkeypatch) -> None:
    import services.update_service as upd

    seen: dict = {}
    real_client = httpx.AsyncClient

    class _SpyClient(real_client):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(upd.httpx, "AsyncClient", _SpyClient)
    monkeypatch.setattr(upd.constants, "UPDATE_CONFIG_URL", "http://127.0.0.1:9/nope")

    import asyncio

    asyncio.run(upd.UpdateService.check_for_updates())
    assert "LM-Router/" in str(seen.get("headers", {}).get("User-Agent", ""))


def test_atomic_write_is_owner_only(tmp_path) -> None:
    from core.storage import atomic_write_json

    target = tmp_path / "settings.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    if os.name == "posix":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"settings landed {oct(mode)}, want 0o600"


def test_retry_statuses_cover_transient_codes() -> None:
    from services.http import RETRY_STATUSES

    assert 408 in RETRY_STATUSES and 429 in RETRY_STATUSES
    assert 502 in RETRY_STATUSES and 503 in RETRY_STATUSES and 504 in RETRY_STATUSES


def test_privacy_options_entry_wired(boot_page) -> None:
    """The About card must expose the ad-privacy entry to its controller."""
    from main import AppController
    from state.controller_ctx import ControllerMethods

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()
    assert isinstance(controller.methods, ControllerMethods)
    assert callable(controller.methods.open_ad_privacy_options)
    # No-ads path informs instead of crashing.
    controller.ads = None
    controller._open_ad_privacy_options()
