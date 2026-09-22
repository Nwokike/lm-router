"""Chat screen: streaming messages, model picker, composer, stop control."""

import flet as ft

from components.banner_ad import build_banner_ad
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def ChatScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    draft, set_draft = ft.use_state("")

    def send() -> None:
        value = draft.strip()
        if not value:
            return
        set_draft("")
        methods.send_message(value)

    def on_field_submit(_: ft.ControlEvent) -> None:
        send()

    def on_field_change(e: ft.ControlEvent) -> None:
        set_draft(str(e.control.value or ""))

    model_ids = [m["id"] for m in state.models if m.get("id")]
    header = ft.Row(
        spacing=8,
        controls=[
            ft.Dropdown(
                value=state.model or None,
                options=model_ids,
                label="Model",
                expand=True,
                text_size=13,
                on_select=lambda e: methods.set_model(str(e.control.value or "")),
            ),
            ft.ProgressRing(width=16, height=16, stroke_width=2)
            if state.busy
            else ft.SizedBox(width=16, height=16),
        ],
    )

    rows: list[ft.Control] = []
    last_index = len(state.messages) - 1
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
                            content=ft.Text(content, size=14),
                            bgcolor=ft.Colors.PRIMARY_CONTAINER,
                            color=ft.Colors.ON_PRIMARY_CONTAINER,
                            border_radius=14,
                            padding=10,
                        )
                    ],
                )
            )
        elif role == "tool":
            rows.append(
                ft.Container(
                    padding=10,
                    border_radius=10,
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    content=ft.Column(
                        spacing=4,
                        controls=[
                            ft.Row(
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Icon(
                                        ft.Icons.SEARCH_ROUNDED,
                                        size=16,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                    ft.Text(
                                        message.get("name", "tool"),
                                        size=12,
                                        family="monospace",
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    ft.Text(
                                        "error" if message.get("is_error") else "tool",
                                        size=10,
                                        color=ft.Colors.ERROR
                                        if message.get("is_error")
                                        else ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                ],
                            ),
                            ft.Text(
                                message.get("content", ""),
                                size=12,
                                selectable=True,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                    ),
                )
            )
        elif role == "error":
            rows.append(
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.Icon(ft.Icons.ERROR, size=18, color=ft.Colors.ERROR),
                        ft.Text(content, size=14, color=ft.Colors.ERROR, selectable=True),
                    ],
                )
            )
        else:
            placeholder = not content and state.busy and index == last_index
            body: ft.Control
            if placeholder:
                body = ft.ProgressRing(width=16, height=16, stroke_width=2)
            else:
                body = ft.Markdown(
                    content or "…",
                    selectable=True,
                    code_theme=ft.MarkdownCodeTheme.MONOKAI,
                )
            usage = message.get("usage")
            caption = None
            if usage:
                in_tokens = usage.get("prompt_tokens", 0)
                out_tokens = usage.get("completion_tokens", 0)
                caption = ft.Text(
                    f"in {in_tokens} · out {out_tokens} tokens",
                    size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
            elif message.get("stopped"):
                caption = ft.Text("stopped", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
            rows.append(
                ft.Column(
                    spacing=4,
                    controls=[body] + ([caption] if caption is not None else []),
                )
            )

    if state.busy:
        last_role = state.messages[-1].get("role") if state.messages else None
        if last_role in ("user", "tool"):
            rows.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=[ft.ProgressRing(width=16, height=16, stroke_width=2)],
                )
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
                        else ft.SizedBox(),
                    ],
                ),
            )
        )

    composer = ft.Container(
        padding=12,
        content=ft.Row(
            spacing=8,
            controls=[
                ft.TextField(
                    value=draft,
                    hint_text="Message… (Enter sends)",
                    min_lines=1,
                    max_lines=5,
                    expand=True,
                    text_size=14,
                    on_change=on_field_change,
                    on_submit=on_field_submit,
                ),
                ft.FilledIconButton(
                    ft.Icons.SEND,
                    tooltip="Send",
                    disabled=state.busy,
                    on_click=lambda _: send(),
                )
                if not state.busy
                else ft.OutlinedIconButton(
                    ft.Icons.STOP,
                    tooltip="Stop generating",
                    on_click=lambda _: methods.stop_generation(),
                ),
            ],
        ),
    )

    return ft.Column(
        expand=True,
        spacing=0,
        controls=[
            ft.Container(padding=ft.padding.symmetric(horizontal=16, vertical=12), content=header),
            ft.ListView(
                expand=True,
                spacing=12,
                padding=ft.padding.symmetric(horizontal=16, vertical=8),
                controls=rows,
                auto_scroll=True,
                auto_scroll_animation=0,
            ),
            composer,
            build_banner_ad(),
        ],
    )
