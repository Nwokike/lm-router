"""Server screen: gateway status, local endpoints, model catalog, live logs."""

import flet as ft

from components.banner_ad import build_banner_ad
from core import logging as applog
from core import theme, tokens
from core.catalog import chat_support_note, endpoint_label, rate_hint_label
from core.settings import AppSettings
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx

_LEVEL_COLORS = {
    "INFO": None,
    "WARNING": ft.Colors.ON_SURFACE_VARIANT,
    "ERROR": ft.Colors.ERROR,
    "DEBUG": ft.Colors.ON_SURFACE_VARIANT,
}

_MAX_LOG_ROWS = 200  # ring holds 500; rendering every row per log bump janked (R9)


def _compact_button_style() -> ft.ButtonStyle:
    """One-line-row button sizing.

    The gateway action row holds four controls; Material 3 defaults made each
    one taller than the status text beside it, so the row never fit a phone
    width. A fresh instance per call keeps each button's internals private.
    """
    return ft.ButtonStyle(
        padding=ft.Padding(
            tokens.SPACE_MD,
            tokens.SPACE_XS,
            tokens.SPACE_MD,
            tokens.SPACE_XS,
        ),
        text_style=ft.TextStyle(size=tokens.FONT_BODY_SM),
        icon_size=tokens.ICON_XS,
        visual_density=ft.VisualDensity.COMPACT,
    )


def _kv_row(label: str, value: str, methods, is_dark: bool) -> ft.Control:
    """A label/value line, with a copy affordance when methods are supplied."""
    trailing: list[ft.Control] = []
    if methods is not None and value:
        trailing.append(
            ft.IconButton(
                ft.Icons.CONTENT_COPY_ROUNDED,
                icon_size=tokens.ICON_XS,
                tooltip=f"Copy {label}",
                on_click=lambda e, v=value: methods.copy_text(v),
            ),
        )
    return ft.Row(
        spacing=tokens.SPACE_SM,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(label, size=tokens.FONT_XS, color=theme.dim(is_dark)),
            ft.Text(
                value,
                size=tokens.FONT_SM,
                color=theme.text_color(is_dark),
                font_family="monospace",
                selectable=True,
                expand=True,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            *trailing,
        ],
    )


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
    log_filter, set_log_filter = ft.use_state("ALL")
    _ = state.log_version  # subscribe: bumping log_version re-renders this screen
    # The share-key switch writes through save_settings, which only bumps
    # settings_version. Without this read the card never re-rendered and the
    # key field below the switch never appeared.
    _ = state.settings_version  # subscribe: settings saves (share key toggle) re-render this screen
    # Load settings ONCE. AppSettings.load() reads disk + parses JSON, and
    # this screen re-renders on every log flush; re-read only on save
    # (same pattern as settings_screen).
    prefs, set_prefs = ft.use_state(lambda: AppSettings.load())
    ft.use_effect(
        lambda: set_prefs(AppSettings.load()),
        [state.settings_version],
    )
    page = getattr(ft.context, "page", None)
    is_dark = theme.is_dark_mode(page, state.theme_mode)

    running = state.gateway_running
    status_color = ft.Colors.GREEN if running else ft.Colors.ERROR
    status_text = "running" if running else "stopped"

    # Filter logs by level (cap rendered rows: 500 Text controls per render
    # caused visible jank on every log bump)
    log_rows: list[ft.Control] = []
    for record in applog.records()[-_MAX_LOG_ROWS:]:
        lvl = record.get("level", "INFO")
        if log_filter != "ALL" and lvl != log_filter:
            continue
        color = _LEVEL_COLORS.get(lvl)
        log_rows.append(
            # Per-line sideways scroll: the ListView below is vertical-only, so
            # a long line used to be cut mid-character with no way to read the
            # tail. The row scrolls, the Text still stays on one line.
            ft.Row(
                wrap=False,
                scroll=ft.ScrollMode.AUTO,
                controls=[
                    ft.Text(
                        f"{record['ts']}  {lvl:<7}  {record['msg']}",
                        size=tokens.FONT_SM,
                        font_family="monospace",
                        max_lines=1,
                        overflow=ft.TextOverflow.CLIP,
                        selectable=True,
                        color=color,
                    ),
                ],
            ),
        )
    if not log_rows:
        log_rows.append(
            ft.Text(
                "No matching log output.",
                size=tokens.FONT_BODY_SM,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
        )

    # Desktop-only: explicit Quit affordance (window X hides to taskbar).
    is_desktop = False
    try:
        from flet import context as _ft_context

        _page = _ft_context.page
        if _page and hasattr(_page, "platform"):
            is_desktop = bool(_page.platform.is_desktop())
    except Exception:
        is_desktop = False

    quit_button: list[ft.Control] = (
        [
            ft.OutlinedButton(
                "Quit",
                icon=ft.Icons.CLOSE,
                style=ft.ButtonStyle(
                    color=ft.Colors.ERROR,
                    padding=ft.Padding(
                        tokens.SPACE_MD,
                        tokens.SPACE_XS,
                        tokens.SPACE_MD,
                        tokens.SPACE_XS,
                    ),
                    text_style=ft.TextStyle(size=tokens.FONT_BODY_SM),
                    icon_size=tokens.ICON_XS,
                    visual_density=ft.VisualDensity.COMPACT,
                ),
                on_click=lambda _: methods.quit_app(),
            ),
        ]
        if is_desktop
        else []
    )
    # Connection card. The gateway binds 127.0.0.1 only, so it is NOT reachable
    # from another device: a QR code / LAN URL would be a lie. Show the real
    # local endpoints, where the engine came from, and the rate-limit picture.
    conn_card: ft.Control | None = None
    if running:
        local_url = f"http://127.0.0.1:{state.gateway_port}/"
        api_url = state.gateway_base_url
        counts = state.gateway_counts or {}
        model_counts = counts.get("models") or {}
        summary = ""
        if model_counts:
            summary = (
                f"{model_counts.get('active', 0)} active"
                + (
                    f" · {model_counts.get('degraded', 0)} rate limited"
                    if model_counts.get("degraded")
                    else ""
                )
                + (
                    f" · {model_counts.get('failed', 0)} failed"
                    if model_counts.get("failed")
                    else ""
                )
            )
        conn_card = ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=tokens.SPACE_MD,
                vertical=tokens.SPACE_SNUG,
            ),
            border_radius=tokens.RADIUS_LG,
            bgcolor=theme.surface_2(is_dark),
            border=ft.Border.all(1, theme.border(is_dark)),
            content=ft.Column(
                spacing=tokens.SPACE_SNUG,
                tight=True,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                "Local gateway",
                                size=tokens.FONT_MD,
                                weight=ft.FontWeight.W_600,
                                color=theme.text_color(is_dark),
                            ),
                            ft.Container(
                                padding=ft.Padding.symmetric(
                                    horizontal=tokens.SPACE_SM,
                                    vertical=tokens.SPACE_XS,
                                ),
                                border_radius=tokens.RADIUS_PILL,
                                bgcolor=ft.Colors.with_opacity(
                                    tokens.OPACITY_MEDIUM,
                                    theme.PRIMARY,
                                ),
                                content=ft.Text(
                                    state.gateway_source or "running",
                                    size=tokens.FONT_2XS,
                                    weight=ft.FontWeight.W_600,
                                    color=theme.PRIMARY,
                                ),
                            ),
                        ],
                    ),
                    _kv_row("Admin console", local_url, methods, is_dark),
                    _kv_row("OpenAI base URL", api_url, methods, is_dark),
                    *([_kv_row("Health", summary, None, is_dark)] if summary else []),
                    ft.Text(
                        "Binds 127.0.0.1. Not reachable from another device unless you share it.",
                        size=tokens.FONT_2XS,
                        color=theme.dim(is_dark),
                    ),
                ],
            ),
        )

    # Share card: publish this device's gateway. The key is OPTIONAL by
    # design; when on, our stdlib proxy enforces it, because the router itself
    # verifies no Authorization header at all.
    share_card: ft.Control | None = None
    if running:
        sharing = bool(state.share_url)
        starting = state.share_starting

        share_action: ft.Control = (
            ft.FilledButton(
                "Stop sharing",
                icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                on_click=lambda _: methods.stop_share(),
            )
            if sharing
            else ft.Row(
                spacing=tokens.SPACE_SM,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.FilledButton(
                        "Start sharing",
                        icon=ft.Icons.PUBLIC_ROUNDED,
                        # The claim retries for 30s; without this the button
                        # read as dead for the whole retry window.
                        disabled=starting,
                        on_click=lambda _: methods.start_share(),
                    ),
                    *(
                        [
                            ft.ProgressRing(
                                width=tokens.ICON_XS,
                                height=tokens.ICON_XS,
                                stroke_width=2,
                            ),
                            # The claim retries for 30s; a bare ring next to a
                            # disabled button read as a dead control.
                            ft.Text(
                                "Starting tunnel…",
                                size=tokens.FONT_XS,
                                color=theme.dim(is_dark),
                            ),
                        ]
                        if starting
                        else []
                    ),
                ],
            )
        )

        controls: list[ft.Control] = [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Column(
                        spacing=0,
                        tight=True,
                        expand=True,
                        controls=[
                            ft.Text(
                                "Share",
                                size=tokens.FONT_MD,
                                weight=ft.FontWeight.W_600,
                                color=theme.text_color(is_dark),
                            ),
                            ft.Text(
                                "Publishes the gateway on a public URL. "
                                "Point any OpenAI client at the base URL.",
                                size=tokens.FONT_XS,
                                color=theme.dim(is_dark),
                            ),
                        ],
                    ),
                    share_action,
                ],
            ),
        ]

        if state.share_error:
            # The claim/tunnel failures used to land in a controller
            # attribute nobody rendered: the card looked fine while nothing
            # was sharing. Render them where the owner is already looking.
            controls.append(
                ft.Text(
                    state.share_error,
                    size=tokens.FONT_XS,
                    color=theme.ERROR,
                ),
            )

        # The key rows stay visible WHILE sharing too. Hiding them made the
        # key look like it was thrown away the moment Start was pressed.
        controls.append(
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        "Require API key",
                        size=tokens.FONT_SM,
                        color=theme.text_color(is_dark),
                    ),
                    ft.Switch(
                        value=prefs.require_share_key,
                        active_color=ft.Colors.PRIMARY,
                        on_change=lambda e: methods.save_settings(
                            {"require_share_key": bool(e.control.value)}
                        ),
                    ),
                ],
            ),
        )
        if prefs.require_share_key:
            # Editable: generate one, or type any key you prefer. Whatever is
            # here is what the other client must send as the Bearer token.
            controls.append(
                ft.Row(
                    spacing=tokens.SPACE_SM,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.TextField(
                            value=prefs.share_key,
                            label="API key",
                            hint_text="Generate a key or type one",
                            text_size=tokens.FONT_SM,
                            font_family="monospace",
                            expand=True,
                            on_change=lambda e: methods.save_settings(
                                {"share_key": str(e.control.value or "").strip()}
                            ),
                        ),
                        ft.IconButton(
                            ft.Icons.AUTORENEW_ROUNDED,
                            tooltip="Generate key",
                            on_click=lambda _: methods.regenerate_share_key(),
                        ),
                        ft.IconButton(
                            ft.Icons.CONTENT_COPY_ROUNDED,
                            tooltip="Copy key",
                            on_click=lambda _e, k=prefs.share_key: methods.copy_text(k),
                        ),
                    ],
                ),
            )
        if sharing:
            # /v1 is what an OpenAI client expects as its base_url: the client
            # appends /chat/completions to it.
            base_url = state.share_url.rstrip("/") + "/v1"
            controls.append(_kv_row("Base URL", base_url, methods, is_dark))
            controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(
                        horizontal=tokens.SPACE_SNUG,
                        vertical=tokens.SPACE_SM,
                    ),
                    border_radius=tokens.RADIUS_MD,
                    bgcolor=ft.Colors.with_opacity(tokens.OPACITY_FAINT, theme.PRIMARY),
                    content=ft.Text(
                        "Any client on this URL sees the full model catalog via "
                        "/v1/models, not only the model selected here. The URL "
                        "changes on every start and stops when you stop sharing.",
                        size=tokens.FONT_2XS,
                        color=theme.PRIMARY,
                    ),
                ),
            )
        if prefs.require_share_key:
            controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(
                        horizontal=tokens.SPACE_SNUG,
                        vertical=tokens.SPACE_SM,
                    ),
                    border_radius=tokens.RADIUS_MD,
                    bgcolor=ft.Colors.with_opacity(tokens.OPACITY_FAINT, theme.WARNING),
                    content=ft.Text(
                        "No key: anyone with the URL can spend the free-model quota.",
                        size=tokens.FONT_2XS,
                        color=theme.WARNING,
                    ),
                ),
            )

        share_card = ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=tokens.SPACE_MD,
                vertical=tokens.SPACE_SNUG,
            ),
            border_radius=tokens.RADIUS_LG,
            bgcolor=theme.surface_2(is_dark),
            border=ft.Border.all(1, theme.border(is_dark)),
            content=ft.Column(spacing=tokens.SPACE_SM, tight=True, controls=controls),
        )

    # Discovered Model Catalog Card: full catalog in its own bounded list (the
    # old [:15] slice hid models, upstream now lists 40+).
    models_count = len(state.models)
    model_items: list[ft.Control] = []
    for m in state.models:
        m_id = m.get("id", "")
        status = m.get("status", "active").lower()
        # Spelled out in full ("Chat completion"), not the truncated "chat".
        endpoint = endpoint_label(m)
        rate_hint = rate_hint_label(m)
        chat_note = chat_support_note(m)
        latency = m.get("latency_ms")

        # Honest status display: verbatim gateway status word + dot.
        # red only for failed; amber for untested/slow/rate limited; green active
        if status == "active":
            status_dot_color = ft.Colors.GREEN
        elif status == "failed":
            status_dot_color = ft.Colors.ERROR
        else:
            status_dot_color = ft.Colors.AMBER

        badges: list[ft.Control] = []
        badges.append(
            ft.Container(
                content=ft.Text(
                    endpoint,
                    size=tokens.FONT_2XS,
                    color=ft.Colors.PRIMARY,
                ),
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_TIGHT,
                    vertical=tokens.SPACE_XXS,
                ),
                border_radius=tokens.RADIUS_SM,
                bgcolor=ft.Colors.PRIMARY_CONTAINER,
            ),
        )
        if rate_hint:
            badges.append(
                ft.Container(
                    content=ft.Text(
                        rate_hint,
                        size=tokens.FONT_2XS,
                        color=theme.dim(is_dark),
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    padding=ft.Padding.symmetric(
                        horizontal=tokens.SPACE_TIGHT,
                        vertical=tokens.SPACE_XXS,
                    ),
                    border_radius=tokens.RADIUS_SM,
                    bgcolor=ft.Colors.with_opacity(
                        tokens.OPACITY_FAINT,
                        theme.dim(is_dark),
                    ),
                ),
            )
        if latency is not None:
            badges.append(
                ft.Text(
                    f"{latency}ms",
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            )
        # Say WHY a catalog row is not offered in chat, rather than silently
        # hiding it from the picker. The note sits on its own line below the
        # badges: as a third flex child it stole width from the model id and
        # squeezed the row into an unreadable strip.
        row_children: list[ft.Control] = [
            ft.Row(
                spacing=tokens.SPACE_TIGHT,
                controls=[
                    ft.Container(
                        width=tokens.DOT_SIZE,
                        height=tokens.DOT_SIZE,
                        border_radius=tokens.DOT_RADIUS,
                        bgcolor=status_dot_color,
                    ),
                    ft.Text(
                        status,
                        size=tokens.FONT_2XS,
                        color=status_dot_color,
                    ),
                    ft.Text(
                        m_id,
                        size=tokens.FONT_SM,
                        weight=ft.FontWeight.W_500,
                        # The row scrolls sideways, so the id needs no
                        # ellipsis; it only must not wrap into a tall blob.
                        no_wrap=True,
                    ),
                ],
            ),
            ft.Row(
                spacing=tokens.SPACE_TIGHT,
                controls=badges,
            ),
        ]

        model_items.append(
            ft.Container(
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_SM,
                    vertical=tokens.SPACE_TIGHT,
                ),
                border_radius=tokens.RADIUS_SM,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW
                if hasattr(ft.Colors, "SURFACE_CONTAINER_LOW")
                else ft.Colors.SURFACE,
                content=ft.Column(
                    spacing=tokens.SPACE_XXS,
                    tight=True,
                    controls=[
                        # One line, scrollable: badges used to overflow the
                        # card instead of wrapping.
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            controls=row_children,
                        ),
                        *(
                            [
                                ft.Text(
                                    chat_note,
                                    size=tokens.FONT_2XS,
                                    color=theme.dim(is_dark),
                                ),
                            ]
                            if chat_note
                            else []
                        ),
                    ],
                ),
            ),
        )

    catalog_card = ft.Container(
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_MD,
            vertical=tokens.SPACE_SM,
        ),
        border_radius=tokens.RADIUS_MD,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        content=ft.Column(
            spacing=tokens.SPACE_SM,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    wrap=False,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[
                        ft.Text(
                            f"Model Catalog ({models_count})",
                            size=tokens.FONT_MD,
                            weight=ft.FontWeight.W_600,
                        ),
                        ft.Text(
                            "free inference" if running else "gateway offline",
                            size=tokens.FONT_XS,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
                # Bounded, self-scrolling viewport, same shape as the log card:
                # 40+ models otherwise made the page an endless strip.
                *(
                    [
                        ft.Container(
                            height=tokens.CATALOG_VIEWPORT,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.ListView(
                                spacing=tokens.SPACE_SM,
                                controls=model_items,
                                # No auto_scroll: same reason as the log card.
                            ),
                        ),
                    ]
                    if model_items
                    else [
                        ft.Text(
                            "No models. Use Refresh models.",
                            size=tokens.FONT_SM,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ]
                ),
            ],
        ),
    )

    # ONE control tree: this row used to be inserted into two headers, which
    # is an invalid tree (a control with two parents) and duplicated the
    # title and the line count.
    filter_chips = ft.Row(
        spacing=tokens.SPACE_TIGHT,
        controls=[
            ft.Chip(
                label="ALL",
                selected=log_filter == "ALL",
                show_checkmark=False,
                on_select=lambda _: set_log_filter("ALL"),
            ),
            ft.Chip(
                label="INFO",
                selected=log_filter == "INFO",
                show_checkmark=False,
                on_select=lambda _: set_log_filter("INFO"),
            ),
            ft.Chip(
                label="WARNING",
                selected=log_filter == "WARNING",
                show_checkmark=False,
                on_select=lambda _: set_log_filter("WARNING"),
            ),
            ft.Chip(
                label="ERROR",
                selected=log_filter == "ERROR",
                show_checkmark=False,
                on_select=lambda _: set_log_filter("ERROR"),
            ),
        ],
    )

    return ft.Column(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        spacing=tokens.SPACE_LG,
        controls=[
            ft.Container(
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_MD,
                    vertical=tokens.SPACE_SM,
                ),
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Column(
                    spacing=tokens.SPACE_MD,
                    controls=[
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            # Zero slack here: dot, status, port, version and
                            # uptime overflowed a phone width.
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Container(
                                    width=tokens.DOT_SIZE,
                                    height=tokens.DOT_SIZE,
                                    border_radius=tokens.DOT_RADIUS,
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
                            size=tokens.FONT_SM,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            state.gateway_base_url,
                            size=tokens.FONT_BODY_SM,
                            font_family="monospace",
                            selectable=True,
                        ),
                        ft.Row(
                            spacing=tokens.SPACE_SM,
                            # Owner: one line, always. A narrow screen
                            # scrolls the row instead of wrapping it to a
                            # second line (Sherlock chip-row pattern).
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.FilledButton(
                                    # Reflect the in-flight start so a second
                                    # press cannot race the first.
                                    (
                                        "Stop"
                                        if running
                                        else (
                                            "Starting…"
                                            if state.gateway_starting
                                            else "Start gateway"
                                        )
                                    ),
                                    icon=(ft.Icons.STOP if running else ft.Icons.PLAY_ARROW),
                                    disabled=state.gateway_starting and not running,
                                    style=_compact_button_style(),
                                    on_click=lambda _: (
                                        methods.stop_gateway()
                                        if running
                                        else methods.start_gateway()
                                    ),
                                ),
                                ft.OutlinedButton(
                                    "Refresh models",
                                    icon=ft.Icons.REFRESH,
                                    style=_compact_button_style(),
                                    on_click=lambda _: methods.refresh_models(),
                                    disabled=not running,
                                ),
                                ft.OutlinedButton(
                                    "Open console",
                                    icon=ft.Icons.OPEN_IN_NEW,
                                    style=_compact_button_style(),
                                    on_click=lambda _: methods.open_gateway_console(),
                                    disabled=not running,
                                ),
                                *quit_button,
                            ],
                        ),
                    ],
                ),
            ),
            *([conn_card] if conn_card is not None else []),
            *([share_card] if share_card is not None else []),
            # Owner: banner after the share gateway, before the model
            # catalog, then one more between the catalog and the logs. None
            # after the logs.
            build_banner_ad(),
            catalog_card,
            build_banner_ad(),
            ft.Container(
                padding=ft.Padding.symmetric(
                    horizontal=tokens.SPACE_MD,
                    vertical=tokens.SPACE_SM,
                ),
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Column(
                    spacing=tokens.SPACE_TIGHT,
                    controls=[
                        ft.Container(
                            padding=ft.Padding(
                                left=tokens.SPACE_LG,
                                right=tokens.SPACE_LG,
                                top=tokens.SPACE_MD,
                                bottom=tokens.SPACE_MD,
                            ),
                            content=ft.Column(
                                spacing=tokens.SPACE_MD,
                                controls=[
                                    ft.Row(
                                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                        wrap=False,
                                        scroll=ft.ScrollMode.AUTO,
                                        controls=[
                                            ft.Row(
                                                spacing=tokens.SPACE_SM,
                                                controls=[
                                                    ft.Text(
                                                        "Logs",
                                                        size=tokens.FONT_MD,
                                                        weight=ft.FontWeight.W_600,
                                                    ),
                                                    ft.Text(
                                                        f"{len(log_rows)} lines",
                                                        size=tokens.FONT_SM,
                                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                                    ),
                                                ],
                                            ),
                                            filter_chips,
                                            ft.TextButton(
                                                "Copy logs",
                                                icon=ft.Icons.CONTENT_COPY_ROUNDED,
                                                tooltip="Copy log file",
                                                on_click=lambda _: methods.copy_logs(),
                                            ),
                                        ],
                                    ),
                                    ft.Container(
                                        height=tokens.LOG_VIEWPORT,
                                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                                        content=ft.ListView(
                                            spacing=tokens.SPACE_XXS,
                                            controls=log_rows,
                                            # No auto_scroll: it yanked the view
                                            # to the bottom on every new line,
                                            # so the owner could never scroll
                                            # up to find the error. "Copy logs"
                                            # still grabs the whole file.
                                        ),
                                    ),
                                ],
                            ),
                        ),
                    ],
                ),
            ),
        ],
    )
