"""GuardedKani: hung tools become error results, never hangs."""

from __future__ import annotations

import asyncio
import time

from kani.ai_function import AIFunction
from kani.engines.base import BaseEngine

from services.guard_kani import TOOL_TIMEOUT_S, GuardedKani


class _StubEngine(BaseEngine):
    max_context_size = 4096

    def prompt_len(self, messages, functions=None, **kwargs) -> int:  # type: ignore[no-untyped-def]
        return 0

    async def predict(self, messages, functions=None, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _kani_with(fn) -> GuardedKani:
    return GuardedKani(_StubEngine(), functions=[fn])


def test_hung_tool_returns_error_result_fast() -> None:
    async def _hangs(s: str) -> str:
        await asyncio.sleep(3600)
        return s  # pragma: no cover

    fn = AIFunction(_hangs, name="slow_tool")
    kani = _kani_with(fn)
    from kani.models import FunctionCall

    async def _run():
        return await kani.do_function_call(
            FunctionCall(name="slow_tool", arguments='{"s": "x"}'),
            tool_call_id="call_1",
        )

    started = time.monotonic()
    result = asyncio.run(_run())
    elapsed = time.monotonic() - started
    assert elapsed < TOOL_TIMEOUT_S + 10, f"took {elapsed:.1f}s, timeout did not fire"
    assert result.message.tool_call_id == "call_1", "pairing id must survive"
    assert result.is_model_turn is True
    assert "timed out" in str(result.message.text or "")
    assert bool(getattr(result.message, "is_tool_call_error", False))


def test_fast_tool_unaffected() -> None:
    async def _quick(s: str = "") -> str:
        return f"ok:{s}"

    fn = AIFunction(_quick, name="quick_tool")
    kani = _kani_with(fn)
    from kani.models import FunctionCall

    async def _run():
        return await kani.do_function_call(
            FunctionCall(name="quick_tool", arguments="{}"),
            tool_call_id="call_2",
        )

    result = asyncio.run(_run())
    assert "ok:" in str(result.message.text or "")
    assert not getattr(result.message, "is_tool_call_error", False)


def test_ensure_kani_builds_guarded(monkeypatch) -> None:
    from core.settings import AppSettings
    from core.state import state
    from services.agent import AgentService

    monkeypatch.setattr(state, "gateway_base_url", "http://127.0.0.1:8082/v1")
    agent = AgentService(AppSettings())
    agent.start()
    try:
        kani = agent.ensure_kani("m", "sys")
        assert isinstance(kani, GuardedKani)
    finally:
        agent.stop()
