"""Design tokens (Sherlock/voicelm naming, LM Router scale).

Radii follow the house rule: 16px is THE card radius. The smaller steps exist
only for nested/inline chrome (chips, caret rows), never for a card.
"""

from core.theme import PRIMARY

# spacing
SPACE_XXS = 2
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

# typography
FONT_XS = 11
FONT_BODY_SM = 13
FONT_MD = 14
FONT_LG = 20
FONT_XXL = 28

# icons
ICON_SM = 20
ICON_MD = 24
ICON_FEATURE = 96

# opacity / motion / radii
OPACITY_LIGHT = 0.12
OPACITY_MUTED = 0.5
ANIM_SLOW = 300
RADIUS_SM = 8
RADIUS_MD = 16
RADIUS_LG = 16
RADIUS_XL = 26
RADIUS_PILL = 999

__all__ = [
    "ANIM_SLOW",
    "FONT_BODY_SM",
    "FONT_LG",
    "FONT_MD",
    "FONT_XS",
    "FONT_XXL",
    "ICON_FEATURE",
    "ICON_MD",
    "ICON_SM",
    "OPACITY_LIGHT",
    "OPACITY_MUTED",
    "PRIMARY",
    "RADIUS_LG",
    "RADIUS_MD",
    "RADIUS_PILL",
    "RADIUS_SM",
    "RADIUS_XL",
    "SPACE_LG",
    "SPACE_MD",
    "SPACE_SM",
    "SPACE_XL",
    "SPACE_XS",
    "SPACE_XXS",
]
