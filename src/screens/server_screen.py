"""Server screen: gateway status, controls and the live redacted log ring."""

import flet as ft

from components.banner_ad import build_banner_ad
from core import logging as applog
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx

_LEVEL_COLORS = {
    "INFO": None,
    "WARNING": ft.Colors.ON_SURFACE_VARIANT,
    "ERROR": ft.Colors.ERROR,
    "DEBUG": ft.Colors.ON_SURFACE_VARIANT,
}


def _fmt_uptime(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@ft.component
def ServerScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    _ = state.log_version  # subscribe: bumping log_version re-renders this screen

    running = state.gateway_running
    status_color = ft.Colors.GREEN if running else ft.Colors.ERROR
    status_text = "running" if running else "stopped"

    log_rows = []
    for record in applog.records():
        color = _LEVEL_COLORS.get(record["level"])
        log_rows.append(
            ft.Text(
                f"{record['ts']}  {record['level']:<7}  {record['msg']}",
                size=12,
                font_family="monospace",
                max_lines=1,
                overflow=ft.TextOverflow.CLIP,
                selectable=True,
                color=color,
            )
        )
    if not log_rows:
        log_rows.append(ft.Text("No log output yet.", size=13, color=ft.Colors.ON_SURFACE_VARIANT))

    return ft.Column(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        spacing=16,
        controls=[
            ft.Container(
                padding=16,
                border_radius=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Column(
                    spacing=12,
                    controls=[
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.Container(
                                    width=10,
                                    height=10,
                                    border_radius=5,
                                    bgcolor=status_color,
                                ),
                                ft.Text(status_text, weight=ft.FontWeight.W_600),
                                ft.Text(
                                    f"port {state.gateway_port}",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                                ft.Text(
                                    f"v{state.gateway_version or '?'}",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                                ft.Text(
                                    f"up {_fmt_uptime(state.gateway_uptime)}",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                        ),
                        ft.Text(
                            state.gateway_source or "engine not loaded",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            state.gateway_base_url,
                            size=13,
                            font_family="monospace",
                            selectable=True,
                        ),
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.FilledButton(
                                    "Stop" if running else "Start gateway",
                                    icon=ft.Icons.STOP if running else ft.Icons.PLAY_ARROW,
                                    on_click=lambda _: (
                                        methods.stop_gateway()
                                        if running
                                        else methods.start_gateway()
                                    ),
                                ),
                                ft.OutlinedButton(
                                    "Refresh models",
                                    icon=ft.Icons.REFRESH,
                                    on_click=lambda _: methods.refresh_models(),
                                    disabled=not running,
                                ),
                                ft.OutlinedButton(
                                    "Open console",
                                    icon=ft.Icons.OPEN_IN_NEW,
                                    on_click=lambda _: methods.open_gateway_console(),
                                    disabled=not running,
                                ),
                            ],
                        ),
                    ],
                ),
            ),
            ft.Container(
                padding=16,
                border_radius=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Column(
                    spacing=6,
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Text("Logs", size=14, weight=ft.FontWeight.W_600),
                                ft.Text(
                                    f"{len(log_rows)} lines",
                                    size=12,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ]
                        ),
                        ft.Container(
                            height=280,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.ListView(
                                spacing=2,
                                controls=log_rows,
                                auto_scroll=True,
                            ),
                        ),
                    ],
                ),
            ),
            build_banner_ad(),
        ],
    )
