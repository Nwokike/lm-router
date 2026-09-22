"""Theme factories: Outfit font + indigo seed (MarkItDown palette lineage)."""

import flet as ft

PRIMARY = "#6366F1"
FONT = "Outfit"

# Served from assets_dir root: page.fonts maps family -> css file.
FONTS = {"Outfit": "outfit.css"}


def get_light_theme() -> ft.Theme:
    return ft.Theme(color_scheme_seed=PRIMARY, font_family=FONT, use_material3=True)


def get_dark_theme() -> ft.Theme:
    return ft.Theme(color_scheme_seed=PRIMARY, font_family=FONT, use_material3=True)


def is_dark_mode(page: object, theme_mode: str) -> bool:
    if theme_mode == "dark":
        return True
    if theme_mode == "light":
        return False
    if page is None:
        return False
    return page.platform_brightness == ft.ThemeMode.DARK
