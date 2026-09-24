"""Chat screen: streaming messages, model picker, composer, stop control."""

import flet as ft

from components.banner_ad import build_banner_ad
from components.chat_controls import Composer, SessionBar
from components.thinking import ThinkingBlock, ToolCallBlock
from core import theme as app_theme
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def ChatScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    page = getattr(ft.context, "page", None)
    is_dark_page = app_theme.is_dark_mode(page, state.theme_mode)

    # Context usage readout, shown only once a turn has reported tokens.
    context_label = (
        f"~{state.context_used_tokens / 1000:.1f}k ctx" if state.context_used_tokens > 0 else ""
    )

    session_bar = ft.Row(
        spacing=0,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            SessionBar(state=state, methods=methods, is_dark=is_dark_page),
            ft.Container(expand=True),
            (
                ft.Row(
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            ft.Icons.DATA_SAVER_ON_ROUNDED,
                            size=14,
                            color=ft.Colors.PRIMARY,
                        ),
                        ft.Text(
                            context_label,
                            size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                )
                if context_label
                else ft.Container()
            ),
            ft.ProgressRing(width=16, height=16, stroke_width=2) if state.busy else ft.Container(),
        ],
    )

    rows: list[ft.Control] = []
    last_index = len(state.messages) - 1
    is_dark = is_dark_page
    for index, message in enumerate(state.messages):
        role = message.get("role", "assistant")
        content = message.get("content", "")
        if role == "user":
            rows.append(
                ft.Row(
                    expand=True,
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.Container(
                            # Container has no `color` in flet 1.0 — the text
                            # colour belongs on the Text child.
                            content=ft.Text(
                                content,
                                size=14,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
                            ),
                            bgcolor=ft.Colors.PRIMARY_CONTAINER,
                            border_radius=14,
                            padding=10,
                        ),
                    ],
                ),
            )
        elif role == "tool":
            rows.append(
                ToolCallBlock(
                    name=str(message.get("name", "tool")),
                    content=str(message.get("content", "")),
                    is_error=bool(message.get("is_error")),
                    is_dark=is_dark,
                ),
            )
        elif role == "error":
            rows.append(
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.Icon(ft.Icons.ERROR, size=18, color=ft.Colors.ERROR),
                        ft.Text(content, size=14, color=ft.Colors.ERROR, selectable=True),
                    ],
                ),
            )
        else:
            placeholder = not content and state.busy and index == last_index
            body: ft.Control
            if placeholder:
                body = ft.ProgressRing(width=16, height=16, stroke_width=2)
            else:
                body = app_theme.markdown(content or "…", is_dark=is_dark)
            usage = message.get("usage")
            finish_reason = usage.get("finish_reason") if usage else None
            caption_parts: list[str] = []
            if usage:
                in_tokens = usage.get("prompt_tokens", 0)
                out_tokens = usage.get("completion_tokens", 0)
                reasoning = usage.get("reasoning_tokens")
                cached = usage.get("cached_tokens")
                tokens_text = f"in {in_tokens} · out {out_tokens}"
                if reasoning:
                    tokens_text += f" (reasoning {reasoning})"
                if cached:
                    tokens_text += f" (cached {cached})"
                tokens_text += " tokens"
                caption_parts.append(tokens_text)
            if message.get("stopped"):
                caption_parts.append("stopped")
            if finish_reason == "length":
                caption_parts.append("truncated (length limit)")

            caption = (
                ft.Text(
                    " · ".join(caption_parts),
                    size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
                if caption_parts
                else None
            )

            meta_controls: list[ft.Control] = []
            if caption is not None:
                meta_controls.append(caption)
            if content and not placeholder:
                meta_controls.append(
                    ft.IconButton(
                        ft.Icons.CONTENT_COPY_ROUNDED,
                        icon_size=13,
                        tooltip="Copy message",
                        on_click=lambda e, txt=content: methods.copy_text(txt),
                    ),
                )

            assistant_controls: list[ft.Control] = []
            # Reasoning sits ABOVE the answer and collapses itself the moment
            # the answer starts, so it never pushes the reply off-screen.
            reasoning_text = str(message.get("reasoning") or "")
            if reasoning_text:
                assistant_controls.append(
                    ThinkingBlock(
                        reasoning=reasoning_text,
                        # Still thinking only while no answer text has arrived.
                        streaming=bool(state.busy and not content and index == last_index),
                        is_dark=is_dark,
                    ),
                )
            assistant_controls.append(body)

            rows.append(
                ft.Column(
                    spacing=4,
                    controls=assistant_controls
                    + (
                        [
                            ft.Row(
                                spacing=4,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=meta_controls,
                            ),
                        ]
                        if meta_controls
                        else []
                    ),
                ),
            )

    if state.busy:
        last_role = state.messages[-1].get("role") if state.messages else None
        if last_role in ("user", "tool"):
            rows.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=[ft.ProgressRing(width=16, height=16, stroke_width=2)],
                ),
            )

    if not rows:
        rows.append(
            ft.Container(
                alignment=ft.Alignment.CENTER,
                padding=24,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Icon(
                            ft.Icons.CHAT_BUBBLE_OUTLINE,
                            size=40,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            "Ask anything."
                            if state.gateway_running
                            else "Gateway is offline. Start it on the Server tab.",
                            size=14,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.FilledButton(
                            "Start gateway",
                            icon=ft.Icons.PLAY_ARROW,
                            on_click=lambda _: methods.start_gateway(),
                        )
                        if not state.gateway_running
                        else ft.Container(),
                    ],
                ),
            ),
        )

    # Composer tool chips (ChatGPT-style composer-level toggles):
    # "Internet" = hosted web search; "MCP" opens the per-tool switch dialog.
    composer = Composer(
        busy=state.busy,
        search_enabled=state.search_enabled,
        mcp_count=len(state.mcp_tools),
        on_send=methods.send_message,
        on_stop=methods.stop_generation,
        on_toggle_search=methods.toggle_search_tool,
        on_open_mcp=methods.open_mcp_tools,
    )

    return ft.Column(
        expand=True,
        spacing=0,
        controls=[
            session_bar,
            ft.ListView(
                expand=True,
                spacing=12,
                padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                controls=rows,
                auto_scroll=True,
                auto_scroll_animation=0,
            ),
            composer,
            build_banner_ad(),
        ],
    )
