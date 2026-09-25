"""Chat chrome: the composer and the session bar.

The composer owns its own draft state. That is the whole point: while `draft`
lived in ChatScreen, every keystroke re-ran the screen body and rebuilt (and
re-parsed the markdown of) the entire transcript. Isolating it means typing
only re-renders the input.

The session bar replaces the old full-width Dropdown: a single compact strip of
pills for model, search and MCP, each opening a rich menu. It uses the
PopupMenuButton + PopupMenuItem(on_click=...) pattern already proven in the
owner's KTV Player, which deliberately avoids reading the selection from
`e.data` — the exact mistake that made this app's tabs dead.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import flet as ft

from core import theme, tokens
from core.catalog import (
    chat_models,
    endpoint_label,
    is_auto,
    model_label,
    rate_hint_label,
)

_MAX_ATTACH_CHARS = 100_000


def _pill(
    label: str,
    *,
    icon=None,
    icon_control=None,
    active: bool = False,
    is_dark: bool = True,
) -> ft.Container:
    """A compact, tappable status pill.

    `icon` is an icon CODE (wrapped in ft.Icon); `icon_control` is an already
    built control such as a ProgressRing. Passing a control as `icon` raises
    "type 'Control' is not a subtype of type 'int?'" at render time.
    """
    fg = theme.PRIMARY if active else theme.dim(is_dark)
    lead: list[ft.Control] = []
    if icon_control is not None:
        lead.append(icon_control)
    elif icon is not None:
        lead.append(ft.Icon(icon, size=14, color=fg))
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=tokens.RADIUS_PILL,
        bgcolor=ft.Colors.with_opacity(0.12 if active else 0.06, theme.PRIMARY if active else fg),
        content=ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                *lead,
                ft.Text(
                    label,
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.W_600,
                    color=fg,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Icon(
                    ft.Icons.EXPAND_MORE_ROUNDED,
                    size=14,
                    color=ft.Colors.with_opacity(0.7, fg),
                ),
            ],
        ),
    )


@ft.component
def ModelPicker(
    *,
    state,
    methods,
    is_dark: bool,
):
    """Model pill + menu. `auto` (the router's rotating model) comes first.

    Every startup circumstance gets its own honest label; the picker must never
    say "Select model"/"No model" while the gateway is still coming up, because
    an empty list during discovery reads as a broken app rather than a wait.
    """
    models = chat_models([m for m in state.models if isinstance(m, dict)])
    current = next((m for m in models if str(m.get("id")) == state.model), None)

    discovering = state.gateway_running and not models
    if current:
        label = model_label(current)
    elif discovering:
        label = "Starting gateway…"
    elif not state.gateway_running:
        label = "Gateway stopped"
    elif state.models:
        label = "No chat models yet"
    else:
        label = "Loading models…"

    items: list[ft.PopupMenuItem] = []
    for model in models:
        model_id = str(model.get("id") or "")
        hint = rate_hint_label(model)
        selected = model_id == state.model
        caption = endpoint_label(model)
        if hint:
            caption = f"{caption} · {hint}"
        items.append(
            ft.PopupMenuItem(
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            ft.Icons.AUTO_AWESOME_ROUNDED
                            if is_auto(model)
                            else ft.Icons.CHAT_BUBBLE_OUTLINE,
                            size=15,
                            color=theme.PRIMARY if selected else theme.dim(is_dark),
                        ),
                        ft.Column(
                            spacing=0,
                            tight=True,
                            expand=True,
                            controls=[
                                ft.Text(
                                    label if is_auto(model) else model_id,
                                    size=tokens.FONT_BODY_SM,
                                    weight=ft.FontWeight.W_600 if selected else ft.FontWeight.W_400,
                                    color=theme.text_color(is_dark),
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    caption,
                                    size=10,
                                    color=theme.dim(is_dark),
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                        ),
                    ],
                ),
                on_click=lambda e, mid=model_id: methods.set_model(mid),
            ),
        )

    if not items:
        # Never a bare "No model": say what is happening and offer the action
        # that actually helps in that state.
        if discovering:
            message, action = "Discovering available models…", None
        elif not state.gateway_running:
            message, action = "The gateway is stopped.", "Start gateway"
        elif state.models:
            message, action = (
                "No chat-capable models right now. Other endpoint types are "
                "listed on the Server tab.",
                "Refresh models",
            )
        else:
            message, action = "No models reported by the gateway.", "Refresh models"
        items.append(
            ft.PopupMenuItem(
                content=ft.Column(
                    spacing=2,
                    tight=True,
                    controls=[
                        ft.Text(
                            message,
                            size=tokens.FONT_BODY_SM,
                            color=theme.dim(is_dark),
                        ),
                        *(
                            [
                                ft.Text(
                                    action,
                                    size=11,
                                    weight=ft.FontWeight.W_600,
                                    color=theme.PRIMARY,
                                ),
                            ]
                            if action
                            else []
                        ),
                    ],
                ),
                on_click=(
                    (lambda e: methods.start_gateway())
                    if action == "Start gateway"
                    else (None if action is None else (lambda e: methods.refresh_models()))
                ),
            ),
        )

    pill_icon = (
        ft.ProgressRing(width=13, height=13, stroke_width=2)
        if discovering
        else (ft.Icons.AUTO_AWESOME_ROUNDED if is_auto(current) else ft.Icons.SMART_TOY_OUTLINED)
    )
    return ft.PopupMenuButton(
        content=_pill(
            label,
            icon_control=(pill_icon if discovering else None),
            icon=None if discovering else pill_icon,
            active=True,
            is_dark=is_dark,
        ),
        items=items,
        menu_position=ft.PopupMenuPosition.UNDER,
        tooltip="Choose a model",
    )


@ft.component
def SessionBar(
    *,
    state,
    methods,
    is_dark: bool,
):
    """Model, search health and MCP count in one compact strip."""
    model_pill = ModelPicker(state=state, methods=methods, is_dark=is_dark)

    search_pill = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=tokens.RADIUS_PILL,
        bgcolor=ft.Colors.with_opacity(
            0.12 if state.search_enabled else 0.06,
            theme.PRIMARY if state.search_enabled else theme.dim(is_dark),
        ),
        on_click=lambda _: methods.toggle_search_tool(),
        tooltip="Toggle web search for this chat",
        content=ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(
                    ft.Icons.PUBLIC_ROUNDED
                    if state.search_enabled
                    else ft.Icons.PUBLIC_OFF_ROUNDED,
                    size=14,
                    color=theme.PRIMARY if state.search_enabled else theme.dim(is_dark),
                ),
                ft.Text(
                    "Internet",
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.W_600,
                    color=theme.PRIMARY if state.search_enabled else theme.dim(is_dark),
                ),
            ],
        ),
    )

    mcp_count = len(state.mcp_tools)
    mcp_pill = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=tokens.RADIUS_PILL,
        bgcolor=ft.Colors.with_opacity(
            0.12 if mcp_count else 0.06,
            theme.PRIMARY if mcp_count else theme.dim(is_dark),
        ),
        on_click=lambda _: methods.open_mcp_tools(),
        tooltip="MCP tools",
        content=ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(
                    ft.Icons.HUB_ROUNDED if mcp_count else ft.Icons.HUB_OUTLINED,
                    size=14,
                    color=theme.PRIMARY if mcp_count else theme.dim(is_dark),
                ),
                ft.Text(
                    f"MCP {mcp_count}" if mcp_count else "MCP",
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.W_600,
                    color=theme.PRIMARY if mcp_count else theme.dim(is_dark),
                ),
            ],
        ),
    )

    return ft.Container(
        width=float("inf"),
        padding=ft.Padding.symmetric(horizontal=16, vertical=8),
        content=ft.Row(
            spacing=tokens.SPACE_SM,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[model_pill, search_pill, mcp_pill],
        ),
    )


@ft.component
def Composer(
    *,
    busy: bool,
    search_enabled: bool,
    mcp_count: int,
    on_send: Callable[[str], None],
    on_stop: Callable[[], None],
    on_toggle_search: Callable[[], None],
    on_open_mcp: Callable[[], None],
):
    """Message input. Draft state is local so typing never rebuilds the thread."""
    draft, set_draft = ft.use_state("")

    # Text-file attachment: stdlib read, inlined as a fenced block. Size is
    # capped so one file cannot eat the whole context window.
    attachment, set_attachment = ft.use_state(None)

    def _attach(_e: ft.ControlEvent) -> None:
        page = getattr(ft.context, "page", None)
        if page is None:
            return

        async def _pick() -> None:
            picker = ft.FilePicker()
            page.services.append(picker)
            files = await picker.pick_files(
                dialog_title="Attach a text file",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[
                    "txt",
                    "md",
                    "py",
                    "json",
                    "csv",
                    "log",
                    "yaml",
                    "yml",
                    "html",
                    "css",
                    "js",
                    "ts",
                    "sh",
                    "toml",
                ],
                allow_multiple=False,
                with_data=True,
            )
            if not files:
                return
            picked = files[0]
            raw = picked.bytes
            if raw is None and picked.path:
                raw = pathlib.Path(picked.path).read_bytes()
            text = (raw or b"").decode("utf-8", errors="replace")
            if len(text) > _MAX_ATTACH_CHARS:
                text = text[:_MAX_ATTACH_CHARS] + "\n… (truncated)"
            set_attachment((picked.name, text))

        page.run_task(_pick)

    def submit() -> None:
        value = draft.strip()
        if not value and not attachment:
            return
        if attachment:
            name, text = attachment
            value = f"[Attached file: {name}]\n\n```\n{text}\n```\n\n{value}".strip()
        set_draft("")
        set_attachment(None)
        on_send(value)

    def on_change(e: ft.ControlEvent) -> None:
        set_draft(str(e.control.value or ""))

    def on_submit(_: ft.ControlEvent) -> None:
        submit()

    # No Internet/MCP chips here on purpose: the SessionBar above the thread
    # already owns both. Duplicating them left the same controls on screen
    # twice and made it unclear which one was live.
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        content=ft.Column(
            spacing=tokens.SPACE_XS,
            controls=[
                *(
                    [
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            controls=[
                                ft.Chip(
                                    label=f"📄 {attachment[0]}",
                                    on_delete=lambda _e: set_attachment(None),
                                ),
                            ],
                        ),
                    ]
                    if attachment
                    else []
                ),
                ft.Row(
                    spacing=tokens.SPACE_SM,
                    controls=[
                        ft.IconButton(
                            ft.Icons.ATTACH_FILE_ROUNDED,
                            tooltip="Attach a text file",
                            on_click=_attach,
                        ),
                        ft.TextField(
                            value=draft,
                            hint_text="Message LM Router…",
                            multiline=True,
                            shift_enter=True,
                            min_lines=1,
                            max_lines=6,
                            expand=True,
                            text_size=tokens.FONT_MD,
                            # TextField.border_radius is deprecated in flet 1.0;
                            # the border object is the supported form.
                            border=ft.OutlineInputBorder(
                                border_radius=tokens.RADIUS_LG,
                            ),
                            on_change=on_change,
                            on_submit=on_submit,
                        ),
                        (
                            ft.FilledIconButton(
                                ft.Icons.ARROW_UPWARD_ROUNDED,
                                tooltip="Send",
                                icon_size=20,
                                disabled=busy,
                                on_click=lambda _: submit(),
                            )
                            if not busy
                            else ft.OutlinedIconButton(
                                ft.Icons.STOP_ROUNDED,
                                tooltip="Stop generating",
                                icon_size=20,
                                on_click=lambda _: on_stop(),
                            )
                        ),
                    ],
                ),
            ],
        ),
    )
