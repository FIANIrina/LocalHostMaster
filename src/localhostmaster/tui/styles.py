"""Colour modes, category styles, and ASCII fallbacks."""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.styles.style import parse_color

from ..icons import ascii_fallback_for_glyph
from ..models import CategoryAssignment, CategoryRule

_SLUG_RE = re.compile(r"[^a-z0-9]+")

DEFAULT_CATEGORY_COLOR = "#9CA3AF"


def is_valid_color(value: Optional[str]) -> bool:
    """Whether ``value`` is a colour prompt_toolkit can parse.

    prompt_toolkit accepts named colours (``red``, ``ansired``), ``#RGB`` and
    ``#RRGGBB``. Anything else makes ``Style.from_dict`` raise, which would stop
    the TUI from starting, so it must be rejected up front.
    """
    if not value:
        return False
    try:
        parse_color(value.strip())
        return True
    except Exception:
        return False


# Compatibility ASCII labels for the builtin categories. These are more
# readable than a single glyph fallback ("DB"/"dev"/"sys"), so they take
# precedence for those known names; everything else goes through the central
# icon registry, and unknown legacy glyphs fall back to "*".
ASCII_ICONS = {
    "llama.cpp": "L",
    "docker": "D",
    "database": "DB",
    "dev-server": "dev",
    "system": "sys",
    "unknown": "?",
}


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "unnamed"


def category_style_name(name: str) -> str:
    return f"cat.{slugify(name)}"


def resolve_color_mode(mode: str, no_color_flag: bool = False) -> str:
    if no_color_flag or os.environ.get("NO_COLOR"):
        return "none"
    if mode in ("auto", "", None):
        return "auto"
    return mode


def color_depth_for(mode: str) -> Optional[ColorDepth]:
    if mode == "truecolor":
        return ColorDepth.TRUE_COLOR
    if mode == "256":
        return ColorDepth.DEPTH_8_BIT
    if mode == "16":
        return ColorDepth.DEPTH_4_BIT
    return None


def category_color(assignment: Optional[CategoryAssignment]) -> str:
    if assignment is None or not assignment.color:
        return DEFAULT_CATEGORY_COLOR
    return assignment.color


def category_label(
    assignment: Optional[CategoryAssignment], ascii_only: bool = False
) -> str:
    """Always include the category *text*; colour is never the only signal."""
    if assignment is None:
        return "unknown"
    icon = assignment.icon
    if ascii_only:
        icon = _ascii_icon(assignment)
    if icon:
        return f"{icon} {assignment.name}"
    return assignment.name


def _ascii_icon(assignment: CategoryAssignment) -> str:
    """Resolve the ASCII-only icon for an assignment.

    Known builtin names keep their readable label; otherwise the central icon
    registry is consulted by glyph, and an unknown legacy glyph becomes ``*``.
    """
    known = ASCII_ICONS.get(assignment.name)
    if known is not None:
        return known
    if assignment.icon:
        return ascii_fallback_for_glyph(assignment.icon)
    return ""


def base_style_dict(color_mode: str) -> dict[str, str]:
    """Style rules that do not depend on category colours."""
    return {
        "lm.title": "bold",
        "lm.subtitle": "dim",
        "lm.status": "",
        "lm.status.notice": "bold",
        "lm.status.error": "bold",
        "lm.status.ok": "bold",
        "lm.hint": "dim",
        "lm.key": "bold",
        "lm.header": "bold underline",
        "lm.row": "",
        "lm.selected": "reverse",
        "lm.dim": "dim",
        "lm.dialog": "",
        "lm.field": "bold",
        "lm.warning": "bold",
        "button": "",
        "button.focused": "reverse",
        "button.arrow": "bold",
        "button.text": "",
    }


def build_style_dict(
    color_mode: str,
    rules: Iterable[CategoryRule] = (),
) -> dict[str, str]:
    style_dict = base_style_dict(color_mode)
    if color_mode != "none":
        for rule in rules:
            color = rule.color or DEFAULT_CATEGORY_COLOR
            if not is_valid_color(color):
                # Never let a hand-written bad colour brick the UI: fall back
                # to the default instead of raising from Style.from_dict.
                color = DEFAULT_CATEGORY_COLOR
            style_dict[category_style_name(rule.name)] = f"fg:{color}"
    return style_dict


def build_style(
    color_mode: str, categories: Iterable[str] = (), ascii_only: bool = False
) -> Style:
    names = [
        CategoryRule(id=f"tmp.{name}", name=name) for name in categories
    ]
    return Style.from_dict(build_style_dict(color_mode, names))
