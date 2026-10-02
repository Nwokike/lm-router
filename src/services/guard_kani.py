"""GuardedKani: kani with a per-tool timeout (DDGS pattern).

kani runs a tool batch with unbounded `asyncio.gather`: one hung MCP/stdio
tool wedges the turn with no error, no log, and busy stuck True — the UI
sits on a spinner. Wrapping the outer `manager.message()` (the old ceiling)
kills the whole turn on a slow tool; wrapping HERE turns the failure into a
model-visible FUNCTION result instead, so the round continues, retry logic
applies, and the transcript stays paired (no orphan claims poisoning later
turns).
"""

from __future__ import annotations

import asyncio
import time

from kani import ChatMessage, Kani
from kani.internal import FunctionCallResult
from kani.models import FunctionCall

from core.logging import LOG

TOOL_TIMEOUT_S = 30.0


class GuardedKani(Kani):
    """Kani whose tool calls time out into error results, never hangs."""

    async def do_function_call(
        self, call: FunctionCall, tool_call_id: str | None = None
    ) -> FunctionCallResult:
        started = time.monotonic()
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_S):
                return await super().do_function_call(call, tool_call_id)
        except TimeoutError:
            elapsed = time.monotonic() - started
            LOG.warning(
                "tool %s timed out after %.0fs; returning error result",
                call.name,
                elapsed,
            )
            message = ChatMessage.function(
                call.name,
                f"Tool '{call.name}' timed out after {TOOL_TIMEOUT_S:.0f}s. "
                "The server may be slow or stuck. Try again, or continue "
                "without it.",
                tool_call_id=tool_call_id,
            )
            message.is_tool_call_error = True
            return FunctionCallResult(is_model_turn=True, message=message)
