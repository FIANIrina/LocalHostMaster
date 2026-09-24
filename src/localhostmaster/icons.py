"""Central preset icon registry for categories.

The TOML schema still stores the *actual glyph* (``icon = "◆"``); this registry
is the single source of truth for which glyphs the UI may offer, what they are
called, and how they degrade in ASCII-only mode.

Every preset glyph is a single code point whose display width is 1. Width is
measured with :func:`prompt_toolkit.utils.get_cwidth` (prompt_toolkit's own
public helper), so no extra Unicode dependency is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from prompt_toolkit.utils import get_cwidth

# Fallback used for an unknown (legacy) glyph so it never disappears entirely.
LEGACY_ASCII_FALLBACK = "*"


@dataclass(frozen=True)
class IconOption:
    id: str
    glyph: str
    label: str
    ascii_fallback: str


# Ordered preset set. The first five glyphs are the ones already used by the
# built-in categories, so existing visuals do not change.
PRESET_ICONS: Tuple[IconOption, ...] = (
    IconOption("diamond", "\u25c6", "Diamond", "*"),
    IconOption("square_marked", "\u25a3", "Marked square", "#"),
    IconOption("square_striped", "\u25a4", "Striped square", "="),
    IconOption("play_small", "\u25b8", "Small arrow", ">"),
    IconOption("bullet", "\u2022", "Bullet", "."),
    IconOption("dot", "\u00b7", "Dot", "."),
    IconOption("circle", "\u25cf", "Circle", "o"),
    IconOption("square", "\u25a0", "Square", "#"),
    IconOption("triangle", "\u25b2", "Triangle", "^"),
    IconOption("star", "\u2605", "Star", "*"),
    IconOption("diamond_hollow", "\u25c7", "Diamond outline", "o"),
    IconOption("square_hollow", "\u25a1", "Square outline", "s"),
    IconOption("circle_hollow", "\u25cb", "Circle outline", "o"),
    IconOption("target", "\u25c9", "Target", "O"),
    IconOption("hexagon", "\u2b22", "Hexagon", "H"),
    IconOption("spark", "\u2726", "Spark", "+"),
)

DEFAULT_ICON_ID = "diamond"

_BY_ID = {option.id: option for option in PRESET_ICONS}
_BY_GLYPH = {option.glyph: option for option in PRESET_ICONS}


def preset_icons() -> Tuple[IconOption, ...]:
    return PRESET_ICONS


def default_icon() -> IconOption:
    return _BY_ID[DEFAULT_ICON_ID]


def icon_by_id(icon_id: str) -> Optional[IconOption]:
    return _BY_ID.get(icon_id)


def icon_by_glyph(glyph: str) -> Optional[IconOption]:
    if not glyph:
        return None
    return _BY_GLYPH.get(glyph)


def is_preset_glyph(glyph: str) -> bool:
    return glyph in _BY_GLYPH


def ascii_fallback_for_glyph(glyph: str) -> str:
    """ASCII fallback for a stored glyph; ``*`` for an unknown legacy glyph."""
    option = icon_by_glyph(glyph)
    if option is not None:
        return option.ascii_fallback
    return LEGACY_ASCII_FALLBACK


def render_glyph(glyph: str, ascii_only: bool = False) -> str:
    """Render a stored glyph, degrading to its ASCII fallback when needed."""
    if not glyph:
        return ""
    if ascii_only:
        return ascii_fallback_for_glyph(glyph)
    return glyph


def glyph_for_id(icon_id: str, ascii_only: bool = False) -> str:
    option = icon_by_id(icon_id)
    if option is None:
        return ""
    return option.ascii_fallback if ascii_only else option.glyph


def icon_width(glyph: str) -> int:
    return get_cwidth(glyph)
