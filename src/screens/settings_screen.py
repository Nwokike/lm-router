"""Settings: appearance, gateway, system prompt, providers, MCP, search, about."""

import json

import flet as ft

from components.banner_ad import build_banner_ad
from components.section_header import section_header
from components.settings_kit import setting_row, settings_card
from core import constants, theme, tokens
from core.logging import LOG
from core.notify import show_snack
from core.settings import AppSettings
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def SettingsScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    _ = state.update_info
    _ = state.mcp_test_results
    # Load settings ONCE. `AppSettings.load()` decrypts provider keys and
    # touches disk; calling it in the render body re-read the file on every
    # keystroke and every log bump.
    settings, set_settings = ft.use_state(lambda: AppSettings.load())
    # Re-read only when the controller says settings changed.
    ft.use_effect(
        lambda: set_settings(AppSettings.load()),
        [state.settings_version],
    )
    page = getattr(ft.context, "page", None)
    is_dark = theme.is_dark_mode(page, state.theme_mode)
    narrow = bool(page and getattr(page, "width", None) and page.width < 600)

    draft_prompt, set_draft_prompt = ft.use_state(settings.system_prompt)
    port_text, set_port_text = ft.use_state(str(settings.gateway_port))
    autostart, set_autostart = ft.use_state(settings.gateway_autostart)
    keep_running, set_keep_running = ft.use_state(settings.keep_running_when_closed)
    search_on, set_search_on = ft.use_state(settings.search_enabled)
    provider_open, set_provider_open = ft.use_state(False)
    mcp_open, set_mcp_open = ft.use_state(False)
    p_name, set_p_name = ft.use_state("")
    p_url, set_p_url = ft.use_state("")
    p_key, set_p_key = ft.use_state("")
    m_name, set_m_name = ft.use_state("")
    m_transport, set_m_transport = ft.use_state("streamable_http")

    # Generation controls (kani / OpenAI hyperparams).
    gen_open, set_gen_open = ft.use_state(False)
    gen_temperature, set_gen_temperature = ft.use_state(f"{settings.temperature:g}")
    gen_top_p, set_gen_top_p = ft.use_state(f"{settings.top_p:g}")
    gen_max_tokens, set_gen_max_tokens = ft.use_state(str(settings.max_reply_tokens))
    gen_presence, set_gen_presence = ft.use_state(f"{settings.presence_penalty:g}")
    gen_frequency, set_gen_frequency = ft.use_state(f"{settings.frequency_penalty:g}")
    gen_context, set_gen_context = ft.use_state(str(settings.max_context_tokens))
    gen_tool_rounds, set_gen_tool_rounds = ft.use_state(str(settings.tool_max_rounds))
    gen_tool_retries, set_gen_tool_retries = ft.use_state(str(settings.tool_retry_attempts))
    gen_reasoning, set_gen_reasoning = ft.use_state(settings.reasoning_effort)
    gen_json_mode, set_gen_json_mode = ft.use_state(settings.json_mode)
    tell_time, set_tell_time = ft.use_state(settings.tell_model_time)
    m_target, set_m_target = ft.use_state("")
    m_headers, set_m_headers = ft.use_state("")

    def _section(title: str, rows: list) -> list[ft.Control]:
        """Uppercase label above a bordered card, Sherlock's layout."""
        return [
            section_header(title),
            settings_card(rows),
        ]

    def _pad(control: ft.Control) -> ft.Container:
        """Inner inset for rows that do not go through setting_row.

        Same 16/12 padding as every standard row, so bare rows line up with
        the ones `setting_row` builds.
        """
        return ft.Container(
            padding=ft.Padding(tokens.SPACE_LG, tokens.SPACE_MD, tokens.SPACE_LG, tokens.SPACE_MD),
            content=control,
        )

    def _theme_card(mode: str, label: str, icon: str) -> ft.Container:
        """One selectable theme option, mirroring Sherlock's card."""
        current = state.theme_mode or "system"
        selected = mode == current

        def _select(e: ft.ControlEvent) -> None:
            if not selected:
                methods.set_theme(mode)

        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(
                        icon,
                        color=theme.PRIMARY if selected else ft.Colors.ON_SURFACE_VARIANT,
                        size=tokens.ICON_SM,
                    ),
                    ft.Text(
                        label,
                        size=tokens.FONT_SM,
                        weight=(ft.FontWeight.W_600 if selected else ft.FontWeight.NORMAL),
                        color=theme.PRIMARY if selected else ft.Colors.ON_SURFACE,
                        font_family=theme.FONT,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=tokens.SPACE_TIGHT,
            ),
            padding=ft.Padding(
                tokens.SPACE_SM,
                tokens.SPACE_SM,
                tokens.SPACE_SM,
                tokens.SPACE_SM,
            ),
            border_radius=tokens.RADIUS_SM,
            border=ft.Border.all(
                1.5 if selected else 1,
                theme.PRIMARY
                if selected
                else ft.Colors.with_opacity(tokens.OPACITY_MEDIUM, ft.Colors.ON_SURFACE),
            ),
            bgcolor=(
                ft.Colors.with_opacity(tokens.OPACITY_TINT, theme.PRIMARY)
                if selected
                else ft.Colors.TRANSPARENT
            ),
            expand=True,
            ink=True,
            on_click=_select,
            animate=ft.Animation(tokens.ANIM_FAST, "easeOut"),
        )

    def _set_autostart(value: bool) -> None:
        set_autostart(value)
        methods.save_settings({"gateway_autostart": value})

    def _save_gateway(_: object) -> None:
        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            show_snack(page, "Port must be 1 to 65535.")
            return
        # gateway_autostart is NOT here: _set_autostart is its single writer.
        methods.save_settings({"gateway_port": port})
        show_snack(page, "Saved. Restart the gateway to use the new port.")

    def _save_prompt(_: object) -> None:
        methods.save_settings({"system_prompt": draft_prompt})
        show_snack(page, "System prompt saved.")

    def _set_tell_time(value: bool) -> None:
        set_tell_time(value)
        methods.save_settings({"tell_model_time": value})

    def _set_keep_running(value: bool) -> None:
        set_keep_running(value)
        methods.save_settings({"keep_running_when_closed": value})

    def _save_generation(_: object) -> None:
        """Persist every generation knob, clamped to the model's own bounds.

        Sherlock clamps client-side: a value that parses but is out of range
        used to reach the controller, fail validation, and leave the field
        showing the rejected number while the error surfaced on a different
        surface. Bounds mirror core/settings.py Field constraints.
        """

        def _num(raw: str, label: str) -> float:
            try:
                return float(raw)
            except TypeError, ValueError:
                raise ValueError(f"{label} must be a number.")

        def _bound(value: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, value))

        try:
            temperature = _bound(_num(gen_temperature, "Temperature"), 0.0, 2.0)
            top_p = _bound(_num(gen_top_p, "Top P"), 0.0, 1.0)
            max_tokens = int(_bound(_num(gen_max_tokens, "Max reply tokens"), 1, 131_072))
            presence = _bound(_num(gen_presence, "Presence penalty"), -2.0, 2.0)
            frequency = _bound(_num(gen_frequency, "Frequency penalty"), -2.0, 2.0)
            context = max(1024, int(_num(gen_context, "Context window")))
            rounds = int(_bound(_num(gen_tool_rounds, "Tool rounds"), 1, 10))
            retries = int(_bound(_num(gen_tool_retries, "Tool retries"), 0, 5))
        except ValueError as exc:
            show_snack(page, str(exc))
            return
        methods.save_settings(
            {
                "temperature": temperature,
                "top_p": top_p,
                "max_reply_tokens": max_tokens,
                "presence_penalty": presence,
                "frequency_penalty": frequency,
                "max_context_tokens": context,
                "tool_max_rounds": rounds,
                "tool_retry_attempts": retries,
                "reasoning_effort": gen_reasoning,
                "json_mode": gen_json_mode,
            },
        )
        # Push the CLAMPED values back into the fields: use_state never
        # re-reads prefs, so "9" would keep displaying while 2.0 is stored.
        set_gen_temperature(f"{temperature:g}")
        set_gen_top_p(f"{top_p:g}")
        set_gen_max_tokens(str(max_tokens))
        set_gen_presence(f"{presence:g}")
        set_gen_frequency(f"{frequency:g}")
        set_gen_context(str(context))
        set_gen_tool_rounds(str(rounds))
        set_gen_tool_retries(str(retries))
        show_snack(page, "Generation settings saved.")
        set_gen_open(False)

    def _add_provider(_: object) -> None:
        if not p_name.strip() or not p_url.strip():
            show_snack(page, "Name and base URL are required.")
            return
        methods.add_provider(
            {
                "name": p_name.strip(),
                "base_url": p_url.strip().rstrip("/"),
                "api_key": p_key,
                "models": [],
            },
        )
        set_p_name("")
        set_p_url("")
        set_p_key("")
        set_provider_open(False)
        show_snack(page, "Provider added.")

    def _add_mcp(_: object) -> None:
        if not m_name.strip() or not m_target.strip():
            show_snack(page, "Name and command or URL are required.")
            return
        headers: dict[str, str] = {}
        if m_headers.strip():
            try:
                parsed = json.loads(m_headers)
                if not isinstance(parsed, dict):
                    raise ValueError("not an object")
                headers = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                show_snack(page, "Headers must be a JSON object.")
                return
        # MCPServerConfig takes `command` (stdio) or `url` (remote). A single
        # `target` key is dropped by extra="ignore", so every add used to fail
        # validation while the screen still reported success.
        target = m_target.strip()
        payload: dict = {
            "name": m_name.strip(),
            "transport": m_transport,
            "headers": headers,
        }
        if m_transport == "stdio":
            payload["command"] = target
            payload["args"] = []
        else:
            url = target if "://" in target else f"https://{target}"
            payload["url"] = url
        methods.add_mcp_server(payload)
        set_m_name("")
        set_m_target("")
        set_m_headers("")
        set_mcp_open(False)
        show_snack(page, "MCP server added.")

    def _test_mcp(server_id: str) -> None:
        if server_id in state.mcp_testing:
            return  # already in flight (spinner shown; re-click guard)
        state.mcp_testing = state.mcp_testing | {server_id}

        def done(result: tuple) -> None:
            kind, message = result
            results = dict(state.mcp_test_results)
            if kind == "ok" and isinstance(message, list):
                results[server_id] = {"kind": "ok", "tools": message}
            else:
                results[server_id] = {"kind": kind, "message": str(message)[:300]}
            state.mcp_test_results = results
            state.mcp_testing = state.mcp_testing - {server_id}

        methods.test_mcp_server(server_id, done)

    appearance = _section(
        "Appearance",
        [
            # Sherlock's preferences pattern: a padded container wrapping the
            # label row and the three theme cards. The bare Row had no padding,
            # so it sat flush against the card edge and misaligned with every
            # other setting row.
            ft.Container(
                padding=ft.Padding(
                    tokens.SPACE_LG,
                    tokens.SPACE_MD,
                    tokens.SPACE_LG,
                    tokens.SPACE_MD,
                ),
                content=ft.Column(
                    spacing=tokens.SPACE_MD,
                    controls=[
                        ft.Row(
                            spacing=tokens.SPACE_MD,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Container(
                                    content=ft.Icon(
                                        ft.Icons.COLOR_LENS_ROUNDED,
                                        size=tokens.ICON_MD,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                    width=tokens.ICON_BACKDROP,
                                    height=tokens.ICON_BACKDROP,
                                    border_radius=tokens.ICON_BACKDROP_RADIUS,
                                    bgcolor=ft.Colors.with_opacity(
                                        tokens.OPACITY_LIGHT,
                                        ft.Colors.ON_SURFACE,
                                    ),
                                    alignment=ft.Alignment.CENTER,
                                ),
                                ft.Column(
                                    spacing=tokens.SPACE_XXS,
                                    expand=True,
                                    controls=[
                                        ft.Text(
                                            "App Theme",
                                            size=tokens.FONT_MD,
                                            weight=ft.FontWeight.W_500,
                                        ),
                                        ft.Text(
                                            "Choose between Light, Dark, or System",
                                            size=tokens.FONT_XS,
                                            color=ft.Colors.with_opacity(
                                                tokens.OPACITY_DIM,
                                                ft.Colors.ON_SURFACE,
                                            ),
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        ft.Row(
                            controls=[
                                _theme_card("light", "Light", ft.Icons.LIGHT_MODE_ROUNDED),
                                _theme_card("dark", "Dark", ft.Icons.DARK_MODE_ROUNDED),
                                _theme_card(
                                    "system",
                                    "System",
                                    ft.Icons.SETTINGS_SYSTEM_DAYDREAM_ROUNDED,
                                ),
                            ],
                            spacing=tokens.SPACE_SM,
                        ),
                    ],
                ),
            ),
        ],
    )

    gateway = _section(
        "Gateway",
        [
            # One house row per setting so nothing overflows on a phone; the
            # old version packed a field and two switches into a single Row.
            setting_row(
                icon=ft.Icons.ROUTER_ROUNDED,
                title="Port",
                subtitle="Local port the gateway listens on",
                trailing=ft.TextField(
                    value=port_text,
                    label="Port",
                    width=120,
                    text_size=tokens.FONT_SM,
                    keyboard_type=ft.KeyboardType.NUMBER,
                    on_change=lambda e: set_port_text(str(e.control.value or "")),
                ),
                stacked=narrow,
            ),
            setting_row(
                icon=ft.Icons.PLAY_CIRCLE_ROUNDED,
                title="Autostart with app",
                subtitle="Start the gateway as soon as the app opens",
                trailing=ft.Switch(
                    value=autostart,
                    active_color=ft.Colors.PRIMARY,
                    # Immediate persist, same as Keep-running below — the two
                    # switches used to have opposite semantics with no label
                    # saying so, and this one silently discarded on navigate.
                    on_change=lambda e: _set_autostart(bool(e.control.value)),
                ),
                stacked=narrow,
            ),
            setting_row(
                icon=ft.Icons.MINIMIZE_ROUNDED,
                title="Keep gateway running when closed",
                # Off means X closes the app and stops the gateway. On means X
                # hides the window; Quit is always in the header, so the app
                # can never look like it crashed.
                subtitle="Closing the window hides it instead",
                trailing=ft.Switch(
                    value=keep_running,
                    active_color=ft.Colors.PRIMARY,
                    on_change=lambda e: _set_keep_running(bool(e.control.value)),
                ),
                stacked=narrow,
            ),
            ft.Container(
                padding=ft.Padding(
                    left=tokens.SPACE_LG,
                    right=tokens.SPACE_LG,
                    bottom=tokens.SPACE_MD,
                ),
                content=ft.Row(
                    spacing=tokens.SPACE_SM,
                    controls=[
                        ft.FilledButton("Save gateway", on_click=_save_gateway),
                        ft.OutlinedButton(
                            "Refresh models",
                            icon=ft.Icons.REFRESH,
                            on_click=lambda _: methods.refresh_models(),
                        ),
                    ],
                ),
            ),
        ],
    )

    prompt_card = _section(
        "System prompt",
        [
            _pad(
                ft.TextField(
                    value=draft_prompt,
                    multiline=True,
                    min_lines=2,
                    max_lines=6,
                    text_size=tokens.FONT_BODY_SM,
                    on_change=lambda e: set_draft_prompt(str(e.control.value or "")),
                ),
            ),
            _pad(
                ft.Row(
                    controls=[ft.FilledButton("Save prompt", on_click=_save_prompt)],
                ),
            ),
        ],
    )

    def _num_field(
        label: str,
        value: str,
        setter,
        width: int = 150,
        decimal: bool = False,
    ) -> ft.TextField:
        return ft.TextField(
            label=label,
            value=value,
            width=width,
            dense=True,
            text_size=tokens.FONT_BODY_SM,
            # Android's NUMBER keypad is digits-only: "0.7" was literally
            # untypable for the float fields (clamping handles the rest).
            keyboard_type=ft.KeyboardType.TEXT if decimal else ft.KeyboardType.NUMBER,
            on_change=lambda e, s=setter: s(str(e.control.value or "")),
        )

    generation_card = _section(
        "Generation",
        [
            _pad(
                ft.Text(
                    "Sent per request. Defaults suit free models with strict limits.",
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ),
            _pad(
                ft.Row(
                    spacing=tokens.SPACE_SM,
                    wrap=True,
                    controls=[
                        _num_field(
                            "Temperature", gen_temperature, set_gen_temperature, decimal=True
                        ),
                        _num_field("Max reply tokens", gen_max_tokens, set_gen_max_tokens),
                    ],
                ),
            ),
            _pad(
                ft.Row(
                    spacing=tokens.SPACE_SM,
                    wrap=True,
                    controls=[
                        ft.Dropdown(
                            label="Reasoning effort",
                            value=gen_reasoning,
                            width=170,
                            text_size=tokens.FONT_BODY_SM,
                            options=[
                                ft.DropdownOption(key=v, text=v)
                                for v in ("auto", "minimal", "low", "medium", "high")
                            ],
                            on_select=lambda e: set_gen_reasoning(str(e.control.value or "auto")),
                        ),
                        ft.Switch(
                            # ~15 tokens/turn. Stops the model guessing at "today".
                            value=tell_time,
                            active_color=ft.Colors.PRIMARY,
                            on_change=lambda e: _set_tell_time(bool(e.control.value)),
                        ),
                        ft.Switch(
                            value=gen_json_mode,
                            active_color=ft.Colors.PRIMARY,
                            on_change=lambda e: set_gen_json_mode(bool(e.control.value)),
                        ),
                    ],
                ),
            ),
            _pad(
                ft.Container(
                    ink=True,
                    on_click=lambda _: set_gen_open(not gen_open),
                    content=ft.Row(
                        spacing=tokens.SPACE_TIGHT,
                        controls=[
                            ft.Icon(
                                ft.Icons.EXPAND_LESS_ROUNDED
                                if gen_open
                                else ft.Icons.EXPAND_MORE_ROUNDED,
                                size=tokens.ICON_XS,
                            ),
                            ft.Text(
                                "Advanced engine options",
                                size=tokens.FONT_SM,
                                weight=ft.FontWeight.W_600,
                            ),
                        ],
                    ),
                ),
            ),
            *(
                [
                    _pad(
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            wrap=True,
                            controls=[
                                _num_field("Top P", gen_top_p, set_gen_top_p, decimal=True),
                                _num_field(
                                    "Context window",
                                    gen_context,
                                    set_gen_context,
                                    width=170,
                                ),
                            ],
                        ),
                    ),
                    _pad(
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            wrap=True,
                            controls=[
                                _num_field(
                                    "Presence penalty", gen_presence, set_gen_presence, decimal=True
                                ),
                                _num_field(
                                    "Frequency penalty",
                                    gen_frequency,
                                    set_gen_frequency,
                                    decimal=True,
                                ),
                            ],
                        ),
                    ),
                    _pad(
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            wrap=True,
                            controls=[
                                _num_field("Tool rounds", gen_tool_rounds, set_gen_tool_rounds),
                                _num_field("Tool retries", gen_tool_retries, set_gen_tool_retries),
                            ],
                        ),
                    ),
                ]
                if gen_open
                else []
            ),
            _pad(
                ft.Row(
                    controls=[ft.FilledButton("Save generation", on_click=_save_generation)],
                ),
            ),
        ],
    )

    provider_rows: list = [
        # Section header supplies the title; this row is the action.
        _pad(
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                wrap=False,
                scroll=ft.ScrollMode.AUTO,
                controls=[
                    ft.Text(
                        "Route chat through another OpenAI-compatible endpoint",
                        size=tokens.FONT_SM,
                        color=theme.dim(is_dark),
                    ),
                    ft.OutlinedButton(
                        "+ Add",
                        on_click=lambda _: set_provider_open(True),
                    ),
                ],
            ),
        ),
    ]
    for provider in settings.providers:
        is_active = provider.id == settings.active_provider_id
        provider_rows.append(
            _pad(
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    wrap=False,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[
                        ft.Column(
                            spacing=tokens.SPACE_XXS,
                            tight=True,
                            controls=[
                                ft.Row(
                                    spacing=tokens.SPACE_TIGHT,
                                    controls=[
                                        ft.Text(provider.name, size=tokens.FONT_MD),
                                        *(
                                            [
                                                ft.Text(
                                                    "active",
                                                    size=tokens.FONT_XS,
                                                    color=ft.Colors.PRIMARY,
                                                )
                                            ]
                                            if is_active
                                            else []
                                        ),
                                    ],
                                ),
                                ft.Text(
                                    str(provider.base_url),
                                    size=tokens.FONT_XS,
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
                                            on_click=lambda e, pid=provider.id: (
                                                methods.select_provider(pid)
                                            ),
                                        ),
                                    ]
                                ),
                                ft.IconButton(
                                    ft.Icons.DELETE_OUTLINE,
                                    icon_size=tokens.ICON_SM,
                                    tooltip="Remove",
                                    on_click=lambda e, pid=provider.id: methods.remove_provider(
                                        pid
                                    ),
                                ),
                            ],
                        ),
                    ],
                ),
            ),
        )
    if provider_open:
        provider_rows.extend(
            [
                _pad(
                    ft.TextField(
                        label="Name",
                        value=p_name,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_p_name(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.TextField(
                        label="Base URL (…/v1)",
                        value=p_url,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_p_url(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.TextField(
                        label="API key (optional)",
                        value=p_key,
                        password=True,
                        can_reveal_password=True,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_p_key(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.Row(
                        controls=[
                            ft.FilledButton("Add provider", on_click=_add_provider),
                            ft.TextButton("Cancel", on_click=lambda _: set_provider_open(False)),
                        ],
                    ),
                ),
            ],
        )
    providers_card = _section("Providers", provider_rows)

    mcp_rows: list = [
        # The section header already says "MCP servers"; this row is the
        # action, not a second title.
        _pad(
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                wrap=False,
                scroll=ft.ScrollMode.AUTO,
                controls=[
                    ft.Text(
                        "Local or remote MCP server",
                        size=tokens.FONT_SM,
                        color=theme.dim(is_dark),
                    ),
                    ft.OutlinedButton("+ Add", on_click=lambda _: set_mcp_open(True)),
                ],
            ),
        ),
    ]
    for server in settings.mcp_servers:
        result = state.mcp_test_results.get(server.id)
        disabled_count = len(getattr(server, "disabled_tools", []))
        status_text = f"{server.transport} · " + ("enabled" if server.enabled else "disabled")
        if server.enabled and disabled_count > 0:
            status_text += f" · {disabled_count} tool(s) disabled"

        test_controls: list[ft.Control] = []
        if result:
            if result.get("kind") == "ok" and "tools" in result:
                tools = result["tools"]
                invalid_schemas = [t for t in tools if not t.get("schema_valid", True)]
                summary_color = ft.Colors.GREEN if not invalid_schemas else ft.Colors.AMBER
                summary_text = (
                    f"✓ {len(tools)} tools verified (schemas valid)"
                    if not invalid_schemas
                    else f"⚠ {len(tools)} tools ({len(invalid_schemas)} schema warning)"
                )
                test_controls.append(
                    ft.Text(summary_text, size=tokens.FONT_SM, color=summary_color)
                )
                for t in tools:
                    t_name = t.get("name", "")
                    t_disabled = t_name in getattr(server, "disabled_tools", [])
                    t_valid = t.get("schema_valid", True)
                    test_controls.append(
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Row(
                                    spacing=tokens.SPACE_XS,
                                    controls=[
                                        ft.Icon(
                                            ft.Icons.CHECK_CIRCLE_OUTLINE
                                            if t_valid
                                            else ft.Icons.WARNING_AMBER_ROUNDED,
                                            size=tokens.ICON_XS,
                                            color=ft.Colors.GREEN if t_valid else ft.Colors.AMBER,
                                        ),
                                        ft.Text(
                                            t_name,
                                            size=tokens.FONT_SM,
                                            weight=ft.FontWeight.W_500,
                                            color=ft.Colors.ON_SURFACE_VARIANT
                                            if not t_disabled
                                            else ft.Colors.OUTLINE,
                                        ),
                                    ],
                                ),
                                ft.TextButton(
                                    "Enable" if t_disabled else "Disable",
                                    on_click=lambda e, sid=server.id, tn=t_name: (
                                        methods.toggle_mcp_tool(sid, tn)
                                    ),
                                ),
                            ],
                        ),
                    )
            else:
                err_msg = result.get("message") or "Test failed."
                test_controls.append(
                    ft.Text(f"✗ {err_msg}", size=tokens.FONT_SM, color=ft.Colors.ERROR)
                )

        mcp_rows.append(
            ft.Container(
                padding=tokens.SPACE_SNUG,
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW
                if hasattr(ft.Colors, "SURFACE_CONTAINER_LOW")
                else ft.Colors.SURFACE,
                content=ft.Column(
                    spacing=tokens.SPACE_XS,
                    controls=[
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Column(
                                    spacing=tokens.SPACE_XXS,
                                    tight=True,
                                    controls=[
                                        ft.Text(server.name, size=tokens.FONT_MD),
                                        ft.Text(
                                            status_text,
                                            size=tokens.FONT_XS,
                                            color=ft.Colors.ON_SURFACE_VARIANT,
                                        ),
                                    ],
                                ),
                                ft.Row(
                                    spacing=0,
                                    controls=[
                                        *(
                                            [
                                                ft.ProgressRing(
                                                    width=tokens.ICON_XS,
                                                    height=tokens.ICON_XS,
                                                    stroke_width=2,
                                                )
                                            ]
                                            if server.id in state.mcp_testing
                                            else [
                                                ft.TextButton(
                                                    "Test",
                                                    on_click=lambda e, sid=server.id: _test_mcp(
                                                        sid
                                                    ),
                                                )
                                            ]
                                        ),
                                        ft.TextButton(
                                            "Disable" if server.enabled else "Enable",
                                            on_click=lambda e, sid=server.id: (
                                                methods.toggle_mcp_server(sid)
                                            ),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.DELETE_OUTLINE,
                                            icon_size=tokens.ICON_SM,
                                            tooltip="Remove",
                                            on_click=lambda e, sid=server.id: (
                                                methods.remove_mcp_server(sid)
                                            ),
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        *test_controls,
                    ],
                ),
            ),
        )
    if mcp_open:
        mcp_rows.extend(
            [
                _pad(
                    ft.TextField(
                        label="Name",
                        value=m_name,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_m_name(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.Dropdown(
                        label="Transport",
                        value=m_transport,
                        options=[
                            ft.DropdownOption(key=t, text=t)
                            for t in ["streamable_http", "sse", "stdio"]
                        ],
                        text_size=tokens.FONT_BODY_SM,
                        on_select=lambda e: set_m_transport(
                            str(e.control.value or "streamable_http")
                        ),
                    ),
                ),
                _pad(
                    ft.TextField(
                        label="Command (stdio) or URL (remote)",
                        value=m_target,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_m_target(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.TextField(
                        label="Headers JSON (optional)",
                        value=m_headers,
                        text_size=tokens.FONT_BODY_SM,
                        on_change=lambda e: set_m_headers(str(e.control.value or "")),
                    ),
                ),
                _pad(
                    ft.Row(
                        controls=[
                            ft.FilledButton("Add server", on_click=_add_mcp),
                            ft.TextButton("Cancel", on_click=lambda _: set_mcp_open(False)),
                        ],
                    ),
                ),
            ],
        )
    mcp_card = _section("MCP servers", mcp_rows)

    search_card = _section(
        "Web search",
        [
            setting_row(
                icon=ft.Icons.TRAVEL_EXPLORE_ROUNDED,
                title="Built-in web search",
                subtitle="Keyless hosted search. Applies from the next message.",
                trailing=ft.Switch(
                    value=search_on,
                    active_color=ft.Colors.PRIMARY,
                    on_change=lambda e: (
                        set_search_on(bool(e.control.value)),
                        methods.save_settings({"search_enabled": bool(e.control.value)}),
                    ),
                ),
                stacked=narrow,
            ),
        ],
    )

    # About: the centred identity block, back as it was, minus the open-source
    # licence line, which the owner asked to drop. Buttons are centred too, so
    # the card reads as one block instead of a left-aligned outlier.
    about_rows: list = [
        _pad(
            ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=0,
                controls=[
                    ft.Image(
                        src="/icon.svg",
                        width=tokens.ICON_EMPTY,
                        height=tokens.ICON_EMPTY,
                        color=ft.Colors.WHITE if is_dark else None,
                    ),
                    ft.Container(height=tokens.SPACE_SM),
                    ft.Text(
                        constants.APP_NAME,
                        size=tokens.FONT_LG,
                        weight=ft.FontWeight.W_700,
                        color=ft.Colors.ON_SURFACE,
                    ),
                    ft.Container(
                        content=ft.Text(
                            f"Version {constants.APP_VERSION} (Build {constants.BUILD_NUMBER})",
                            size=tokens.FONT_SM,
                            color=ft.Colors.with_opacity(tokens.OPACITY_DIM, ft.Colors.ON_SURFACE),
                        ),
                        ink=True,
                        tooltip="Tap to check for updates",
                        on_click=lambda _: methods.check_update(),
                    ),
                    ft.Container(height=tokens.SPACE_XS),
                    ft.Text(
                        "Your own Kiri Router gateway, running on-device.\n"
                        "No account, no API key, no credits.",
                        size=tokens.FONT_SM,
                        color=ft.Colors.with_opacity(tokens.OPACITY_DIM, ft.Colors.ON_SURFACE),
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
            ),
        ),
        _pad(
            ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=tokens.SPACE_SM,
                controls=[
                    ft.FilledButton(
                        "Check for updates",
                        icon=ft.Icons.CLOUD_DOWNLOAD_ROUNDED,
                        on_click=lambda _: methods.check_update(),
                    ),
                    *(
                        [
                            ft.OutlinedButton(
                                "View update",
                                on_click=lambda _: methods.open_update_dialog(),
                            ),
                        ]
                        if state.update_info
                        else []
                    ),
                ],
            ),
        ),
    ]
    if page is not None:
        try:
            if page.platform.is_desktop():
                about_rows.append(
                    _pad(
                        ft.Row(
                            alignment=ft.MainAxisAlignment.CENTER,
                            controls=[ft.TextButton("Quit", on_click=lambda _: methods.quit_app())],
                        ),
                    ),
                )
        except Exception as exc:
            LOG.debug("platform probe failed: %s", exc)
    about_card = _section("About", about_rows)

    # Sherlock's skeleton: a virtualised ListView of SectionHeader + card
    # pairs with banners interleaved, no outer padding, and generous top and
    # bottom breathing room.
    return ft.ListView(
        expand=True,
        spacing=0,
        controls=[
            ft.Container(height=tokens.SPACE_SM),
            *appearance,
            *gateway,
            build_banner_ad(),
            *prompt_card,
            *generation_card,
            *providers_card,
            *mcp_card,
            build_banner_ad(),
            *search_card,
            *about_card,
            # No banner at the floor: the owner banned them there. The spacer
            # stays last.
            ft.Container(height=tokens.SPACE_XXXL),
        ],
    )
