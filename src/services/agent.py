"""Chat agent: kani turns bridged to Flet through a dedicated anyio portal.

One BlockingPortal (own thread + own asyncio loop) hosts every kani turn and
every httpx call, which keeps kani's single-loop lock invariant intact while
Flet stays on its own loop (anyio study). Stop = Future.cancel(), which the
portal maps to a task-group cancel; we never break out of the async-for
(kani StreamManager hangs if you do).

Turn message protocol (chronological, so tool cards land in order):
- on_delta(chunk): streamed text for the current round
- on_tool(name, text, is_error): a FUNCTION-role result (search, MCP tools)
- on_done(message, usage): ONLY the final assistant reply (no tool_calls)
- on_error(kind, text): failures incl. "stopped"
"""

import asyncio
from collections.abc import Callable
from typing import Any

import openai
from anyio.from_thread import BlockingPortalProvider
from kani import Kani
from kani.engines.openai import OpenAIEngine

from core.logging import LOG
from core.settings import AppSettings
from core.state import state

DeltaCB = Callable[[str], None]
DoneCB = Callable[[Any, dict | None], None]
ErrorCB = Callable[[str, str], None]
ToolCB = Callable[[str, str, bool], None]
ExtraTools = Callable[[], tuple[list, object]]


def _default_engine_factory(settings: AppSettings, model: str) -> OpenAIEngine:
    return OpenAIEngine(
        api_key="lm-router",
        api_base=state.gateway_base_url,
        model=model,
        api_type="chat_completions",
        max_context_size=settings.max_context_tokens,
    )


def _usage_from(message: Any) -> dict | None:
    extra = getattr(message, "extra", None)
    if not hasattr(extra, "get"):
        return None
    usage = extra.get("openai_usage")
    if usage is None or not hasattr(usage, "get"):
        return None
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


class AgentService:
    def __init__(
        self,
        settings: AppSettings,
        engine_factory: Callable[[AppSettings, str], Any] | None = None,
        extra_tools: ExtraTools | None = None,
    ) -> None:
        self.settings = settings
        self._engine_factory = engine_factory or _default_engine_factory
        self._extra_tools = extra_tools
        self._provider: BlockingPortalProvider | None = None
        self._portal: Any = None
        self._kani: Kani | None = None
        self._model = ""
        self._system = ""
        self._base = ""
        self._tools_gen: object = None
        self._current: Any = None

    # lifecycle

    @property
    def portal(self) -> Any:
        return self._portal

    @property
    def kani(self) -> Kani | None:
        return self._kani

    @property
    def busy(self) -> bool:
        return self._current is not None

    def start(self) -> None:
        if self._provider is not None:
            return
        self._provider = BlockingPortalProvider("asyncio")
        self._portal = self._provider.__enter__()
        LOG.info("agent portal started")

    def stop(self) -> None:
        provider, self._provider = self._provider, None
        self._portal = None
        self._current = None
        self._kani = None
        if provider is not None:
            try:
                provider.__exit__(None, None, None)
            except Exception as exc:
                LOG.warning("agent portal shutdown: %s", exc)
            LOG.info("agent portal stopped")

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run an async callable on the portal loop, blocking the caller."""
        if self._portal is None:
            raise RuntimeError("agent portal not started")
        return self._portal.call(fn, *args)

    # kani management

    def _current_tools(self) -> tuple[list, object]:
        if self._extra_tools is None:
            return [], None
        try:
            tools, generation = self._extra_tools()
        except Exception as exc:
            LOG.warning("tool registry unavailable: %s", exc)
            return [], None
        return list(tools), generation

    def ensure_kani(self, model: str, system_prompt: str) -> Kani | None:
        base = state.gateway_base_url
        if not model or not base:
            return None
        tools, tools_gen = self._current_tools()
        if (
            self._kani is not None
            and self._model == model
            and self._system == system_prompt
            and self._base == base
            and self._tools_gen == tools_gen
        ):
            return self._kani
        history = list(self._kani.chat_history) if self._kani is not None else []
        engine = self._engine_factory(self.settings, model)
        self._kani = Kani(
            engine,
            system_prompt=system_prompt or None,
            chat_history=history,
            functions=tools or None,
        )
        self._model = model
        self._system = system_prompt
        self._base = base
        self._tools_gen = tools_gen
        LOG.info("kani ready model=%s base=%s tools=%d", model, base, len(tools))
        return self._kani

    def reset_conversation(self) -> None:
        if self._kani is not None:
            self._kani.chat_history.clear()
        LOG.info("conversation reset")

    # turns

    def start_turn(
        self,
        prompt: str,
        on_delta: DeltaCB,
        on_done: DoneCB,
        on_error: ErrorCB,
        on_tool: ToolCB | None = None,
    ) -> bool:
        if self._portal is None:
            on_error("config", "Agent is not running.")
            return False
        if self._current is not None:
            return False
        if not state.gateway_running:
            on_error("offline", "Gateway is not running. Start it on the Server tab.")
            return False
        kani = self._kani
        if kani is None:
            on_error("config", "No model selected.")
            return False

        async def _run() -> None:
            done_fired = False
            last_message: Any = None
            try:
                async for manager in kani.full_round_stream(prompt):
                    role = str(getattr(manager, "role", ""))
                    is_function = "function" in role.lower()
                    if not is_function:
                        async for chunk in manager:
                            if chunk:
                                on_delta(chunk)
                    message = await manager.message()
                    last_message = message
                    if is_function:
                        if on_tool is not None:
                            is_error = bool(getattr(message, "is_tool_call_error", False))
                            on_tool(
                                str(getattr(message, "name", "tool")),
                                str(getattr(message, "text", "") or ""),
                                is_error,
                            )
                        continue
                    if getattr(message, "tool_calls", None):
                        # intermediate round: it asked for tools; the reply comes
                        # after the tool results
                        continue
                    done_fired = True
                    on_done(message, _usage_from(message))
                if not done_fired and last_message is not None:
                    # model stopped right after requesting tools; surface what we have
                    done_fired = True
                    on_done(last_message, _usage_from(last_message))
            except asyncio.CancelledError:
                on_error("stopped", "Generation stopped.")
                raise
            except openai.RateLimitError:
                on_error("rate_limited", "Rate limited. Try again in a moment.")
            except openai.AuthenticationError:
                on_error("auth", "Authentication failed. Check the provider key.")
            except openai.NotFoundError:
                on_error("model", "Model not found on this gateway.")
            except openai.APIConnectionError, openai.APITimeoutError:
                on_error("offline", "Gateway unreachable.")
            except openai.APIStatusError as exc:
                on_error("upstream", f"Upstream HTTP {exc.status_code}")
            except Exception as exc:
                LOG.error("turn failed: %s", exc)
                on_error("error", str(exc)[:300])
            finally:
                self._current = None

        self._current = self._portal.start_task_soon(_run)
        return True

    def stop_turn(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            try:
                current.cancel()
            except Exception as exc:
                LOG.warning("stop failed: %s", exc)
