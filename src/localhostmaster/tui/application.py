"""Full-screen prompt_toolkit application for LocalhostMaster."""

from __future__ import annotations

import time
from typing import Callable, Optional

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Dimension,
    DynamicContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.widgets import Button, Label, TextArea

from .. import __version__
from ..config import AppConfig, default_categories_path
from ..models import EndpointKey, PortEntry, Protocol, Snapshot
from ..state import (
    ArmDecision,
    OpenArmController,
    apply_filter,
    clamp_cursor,
    matches_search,
    next_sort_mode,
    retain_selection,
    sort_entries,
)
from ..url_builder import is_openable, url_for_entry
from .controls import build_table
from .styles import (
    build_style_dict,
    category_style_name,
    color_depth_for,
    resolve_color_mode,
)

MAIN = "main"
HELP = "help"
SEARCH = "search"
FILTER = "filter"
CATEGORIES = "categories"
FORM = "category_form"
EDIT_MODES = {SEARCH, FORM}

HELP_TEXT = [
    ("class:lm.header", "LocalhostMaster - help\n\n"),
    ("class:lm.dim", "Navigation\n"),
    ("", "  Up/Down, j/k        move cursor\n"),
    ("", "  PageUp/PageDown     page\n"),
    ("", "  Home/End            first / last\n\n"),
    ("class:lm.dim", "Actions\n"),
    ("", "  Enter               arm; press again within 2.5s to open\n"),
    ("", "  Esc                 cancel confirmation / close dialog\n"),
    ("", "  r                   refresh now\n"),
    ("", "  Space               pause / resume auto refresh\n"),
    ("", "  /                   search\n"),
    ("", "  f                   filter\n"),
    ("", "  s                   cycle sort (port/process/category/pid)\n"),
    ("", "  a                   toggle established TCP connections\n"),
    ("", "  c                   copy URL of selected TCP endpoint\n"),
    ("", "  C                   manage categories\n"),
    ("", "  ?                   this help\n"),
    ("", "  q / Ctrl-C          quit\n\n"),
    ("class:lm.dim", "Notes\n"),
    ("", "  Only TCP endpoints can be opened in a browser.\n"),
    ("", "  IPv6 URLs use brackets, e.g. http://[::1]:8080/\n"),
    ("", "  Wildcard bind addresses map to 127.0.0.1 / [::1].\n"),
    ("", "  A category is always shown as text; colour is not the only signal.\n\n"),
    ("class:lm.hint", "Press any key to return."),
]

FILTER_OPTIONS = ["all", "container", "openable"]


class LocalhostMasterApp:
    def __init__(
        self,
        config: AppConfig,
        classifier,
        resolver,
        docker,
        opener,
        clipboard,
        categories_path=None,
        user_rules=None,
        warnings: Optional[list[str]] = None,
        no_color: bool = False,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.resolver = resolver
        self.docker = docker
        self.opener = opener
        self.clipboard = clipboard
        self.categories_path = categories_path or default_categories_path()
        self.user_rules = list(user_rules or [])
        self.warnings = list(warnings or [])

        self.color_mode = resolve_color_mode(config.color_mode, no_color)
        self.ascii_only = self.color_mode == "none"

        self.mode = MAIN
        self._active_root = None
        self._pending_focus = None

        self.show_established = config.show_established
        self._all_entries: list[PortEntry] = []
        self.view: list[PortEntry] = []
        self.cursor = 0
        self._cursor_key: Optional[EndpointKey] = None
        self.sort_mode = "port"
        self.filter_spec = "all"
        self.search_query = ""
        self.filter_cursor = 0
        self.category_cursor = 0
        self._category_delete_confirm = False
        self._editing_rule = None
        self._form_fields: dict[str, TextArea] = {}
        self._form_buttons: list[Button] = []

        self.arm = OpenArmController(
            guard_s=max(0.0, config.arm_guard_ms / 1000.0),
            timeout_s=max(0.1, config.double_enter_ms / 1000.0),
        )
        self.notice: Optional[tuple[str, str, float]] = None
        self._paused = False
        self._last_generation = -1
        self._first_seen: dict[EndpointKey, float] = {}
        self.worker = None

        self._style_dict = build_style_dict(self.color_mode, self._style_rules())
        self._style = Style.from_dict(self._style_dict)

        self.kb = self._make_keybindings()
        self._main_root = self._build_main_root()
        self._active_root = self._main_root

        self.app = Application(
            layout=Layout(DynamicContainer(self._get_root)),
            key_bindings=self.kb,
            style=DynamicStyle(self._get_style),
            full_screen=True,
            mouse_support=False,
            refresh_interval=1.0,
            color_depth=color_depth_for(self.color_mode),
        )
        self.app.pre_run_callables.append(self._pre_run)

    # ------------------------------------------------------------------ style
    def _style_rules(self):
        return list(self.classifier.user_rules) + list(self.classifier.builtin_rules)

    def _get_style(self):
        return self._style

    def _refresh_category_styles(self) -> None:
        self._style_dict = build_style_dict(self.color_mode, self._style_rules())
        self._style = Style.from_dict(self._style_dict)

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        self.app.run()

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def _pre_run(self) -> None:
        try:
            self.refresh_now()
        except Exception as exc:  # pragma: no cover - defensive
            self._set_notice(f"initial scan failed: {exc}", "error")
        self._start_worker()

    def _start_worker(self) -> None:
        from ..refresh import RefreshWorker

        self.worker = RefreshWorker(
            build=self._build_snapshot,
            interval_ms=self.config.refresh_ms,
            on_snapshot=self._on_worker_snapshot,
            on_error=self._on_worker_error,
        )
        self.worker.start()

    def refresh_now(self) -> None:
        snapshot = self._build_snapshot(0)
        if snapshot is not None:
            self._apply_snapshot(snapshot)

    def _build_snapshot(self, generation: int) -> Snapshot:
        from ..scanner import scan_connections
        from ..refresh import build_snapshot

        raw = scan_connections(include_established=self.show_established)
        docker = self.docker if self.config.docker_enabled else None
        docker_ok = True if docker is None else bool(getattr(docker, "available", True))
        snapshot = build_snapshot(
            raw,
            self.resolver,
            self.classifier,
            docker=docker,
            generation=generation,
            previous_first_seen=self._first_seen,
        )
        snapshot.docker_ok = docker_ok
        self._first_seen = {e.key: e.first_seen for e in snapshot.entries}
        return snapshot

    def _on_worker_snapshot(self, snapshot: Snapshot) -> None:
        self._schedule(lambda: self._apply_snapshot(snapshot))

    def _on_worker_error(self, exc: Exception) -> None:
        self._schedule(lambda: self._set_notice(f"scan error: {type(exc).__name__}", "error"))

    def _schedule(self, fn: Callable[[], None]) -> None:
        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(fn)
        else:
            fn()

    # --------------------------------------------------------------- snapshot
    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        if snapshot.generation < self._last_generation:
            return
        self._last_generation = snapshot.generation
        self._all_entries = sort_entries(snapshot.entries, self.sort_mode)

        present = {}
        for entry in self._all_entries:
            if is_openable(entry):
                present[entry.key] = url_for_entry(entry)
        self.arm.reconcile(present)

        self._recompute_view()
        if snapshot.error:
            self._set_notice(snapshot.error, "error")
        self._invalidate()

    def _recompute_view(self) -> None:
        view = apply_filter(self._all_entries, self.filter_spec)
        if self.search_query:
            view = [e for e in view if matches_search(e, self.search_query)]
        self.view = view
        self.cursor = retain_selection(self.view, self._cursor_key)
        self.cursor = clamp_cursor(self.cursor, len(self.view))
        self._cursor_key = self.view[self.cursor].key if self.view else None

    # -------------------------------------------------------------- rendering
    def _get_root(self):
        return self._active_root

    def _build_main_root(self) -> HSplit:
        self._table_control = FormattedTextControl(
            text=self._render_table,
            focusable=True,
            show_cursor=False,
        )
        return HSplit(
            [
                Window(
                    FormattedTextControl(self._render_title),
                    height=Dimension.exact(1),
                    style="class:lm.title",
                ),
                Window(content=self._table_control, wrap_lines=False),
                Window(
                    FormattedTextControl(self._render_status),
                    height=Dimension.exact(1),
                    style="class:lm.status",
                ),
                Window(
                    FormattedTextControl(self._render_hint),
                    height=Dimension.exact(1),
                    style="class:lm.hint",
                ),
            ]
        )

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = get_app().output.get_size()
            return size.columns, size.rows
        except Exception:
            return 100, 30

    def _render_table(self):
        columns, rows = self._terminal_size()
        body_height = max(1, rows - 3)
        return build_table(
            self.view,
            self.cursor,
            columns,
            body_height,
            search=self.search_query,
            filter_spec=self.filter_spec,
            armed_key=self.arm.armed_key(),
            ascii_only=self.ascii_only,
            show_remote=self.show_established,
        )

    def _render_title(self):
        docker_state = "on" if self.config.docker_enabled else "off"
        if self.config.docker_enabled and self.docker is not None:
            docker_state = "ok" if getattr(self.docker, "available", False) else "starting"
        text = (
            f" LocalhostMaster {__version__}  |  endpoints: {len(self.view)}/{len(self._all_entries)}"
            f"  |  sort: {self.sort_mode}  filter: {self.filter_spec}"
            f"  |  docker: {docker_state}"
        )
        if self.search_query:
            text += f"  search: {self.search_query}"
        if self._paused:
            text += "  [PAUSED]"
        return text + "\n"

    def _render_status(self):
        notice = self._active_notice()
        if notice is not None:
            text, kind = notice
            style = {
                "error": "class:lm.status.error",
                "ok": "class:lm.status.ok",
                "info": "class:lm.status.notice",
            }.get(kind, "class:lm.status.notice")
            return [(style, f" {text}")]
        if self.arm.is_armed and self.arm.armed is not None:
            remaining = self.arm.remaining()
            return [
                (
                    "class:lm.status.notice",
                    f" Press Enter again within {remaining:.1f}s to open {self.arm.armed.url}",
                )
            ]
        if self.warnings:
            return [("class:lm.status.error", f" {self.warnings[0]}")]
        return [("class:lm.dim", " Ready. Press ? for help, q to quit.")]

    def _render_hint(self):
        return (
            " [\u2191\u2193] move  [Enter] open  [r] refresh  [Space] pause  [/] search"
            "  [f] filter  [s] sort  [a] conns  [c] copy  [C] categories  [?] help  [q] quit"
        )

    # ---------------------------------------------------------------- notices
    def _set_notice(self, text: str, kind: str = "info", ttl: float = 4.0) -> None:
        self.notice = (text, kind, time.monotonic() + ttl)
        self._invalidate()

    def _active_notice(self):
        if self.notice is None:
            return None
        text, kind, expires = self.notice
        if time.monotonic() > expires:
            self.notice = None
            return None
        return text, kind

    def _invalidate(self) -> None:
        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            self.app.invalidate()
        else:
            try:
                get_app().invalidate()
            except Exception:
                pass

    def _current_entry(self) -> Optional[PortEntry]:
        if 0 <= self.cursor < len(self.view):
            return self.view[self.cursor]
        return None

    # ------------------------------------------------------------- navigation
    def _move(self, delta: int) -> None:
        if not self.view:
            return
        self.cursor = clamp_cursor(self.cursor + delta, len(self.view))
        self._cursor_key = self.view[self.cursor].key
        self._cancel_arm()
        self._invalidate()

    def _move_to(self, index: int) -> None:
        if not self.view:
            return
        self.cursor = clamp_cursor(index, len(self.view))
        self._cursor_key = self.view[self.cursor].key
        self._cancel_arm()
        self._invalidate()

    def _cancel_arm(self) -> None:
        if self.arm.is_armed:
            self.arm.cancel()

    # ------------------------------------------------------------- key actions
    def _handle_enter(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if not is_openable(entry):
            if entry.protocol == Protocol.UDP:
                self._set_notice("UDP endpoint cannot be opened in a browser", "error")
            else:
                self._set_notice("This endpoint cannot be opened in a browser", "error")
            self._cancel_arm()
            return
        url = url_for_entry(entry)
        decision = self.arm.press(entry.key, url)
        if decision == ArmDecision.IGNORED:
            self._invalidate()
            return
        if decision == ArmDecision.ARMED:
            self._set_notice(
                f"Press Enter again within {self.config.double_enter_ms / 1000:.1f}s to open {url}",
                "info",
                ttl=self.config.double_enter_ms / 1000,
            )
            self._invalidate()
            return
        if decision == ArmDecision.OPEN:
            self._open_url(url)

    def _open_url(self, url: str) -> None:
        try:
            self.opener.open(url)
            self._set_notice(f"Opened {url}", "ok")
        except Exception as exc:
            self._set_notice(f"Could not open browser: {exc}", "error")

    def _copy_url(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if not is_openable(entry):
            self._set_notice("No URL to copy for this endpoint", "error")
            return
        url = url_for_entry(entry)
        try:
            self.clipboard.copy(url)
            self._set_notice(f"Copied {url}", "ok")
        except Exception as exc:
            self._set_notice(f"Could not copy URL: {exc}", "error")

    def _toggle_sort(self) -> None:
        self.sort_mode = next_sort_mode(self.sort_mode)
        self._all_entries = sort_entries(self._all_entries, self.sort_mode)
        self._cancel_arm()
        self._recompute_view()
        self._invalidate()

    def _toggle_established(self) -> None:
        self.show_established = not self.show_established
        self._cancel_arm()
        self._set_notice(
            "Showing established connections" if self.show_established else "Hiding established connections",
            "info",
        )
        self._trigger_refresh()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        if self.worker is not None:
            if self._paused:
                self.worker.pause()
            else:
                self.worker.resume()
        self._set_notice("Auto refresh paused" if self._paused else "Auto refresh resumed", "info")

    def _trigger_refresh(self) -> None:
        if self.worker is not None:
            self.worker.trigger()
        self._invalidate()

    # ------------------------------------------------------------------ modes
    def _switch_mode(self, mode: str, root, focus=None) -> None:
        self.mode = mode
        self._active_root = root
        self._pending_focus = focus
        self._invalidate()
        if focus is not None:
            loop = getattr(self.app, "loop", None)
            if loop is not None and loop.is_running():
                self.app.layout.focus(focus)
            else:
                try:
                    self.app.layout.focus(focus)
                except Exception:
                    pass

    def _return_to_main(self) -> None:
        self.mode = MAIN
        self._active_root = self._main_root
        self._pending_focus = None
        try:
            self.app.layout.focus(self._table_control)
        except Exception:
            pass
        self._invalidate()

    def _open_help(self) -> None:
        root = HSplit(
            [
                Window(FormattedTextControl(HELP_TEXT), wrap_lines=True),
            ]
        )
        self._switch_mode(HELP, root)

    # ---------------------------------------------------------------- search
    def _build_search_root(self):
        self._search_area = TextArea(
            text=self.search_query,
            multiline=False,
            prompt=" Search: ",
            height=Dimension.exact(1),
        )
        kb = KeyBindings()

        @kb.add("enter")
        def _accept(event) -> None:
            self.search_query = self._search_area.text
            self._cancel_arm()
            self._recompute_view()
            self._return_to_main()

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event) -> None:
            self._return_to_main()

        self._search_area.control.key_bindings = kb
        return HSplit(
            [
                Window(FormattedTextControl([("class:lm.header", " Search\n")]), height=1),
                self._search_area,
                Window(
                    FormattedTextControl(
                        [("class:lm.hint", " Enter: apply   Esc: cancel   (matches address, port, process, category)")]
                    ),
                    height=1,
                ),
            ]
        )

    def _open_search(self) -> None:
        root = self._build_search_root()
        self._cancel_arm()
        self._switch_mode(SEARCH, root, focus=self._search_area)

    # ---------------------------------------------------------------- filter
    def _filter_options(self) -> list[str]:
        names = sorted(
            {e.category.name for e in self._all_entries if e.category is not None}
        )
        return FILTER_OPTIONS + [f"category:{n}" for n in names]

    def _build_filter_root(self):
        self._filter_control = FormattedTextControl(
            self._render_filter, focusable=True, show_cursor=False
        )
        return HSplit(
            [
                Window(FormattedTextControl([("class:lm.header", " Filter\n")]), height=1),
                Window(content=self._filter_control, wrap_lines=False),
            ]
        )

    def _render_filter(self):
        lines = []
        for index, option in enumerate(self._filter_options()):
            marker = "> " if index == self.filter_cursor else "  "
            style = "class:lm.selected" if index == self.filter_cursor else "class:lm.row"
            lines.append((style, f"{marker}{option}\n"))
        lines.append(("class:lm.hint", " Up/Down: choose   Enter: apply   Esc: cancel"))
        return lines

    def _open_filter(self) -> None:
        self.filter_cursor = 0
        self._switch_mode(FILTER, self._build_filter_root(), focus=self._filter_control)

    def _filter_move(self, delta: int) -> None:
        options = self._filter_options()
        self.filter_cursor = clamp_cursor(self.filter_cursor + delta, len(options))
        self._invalidate()

    def _filter_apply(self) -> None:
        options = self._filter_options()
        if 0 <= self.filter_cursor < len(options):
            self.filter_spec = options[self.filter_cursor]
        self._cancel_arm()
        self._recompute_view()
        self._return_to_main()

    # ------------------------------------------------------------ categories
    def _all_rules(self):
        builtin = list(self.classifier.builtin_rules)
        user = list(self.user_rules)
        return [("builtin", r) for r in builtin] + [("user", r) for r in user]

    def _build_categories_root(self):
        self._category_control = FormattedTextControl(
            self._render_categories, focusable=True, show_cursor=False
        )
        self._category_delete_confirm = False
        return HSplit(
            [
                Window(
                    FormattedTextControl([("class:lm.header", " Categories - builtin (read-only) and user\n")]),
                    height=1,
                ),
                Window(content=self._category_control, wrap_lines=False),
            ]
        )

    def _render_categories(self):
        lines = []
        rules = self._all_rules()
        if not rules:
            lines.append(("class:lm.dim", "  (no categories)\n"))
        for index, (source, rule) in enumerate(rules):
            marker = "> " if index == self.category_cursor else "  "
            selected = index == self.category_cursor
            style = "class:lm.selected" if selected else "class:lm.row"
            tag = "[user]" if source == "user" else "[builtin]"
            range_bits = []
            if rule.process_globs:
                range_bits.append("proc=" + ",".join(rule.process_globs))
            if rule.ports:
                range_bits.append("ports=" + ",".join(str(p) for p in rule.ports))
            if rule.match_any_container:
                range_bits.append("any-container")
            detail = " ".join(range_bits)
            lines.append((style, f"{marker}{rule.name:<16} {tag:<10} prio={rule.priority:<4} {detail}\n"))
        lines.append(
            (
                "class:lm.hint",
                " n: new   e: edit   d: delete (user only)   Esc: close",
            )
        )
        if self._category_delete_confirm:
            lines.append(("class:lm.status.error", "  Press d again to confirm deletion, any other key cancels.\n"))
        return lines

    def _category_move(self, delta: int) -> None:
        self.category_cursor = clamp_cursor(self.category_cursor + delta, len(self._all_rules()))
        self._category_delete_confirm = False
        self._invalidate()

    def _open_categories(self) -> None:
        self.category_cursor = 0
        self._switch_mode(CATEGORIES, self._build_categories_root(), focus=self._category_control)

    def _selected_category_rule(self):
        rules = self._all_rules()
        if 0 <= self.category_cursor < len(rules):
            return rules[self.category_cursor]
        return None

    def _category_new(self) -> None:
        entry = self._current_entry()
        prefill = {
            "name": "",
            "color": "#7C3AED",
            "icon": "",
            "priority": "50",
            "process_globs": "",
            "ports": "",
            "protocols": "",
            "scheme": "http",
            "open_in_browser": "true",
        }
        if entry is not None:
            if entry.process is not None and entry.process.name:
                prefill["process_globs"] = entry.process.name
            prefill["ports"] = str(entry.port)
            prefill["protocols"] = entry.protocol.value.lower()
            if entry.category is not None and entry.category.name not in ("", "unknown"):
                prefill["name"] = f"{entry.category.name}-custom"
            else:
                prefill["name"] = "my-category"
        self._editing_rule = None
        self._open_form(prefill, title="New category")

    def _category_edit(self) -> None:
        selected = self._selected_category_rule()
        if selected is None:
            return
        source, rule = selected
        if source != "user":
            self._set_notice("Builtin categories are read-only; create a user rule to override", "error")
            return
        prefill = {
            "name": rule.name,
            "color": rule.color or "#7C3AED",
            "icon": rule.icon,
            "priority": str(rule.priority),
            "process_globs": ",".join(rule.process_globs),
            "ports": ",".join(str(p) for p in rule.ports),
            "protocols": ",".join(rule.protocols),
            "scheme": rule.scheme or "http",
            "open_in_browser": "true" if rule.open_in_browser else "false",
        }
        self._editing_rule = rule
        self._open_form(prefill, title="Edit category")

    def _category_delete(self) -> None:
        selected = self._selected_category_rule()
        if selected is None:
            return
        source, rule = selected
        if source != "user":
            self._set_notice("Builtin categories cannot be deleted", "error")
            return
        if not self._category_delete_confirm:
            self._category_delete_confirm = True
            self._invalidate()
            return
        self._category_delete_confirm = False
        remaining = [r for r in self.user_rules if r.id != rule.id]
        if self._commit_user_rules(remaining):
            self._set_notice(f"Deleted category {rule.name}", "ok")
            self._switch_mode(CATEGORIES, self._build_categories_root(), focus=self._category_control)

    # -------------------------------------------------------------- form
    def _open_form(self, prefill: dict, title: str) -> None:
        self._form_title = title
        fields = [
            ("name", "Name", True),
            ("color", "Color", False),
            ("icon", "Icon", False),
            ("priority", "Priority", False),
            ("process_globs", "Process globs", False),
            ("ports", "Ports/ranges", False),
            ("protocols", "Protocols", False),
            ("scheme", "Scheme", False),
            ("open_in_browser", "Open in browser", False),
        ]
        self._form_fields = {}
        rows = [
            Window(
                FormattedTextControl([("class:lm.header", f" {title}\n")]),
                height=1,
            )
        ]
        form_kb = KeyBindings()

        @form_kb.add("tab")
        @form_kb.add("down")
        def _next(event) -> None:
            focus_next(event)

        @form_kb.add("s-tab")
        @form_kb.add("up")
        def _prev(event) -> None:
            focus_previous(event)

        @form_kb.add("escape")
        @form_kb.add("c-c")
        def _cancel(event) -> None:
            self._return_to_categories()

        @form_kb.add("enter")
        def _accept(event) -> None:
            focus_next(event)

        for key, label, _required in fields:
            area = TextArea(
                text=prefill.get(key, ""),
                multiline=False,
                height=Dimension.exact(1),
            )
            area.control.key_bindings = form_kb
            self._form_fields[key] = area
            rows.append(
                VSplit(
                    [
                        Label(f" {label:<16}", width=18, style="class:lm.field"),
                        area,
                    ],
                    height=Dimension.exact(1),
                )
            )

        save_button = Button("Save", handler=self._save_form)
        cancel_button = Button("Cancel", handler=self._return_to_categories)
        self._form_buttons = [save_button, cancel_button]
        rows.append(
            VSplit(
                [
                    Window(width=Dimension.exact(18)),
                    save_button,
                    Window(width=Dimension.exact(2)),
                    cancel_button,
                ],
                height=Dimension.exact(1),
            )
        )
        rows.append(
            Window(
                FormattedTextControl(
                    [
                        (
                            "class:lm.hint",
                            " Tab/Up/Down: next field   Enter: next   Save: store   Esc: cancel\n"
                            " Process globs / ports / protocols accept comma-separated values.",
                        )
                    ]
                ),
                height=Dimension.exact(2),
            )
        )
        self._switch_mode(FORM, HSplit(rows), focus=self._form_fields["name"])

    def _return_to_categories(self) -> None:
        self._switch_mode(CATEGORIES, self._build_categories_root(), focus=self._category_control)

    def _save_form(self) -> None:
        from ..models import CategoryRule

        values = {key: area.text for key, area in self._form_fields.items()}
        name = values.get("name", "").strip()
        if not name:
            self._set_notice("Category name is required", "error")
            return
        if _has_control_chars(name):
            self._set_notice("Category name may not contain control characters", "error")
            return
        scheme = values.get("scheme", "").strip().rstrip(":").lower()
        if scheme and scheme not in ("http", "https"):
            self._set_notice("Scheme must be http or https", "error")
            return
        try:
            priority = int(values.get("priority", "0") or "0")
        except ValueError:
            self._set_notice("Priority must be an integer", "error")
            return
        try:
            ports = _parse_ports(values.get("ports", ""))
        except ValueError as exc:
            self._set_notice(f"Invalid ports: {exc}", "error")
            return
        protocols = [
            p.strip().upper()
            for p in values.get("protocols", "").split(",")
            if p.strip()
        ]
        for proto in protocols:
            if proto not in ("TCP", "UDP"):
                self._set_notice(f"Invalid protocol: {proto}", "error")
                return
        open_in_browser = values.get("open_in_browser", "true").strip().lower() not in (
            "false",
            "no",
            "0",
            "",
        )
        rule_id = self._editing_rule.id if self._editing_rule else _slug_rule_id(name)
        preserved = _preserved_rule_fields(self._editing_rule)
        rule = CategoryRule(
            id=rule_id,
            name=name,
            color=values.get("color", "").strip(),
            icon=values.get("icon", "").strip(),
            priority=priority,
            process_globs=_split_csv(values.get("process_globs", "")),
            ports=ports,
            protocols=protocols,
            scheme=scheme,
            open_in_browser=open_in_browser,
            source="user",
            **preserved,
        )
        if self._editing_rule is not None:
            new_rules = [rule if r.id == rule_id else r for r in self.user_rules]
        else:
            new_rules = [r for r in self.user_rules if r.id != rule_id] + [rule]
        if self._commit_user_rules(new_rules):
            self._set_notice(f"Saved category {name}", "ok")
            self._trigger_refresh()
            self._return_to_categories()

    def _commit_user_rules(self, rules) -> bool:
        """Persist ``rules`` first; only publish them in memory if the write
        succeeded, so a failed save never discards the working rule set."""
        from ..category_store import save_categories

        try:
            save_categories(self.categories_path, rules)
        except Exception as exc:
            self._set_notice(f"Could not save categories: {exc}", "error")
            return False
        self.user_rules = list(rules)
        self.classifier.replace_user_rules(self.user_rules)
        self._refresh_category_styles()
        return True

    # ------------------------------------------------------------ keybindings
    def _make_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        app = self

        def table_active():
            return app.mode == MAIN

        def category_active():
            return app.mode == CATEGORIES

        def filter_active():
            return app.mode == FILTER

        @kb.add("q", filter=Condition(lambda: app.mode in (MAIN, HELP)))
        @kb.add("c-c", filter=Condition(lambda: app.mode in (MAIN, HELP)))
        def _quit(event) -> None:
            event.app.exit()

        @kb.add("up", filter=Condition(table_active))
        @kb.add("k", filter=Condition(table_active))
        def _up(event) -> None:
            app._move(-1)

        @kb.add("down", filter=Condition(table_active))
        @kb.add("j", filter=Condition(table_active))
        def _down(event) -> None:
            app._move(1)

        @kb.add("pageup", filter=Condition(table_active))
        def _pageup(event) -> None:
            _, rows = app._terminal_size()
            app._move(-max(1, rows - 4))

        @kb.add("pagedown", filter=Condition(table_active))
        def _pagedown(event) -> None:
            _, rows = app._terminal_size()
            app._move(max(1, rows - 4))

        @kb.add("home", filter=Condition(table_active))
        def _home(event) -> None:
            app._move_to(0)

        @kb.add("end", filter=Condition(table_active))
        def _end(event) -> None:
            app._move_to(len(app.view) - 1)

        @kb.add("enter", filter=Condition(table_active))
        def _enter(event) -> None:
            app._handle_enter()

        @kb.add("escape", filter=Condition(table_active))
        def _escape_main(event) -> None:
            app._cancel_arm()
            app.notice = None
            app._invalidate()

        @kb.add("r", filter=Condition(table_active))
        def _refresh(event) -> None:
            app._trigger_refresh()

        @kb.add("space", filter=Condition(table_active))
        def _pause(event) -> None:
            app._toggle_pause()

        @kb.add("/", filter=Condition(table_active))
        def _search(event) -> None:
            app._open_search()

        @kb.add("f", filter=Condition(table_active))
        def _filter(event) -> None:
            app._open_filter()

        @kb.add("s", filter=Condition(table_active))
        def _sort(event) -> None:
            app._toggle_sort()

        @kb.add("a", filter=Condition(table_active))
        def _established(event) -> None:
            app._toggle_established()

        @kb.add("c", filter=Condition(table_active))
        def _copy(event) -> None:
            app._copy_url()

        @kb.add("C", filter=Condition(table_active))
        def _categories(event) -> None:
            app._open_categories()

        @kb.add("?", filter=Condition(lambda: app.mode in (MAIN, HELP)))
        def _help(event) -> None:
            app._open_help()

        @kb.add(Keys.Any, filter=Condition(lambda: app.mode == HELP))
        def _any_help(event) -> None:
            # ``KeyPressEvent`` has no ``.key`` attribute; use ``.data`` (the
            # printable character) to keep quit working even if the dedicated
            # ``q`` / ``Ctrl-C`` bindings are ever shadowed.
            if event.data in ("q", "\x03"):
                event.app.exit()
            else:
                app._return_to_main()

        # Filter mode
        @kb.add("up", filter=Condition(filter_active))
        @kb.add("k", filter=Condition(filter_active))
        def _filter_up(event) -> None:
            app._filter_move(-1)

        @kb.add("down", filter=Condition(filter_active))
        @kb.add("j", filter=Condition(filter_active))
        def _filter_down(event) -> None:
            app._filter_move(1)

        @kb.add("enter", filter=Condition(filter_active))
        def _filter_enter(event) -> None:
            app._filter_apply()

        @kb.add("escape", filter=Condition(filter_active))
        def _filter_escape(event) -> None:
            app._return_to_main()

        # Category list mode
        @kb.add("up", filter=Condition(category_active))
        @kb.add("k", filter=Condition(category_active))
        def _cat_up(event) -> None:
            app._category_move(-1)

        @kb.add("down", filter=Condition(category_active))
        @kb.add("j", filter=Condition(category_active))
        def _cat_down(event) -> None:
            app._category_move(1)

        @kb.add("n", filter=Condition(category_active))
        def _cat_new(event) -> None:
            app._category_new()

        @kb.add("e", filter=Condition(category_active))
        def _cat_edit(event) -> None:
            app._category_edit()

        @kb.add("d", filter=Condition(category_active))
        def _cat_delete(event) -> None:
            app._category_delete()

        @kb.add("escape", filter=Condition(category_active))
        def _cat_close(event) -> None:
            app._return_to_main()

        return kb


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_ports(value: str) -> list[int]:
    ports: list[int] = []
    for part in _split_csv(value):
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            ports.extend(range(lo, hi + 1))
        else:
            ports.append(int(part))
    for port in ports:
        if not (1 <= port <= 65535):
            raise ValueError(f"port out of range: {port}")
    return ports


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


# Rule fields that the TUI form cannot represent. When editing an existing user
# rule these must be carried over verbatim, otherwise saving (even just a colour
# change) would silently destroy hand-written configuration.
_PRESERVED_RULE_FIELDS = (
    "port_ranges",
    "executable_globs",
    "command_line_globs",
    "address_globs",
    "container_name_globs",
    "container_image_globs",
    "exclude_process_globs",
    "exclude_executable_globs",
    "exclude_command_line_globs",
    "exclude_ports",
)


def _preserved_rule_fields(base) -> dict:
    if base is None:
        return {}
    preserved: dict = {name: list(getattr(base, name)) for name in _PRESERVED_RULE_FIELDS}
    preserved["match_any_container"] = bool(base.match_any_container)
    return preserved


def _slug_rule_id(name: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    return f"user.{slug or 'rule'}"
