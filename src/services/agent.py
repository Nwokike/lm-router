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
- on_thought(text): reasoning deltas, retargeted per turn via ensure_kani
- on_settled(): fires exactly once per turn, even when a callback raised
"""

import asyncio
import threading
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

# Placeholder parked in AgentService._current while a turn is reserved but
# its portal task is not yet created. Never cancelled, never awaited — the
# dispatch sites below replace it with the real task handle.
_TURN_RESERVED: Any = object()


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
        # Build-time settings this kani was constructed WITH — both are read
        # only at construction, so they must join the reuse comparison below
        # or changing them silently does nothing for the session.
        self._retry: int | None = None
        self._ctx: int | None = None
        # Live HTTP resources owned by the current engine: closed via the
        # portal before the engine is replaced or the portal dies, so model
        # switches never leak sockets/pools.
        self._engine_client: Any = None
        self._engine_http: httpx.AsyncClient | None = None
        # Set by the controller to re-spawn long-lived portal tasks (the MCP
        # owner loop) after a portal restart — without it, a restart silently
        # kills MCP for the rest of the session.
        self.on_portal_restart: Callable[[], None] | None = None
        # Guards the busy check-then-set in start_turn and the take-and-clear
        # in stop_turn: two racing callers must not both dispatch, and a
        # stop must not clear a NEW turn's handle set after it ran.
        self._turn_lock = threading.Lock()

    # lifecycle

    @property
    def portal(self) -> Any:
        return self._portal

    @property
    def kani(self) -> Kani | None:
        return self._kani

    @property
    def busy(self) -> bool:
        # Read-only: no mutation here. Clearing a finished handle is the
        # dispatcher's job (finally in _run / stop_turn); a getter that
        # writes races with the lock-protected reserve path above.
        curr = self._current
        if curr is None:
            return False
        if curr is _TURN_RESERVED:
            return True
        done = getattr(curr, "done", None)
        if callable(done):
            try:
                if done():
                    return False
            except Exception:
                return True
            return True
        # Cancel scopes and other handles without .done() count as busy
        # until explicitly cleared.
        return True

    def start(self) -> None:
        if self._provider is not None:
            return
        self._provider = BlockingPortalProvider("asyncio")
        self._portal = self._provider.__enter__()
        LOG.info("agent portal started")

    def _close_engine_clients(self) -> None:
        """Close the current engine's HTTP resources on the portal loop.

        Must run BEFORE the portal dies (aclose on a dead loop raises) and
        before a replacement engine is installed (or the old pool leaks).
        Failures are logged, never raised — shutdown paths call this
        best-effort.
        """
        client, http = self._engine_client, self._engine_http
        self._engine_client, self._engine_http = None, None
        portal = self._portal
        if portal is None:
            return
        for closer, label in ((client, "openai client"), (http, "http client")):
            if closer is None:
                continue
            close = getattr(closer, "close", None)
            if close is None:
                continue
            try:
                result = portal.call(close)
                if asyncio.iscoroutine(result):
                    # portal.call already awaited it; nothing left to do.
                    pass
            except Exception as exc:
                LOG.debug("engine %s close: %s", label, exc)

    def stop(self) -> None:
        self._close_engine_clients()
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
        # The old engine's httpx pool/transport was created under the dead
        # portal's loop: reusing it raises "Event loop is closed" or binds a
        # live client to a dead loop. Force a rebuild on the next turn.
        self._close_engine_clients()
        self._kani = None
        self._model = ""
        self.start()
        LOG.warning("agent portal restarted")
        if self.on_portal_restart is not None:
            try:
                self.on_portal_restart()
            except Exception as exc:
                LOG.warning("portal-restart callback failed: %s", exc)

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
            and self._retry == self.settings.tool_retry_attempts
            and self._ctx == self.settings.max_context_tokens
        ):
            # Same turn shape: reuse the engine, but re-point its reasoning tap
            # so a rebuild is not needed to capture this turn's thoughts.
            engine = getattr(self._kani, "engine", None)
            if engine is not None and hasattr(engine, "on_thought"):
                engine.on_thought = on_thought
            return self._kani
        history = list(self._kani.chat_history) if self._kani is not None else []
        # Close the outgoing engine's HTTP resources BEFORE replacing it:
        # every rebuild otherwise leaks a connection pool on the portal loop.
        self._close_engine_clients()
        # The reasoning tap is delivered via an attribute rather than a third
        # positional argument so an injected 2-arg engine_factory keeps working.
        self._on_thought = on_thought
        engine = self._engine_factory(self.settings, model)
        if hasattr(engine, "on_thought"):
            engine.on_thought = on_thought
        # Track the live clients so the next rebuild (or stop/restart) can
        # close them. openai.AsyncOpenAI owns ._client (an httpx.AsyncClient
        # subclass) and exposes async .close(); closing it also closes the
        # pool. Read defensively for injected test engines.
        sdk_client = getattr(engine, "client", None)
        inner = getattr(sdk_client, "_client", None)
        http_client = inner if isinstance(inner, httpx.AsyncClient) else None
        self._engine_client = sdk_client
        self._engine_http = http_client
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
        self._retry = self.settings.tool_retry_attempts
        self._ctx = self.settings.max_context_tokens
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
        with self._turn_lock:
            if self.busy:
                return False
            # Reserve the turn under the lock: a second caller racing past
            # the check would otherwise dispatch a parallel _run against the
            # same chat_history and lose one task handle (uncancellable).
            self._current = _TURN_RESERVED
        if not state.gateway_running:
            with self._turn_lock:
                self._current = None
            on_error("offline", "Gateway is not running. Start it on the Server tab.")
            return False
        kani = self._kani
        if kani is None:
            with self._turn_lock:
                self._current = None
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
            # One encoding for estimate, budget, and trim: the old
            # count_tokens(prompt) used the heuristic default while the
            # other two used the real encoding, inflating the budget and
            # under-trimming CJK/code by up to 3x.
            from services.tokenizer import get_encoding_for_model

            shared_encoding, _ = get_encoding_for_model(self._model)
            trimmed, dropped = truncate_history_to_budget(
                kani.chat_history,
                budget_tokens=budget_for_prompt - count_tokens(prompt, shared_encoding),
                model=self._model,
            )
            # A tool call and its result are one unit: dropping the call but
            # keeping the result (or the reverse) produces an invalid
            # transcript, which models answer to badly or not at all.
            trimmed = _drop_orphan_function_messages(trimmed)
            kani.chat_history.clear()
            kani.chat_history.extend(trimmed)
            LOG.info("trimmed %d messages from chat history", dropped)

        def _safe_callback(label: str, fn: Callable[[], None]) -> None:
            # A UI callback raising mid-turn used to abort the async-for and
            # misreport a local bug as an upstream "error" — possibly AFTER
            # on_done already fired. Isolate each call; the turn continues.
            try:
                fn()
            except Exception as exc:
                LOG.warning("turn callback %s failed: %s", label, exc)

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
                                _safe_callback("on_delta", lambda: on_delta(chunk))
                    message = await manager.message()
                    last_message = message
                    if is_function:
                        if on_tool is not None:
                            is_error = bool(getattr(message, "is_tool_call_error", False))
                            _safe_callback(
                                "on_tool",
                                lambda: on_tool(
                                    str(getattr(message, "name", "tool")),
                                    str(getattr(message, "text", "") or ""),
                                    is_error,
                                ),
                            )
                        continue
                    if getattr(message, "tool_calls", None):
                        # intermediate round: it asked for tools; the reply comes
                        # after the tool results
                        continue
                    done_fired = True
                    _safe_callback("on_done", lambda: on_done(message, _usage_from(message)))
                if not done_fired and last_message is not None:
                    # model stopped right after requesting tools; surface what we have
                    done_fired = True
                    _safe_callback(
                        "on_done", lambda: on_done(last_message, _usage_from(last_message))
                    )
            except asyncio.CancelledError:
                # Swallow: re-raising re-enters the anyio task group with a
                # cancellation — the exact collapse mode the poisoned-portal
                # test guards against. restart_portal stays a backstop, not
                # the normal stop path.
                _safe_callback("on_error", lambda: on_error("stopped", "Generation stopped."))
            except openai.RateLimitError:
                _safe_callback(
                    "on_error",
                    lambda: on_error(
                        "rate_limited",
                        rate_limit_advice(self._model, state.models),
                    ),
                )
            except openai.AuthenticationError:
                _safe_callback(
                    "on_error",
                    lambda: on_error(
                        "auth",
                        "Gateway returned 401. Unknown model, or gateway auth failed.",
                    ),
                )
            except openai.PermissionDeniedError:
                _safe_callback(
                    "on_error", lambda: on_error("forbidden", "Access denied by gateway (403).")
                )
            except openai.NotFoundError:
                _safe_callback(
                    "on_error", lambda: on_error("model", "Model not found on this gateway.")
                )
            except openai.BadRequestError as exc:
                msg = getattr(exc, "message", str(exc))[:200]
                _safe_callback(
                    "on_error",
                    lambda msg=msg: on_error("invalid_request", f"Invalid request: {msg}"),
                )
            except openai.UnprocessableEntityError:
                _safe_callback(
                    "on_error",
                    lambda: on_error("rejected", "Request rejected by model provider."),
                )
            except openai.APITimeoutError:
                _safe_callback(
                    "on_error", lambda: on_error("timeout", "Gateway request timed out.")
                )
            except openai.APIConnectionError:
                _safe_callback("on_error", lambda: on_error("offline", "Gateway unreachable."))
            except openai.APIResponseValidationError:
                _safe_callback(
                    "on_error",
                    lambda: on_error("upstream", "Gateway returned an unparseable response."),
                )
            except openai.InternalServerError:
                _safe_callback(
                    "on_error",
                    lambda: on_error("upstream", "Upstream server error (500). Retrying may help."),
                )
            except openai.APIStatusError as exc:
                status = exc.status_code
                _safe_callback(
                    "on_error",
                    lambda status=status: on_error("upstream", f"Upstream HTTP {status}"),
                )
            except Exception as exc:
                LOG.error("turn failed: %s", exc)
                detail = str(exc)[:300]
                _safe_callback("on_error", lambda: on_error("error", detail))
            finally:
                with self._turn_lock:
                    self._current = None
                if on_settled is not None:
                    # Fires even when on_done/on_error raised — the controller
                    # uses it to guarantee the UI busy flag clears (forensics R6).
                    try:
                        on_settled()
                    except Exception as exc:
                        LOG.warning("turn settled callback failed: %s", exc)

        try:
            with self._turn_lock:
                self._current = self._portal.start_task_soon(_run)
        except RuntimeError as exc:
            # A poisoned portal (task-group collapse) used to kill the send
            # worker here: unhandled, no error row, busy stuck True forever.
            LOG.warning("turn dispatch failed (%s); restarting portal once", exc)
            self.restart_portal()
            try:
                with self._turn_lock:
                    self._current = self._portal.start_task_soon(_run)
            except Exception as retry_exc:
                LOG.error("portal unusable after restart: %s", retry_exc)
                with self._turn_lock:
                    self._current = None
                on_error("config", "Engine crashed. Restart LM Router.")
                return False
        return True

    def stop_turn(self) -> None:
        with self._turn_lock:
            current, self._current = self._current, None
        # The reservation sentinel is not a task: nothing to cancel, and
        # clearing it above already released the turn.
        if current is None or current is _TURN_RESERVED:
            return
        try:
            current.cancel()
        except Exception as exc:
            LOG.warning("stop failed: %s", exc)
