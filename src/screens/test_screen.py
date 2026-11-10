"""Test screen: one prompt, one chat-completion call, full raw JSON.

The app is a gateway, not a chat client; this screen is the "does my model
answer?" probe — and it is TECHNICAL: after every call the exact request
body and the raw response JSON are shown in full. Reasoning models surface
their thinking in a collapsible block, parsed straight from the completion
JSON. No sessions, no history, no streaming — one non-streaming POST to the
local gateway.

The composer is the DDGS/CollabShell design: docked to the bottom, Enter
sends (shift_enter=True), Shift+Enter breaks the line, and one circular
button flips between Send and Stop faces.
"""

import contextlib
import json
import time

import flet as ft

from components.banner_ad import build_banner_ad
from components.thinking import ThinkingBlock
from core import theme, tokens
from core.catalog import chat_models
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx
from state.service_ctx import ServiceCtx

# Bounded probe: free models can be slow, but a test call must never hang
# the button forever. Same shape as the model bench's timeout.
PROBE_TIMEOUT_S = 120.0
MAX_REPLY_TOKENS = 1024


def _pretty(payload: object) -> str:
    """Stable, selectable JSON text for the technical blocks."""
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except TypeError, ValueError:
        return str(payload)


def _reasoning_of(message: dict) -> str:
    """Reasoning text wherever the provider put it (DeepSeek-style first)."""
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = str(message.get(key) or "")
        if text.strip():
            return text
    return ""


@ft.component
def TestScreen():
    services = ft.use_context(ServiceCtx)
    state_ctx = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    page = getattr(ft.context, "page", None)
    is_dark = theme.is_dark_mode(page, state_ctx.theme_mode)

    # Local probe state: the LAST call only. Tab switches remount this
    # screen and reset it — a probe, not a record.
    busy, set_busy = ft.use_state(False)
    answer, set_answer = ft.use_state("")
    reasoning, set_reasoning = ft.use_state("")
    error, set_error = ft.use_state("")
    request_json, set_request_json = ft.use_state("")
    response_json, set_response_json = ft.use_state("")
    meta_line, set_meta_line = ft.use_state("")
    draft, set_draft = ft.use_state("")

    controller = getattr(page, "_lmrouter_controller", None)
    # The in-flight probe's future: the Stop face of the composer button
    # cancels it (a bounded 120s call, but nobody should wait that out).
    probe_future: list = []

    def _apply(
        *,
        answer_text: str,
        reasoning_text: str,
        err: str,
        request: str,
        response: str,
        meta: str,
    ) -> None:
        set_answer(answer_text)
        set_reasoning(reasoning_text)
        set_error(err)
        set_request_json(request)
        set_response_json(response)
        set_meta_line(meta)
        set_busy(False)

    def _stop_probe() -> None:
        """Stop face: cancel the in-flight request and settle the UI."""
        if probe_future:
            with contextlib.suppress(Exception):
                probe_future[0].cancel()
        probe_future.clear()
        set_busy(False)

    def _send(_e: object | None = None) -> None:
        prompt = draft.strip()
        if not prompt or busy:
            return
        if not state_ctx.gateway_running:
            methods.start_gateway()
            return
        model = state_ctx.model or "auto"
        base = state_ctx.gateway_base_url
        http = services.http
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": MAX_REPLY_TOKENS,
            "stream": False,
        }
        request_text = _pretty(body)
        set_busy(True)
        set_error("")
        set_answer("")
        set_reasoning("")
        set_request_json(request_text)
        set_response_json("")
        set_meta_line("")
        set_draft("")  # the box clears the moment the prompt goes out

        async def call() -> tuple[int, dict]:
            """One non-streaming completion through the shared client."""
            started = time.monotonic()
            resp = await http.post(
                f"{base}/chat/completions",
                json=body,
                timeout=PROBE_TIMEOUT_S,
            )
            ms = int((time.monotonic() - started) * 1000)
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text[:4000]}
            return resp.status_code, {"ms": ms, "payload": payload}

        def work() -> None:
            if controller is None:
                # Headless/tests: no controller, no runner — nothing to do.
                return
            try:
                # Spawn (not run): the future is kept so the Stop face can
                # cancel the in-flight request on the runner loop.
                future = controller._async.spawn(call)
                probe_future.clear()
                probe_future.append(future)
                status, result = future.result(timeout=PROBE_TIMEOUT_S + 10)
            except Exception as exc:
                if probe_future and probe_future[0].cancelled():
                    return  # user pressed Stop; busy already cleared
                name = str(exc) or type(exc).__name__
                controller._run_on_ui(
                    lambda: _apply(
                        answer_text="",
                        reasoning_text="",
                        err=f"Request failed: {name[:300]}",
                        request=request_text,
                        response="",
                        meta="",
                    )
                )()
                return
            ms = int(result.get("ms") or 0)
            payload = result.get("payload") or {}
            response_text = _pretty(payload)
            content = ""
            thought = ""
            for choice in payload.get("choices") or []:
                if isinstance(choice, dict):
                    message = choice.get("message") or {}
                    content = str(message.get("content") or "")
                    thought = _reasoning_of(message)
                    break
            usage = payload.get("usage") or {}
            usage_bits = [f"{ms} ms"]
            if usage:
                usage_bits.append(
                    f"in {usage.get('prompt_tokens', 0)} · out "
                    f"{usage.get('completion_tokens', 0)} tokens"
                )
            meta = " · ".join(usage_bits)
            if status != 200:
                controller._run_on_ui(
                    lambda: _apply(
                        answer_text="",
                        reasoning_text=thought,
                        err=f"HTTP {status} — see the raw response below.",
                        request=request_text,
                        response=response_text,
                        meta=meta,
                    )
                )()
                return
            if not content.strip():
                controller._run_on_ui(
                    lambda: _apply(
                        answer_text="",
                        reasoning_text=thought,
                        err=(
                            "The model returned an empty reply. Pick another model "
                            "or hit Refresh models on the Server tab."
                        ),
                        request=request_text,
                        response=response_text,
                        meta=meta,
                    )
                )()
                return
            controller._run_on_ui(
                lambda: _apply(
                    answer_text=content,
                    reasoning_text=thought,
                    err="",
                    request=request_text,
                    response=response_text,
                    meta=meta,
                )
            )()

        page.run_thread(work)

    # ── model picker pill ────────────────────────────────────────────────
    model = state_ctx.model or "auto"
    eligible = chat_models([m for m in state_ctx.models if isinstance(m, dict)])
    items = [
        ft.PopupMenuItem(
            content=ft.Text(str(m.get("id") or ""), size=tokens.FONT_SM),
            on_click=lambda _e, mid=str(m.get("id") or ""): methods.set_model(mid),
        )
        for m in eligible
        if m.get("id")
    ]
    picker = (
        ft.PopupMenuButton(
            content=ft.Container(
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_SNUG,
                    vertical=tokens.SPACE_XXS,
                ),
                border_radius=tokens.RADIUS_PILL,
                bgcolor=theme.surface_2(is_dark),
                border=ft.Border.all(1, theme.border(is_dark)),
                content=ft.Row(
                    spacing=tokens.SPACE_TIGHT,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(ft.Icons.DNS_ROUNDED, size=tokens.ICON_SM, color=theme.PRIMARY),
                        ft.Text(
                            model,
                            size=tokens.FONT_SM,
                            weight=ft.FontWeight.W_600,
                            color=theme.text_color(is_dark),
                        ),
                        ft.Icon(
                            ft.Icons.EXPAND_MORE_ROUNDED,
                            size=tokens.ICON_XS,
                            color=theme.dim(is_dark),
                        ),
                    ],
                ),
            ),
            tooltip="Pick a model",
            items=items,
        )
        if items
        else ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=tokens.SPACE_SNUG,
                vertical=tokens.SPACE_XXS,
            ),
            border_radius=tokens.RADIUS_PILL,
            bgcolor=theme.surface_2(is_dark),
            content=ft.Text(
                "No models yet — start the gateway on the Server tab.",
                size=tokens.FONT_SM,
                color=theme.dim(is_dark),
            ),
        )
    )

    # ── transcript: reasoning + answer + the RAW request/response JSON ───
    def _tech_block(title: str, body: str) -> ft.Control:
        return ft.Column(
            spacing=tokens.SPACE_XXS,
            tight=True,
            controls=[
                ft.Text(
                    title,
                    size=tokens.FONT_2XS,
                    weight=ft.FontWeight.W_600,
                    color=theme.dim(is_dark),
                ),
                ft.Container(
                    width=float("inf"),
                    padding=ft.Padding.all(tokens.SPACE_SNUG),
                    border_radius=tokens.RADIUS_SM,
                    bgcolor=theme.surface_2(is_dark),
                    content=ft.Text(
                        body,
                        size=tokens.FONT_2XS,
                        font_family="monospace",
                        color=theme.text_color(is_dark),
                        selectable=True,
                    ),
                ),
            ],
        )

    rows: list[ft.Control] = []
    thinking_now = busy and not answer
    if reasoning or thinking_now:
        rows.append(ThinkingBlock(reasoning=reasoning, streaming=thinking_now, is_dark=is_dark))
    if answer:
        rows.append(theme.markdown(answer, is_dark=is_dark))
    if error:
        rows.append(
            ft.Row(
                spacing=tokens.SPACE_SM,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Icon(ft.Icons.ERROR, size=tokens.ICON_SM, color=ft.Colors.ERROR),
                    ft.Text(
                        error,
                        size=tokens.FONT_MD,
                        color=ft.Colors.ERROR,
                        selectable=True,
                        expand=True,
                    ),
                ],
            ),
        )
    if request_json:
        rows.append(_tech_block("REQUEST", request_json))
    if response_json:
        rows.append(_tech_block("RESPONSE", response_json))
    if meta_line:
        rows.append(
            ft.Text(
                meta_line,
                size=tokens.FONT_XS,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
        )
    if not rows and not busy:
        rows.append(
            ft.Text(
                "Pick a model, type a prompt, and send.",
                size=tokens.FONT_MD,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
        )

    # ── composer, docked to the BOTTOM (DDGS/CollabShell design) ─────────
    composer = ft.Container(
        padding=ft.Padding.symmetric(horizontal=tokens.SPACE_MD, vertical=tokens.SPACE_SM),
        content=ft.Row(
            spacing=tokens.SPACE_SM,
            vertical_alignment=ft.CrossAxisAlignment.END,
            controls=[
                ft.TextField(
                    value=draft,
                    hint_text="Ask anything",
                    expand=True,
                    dense=True,
                    multiline=True,
                    min_lines=1,
                    max_lines=5,
                    # Enter sends; Shift+Enter adds the newline. On a
                    # multiline field WITHOUT this flag, flet never fires
                    # on_submit (documented in flet 1.x).
                    shift_enter=True,
                    fill_color=ft.Colors.with_opacity(0.06, ft.Colors.ON_SURFACE),
                    content_padding=ft.Padding.all(tokens.SPACE_MD),
                    border=ft.OutlineInputBorder(border_radius=tokens.RADIUS_LG),
                    on_change=lambda e: set_draft(str(e.control.value or "")),
                    on_submit=_send,
                ),
                # One circular button, two faces: send when idle, stop when
                # a probe is in flight (Stop cancels the in-flight request).
                ft.IconButton(
                    icon=ft.Icons.STOP_ROUNDED if busy else ft.Icons.SEND_ROUNDED,
                    icon_color=ft.Colors.WHITE,
                    tooltip="Stop" if busy else "Send",
                    disabled=not busy and not draft.strip(),
                    style=ft.ButtonStyle(shape=ft.CircleBorder(), bgcolor=theme.PRIMARY),
                    on_click=lambda _e: _stop_probe() if busy else _send(),
                ),
            ],
        ),
    )

    return ft.Column(
        expand=True,
        spacing=0,
        controls=[
            ft.Container(
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_LG,
                    vertical=tokens.SPACE_SM,
                ),
                content=ft.Row(
                    spacing=tokens.SPACE_SM,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[picker, ft.Container(expand=True)],
                ),
            ),
            ft.Container(
                expand=True,
                padding=ft.Padding.symmetric(horizontal=tokens.SPACE_LG, vertical=tokens.SPACE_SM),
                content=ft.ListView(
                    expand=True,
                    spacing=tokens.SPACE_MD,
                    controls=rows,
                    auto_scroll=False,
                ),
            ),
            build_banner_ad(),
            composer,
        ],
    )
