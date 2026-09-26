"""Server screen: gateway status, local endpoints, model catalog, live logs."""

import flet as ft

from components.banner_ad import build_banner_ad
from core import logging as applog
from core import theme, tokens
from core.catalog import (
    chat_support_note,
    endpoint_label,
    rate_hint_label,
    status_label,
)
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
    # Share-key draft: typing must not write the settings JSON on every
    # keystroke (atomic rewrite + version bump + full screen reload PER KEY).
    # Commit on submit/blur — Sherlock's keyword-field pattern — and follow
    # regenerate through the prefs reload above.
    key_draft, set_key_draft = ft.use_state(prefs.share_key)
    ft.use_effect(lambda: set_key_draft(prefs.share_key), [prefs.share_key])

    def _commit_key(raw: object) -> None:
        value = str(raw or "").strip()
        if value != prefs.share_key:
            methods.save_settings({"share_key": value})

    def _test_pill(model_id: str) -> ft.Control:
        """Verdict chip + the Test/re-test affordance in one (console parity).

        Idle = "Test"; in-flight = spinner; done = colored verdict. Tapping a
        verdict runs the probe again, which is the per-model retry the owner
        asked for. Labels stay plain ("rate limited", never a source word).
        """
        if model_id in state.model_testing:
            return ft.Row(
                spacing=tokens.SPACE_XXS,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.ProgressRing(
                        width=tokens.ICON_XS,
                        height=tokens.ICON_XS,
                        stroke_width=2,
                    ),
                    ft.Text("testing", size=tokens.FONT_2XS, color=theme.dim(is_dark)),
                ],
            )
        result = state.model_test_results.get(model_id)
        if result:
            verdict = str(result.get("verdict") or "")
            ms = result.get("ms")
            if verdict == "OK":
                label, color = f"\u2713 {ms}ms", ft.Colors.GREEN
            elif verdict == "RATE":
                label, color = "\u2717 rate limited", ft.Colors.AMBER
            elif verdict == "EMPTY":
                label, color = "\u2717 no reply", ft.Colors.AMBER
            else:
                label = f"\u2717 failed ({ms}ms)" if ms else "\u2717 failed"
                color = ft.Colors.ERROR
            tooltip = f"Test again (last: {verdict})"
        else:
            label, color = "Test", theme.dim(is_dark)
            tooltip = "Probe this model through the gateway"
        return ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=tokens.SPACE_TIGHT,
                vertical=tokens.SPACE_XXS,
            ),
            border_radius=tokens.RADIUS_PILL,
            bgcolor=ft.Colors.with_opacity(tokens.OPACITY_FAINT, color),
            tooltip=tooltip,
            on_click=lambda _, mid=model_id: methods.test_model(mid),
            content=ft.Text(
                label,
                size=tokens.FONT_2XS,
                weight=ft.FontWeight.W_600,
                color=color,
            ),
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
                    f" · {model_counts.get('degraded', 0)} capped or slow"
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
                            value=key_draft,
                            label="API key",
                            hint_text="Generate a key or type one",
                            text_size=tokens.FONT_SM,
                            # TextField has no font_family in flet 1.0 — the
                            # font belongs to a TextStyle (a bare kwarg raises
                            # TypeError and took the whole Server tab down).
                            text_style=ft.TextStyle(font_family="monospace"),
                            expand=True,
                            on_change=lambda e: set_key_draft(str(e.control.value or "")),
                            on_submit=lambda e: _commit_key(e.control.value),
                            on_blur=lambda e: _commit_key(e.control.value),
                        ),
                        ft.IconButton(
                            ft.Icons.AUTORENEW_ROUNDED,
                            tooltip="Generate key",
                            on_click=lambda _: methods.regenerate_share_key(),
                        ),
                        ft.IconButton(
                            ft.Icons.CONTENT_COPY_ROUNDED,
                            tooltip="Copy key",
                            # Draft, not prefs: copy what the user SEES —
                            # blur/submit commits it moments later.
                            on_click=lambda _e, k=key_draft: methods.copy_text(k),
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
        if not prefs.require_share_key:
            # Inverted on purpose: the warning belongs to the INSECURE
            # configuration (no key required), not the secure one — the old
            # condition told users they had no key exactly when they did.
            controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(
                        horizontal=tokens.SPACE_SNUG,
                        vertical=tokens.SPACE_SM,
                    ),
                    border_radius=tokens.RADIUS_MD,
                    bgcolor=ft.Colors.with_opacity(tokens.OPACITY_FAINT, theme.WARNING),
                    content=ft.Text(
                        "Open sharing: anyone with this URL runs on your "
                        "connection, so their requests count against your "
                        "free-model rate limits. Turn on Require API key to "
                        "restrict who can use it.",
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

    def _not_ready_count() -> int:
        # "not ready" = the router's own non-active statuses (untested shows
        # as rate limited in the UI); this is the retest-not-ready button's N.
        return sum(
            1
            for row in state.models
            if isinstance(row, dict) and str(row.get("status", "active")).lower() != "active"
        )

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
                    f"{latency} ms",
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
                        status_label(status),
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
                    _test_pill(m_id),
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
                        ft.Row(
                            spacing=tokens.SPACE_TIGHT,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                *(
                                    [
                                        ft.Text(
                                            f"Testing {state.retest_progress[0]}/"
                                            f"{state.retest_progress[1]}\u2026",
                                            size=tokens.FONT_XS,
                                            color=theme.PRIMARY,
                                        ),
                                        ft.TextButton(
                                            "Stop",
                                            style=_compact_button_style(),
                                            on_click=lambda _: methods.stop_retest(),
                                        ),
                                    ]
                                    if state.retesting
                                    else [
                                        ft.TextButton(
                                            f"Retest not-ready ({_not_ready_count()})",
                                            style=_compact_button_style(),
                                            disabled=_not_ready_count() == 0,
                                            on_click=lambda _: methods.retest_models(True),
                                        ),
                                        ft.TextButton(
                                            "Test all",
                                            style=_compact_button_style(),
                                            disabled=models_count == 0,
                                            on_click=lambda _: methods.retest_models(False),
                                        ),
                                    ]
                                ),
                                ft.Text(
                                    "free inference" if running else "gateway offline",
                                    size=tokens.FONT_XS,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
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
    def _log_chip(label: str) -> ft.Container:
        """Filter toggle at the chat row's pill scale.

        Material Chips were taller than the log lines themselves (owner
        complaint: the filter bar out-sized the logs it filters). This is the
        same compact pill shape the SessionBar uses above the responses.
        """
        active = log_filter == label
        fg = theme.PRIMARY if active else theme.dim(is_dark)
        return ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=tokens.SPACE_SNUG,
                vertical=tokens.SPACE_XXS,
            ),
            border_radius=tokens.RADIUS_PILL,
            bgcolor=ft.Colors.with_opacity(
                tokens.OPACITY_MEDIUM if active else tokens.OPACITY_FAINT,
                fg,
            ),
            on_click=lambda _, lvl=label: set_log_filter(lvl),
            content=ft.Text(
                label,
                size=tokens.FONT_XS,
                weight=ft.FontWeight.W_600,
                color=fg,
            ),
        )

    filter_chips = ft.Row(
        spacing=tokens.SPACE_TIGHT,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            _log_chip("ALL"),
            _log_chip("INFO"),
            _log_chip("WARNING"),
            _log_chip("ERROR"),
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
                                        "Stop gateway"
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
                                                "Copy log file",
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
                                            # up to find the error. "Copy log file"
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
