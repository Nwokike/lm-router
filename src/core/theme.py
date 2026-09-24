"""Theme factories: the house slate palette + Outfit, shared across the app.

Values match the owner's other production apps (DDGS, Sherlock, KTV Player):
slate surfaces rather than raw Material tonal greys, a single indigo accent,
one 16px radius scale, and a consistent spacing ramp. Screens pull from
core.tokens so nothing hardcodes a size.
"""

import flet as ft

# Accent (the router's own brand accent).
PRIMARY = "#6366F1"
PRIMARY_DARK = "#4F46E5"
ACCENT = "#0EA5E9"
FONT = "Outfit"

# Dark slate neutrals — the house palette.
DARK_BG = "#0F1114"
DARK_BG_ALT = "#121518"
DARK_SURFACE = "#1A1D22"
DARK_SURFACE_2 = "#252A30"
DARK_BORDER = "#2E3339"
DARK_TEXT = "#ECEFF1"
DARK_DIM = "#90A4AE"

# Light neutrals.
LIGHT_BG = "#FAFAFA"
LIGHT_SURFACE = "#FFFFFF"
LIGHT_SURFACE_2 = "#F5F5F5"
LIGHT_BORDER = "#E0E0E0"
LIGHT_TEXT = "#1A1A2E"
LIGHT_DIM = "#757575"

# Semantic status colours (shared, not Material defaults).
SUCCESS = "#2E7D32"
WARNING = "#F9A825"
ERROR = "#D32F2F"

# Served from the assets_dir root. Flet 1.0 accepts .ttf/.ttc/.otf or an
# absolute URL; a .css value is ignored and the app falls back to the system
# font, so the binary is referenced directly.
FONTS = {"Outfit": "fonts/Outfit-Regular.ttf"}


def get_light_theme() -> ft.Theme:
    return ft.Theme(
        color_scheme_seed=PRIMARY,
        font_family=FONT,
        use_material3=True,
        scaffold_bgcolor=LIGHT_BG,
        canvas_color=LIGHT_BG,
        card_bgcolor=LIGHT_SURFACE,
    )


def get_dark_theme() -> ft.Theme:
    return ft.Theme(
        color_scheme_seed=PRIMARY,
        font_family=FONT,
        use_material3=True,
        scaffold_bgcolor=DARK_BG,
        canvas_color=DARK_BG_ALT,
        card_bgcolor=DARK_SURFACE,
    )


def is_dark_mode(page: object, theme_mode: str) -> bool:
    if theme_mode == "dark":
        return True
    if theme_mode == "light":
        return False
    if page is None:
        return False
    # platform_brightness is None until the client reports it (and absent on
    # some platforms), so read it defensively. It is a Brightness enum, not a
    # ThemeMode: comparing the two was always False.
    return getattr(page, "platform_brightness", None) == ft.Brightness.DARK


def surface(is_dark: bool) -> str:
    return DARK_SURFACE if is_dark else LIGHT_SURFACE


def surface_2(is_dark: bool) -> str:
    return DARK_SURFACE_2 if is_dark else LIGHT_SURFACE_2


def border(is_dark: bool) -> str:
    return DARK_BORDER if is_dark else LIGHT_BORDER


def text_color(is_dark: bool) -> str:
    return DARK_TEXT if is_dark else LIGHT_TEXT


def dim(is_dark: bool) -> str:
    return DARK_DIM if is_dark else LIGHT_DIM


def glass(is_dark: bool, opacity: float = 0.06) -> str:
    """Low-alpha overlay used for cards sitting on the page background."""
    return ft.Colors.with_opacity(opacity, ft.Colors.WHITE if is_dark else ft.Colors.BLACK)


def markdown(
    content: str,
    *,
    is_dark: bool,
    selectable: bool = True,
    on_tap_link: object | None = None,
) -> ft.Markdown:
    """The app's one Markdown constructor.

    `extension_set` defaults to NONE, which is plain CommonMark with no extra
    syntax — so GFM tables rendered as literal pipe text. Always going through
    this helper means a site-wide setting can never be forgotten again.
    """
    return ft.Markdown(
        content,
        selectable=selectable,
        code_theme=ft.MarkdownCodeTheme.MONOKAI if is_dark else ft.MarkdownCodeTheme.GITHUB,
        extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED,
        on_tap_link=on_tap_link,
        md_style_sheet=ft.MarkdownStyleSheet(
            table_head_text_style=ft.TextStyle(
                weight=ft.FontWeight.W_600,
                color=text_color(is_dark),
            ),
            table_body_text_style=ft.TextStyle(color=text_color(is_dark)),
            table_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            table_cells_decoration=ft.BoxDecoration(
                border=ft.Border.all(1, border(is_dark)),
                border_radius=8,
            ),
        ),
    )
