"""Settings: appearance, gateway, system prompt, providers, MCP, search, about."""

import json

import flet as ft

from components.banner_ad import build_banner_ad
from core import constants, tokens
from core.logging import LOG
from core.settings import AppSettings
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def SettingsScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    _ = state.settings_version  # re-render after saves
    _ = state.update_info
    _ = state.mcp_test_results
    settings = AppSettings.load()
    page = getattr(ft.context, "page", None)

    draft_prompt, set_draft_prompt = ft.use_state(settings.system_prompt)
    port_text, set_port_text = ft.use_state(str(settings.gateway_port))
    autostart, set_autostart = ft.use_state(settings.gateway_autostart)
    search_on, set_search_on = ft.use_state(settings.search_enabled)
    provider_open, set_provider_open = ft.use_state(False)
    mcp_open, set_mcp_open = ft.use_state(False)
    p_name, set_p_name = ft.use_state("")
    p_url, set_p_url = ft.use_state("")
    p_key, set_p_key = ft.use_state("")
    m_name, set_m_name = ft.use_state("")
    m_transport, set_m_transport = ft.use_state("streamable_http")
    m_target, set_m_target = ft.use_state("")
    m_headers, set_m_headers = ft.use_state("")
    notice, set_notice = ft.use_state("")

    def _section(title: str, rows: list) -> ft.Container:
        return ft.Container(
            padding=16,
            border_radius=tokens.RADIUS_MD,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        title,
                        size=13,
                        weight=ft.FontWeight.W_600,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    *rows,
                ],
            ),
        )

    def _cycle_theme(_: object) -> None:
        order = {"dark": "light", "light": "system", "system": "dark"}
        methods.set_theme(order.get(state.theme_mode, "system"))

    def _theme_icon() -> object:
        if state.theme_mode == "dark":
            return ft.Icons.DARK_MODE_ROUNDED
        if state.theme_mode == "light":
            return ft.Icons.LIGHT_MODE_ROUNDED
        return ft.Icons.SETTINGS_SYSTEM_DAYDREAM_ROUNDED

    def _save_gateway(_: object) -> None:
        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            set_notice("Port must be a number from1 to 65535.")
            return
        methods.save_settings({"gateway_port": port, "gateway_autostart": autostart})
        set_notice("Gateway saved. Restart it to apply a new port.")

    def _save_prompt(_: object) -> None:
        methods.save_settings({"system_prompt": draft_prompt})
        set_notice("System prompt saved.")

    def _add_provider(_: object) -> None:
        if not p_name.strip() or not p_url.strip():
            set_notice("Name and base URL are required.")
            return
        methods.add_provider(
            {
                "name": p_name.strip(),
                "base_url": p_url.strip().rstrip("/"),
                "api_key": p_key,
                "models": [],
            }
        )
        set_p_name("")
        set_p_url("")
        set_p_key("")
        set_provider_open(False)
        set_notice("Provider added.")

    def _add_mcp(_: object) -> None:
        if not m_name.strip() or not m_target.strip():
            set_notice("Name and command or URL are required.")
            return
        headers: dict[str, str] = {}
        if m_headers.strip():
            try:
                parsed = json.loads(m_headers)
                if not isinstance(parsed, dict):
                    raise ValueError("not an object")
                headers = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                set_notice("Headers must be a JSON object.")
                return
        methods.add_mcp_server(
            {
                "name": m_name.strip(),
                "transport": m_transport,
                "target": m_target.strip(),
                "headers": headers,
            }
        )
        set_m_name("")
        set_m_target("")
        set_m_headers("")
        set_mcp_open(False)
        set_notice("MCP server added.")

    def _test_mcp(server_id: str) -> None:
        def done(result: tuple) -> None:
            kind, message = result
            results = dict(state.mcp_test_results)
            results[server_id] = {"kind": kind, "message": str(message)[:300]}
            state.mcp_test_results = results

        methods.test_mcp_server(server_id, done)

    appearance = _section(
        "Appearance",
        [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(f"Theme: {state.theme_mode}", size=14),
                    ft.IconButton(
                        _theme_icon(),
                        tooltip="Cycle theme (light / dark / system)",
                        on_click=_cycle_theme,
                    ),
                ],
            ),
        ],
    )

    gateway = _section(
        "Gateway",
        [
            ft.Row(
                spacing=8,
                controls=[
                    ft.TextField(
                        value=port_text,
                        label="Port",
                        width=140,
                        text_size=13,
                        keyboard_type=ft.KeyboardType.NUMBER,
                        on_change=lambda e: set_port_text(str(e.control.value or "")),
                    ),
                    ft.Switch(
                        label="Autostart with app",
                        value=autostart,
                        on_change=lambda e: set_autostart(bool(e.control.value)),
                    ),
                ],
            ),
            ft.Row(
                controls=[
                    ft.FilledButton("Save gateway", on_click=_save_gateway),
                    ft.OutlinedButton(
                        "Refresh models",
                        icon=ft.Icons.REFRESH,
                        on_click=lambda _: methods.refresh_models(),
                    ),
                ],
            ),
        ],
    )

    prompt_card = _section(
        "System prompt",
        [
            ft.TextField(
                value=draft_prompt,
                multiline=True,
                min_lines=2,
                max_lines=6,
                text_size=13,
                on_change=lambda e: set_draft_prompt(str(e.control.value or "")),
            ),
            ft.Row(
                controls=[ft.FilledButton("Save prompt", on_click=_save_prompt)],
            ),
        ],
    )

    provider_rows: list = [
        ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Text("Providers", size=14, weight=ft.FontWeight.W_600),
                ft.OutlinedButton(
                    "+ Add",
                    on_click=lambda _: set_provider_open(True),
                ),
            ],
        )
    ]
    for provider in settings.providers:
        is_active = provider.id == settings.active_provider_id
        provider_rows.append(
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Column(
                        spacing=2,
                        tight=True,
                        controls=[
                            ft.Row(
                                spacing=6,
                                controls=[
                                    ft.Text(provider.name, size=14),
                                    *(
                                        [ft.Text("active", size=11, color=ft.Colors.PRIMARY)]
                                        if is_active
                                        else []
                                    ),
                                ],
                            ),
                            ft.Text(
                                str(provider.base_url),
                                size=11,
                                font_family="monospace",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                    ),
                    ft.Row(
                        spacing=0,
                        controls=[
                            *(
                                []
                                if is_active
                                else [
                                    ft.TextButton(
                                        "Use",
                                        on_click=lambda e, pid=provider.id: methods.select_provider(
                                            pid
                                        ),
                                    )
                                ]
                            ),
                            ft.IconButton(
                                ft.Icons.DELETE_OUTLINE,
                                icon_size=18,
                                tooltip="Remove",
                                on_click=lambda e, pid=provider.id: methods.remove_provider(pid),
                            ),
                        ],
                    ),
                ],
            )
        )
    if provider_open:
        provider_rows.extend(
            [
                ft.TextField(
                    label="Name",
                    value=p_name,
                    text_size=13,
                    on_change=lambda e: set_p_name(str(e.control.value or "")),
                ),
                ft.TextField(
                    label="Base URL (…/v1)",
                    value=p_url,
                    text_size=13,
                    on_change=lambda e: set_p_url(str(e.control.value or "")),
                ),
                ft.TextField(
                    label="API key (optional)",
                    value=p_key,
                    password=True,
                    can_reveal_password=True,
                    text_size=13,
                    on_change=lambda e: set_p_key(str(e.control.value or "")),
                ),
                ft.Row(
                    controls=[
                        ft.FilledButton("Add provider", on_click=_add_provider),
                        ft.TextButton("Cancel", on_click=lambda _: set_provider_open(False)),
                    ],
                ),
            ]
        )
    providers_card = _section("Providers", provider_rows)

    mcp_rows: list = [
        ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Text("MCP servers", size=14, weight=ft.FontWeight.W_600),
                ft.OutlinedButton("+ Add", on_click=lambda _: set_mcp_open(True)),
            ],
        )
    ]
    for server in settings.mcp_servers:
        result = state.mcp_test_results.get(server.id)
        mcp_rows.append(
            ft.Container(
                padding=10,
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW
                if hasattr(ft.Colors, "SURFACE_CONTAINER_LOW")
                else ft.Colors.SURFACE,
                content=ft.Column(
                    spacing=4,
                    controls=[
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            controls=[
                                ft.Column(
                                    spacing=2,
                                    tight=True,
                                    controls=[
                                        ft.Text(server.name, size=14),
                                        ft.Text(
                                            f"{server.transport} · "
                                            + ("enabled" if server.enabled else "disabled"),
                                            size=11,
                                            color=ft.Colors.ON_SURFACE_VARIANT,
                                        ),
                                    ],
                                ),
                                ft.Row(
                                    spacing=0,
                                    controls=[
                                        ft.TextButton(
                                            "Test", on_click=lambda e, sid=server.id: _test_mcp(sid)
                                        ),
                                        ft.TextButton(
                                            "Disable" if server.enabled else "Enable",
                                            on_click=lambda e, sid=server.id: (
                                                methods.toggle_mcp_server(sid)
                                            ),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.DELETE_OUTLINE,
                                            icon_size=18,
                                            tooltip="Remove",
                                            on_click=lambda e, sid=server.id: (
                                                methods.remove_mcp_server(sid)
                                            ),
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        *(
                            [
                                ft.Text(
                                    ("✓ " if result["kind"] == "ok" else "✗ ") + result["message"],
                                    size=12,
                                    color=ft.Colors.GREEN
                                    if result["kind"] == "ok"
                                    else ft.Colors.ERROR,
                                )
                            ]
                            if result
                            else []
                        ),
                    ],
                ),
            )
        )
    if mcp_open:
        mcp_rows.extend(
            [
                ft.TextField(
                    label="Name",
                    value=m_name,
                    text_size=13,
                    on_change=lambda e: set_m_name(str(e.control.value or "")),
                ),
                ft.Dropdown(
                    label="Transport",
                    value=m_transport,
                    options=["streamable_http", "sse", "stdio"],
                    text_size=13,
                    on_select=lambda e: set_m_transport(str(e.control.value or "streamable_http")),
                ),
                ft.TextField(
                    label="Command (stdio) or URL (remote)",
                    value=m_target,
                    text_size=13,
                    on_change=lambda e: set_m_target(str(e.control.value or "")),
                ),
                ft.TextField(
                    label="Headers JSON (optional)",
                    value=m_headers,
                    text_size=13,
                    on_change=lambda e: set_m_headers(str(e.control.value or "")),
                ),
                ft.Row(
                    controls=[
                        ft.FilledButton("Add server", on_click=_add_mcp),
                        ft.TextButton("Cancel", on_click=lambda _: set_mcp_open(False)),
                    ],
                ),
            ]
        )
    mcp_card = _section("MCP servers", mcp_rows)

    search_card = _section(
        "Web search",
        [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text("Built-in web search tool", size=14),
                    ft.Switch(
                        value=search_on,
                        on_change=lambda e: (
                            set_search_on(bool(e.control.value)),
                            methods.save_settings({"search_enabled": bool(e.control.value)}),
                        ),
                    ),
                ],
            ),
            ft.Text(
                "Keyless hosted search. Applies from the next message.",
                size=12,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
        ],
    )

    about_rows: list = [
        ft.Row(
            controls=[
                ft.Text("Version", size=14),
                ft.Text(
                    f"{constants.APP_VERSION} (build {constants.BUILD_NUMBER})",
                    size=13,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ]
        ),
        ft.Row(
            controls=[
                ft.FilledButton("Check for updates", on_click=lambda _: methods.check_update()),
                *(
                    [
                        ft.OutlinedButton(
                            "View update",
                            on_click=lambda _: methods.open_update_dialog(),
                        )
                    ]
                    if state.update_info
                    else []
                ),
            ]
        ),
        ft.Text(
            "Open source: kani (MIT), mcp (MIT), Flet & flet-ads (Apache-2.0), "
            "cryptography (Apache-2.0 OR BSD-3-Clause), pydantic (MIT).",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        ),
    ]
    if page is not None:
        try:
            if page.platform.is_desktop():
                about_rows.append(
                    ft.Row(
                        controls=[
                            ft.TextButton(
                                "Quit",
                                style=ft.ButtonStyle(color=ft.Colors.ERROR),
                                on_click=lambda _: methods.quit_app(),
                            ),
                        ],
                    )
                )
        except Exception as exc:
            LOG.debug("platform probe failed: %s", exc)
    about_card = _section("About", about_rows)

    return ft.Container(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        padding=16,
        content=ft.Column(
            spacing=14,
            controls=[
                ft.Text(notice, size=13, color=ft.Colors.PRIMARY, visible=bool(notice)),
                appearance,
                gateway,
                prompt_card,
                providers_card,
                mcp_card,
                search_card,
                about_card,
                build_banner_ad(),
            ],
        ),
    )
