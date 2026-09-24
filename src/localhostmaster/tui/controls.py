"""Table rendering helpers (pure functions returning prompt_toolkit formatted text)."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from ..models import EndpointKey, PortEntry
from ..state import matches_search
from .styles import category_label, category_style_name

# key, title, width, align
COLUMNS: list[tuple[str, str, int, str]] = [
    ("proto", "PROTO", 5, "left"),
    ("address", "ADDRESS", 24, "left"),
    ("remote", "REMOTE", 22, "left"),
    ("port", "PORT", 5, "right"),
    ("pid", "PID", 7, "right"),
    ("process", "PROCESS", 21, "left"),
    ("container", "CONTAINER", 18, "left"),
    ("category", "CATEGORY", 15, "left"),
    ("open", "OPEN", 5, "left"),
]

# Columns are selected by width budget rather than fixed tiers, so a column
# (notably OPEN) is never shown in the list yet clipped off the right edge
# because a hard threshold was crossed. Order = most useful first.
_MIN_TERMINAL_WIDTH = 40
_COLUMN_PRIORITY = [
    "proto",
    "port",
    "process",
    "open",
    "category",
    "address",
    "pid",
    "container",
]
# ``remote`` only appears when connected rows are shown, and then it must be
# budgeted for like any other column (it is wide, so it lands near the end).
_REMOTE = "remote"
_COLUMN_WIDTH = {key: width for key, _title, width, _align in COLUMNS}


def select_columns(width: int, show_remote: bool = False) -> list[str]:
    if width < _MIN_TERMINAL_WIDTH:
        return []
    priority = list(_COLUMN_PRIORITY)
    if show_remote and _REMOTE not in priority:
        # Before ``container`` so a connected peer is visible in reasonably
        # wide layouts, but only when it actually fits.
        priority.insert(priority.index("container"), _REMOTE)
    chosen: set[str] = set()
    total = 0
    for key in priority:
        addition = _COLUMN_WIDTH[key] + (1 if chosen else 0)  # 1-cell separator
        if total + addition > width:
            # Skip this one; a narrower lower-priority column may still fit.
            continue
        chosen.add(key)
        total += addition
    # Return in canonical display order (COLUMNS order).
    return [key for key, _title, _width, _align in COLUMNS if key in chosen]


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "\u2026"


def _pad(text: str, width: int, align: str) -> str:
    text = truncate(text, width)
    if align == "right":
        return text.rjust(width)
    return text.ljust(width)


def window_slice(total: int, cursor: int, height: int) -> tuple[int, int]:
    if total <= 0 or height <= 0:
        return 0, 0
    if total <= height:
        return 0, total
    cursor = max(0, min(cursor, total - 1))
    start = cursor - height // 2
    start = max(0, min(start, total - height))
    return start, start + height


def _visible_entries(
    entries: Sequence[PortEntry],
    search: str,
    filter_spec: str,
) -> list[PortEntry]:
    from ..state import apply_filter

    result = apply_filter(list(entries), filter_spec)
    if search:
        result = [e for e in result if matches_search(e, search)]
    return result


def build_table(
    entries: Sequence[PortEntry],
    cursor: int,
    width: int,
    height: int,
    search: str = "",
    filter_spec: str = "all",
    armed_key: Optional[EndpointKey] = None,
    ascii_only: bool = False,
    show_remote: bool = False,
) -> list[tuple[str, str]]:
    # Column selection already budgets for the remote column when it is shown,
    # so rows are guaranteed to fit and remote never depends on another column
    # being present.
    keys = select_columns(width, show_remote=show_remote)
    selected_columns = [c for c in COLUMNS if c[0] in keys]

    if not selected_columns:
        return [
            ("class:lm.warning", "Terminal too narrow.\n"),
            ("class:lm.dim", "Widen the window to see the port table.\n"),
            ("class:lm.hint", "q or Ctrl-C to quit.\n"),
        ]

    total_width = sum(col[2] for col in selected_columns) + (len(selected_columns) - 1)
    formatted: list[tuple[str, str]] = []

    visible = _visible_entries(entries, search, filter_spec)
    start, end = window_slice(len(visible), cursor, max(1, height - 1))

    header = " ".join(_pad(col[1], col[2], col[3]) for col in selected_columns)
    formatted.append(("class:lm.header", header[:total_width] + "\n"))

    if not visible:
        formatted.append(("class:lm.dim", "  (no matching endpoints)\n"))
        return formatted

    for index in range(start, end):
        entry = visible[index]
        selected = index == cursor
        armed = armed_key is not None and entry.key == armed_key
        row_style = "class:lm.selected" if selected else "class:lm.row"
        marker = "> " if selected else "  "
        if armed:
            marker = "* " if not selected else "> "
        formatted.append((row_style, marker))

        for col_key, _, col_width, align in selected_columns:
            text = _cell_text(entry, col_key, ascii_only)
            cell = _pad(text, col_width, align)
            if col_key == "category" and not selected and entry.category is not None:
                style = "class:" + category_style_name(entry.category.name)
            else:
                style = row_style
            formatted.append((style, cell))
            formatted.append((row_style, " "))
        # trailing space cleanup
        formatted.pop()
        formatted.append((row_style, "\n"))

    return formatted


def _cell_text(entry: PortEntry, col_key: str, ascii_only: bool) -> str:
    if col_key == "proto":
        return entry.protocol.value
    if col_key == "address":
        return entry.local_address
    if col_key == "remote":
        return entry.remote_display if entry.has_remote else "-"
    if col_key == "port":
        return str(entry.port)
    if col_key == "pid":
        return str(entry.pid) if entry.pid is not None else "-"
    if col_key == "process":
        return entry.process_name
    if col_key == "container":
        if entry.container is None:
            return "-"
        return entry.container.name or entry.container.short_id or "container"
    if col_key == "category":
        return category_label(entry.category, ascii_only=ascii_only)
    if col_key == "open":
        from ..url_builder import is_openable

        return "yes" if is_openable(entry) else "-"
    return ""
