"""Settings: appearance, gateway, about."""

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


def _is_mobile_page(page) -> bool:
    """One platform probe, KTV's `_is_store_device` idiom: everything
    downstream (rate target, more-apps target) is honest about the device.
    Unknown page/platform -> False (desktop behavior)."""
    try:
        if page is None or not getattr(page, "platform", None):
            return False
        return bool(page.platform.is_mobile())
    except Exception:
        return False


def _open_legal_dialog(page, methods) -> None:
    """KTV's 'Usage Agreement & Legal Terms' slot: ONE button, one in-app
    dialog. The documents themselves live at kiri.ng (single source of
    truth; onboarding links there too), so the dialog offers both as rows
    instead of forking the legal copy into the app."""
    if page is None:
        return

    def _open_doc(url: str) -> None:
        page.pop_dialog()
        methods.open_url(url)

    def _doc(title: str, url: str):
        # Whole-row tap (setting_row rule): the old bare Row left only the
        # 14px icon tappable.
        return ft.Container(
            ink=True,
            border_radius=tokens.RADIUS_SM,
            padding=ft.Padding.symmetric(horizontal=tokens.SPACE_SM, vertical=tokens.SPACE_XS),
            on_click=lambda _e, u=url: _open_doc(u),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(title, size=tokens.FONT_BODY_SM),
                    ft.Icon(ft.Icons.OPEN_IN_NEW, size=tokens.ICON_XS),
                ],
            ),
        )

    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "Usage agreement & legal",
                size=tokens.FONT_TITLE,
                weight=ft.FontWeight.W_600,
            ),
            content=ft.Container(
                width=360,
                content=ft.Column(
                    spacing=tokens.SPACE_SM,
                    tight=True,
                    controls=[
                        ft.Text(
                            "Both documents live at kiri.ng and open in your browser.",
                            size=tokens.FONT_XS,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        _doc("Privacy Policy", "https://kiri.ng/privacy"),
                        _doc("Terms of Service", "https://kiri.ng/terms"),
                    ],
                ),
            ),
            actions=[
                ft.TextButton("Close", on_click=lambda _: page.pop_dialog()),
            ],
        )
    )


def _open_more_apps(page, methods, is_mobile: bool) -> None:
    """Sibling apps dialog (the family pattern KTV/DDGS/Sherlock share).

    Each entry opens that app's Play listing on phones and its GitHub repo
    on desktop, the same swap the Rate row uses."""
    if page is None:
        return
    entries = [
        ("Sherlock", "ng.kiri.sherlock", "Nwokike/Sherlock"),
        ("DDGS", "ng.kiri.ddgs", "Nwokike/DDGS"),
        ("KTV Player", "ng.kiri.ktvplayer", "Nwokike/ktv-player"),
    ]

    def _open_app(target: str) -> None:
        page.pop_dialog()
        methods.open_url(target)

    rows: list[ft.Control] = []
    for name, play_id, repo in entries:
        target = (
            f"https://play.google.com/store/apps/details?id={play_id}"
            if is_mobile
            else f"https://github.com/{repo}"
        )
        where = "Google Play" if is_mobile else "GitHub"
        rows.append(
            # Whole-row tap (setting_row rule): only the icon was tappable.
            ft.Container(
                ink=True,
                border_radius=tokens.RADIUS_SM,
                padding=ft.Padding.symmetric(horizontal=tokens.SPACE_SM, vertical=tokens.SPACE_XS),
                on_click=lambda _e, url=target: _open_app(url),
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Column(
                            spacing=0,
                            tight=True,
                            controls=[
                                ft.Text(name, size=tokens.FONT_BODY_SM),
                                ft.Text(
                                    where,
                                    size=tokens.FONT_2XS,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                        ),
                        ft.Icon(ft.Icons.OPEN_IN_NEW, size=tokens.ICON_XS),
                    ],
                ),
            )
        )
    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("More apps", size=tokens.FONT_TITLE, weight=ft.FontWeight.W_600),
            content=ft.Container(
                width=360,
                content=ft.Column(spacing=tokens.SPACE_SM, controls=rows, tight=True),
            ),
            actions=[ft.TextButton("Close", on_click=lambda _: page.pop_dialog())],
        )
    )


@ft.component
def SettingsScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    _ = state.update_info
    # Load settings ONCE. `AppSettings.load()` touches disk; calling it in
    # the render body re-read the file on every keystroke and every log bump.
    settings, set_settings = ft.use_state(lambda: AppSettings.load())

    # Re-read only when the controller says settings changed — and re-sync
    # every derived draft with it. Without this, an external settings change
    # (toggle from another surface, disk reload) left stale field values.
    def _reload_settings() -> None:
        fresh = AppSettings.load()
        set_settings(fresh)
        set_port_text(str(fresh.gateway_port))
        set_autostart(fresh.gateway_autostart)

    ft.use_effect(
        _reload_settings,
        [state.settings_version],
    )
    page = getattr(ft.context, "page", None)
    is_dark = theme.is_dark_mode(page, state.theme_mode)
    narrow = bool(page and getattr(page, "width", None) and page.width < 600)
    # Mobile opens the Play listing for rate/more-apps; desktop opens GitHub.
    is_mobile = _is_mobile_page(page)

    port_text, set_port_text = ft.use_state(str(settings.gateway_port))
    autostart, set_autostart = ft.use_state(settings.gateway_autostart)

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
        # Family rows (the pattern KTV and DDGS share): contact, rate with
        # a platform-swapped target, the sibling apps, and the legal links.
        # No licence line: the owner removed it once, standing rule.
        ft.Divider(),
        setting_row(
            icon=ft.Icons.MAIL_OUTLINE,
            title="Contact developer",
            subtitle=constants.CONTACT_EMAIL,
            stacked=narrow,
            on_click=lambda _: methods.open_url(f"mailto:{constants.CONTACT_EMAIL}"),
            # No trailing on_click: the row already handles the tap, and a
            # second handler on the icon double-fires open_url.
            trailing=ft.Icon(
                ft.Icons.OPEN_IN_NEW,
                size=tokens.ICON_XS,
            ),
        ),
        setting_row(
            icon=ft.Icons.STAR_ROUNDED,
            title="Rate 5 stars",
            subtitle="Rate us on Google Play" if is_mobile else "Star us on GitHub",
            stacked=narrow,
            on_click=lambda _: methods.open_url(
                constants.PLAYSTORE_URL if is_mobile else constants.GITHUB_REPO_URL
            ),
            trailing=ft.Icon(
                ft.Icons.OPEN_IN_NEW,
                size=tokens.ICON_XS,
            ),
        ),
        setting_row(
            icon=ft.Icons.APPS_ROUNDED,
            title="More apps",
            subtitle="Sherlock, DDGS, KTV Player",
            stacked=narrow,
            on_click=lambda _: _open_more_apps(page, methods, is_mobile),
        ),
        # KTV's exact legal slot: one divider, one button, one in-app
        # dialog (the two documents as rows inside it).
        ft.Divider(),
        ft.TextButton(
            "Usage agreement & legal",
            icon=ft.Icons.GAVEL_ROUNDED,
            on_click=lambda _: _open_legal_dialog(page, methods),
        ),
        setting_row(
            icon=ft.Icons.PRIVACY_TIP_OUTLINED,
            title="Ad privacy options",
            subtitle="Consent choices for personalized ads",
            stacked=narrow,
            on_click=lambda _: methods.open_ad_privacy_options(),
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
            build_banner_ad(),
            *about_card,
            # No banner at the floor: the owner banned them there. The spacer
            # stays last.
            ft.Container(height=tokens.SPACE_XXXL),
        ],
    )
