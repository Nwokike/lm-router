"""Chat screen: streaming messages, model picker, composer, stop control."""

from collections.abc import Callable

import flet as ft

from components.banner_ad import build_banner_ad
from components.chat_controls import Composer, SessionBar
from components.thinking import ThinkingBlock, ToolCallBlock
from core import theme as app_theme
from core import tokens
from core.catalog import rate_limit_suggestion
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx

# Keep ads in the conversation, but spaced so a long chat does not become a
# wall of banners.  The owner-facing default is one banner per assistant reply.
# A banner is never the last control of the ListView: a banner ad stranded at
# the floor of the thread (with nothing under it but the composer) reads as
# chrome the app forgot to clear, so the modulo check is ANDed with
# "this reply is not the final message".
BANNER_AD_EVERY_N_REPLIES = 1


def _open_edit_dialog(current_text: str) -> None:
    """Edit-and-resend the last user turn via a small dialog."""
    page = getattr(ft.context, "page", None)
    if page is None:
        return
    # House escape hatch (DDGS stashes its controller the same way): the
    # dialog runs outside the component tree, so use_context is unavailable.
    controller = getattr(page, "_lmrouter_controller", None)
    if controller is None:
        return
    field = ft.TextField(
        value=current_text,
        multiline=True,
        min_lines=3,
        max_lines=8,
        autofocus=True,
    )

    def _submit(_e: ft.ControlEvent) -> None:
        page.pop_dialog()
        controller.edit_last_user(str(field.value or ""))

    page.show_dialog(
        ft.AlertDialog(
            modal=False,
            title=ft.Text("Edit & resend", size=tokens.FONT_TITLE, weight=ft.FontWeight.W_600),
            # 420: tokens.DIALOG_WIDTH_LG is gone from the current tokens.py
            # (only MD=400 / XL=560 remain), so the literal stays for now.
            content=ft.Container(content=field, width=420),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: page.pop_dialog()),
                ft.FilledButton("Save & resend", on_click=_submit),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ),
    )


def _with_menu(control: ft.Control, actions: list[tuple[str, Callable[[], None]]]) -> ft.Control:
    """Wrap a bubble in a long-press / right-click menu.

    Mobile gets long-press (the standard chat gesture); desktop gets right
    click. `actions` are (label, callback) pairs; empty actions return the
    control untouched.
    """
    if not actions:
        return control
    items = [
        ft.PopupMenuItem(content=ft.Row([ft.Text(label, size=tokens.FONT_BODY_SM)]))
        for label, _callback in actions
    ]

    def _selected(e: ft.ControlEvent) -> None:
        index = getattr(e, "item_index", None)
        if index is not None and 0 <= int(index) < len(actions):
            actions[int(index)][1]()

    return ft.ContextMenu(
        content=control,
        items=items,
        # Each trigger reads ITS OWN list in flet 1.0 (`items=` is used only
        # by programmatic open()), and the touch path additionally requires
        # primary_trigger to be armed — with either missing, long-press and
        # right-click silently fire dismiss with an empty menu.
        primary_items=items,
        primary_trigger=ft.ContextMenuTrigger.LONG_PRESS,
        secondary_items=items,
        on_select=_selected,
    )


# Label → icon for the visible action row (DDGS ships the same three).
_ACTION_ICONS = {
    "Copy": ft.Icons.CONTENT_COPY_ROUNDED,
    "Edit & resend": ft.Icons.EDIT_ROUNDED,
    "Regenerate": ft.Icons.REFRESH_ROUNDED,
}


def _action_row(actions: list[tuple[str, Callable[[], None]]]) -> ft.Control:
    """Visible Copy/Edit/Regenerate buttons under a bubble.

    DDGS renders these as a plain row because long press alone is not
    discoverable and has no keyboard equivalent; the gesture menu stays as
    a shortcut, this row is the primary path. Empty actions yield an empty
    row so callers don't need a branch.
    """
    return ft.Row(
        spacing=0,
        controls=[
            ft.IconButton(
                _ACTION_ICONS.get(label, ft.Icons.MORE_HORIZ),
                icon_size=tokens.ICON_SM,
                icon_color=ft.Colors.ON_SURFACE_VARIANT,
                tooltip=label,
                on_click=lambda _e, cb=callback: cb(),
            )
            for label, callback in actions
        ],
    )


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
            # expand=True here, not inside SessionBar: a scrollable strip only
            # scrolls once something constrains its width. Letting SessionBar
            # claim an unbounded width instead pushed this label and the
            # spinner off-screen whenever a model had a long name.
            ft.Container(
                expand=True,
                content=SessionBar(state=state, methods=methods, is_dark=is_dark_page),
            ),
            (
                ft.Row(
                    spacing=tokens.SPACE_XS,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            ft.Icons.DATA_SAVER_ON_ROUNDED,
                            size=tokens.ICON_XS,
                            color=ft.Colors.PRIMARY,
                        ),
                        ft.Text(
                            context_label,
                            size=tokens.FONT_XS,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                )
                if context_label
                else ft.Container()
            ),
            (
                ft.ProgressRing(width=tokens.ICON_XS, height=tokens.ICON_XS, stroke_width=2)
                if state.busy
                else ft.Container()
            ),
        ],
    )

    def _error_actions(kind: str) -> list[ft.Control]:
        """Recovery affordances for a failed turn (DDGS parity).

        A dead red line tells the user nothing about what to do next; every
        error kind names its own way forward. Rows without a kind (legacy)
        render unchanged.
        """
        if not kind:
            return []
        if kind == "rate_limited":
            suggestion = rate_limit_suggestion(state.model, state.models)
            if suggestion:

                def _switch_and_retry(*_a: object, s: str = suggestion) -> None:
                    methods.set_model(s)
                    methods.regenerate_last()

                return [
                    ft.TextButton(
                        f"Use {suggestion} and retry",
                        on_click=_switch_and_retry,
                    ),
                ]
        if kind == "offline":
            return [
                ft.TextButton(
                    "Start gateway",
                    on_click=lambda *_a: methods.start_gateway(),
                ),
            ]
        return [
            ft.TextButton("Try again", on_click=lambda *_a: methods.regenerate_last()),
        ]

    rows: list[ft.Control] = []
    last_index = len(state.messages) - 1
    last_user_index = max(
        (i for i, m in enumerate(state.messages) if m.get("role") == "user"), default=-1
    )
    last_assistant_index = max(
        (i for i, m in enumerate(state.messages) if m.get("role") == "assistant"),
        default=-1,
    )
    is_dark = is_dark_page
    assistant_replies = 0
    for index, message in enumerate(state.messages):
        role = message.get("role", "assistant")
        content = message.get("content", "")
        if role == "user":
            bubble = ft.Container(
                # Container has no `color` in flet 1.0 — the text
                # colour belongs on the Text child.
                content=ft.Text(
                    content,
                    size=tokens.FONT_MD,
                    color=ft.Colors.ON_PRIMARY_CONTAINER,
                ),
                bgcolor=ft.Colors.PRIMARY_CONTAINER,
                border_radius=tokens.RADIUS_BUBBLE,
                padding=ft.Padding.all(tokens.SPACE_SNUG),
            )
            user_actions: list[tuple[str, Callable[[], None]]] = [
                ("Copy", lambda c=content: methods.copy_text(c))
            ]
            if index == last_user_index and not state.busy:
                user_actions.append(
                    (
                        "Edit & resend",
                        lambda c=content: _open_edit_dialog(c),
                    ),
                )
            rows.append(
                ft.Row(
                    expand=True,
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.Column(
                            spacing=tokens.SPACE_XXS,
                            horizontal_alignment=ft.CrossAxisAlignment.END,
                            controls=[
                                _with_menu(bubble, user_actions),
                                _action_row(user_actions),
                            ],
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
                ft.Column(
                    spacing=tokens.SPACE_XXS,
                    controls=[
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            controls=[
                                ft.Icon(
                                    ft.Icons.ERROR,
                                    size=tokens.ICON_SM,
                                    color=ft.Colors.ERROR,
                                ),
                                ft.Text(
                                    content,
                                    size=tokens.FONT_MD,
                                    color=ft.Colors.ERROR,
                                    selectable=True,
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=tokens.SPACE_XS,
                            controls=_error_actions(str(message.get("kind") or "")),
                        ),
                    ],
                ),
            )
        else:
            placeholder = not content and state.busy and index == last_index
            body: ft.Control
            if placeholder:
                body = ft.ProgressRing(width=tokens.ICON_XS, height=tokens.ICON_XS, stroke_width=2)
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
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
                if caption_parts
                else None
            )

            meta_controls: list[ft.Control] = []
            if caption is not None:
                meta_controls.append(caption)
            # Copy lives in the visible action row below (one copy path, the
            # DDGS model) — no second button here.

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

            asst_actions: list[tuple[str, Callable[[], None]]] = []
            if content:
                asst_actions.append(("Copy", lambda c=content: methods.copy_text(c)))
            if index == last_assistant_index and not state.busy:
                asst_actions.append(("Regenerate", lambda: methods.regenerate_last()))
            rows.append(
                ft.Column(
                    spacing=tokens.SPACE_XXS,
                    controls=[
                        _with_menu(
                            ft.Column(
                                spacing=tokens.SPACE_XS,
                                controls=assistant_controls
                                + (
                                    [
                                        ft.Row(
                                            spacing=tokens.SPACE_XS,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=meta_controls,
                                        ),
                                    ]
                                    if meta_controls
                                    else []
                                ),
                            ),
                            asst_actions,
                        ),
                        _action_row(asst_actions),
                    ],
                ),
            )
            assistant_replies += 1
            # Never-last rule: a banner is skipped when this reply is the final
            # message, so the bottom of the thread is always a real message.
            if assistant_replies % BANNER_AD_EVERY_N_REPLIES == 0 and index < last_index:
                rows.append(build_banner_ad())

    if state.busy:
        last_role = state.messages[-1].get("role") if state.messages else None
        if last_role in ("user", "tool"):
            rows.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=[
                        ft.ProgressRing(width=tokens.ICON_XS, height=tokens.ICON_XS, stroke_width=2)
                    ],
                ),
            )

    if not rows:
        rows.append(
            ft.Container(
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.all(tokens.SPACE_XL),
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=tokens.SPACE_SM,
                    controls=[
                        ft.Icon(
                            ft.Icons.CHAT_BUBBLE_OUTLINE,
                            size=tokens.ICON_XL,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            "Ask anything."
                            if state.gateway_running
                            else "Gateway is offline. Start it on the Server tab.",
                            size=tokens.FONT_MD,
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
                spacing=tokens.SPACE_MD,
                padding=ft.Padding.symmetric(horizontal=tokens.SPACE_LG, vertical=tokens.SPACE_SM),
                controls=rows,
                auto_scroll=True,
                auto_scroll_animation=0,
            ),
            composer,
        ],
    )
