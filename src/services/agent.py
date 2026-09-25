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

import httpx
import openai
from anyio.from_thread import BlockingPortalProvider
from kani import Kani
from kani.engines.openai import OpenAIEngine

from core.catalog import api_type_for, rate_limit_advice
from core.logging import LOG
from core.settings import AppSettings
from core.state import state
from services.reasoning import (
    ReasoningEngine,
    ThoughtTap,
    build_thought_client,
)
from services.tokenizer import (
    count_tokens,
    estimate_turn_tokens,
    tokenizer_or_heuristic,
    truncate_history_to_budget,
)

DeltaCB = Callable[[str], None]
ThoughtCB = Callable[[str], None]
DoneCB = Callable[[Any, dict | None], None]
ErrorCB = Callable[[str, str], None]
ToolCB = Callable[[str, str, bool], None]
ExtraTools = Callable[[], tuple[list, object]]


def _catalog_row(model_id: str) -> dict | None:
    for row in state.models:
        if isinstance(row, dict) and str(row.get("id") or "") == model_id:
            return row
    return None


def _active_provider(settings: AppSettings):
    """The provider a turn should actually reach, or None for the gateway."""
    provider_id = str(getattr(settings, "active_provider_id", "") or "")
    if not provider_id:
        return None
    for provider in settings.providers:
        if provider.id == provider_id and provider.enabled:
            return provider
    return None


def _default_engine_factory(
    settings: AppSettings,
    model: str,
    on_thought: Callable[[str], None] | None = None,
) -> OpenAIEngine:
    """Build the kani engine for a turn.

    Target selection: an explicitly selected external provider routes straight
    to its base_url with its own key (bypassing the local gateway); otherwise
    the local Kiri gateway serves the model. `api_type` follows the catalog's
    endpoint so the right request shape is used.
    """
    row = _catalog_row(model)
    api_type = api_type_for(row) or "chat_completions"

    provider = _active_provider(settings)
    if provider is not None:
        base_url = str(provider.base_url).rstrip("/")
        secret = provider.api_key
        api_key = secret.get_secret_value() if secret is not None else "lm-router"
        target = f"provider {provider.name}"
    else:
        base_url = state.gateway_base_url
        api_key = "lm-router"
        target = "gateway"

    max_retries = 2

    # Explicit client so a stalled endpoint can never hang a turn for the SDK's
    # default 600s: read=90s bounds dead air BETWEEN chunks (streaming keeps
    # chunks flowing, so slow-but-alive models are unaffected); retries capped.
    # The transport tees the SSE stream: kani discards reasoning deltas, so
    # this is the only place they can be captured.
    tap = ThoughtTap(on_thought)
    http_client = build_thought_client(
        base_url=base_url,
        api_key=api_key,
        timeout=httpx.Timeout(90.0, connect=5.0),
        max_retries=max_retries,
        tap=tap,
    )
    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=http_client,
        max_retries=max_retries,
    )
    engine = ReasoningEngine(
        client=client,
        model=model,
        api_type=api_type,
        max_context_size=settings.max_context_tokens,
        # kani's lazy `tokenizer` property calls tiktoken.get_encoding() for
        # unknown models, which DOWNLOADS the BPE ranks with no timeout — a
        # 30-60s stall inside the first chat turn. Hand it the encoding our
        # own bounded loader already resolved. None is safe.
        tokenizer=tokenizer_or_heuristic(model),
        tap=tap,
    )
    LOG.info(
        "engine ready model=%s via=%s api_type=%s tools_ok=%s",
        model,
        target,
        api_type,
        row is not None,
    )
    return engine


def _drop_orphan_function_messages(messages: list) -> list:
    """Remove function messages whose paired call is no longer present.

    Trimming oldest-first can cut a message pair in half. kani then sends a
    tool result with no preceding call, which the provider rejects or the
    model answers to as if it were a user turn.
    """
    kept: list = []
    for message in messages:
        role = str(getattr(message, "role", "") or "")
        text = str(getattr(message, "text", "") or "")
        if (
            role == "function"
            and "call_" not in text
            and not any(
                str(getattr(m, "role", "")) == "assistant" and getattr(m, "tool_calls", None)
                for m in kept[-2:]
            )
        ):
            # No call in the immediately preceding assistant turn.
            if not any(getattr(m, "tool_calls", None) for m in kept[-2:]):
                continue
        kept.append(message)
    return kept


def _usage_from(message: Any) -> dict | None:
    extra = getattr(message, "extra", None)
    if not hasattr(extra, "get"):
        return None
    usage = extra.get("openai_usage")
    if usage is None or not hasattr(usage, "get"):
        return None

    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}

    result = {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }

    if hasattr(completion_details, "get"):
        reasoning = completion_details.get("reasoning_tokens")
        if reasoning:
            result["reasoning_tokens"] = int(reasoning)
    if hasattr(prompt_details, "get"):
        cached = prompt_details.get("cached_tokens")
        if cached:
            result["cached_tokens"] = int(cached)

    completion = extra.get("openai_completion")
    if completion is not None:
        choices = getattr(completion, "choices", None)
        if choices and hasattr(choices[0], "finish_reason"):
            result["finish_reason"] = choices[0].finish_reason
        elif isinstance(completion, dict):
            c_list = completion.get("choices") or []
            if c_list and isinstance(c_list[0], dict):
                result["finish_reason"] = c_list[0].get("finish_reason")

    return result


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
        self._tools_error: str | None = None

    # lifecycle

    @property
    def portal(self) -> Any:
        return self._portal

    @property
    def kani(self) -> Kani | None:
        return self._kani

    @property
    def busy(self) -> bool:
        curr = self._current
        if curr is None:
            return False
        if hasattr(curr, "done") and curr.done():
            self._current = None
            return False
        return True

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

    def restart_portal(self) -> None:
        """Rebuild the anyio portal after a task-group collapse (a poisoned
        portal raises 'This portal is not running' for every later call)."""
        old, self._provider = self._provider, None
        self._portal = None
        self._current = None
        if old is not None:
            try:
                old.__exit__(None, None, None)
            except Exception as exc:
                LOG.warning("old portal teardown: %s", exc)
        self.start()
        LOG.warning("agent portal restarted")

    def spawn(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Start a long-lived portal task (e.g. the MCP owner loop)."""
        if self._portal is None:
            raise RuntimeError("agent portal not started")
        return self._portal.start_task_soon(fn, *args)

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run an async callable on the portal loop, blocking the caller.

        Auto-restarts a dead portal once (anyio task-group collapse makes
        every later call fail permanently otherwise)."""
        try:
            if self._portal is None:
                raise RuntimeError("agent portal not started")
            return self._portal.call(fn, *args)
        except RuntimeError as exc:
            if "portal is not running" not in str(exc):
                raise
            LOG.warning("portal dead (%s); restarting and retrying once", exc)
            self.restart_portal()
            return self._portal.call(fn, *args)

    # kani management

    def _current_tools(self) -> tuple[list, object]:
        if self._extra_tools is None:
            return [], None
        try:
            tools, generation = self._extra_tools()
        except Exception as exc:
            # A broken registry used to silently run the turn with no
            # search/MCP tools. Remember it; the controller consumes it and
            # tells the user (audit D, matrix row 12).
            LOG.warning("tool registry unavailable: %s", exc)
            self._tools_error = str(exc)
            return [], None
        return list(tools), generation

    def consume_tools_error(self) -> str | None:
        """One-shot read of the last tool-registry failure (controller → banner)."""
        err, self._tools_error = self._tools_error, None
        return err

    def ensure_kani(
        self,
        model: str,
        system_prompt: str,
        on_thought: ThoughtCB | None = None,
    ) -> Kani | None:
        base = state.gateway_base_url
        provider = _active_provider(self.settings)
        base = str(provider.base_url).rstrip("/") if provider is not None else base
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
            # Same turn shape: reuse the engine, but re-point its reasoning tap
            # so a rebuild is not needed to capture this turn's thoughts.
            engine = getattr(self._kani, "engine", None)
            if engine is not None and hasattr(engine, "on_thought"):
                engine.on_thought = on_thought
            return self._kani
        history = list(self._kani.chat_history) if self._kani is not None else []
        # The reasoning tap is delivered via an attribute rather than a third
        # positional argument so an injected 2-arg engine_factory keeps working.
        self._on_thought = on_thought
        engine = self._engine_factory(self.settings, model)
        if hasattr(engine, "on_thought"):
            engine.on_thought = on_thought
        self._kani = Kani(
            engine,
            system_prompt=system_prompt or None,
            chat_history=history,
            functions=tools or None,
            retry_attempts=self.settings.tool_retry_attempts,
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

    def _generation_params(self) -> dict[str, Any]:
        """Per-turn generation kwargs derived from user settings.

        Only values the user actually set are sent, so a provider that rejects
        an exotic knob (reasoning_effort on a non-reasoning model, say) does
        not turn every free-tier turn into a 400. `auto` reasoning effort is
        the default and is therefore omitted entirely.
        """
        s = self.settings
        params: dict[str, Any] = {
            "temperature": s.temperature,
            # `max_tokens` is deprecated in the installed OpenAI SDK and is
            # incompatible with o-series reasoning models, which want
            # `max_completion_tokens`.
            "max_completion_tokens": s.max_reply_tokens,
        }
        if s.top_p != 1.0:
            params["top_p"] = s.top_p
        if s.seed is not None:
            params["seed"] = s.seed
        if s.presence_penalty:
            params["presence_penalty"] = s.presence_penalty
        if s.frequency_penalty:
            params["frequency_penalty"] = s.frequency_penalty
        if s.reasoning_effort != "auto":
            params["reasoning_effort"] = s.reasoning_effort
        if s.json_mode:
            params["response_format"] = {"type": "json_object"}
        return params

    def start_turn(
        self,
        prompt: str,
        on_delta: DeltaCB,
        on_done: DoneCB,
        on_error: ErrorCB,
        on_tool: ToolCB | None = None,
        on_settled: Callable[[], None] | None = None,
    ) -> bool:
        if self._portal is None:
            on_error("config", "Agent is not running.")
            return False
        if self.busy:
            return False
        if not state.gateway_running:
            on_error("offline", "Gateway is not running. Start it on the Server tab.")
            return False
        kani = self._kani
        if kani is None:
            on_error("config", "No model selected.")
            return False

        # Pre-flight context budget estimation and safe truncation
        tools, _ = self._current_tools()
        max_ctx = self.settings.max_context_tokens
        completion_reserve = min(8192, max(1024, max_ctx // 10))
        budget_for_prompt = max(1024, max_ctx - completion_reserve)

        estimated, is_approx = estimate_turn_tokens(
            system_prompt=self._system,
            chat_history=kani.chat_history,
            user_prompt=prompt,
            tools=tools,
            model=self._model,
        )
        LOG.debug(
            "preflight token estimate=%d%s budget=%d",
            estimated,
            " (approx)" if is_approx else "",
            budget_for_prompt,
        )

        if estimated > budget_for_prompt and kani.chat_history:
            LOG.warning(
                "prompt exceeds budget (%d > %d), trimming oldest history",
                estimated,
                budget_for_prompt,
            )
            trimmed, dropped = truncate_history_to_budget(
                kani.chat_history,
                budget_tokens=budget_for_prompt - count_tokens(prompt),
                model=self._model,
            )
            # A tool call and its result are one unit: dropping the call but
            # keeping the result (or the reverse) produces an invalid
            # transcript, which models answer to badly or not at all.
            trimmed = _drop_orphan_function_messages(trimmed)
            kani.chat_history.clear()
            kani.chat_history.extend(trimmed)
            LOG.info("trimmed %d messages from chat history", dropped)

        async def _run() -> None:
            done_fired = False
            last_message: Any = None
            try:
                async for manager in kani.full_round_stream(
                    prompt,
                    max_function_rounds=self.settings.tool_max_rounds,
                    **self._generation_params(),
                ):
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
                on_error(
                    "rate_limited",
                    rate_limit_advice(self._model, state.models),
                )
            except openai.AuthenticationError:
                on_error(
                    "auth",
                    "Gateway returned 401. Unknown model, or gateway auth failed.",
                )
            except openai.PermissionDeniedError:
                on_error("forbidden", "Access denied by gateway (403).")
            except openai.NotFoundError:
                on_error("model", "Model not found on this gateway.")
            except openai.BadRequestError as exc:
                msg = getattr(exc, "message", str(exc))[:200]
                on_error("invalid_request", f"Invalid request: {msg}")
            except openai.UnprocessableEntityError:
                on_error("rejected", "Request rejected by model provider.")
            except openai.APITimeoutError:
                on_error("timeout", "Gateway request timed out.")
            except openai.APIConnectionError:
                on_error("offline", "Gateway unreachable.")
            except openai.APIResponseValidationError:
                on_error("upstream", "Gateway returned an unparseable response.")
            except openai.InternalServerError:
                on_error("upstream", "Upstream server error (500). Retrying may help.")
            except openai.APIStatusError as exc:
                on_error("upstream", f"Upstream HTTP {exc.status_code}")
            except Exception as exc:
                LOG.error("turn failed: %s", exc)
                on_error("error", str(exc)[:300])
            finally:
                self._current = None
                if on_settled is not None:
                    # Fires even when on_done/on_error raised — the controller
                    # uses it to guarantee the UI busy flag clears (forensics R6).
                    try:
                        on_settled()
                    except Exception as exc:
                        LOG.warning("turn settled callback failed: %s", exc)

        try:
            self._current = self._portal.start_task_soon(_run)
        except RuntimeError as exc:
            # A poisoned portal (task-group collapse) used to kill the send
            # worker here: unhandled, no error row, busy stuck True forever.
            LOG.warning("turn dispatch failed (%s); restarting portal once", exc)
            self.restart_portal()
            try:
                self._current = self._portal.start_task_soon(_run)
            except Exception as retry_exc:
                LOG.error("portal unusable after restart: %s", retry_exc)
                on_error("config", "Engine crashed. Restart LM Router.")
                return False
        return True

    def stop_turn(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            try:
                current.cancel()
            except Exception as exc:
                LOG.warning("stop failed: %s", exc)
