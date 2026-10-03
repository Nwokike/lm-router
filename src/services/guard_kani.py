"""GuardedKani: bare-name resolution + per-tool timeout (DDGS pattern).

Two failure modes the owner hit live (2026-10-03), both fixed here:

1. **Bare-name NoSuchFunction** — MCP tools register as `Server.tool`
   (Exa.web_fetch_exa), but models often call the bare tail
   (`web_fetch_exa`). kani then raises "not defined. Only use the provided
   functions" and the call dies as `None (error)`. A UNIQUE suffix match
   maps bare -> prefixed before dispatch; ambiguous/unmatched names pass
   through so kani's honest error stands.
2. **Hung tool wedges the turn** — kani runs a tool batch with unbounded
   `asyncio.gather`: one hung MCP/stdio tool blocks `manager.message()`
   forever with no error and busy stuck True. Wrapping the outer ceiling
   kills the whole turn; wrapping HERE turns the failure into a
   model-visible FUNCTION result so the round continues, retry logic
   applies, and the transcript stays paired (no orphan claims).
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
    """Kani that resolves bare tool names and times out into error results."""

    def _resolve_call_name(self, call: FunctionCall) -> FunctionCall:
        """Exact match wins; a UNIQUE suffix match maps bare -> prefixed."""
        if call.name in self.functions or not self.functions:
            return call
        tail = call.name.rsplit(".", 1)[-1]
        matches = [key for key in self.functions if key.rsplit(".", 1)[-1] == tail]
        if len(matches) == 1:
            LOG.info("tool call resolved: %s -> %s", call.name, matches[0])
            return FunctionCall(name=matches[0], arguments=call.arguments)
        return call

    async def do_function_call(
        self, call: FunctionCall, tool_call_id: str | None = None
    ) -> FunctionCallResult:
        call = self._resolve_call_name(call)
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
