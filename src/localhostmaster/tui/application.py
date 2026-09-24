"""Full-screen prompt_toolkit application for LocalhostMaster."""

from __future__ import annotations

import os
import threading
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
from ..icons import (
    LEGACY_ASCII_FALLBACK,
    default_icon,
    icon_by_glyph,
    is_preset_glyph,
    preset_icons,
    render_glyph,
)
from ..kill_state import KillArmController, KillDecision
from ..models import (
    KILL_GUARD_S,
    KILL_TIMEOUT_S,
    EndpointKey,
    PortEntry,
    ProcessIdentity,
    Protocol,
    Snapshot,
)
from ..process_terminator import (
    LISTENER_STATES,
    TerminationResult,
    TerminationStatus,
    protected_reason,
)
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
    is_valid_color,
    resolve_color_mode,
)

MAIN = "main"
HELP = "help"
SEARCH = "search"
FILTER = "filter"
CATEGORIES = "categories"
FORM = "category_form"
ICON_PICKER = "icon_picker"
EDIT_MODES = {SEARCH, FORM}

HELP_TEXT = [
    ("class:lm.header", "LocalhostMaster - help\n\n"),
    ("class:lm.dim", "Navigation\n"),
    ("", "  Up/Down             move cursor\n"),
    ("", "  PageUp/PageDown     page\n"),
    ("", "  Home/End            first / last\n\n"),
    ("class:lm.dim", "Actions\n"),
    ("", "  Enter               arm; press again within 2.5s to open\n"),
    ("", "  k                   arm; press again within 2.5s to force-stop\n"),
    ("", "                      the whole process behind the selected port\n"),
    ("", "  Esc                 cancel confirmation / close dialog\n"),
    ("", "  r                   refresh now\n"),
    ("", "  Space               pause / resume auto refresh\n"),
    ("", "  /                   search\n"),
    ("", "  f                   filter (j/k move, Enter apply)\n"),
    ("", "  s                   cycle sort (port/process/category/pid)\n"),
    ("", "  a                   toggle established TCP connections\n"),
    ("", "  c                   copy URL of selected TCP endpoint\n"),
    ("", "  C                   manage categories (j/k move)\n"),
    ("", "  ?                   this help\n"),
    ("", "  q / Ctrl-C          quit\n\n"),
    ("class:lm.dim", "Notes\n"),
    ("", "  Only TCP endpoints can be opened in a browser.\n"),
    ("", "  Force-stop only accepts TCP LISTEN / UDP BOUND rows and only\n"),
    ("", "  stops the single host process shown; it never kills a process\n"),
    ("", "  tree. LocalhostMaster itself, PID 0, PID 4, Windows core\n"),
    ("", "  processes and Docker host proxies are refused.\n"),
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
        terminator=None,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.resolver = resolver
        self.docker = docker
        self.opener = opener
        self.clipboard = clipboard
        # Injected from the production CLI. ``None`` disables real termination
        # entirely, so constructing an app in a test never arms a real kill.
        self.terminator = terminator
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
        self._form_root = None
        self._form_icon_glyph = ""
        self._form_icon_original = ""
        self._form_icon_is_legacy = False
        self._icon_picker_cursor = 0

        self.arm = OpenArmController(
            guard_s=max(0.0, config.arm_guard_ms / 1000.0),
            timeout_s=max(0.1, config.double_enter_ms / 1000.0),
        )
        # Fixed (not user-configurable) timing for the destructive double-k.
        self.kill_arm = KillArmController(guard_s=KILL_GUARD_S, timeout_s=KILL_TIMEOUT_S)
        self.kill_in_progress = False
        self._kill_target: Optional[ProcessIdentity] = None
        self._kill_thread: Optional[threading.Thread] = None
        self._kill_stop = threading.Event()
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
        # Stop accepting kill callbacks, then let the worker end.
        self._kill_stop.set()
        kill_thread = self._kill_thread
        if kill_thread is not None and kill_thread.is_alive():
            kill_thread.join(timeout=2.0)
        self._kill_thread = None
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
        kill_present = {}
        for entry in self._all_entries:
            if is_openable(entry):
                present[entry.key] = url_for_entry(entry)
            identity = self._identity_for(entry)
            if identity is not None:
                kill_present[entry.key] = identity
        self.arm.reconcile(present)
        self.kill_arm.reconcile(kill_present)

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
        if self.kill_in_progress and self._kill_target is not None:
            target = self._kill_target
            return [
                (
                    "class:lm.status.notice",
                    f" Force-stopping {target.process_name} (PID {target.pid})\u2026",
                )
            ]
        if self.arm.is_armed and self.arm.armed is not None:
            remaining = self.arm.remaining()
            return [
                (
                    "class:lm.status.notice",
                    f" Press Enter again within {remaining:.1f}s to open {self.arm.armed.url}",
                )
            ]
        if self.kill_arm.is_armed:
            target = self.kill_arm.target()
            if target is not None:
                remaining = self.kill_arm.remaining()
                key = target.endpoint_key
                text = (
                    f" Press k again within {remaining:.1f}s to force-stop "
                    f"{target.process_name} (PID {target.pid})"
                    f" \u00b7 {key.protocol.value} {key.local_address}:{key.port}"
                )
                others = self._other_endpoint_count(target)
                if others > 0:
                    text += (
                        f" \u00b7 stopping this process may also close "
                        f"{others} other visible endpoint(s)"
                    )
                return [("class:lm.status.notice", text)]
        notice = self._active_notice()
        if notice is not None:
            text, kind = notice
            style = {
                "error": "class:lm.status.error",
                "ok": "class:lm.status.ok",
                "info": "class:lm.status.notice",
            }.get(kind, "class:lm.status.notice")
            return [(style, f" {text}")]
        if self.warnings:
            return [("class:lm.status.error", f" {self.warnings[0]}")]
        return [("class:lm.dim", " Ready. Press ? for help, q to quit.")]

    def _render_hint(self):
        return (
            " [\u2191\u2193] move  [Enter Enter] open  [k k] force-stop  [r] refresh  [Space] pause"
            "  [/] search  [f] filter  [s] sort  [a] conns  [c] copy  [C] categories"
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
        self._cancel_confirmations()
        self._invalidate()

    def _move_to(self, index: int) -> None:
        if not self.view:
            return
        self.cursor = clamp_cursor(index, len(self.view))
        self._cursor_key = self.view[self.cursor].key
        self._cancel_confirmations()
        self._invalidate()

    def _cancel_arm(self) -> None:
        if self.arm.is_armed:
            self.arm.cancel()

    def _cancel_kill_arm(self) -> None:
        if self.kill_arm.is_armed:
            self.kill_arm.cancel()

    def _cancel_confirmations(self) -> None:
        """Cancel both confirmation states (used by navigation/mode switches)."""
        self._cancel_arm()
        self._cancel_kill_arm()

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
            self._cancel_kill_arm()
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
        self._cancel_confirmations()
        self._recompute_view()
        self._invalidate()

    def _toggle_established(self) -> None:
        self.show_established = not self.show_established
        self._cancel_confirmations()
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

    # -------------------------------------------------------- kill (double k)
    def _identity_for(self, entry: Optional[PortEntry]) -> Optional[ProcessIdentity]:
        if entry is None or entry.pid is None or entry.process is None:
            return None
        if entry.process.create_time is None:
            return None
        return ProcessIdentity(
            pid=entry.pid,
            create_time=float(entry.process.create_time),
            endpoint_key=entry.key,
            process_name=entry.process_name,
            has_container_mapping=entry.container is not None,
        )

    def _other_endpoint_count(self, identity: Optional[ProcessIdentity]) -> int:
        if identity is None:
            return 0
        count = 0
        # "visible" in the status hint means the currently filtered/searched
        # view, not every scanned entry.
        for entry in self.view:
            if entry.key == identity.endpoint_key:
                continue
            if entry.pid == identity.pid:
                count += 1
        return count

    def _kill_eligibility_reason(self, entry: PortEntry) -> Optional[str]:
        """Lightweight, read-only precheck for the first ``k``."""
        if self.terminator is None:
            return "force-stop is not available in this session"
        if entry.pid is None:
            return "this endpoint has no process (PID unknown)"
        if entry.socket_state not in LISTENER_STATES:
            return "only listening (TCP) or bound (UDP) endpoints can be force-stopped"
        if entry.process is None:
            return "process metadata is unavailable"
        if entry.process.create_time is None:
            return "process creation time is unavailable; refusing an unverified target"
        return protected_reason(
            pid=entry.pid,
            name=entry.process_name,
            username=entry.process.username,
            self_pid=os.getpid(),
            has_container_mapping=entry.container is not None,
        )

    def _handle_kill(self) -> None:
        if self.kill_in_progress:
            self._set_notice("A force-stop is already in progress", "info")
            return
        entry = self._current_entry()
        if entry is None:
            return
        reason = self._kill_eligibility_reason(entry)
        if reason is not None:
            self._cancel_kill_arm()
            self._set_notice(f"Cannot force-stop: {reason}", "error")
            self._invalidate()
            return
        identity = self._identity_for(entry)
        if identity is None:
            self._cancel_kill_arm()
            self._set_notice("Cannot force-stop: process identity is unavailable", "error")
            self._invalidate()
            return
        decision = self.kill_arm.press(identity)
        if decision == KillDecision.IGNORED:
            self._invalidate()
            return
        if decision == KillDecision.ARMED:
            # Kill and open confirmations are mutually exclusive.
            self._cancel_arm()
            self._invalidate()
            return
        self._submit_kill(identity)

    def _submit_kill(self, identity: ProcessIdentity) -> None:
        self.kill_in_progress = True
        self._kill_target = identity
        self._kill_stop.clear()
        self._start_kill_thread(identity)
        self._invalidate()

    def _start_kill_thread(self, identity: ProcessIdentity) -> None:
        terminator = self.terminator

        def run() -> None:
            try:
                result = terminator.force_stop(identity)
            except Exception:
                result = TerminationResult(
                    TerminationStatus.UNEXPECTED_ERROR,
                    "The force-stop request failed unexpectedly.",
                    identity.pid,
                )
            self._schedule(lambda: self._on_kill_done(identity, result))

        thread = threading.Thread(target=run, name="lm-kill", daemon=True)
        self._kill_thread = thread
        thread.start()

    def _on_kill_done(self, identity: ProcessIdentity, result: TerminationResult) -> None:
        if self._kill_stop.is_set():
            # Application is shutting down; do not touch a closed TUI.
            return
        self.kill_in_progress = False
        self._kill_target = None
        if result.status in (
            TerminationStatus.EXITED,
        ):
            kind = "ok"
        elif result.status in (
            TerminationStatus.ALREADY_GONE,
            TerminationStatus.UNCONFIRMED,
        ):
            kind = "info"
        else:
            kind = "error"
        self._set_notice(result.message, kind)
        # Reflect reality immediately, whatever the outcome.
        self._trigger_refresh()

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
        self._cancel_confirmations()
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
            self._cancel_confirmations()
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
        self._cancel_confirmations()
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
        self._cancel_confirmations()
        self._switch_mode(FILTER, self._build_filter_root(), focus=self._filter_control)

    def _filter_move(self, delta: int) -> None:
        options = self._filter_options()
        self.filter_cursor = clamp_cursor(self.filter_cursor + delta, len(options))
        self._invalidate()

    def _filter_apply(self) -> None:
        options = self._filter_options()
        if 0 <= self.filter_cursor < len(options):
            self.filter_spec = options[self.filter_cursor]
        self._cancel_confirmations()
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
            icon = render_glyph(rule.icon, self.ascii_only) or LEGACY_ASCII_FALLBACK
            lines.append(
                (
                    style,
                    f"{marker}{icon} {rule.name:<16} {tag:<10} prio={rule.priority:<4} {detail}\n",
                )
            )
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
        self._cancel_confirmations()
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
            "icon": default_icon().glyph,
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
        # Remove by object identity: two hand-written rules may share an id, and
        # deleting one must not silently delete the other.
        remaining = [r for r in self.user_rules if r is not rule]
        if self._commit_user_rules(remaining):
            self._set_notice(f"Deleted category {rule.name}", "ok")
            self._switch_mode(CATEGORIES, self._build_categories_root(), focus=self._category_control)

    def _cancel_category_delete_confirm(self) -> None:
        if self._category_delete_confirm:
            self._category_delete_confirm = False
            self._invalidate()

    # -------------------------------------------------------------- form
    def _open_form(self, prefill: dict, title: str) -> None:
        self._cancel_confirmations()
        self._form_title = title
        fields = [
            ("name", "Name", True),
            ("color", "Color", False),
            ("priority", "Priority", False),
            ("process_globs", "Process globs", False),
            ("ports", "Ports/ranges", False),
            ("protocols", "Protocols", False),
            ("scheme", "Scheme", False),
            ("open_in_browser", "Open in browser", False),
        ]
        raw_icon = (prefill.get("icon", "") or "").strip()
        if not raw_icon:
            # A category always has an icon; default to a preset for new rules
            # and for rules whose stored icon is empty.
            raw_icon = default_icon().glyph
        self._form_icon_glyph = raw_icon
        self._form_icon_original = raw_icon
        self._form_icon_is_legacy = not is_preset_glyph(raw_icon)
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

        # The icon field is a read-only selector, not a free-text input.
        icon_kb = KeyBindings()

        @icon_kb.add("enter")
        @icon_kb.add("space")
        def _choose_icon(event) -> None:
            self._open_icon_picker()

        @icon_kb.add("tab")
        @icon_kb.add("down")
        def _icon_next(event) -> None:
            focus_next(event)

        @icon_kb.add("s-tab")
        @icon_kb.add("up")
        def _icon_prev(event) -> None:
            focus_previous(event)

        @icon_kb.add("escape")
        @icon_kb.add("c-c")
        def _icon_cancel(event) -> None:
            self._return_to_categories()

        self._icon_control = FormattedTextControl(
            self._render_icon_field, focusable=True, show_cursor=False
        )
        self._icon_control.key_bindings = icon_kb

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
            if key == "color":
                # Icon sits directly under Color, matching the old field order.
                rows.append(
                    VSplit(
                        [
                            Label(" Icon".ljust(17), width=18, style="class:lm.field"),
                            Window(self._icon_control, height=Dimension.exact(1)),
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
                            " Tab/Up/Down: next field   Enter: next   Icon field: Enter/Space chooses a preset"
                            "   Save: store   Esc: cancel\n"
                            " Process globs / ports / protocols accept comma-separated values.",
                        )
                    ]
                ),
                height=Dimension.exact(2),
            )
        )
        self._form_root = HSplit(rows)
        self._switch_mode(FORM, self._form_root, focus=self._form_fields["name"])

    def _return_to_categories(self) -> None:
        self._switch_mode(CATEGORIES, self._build_categories_root(), focus=self._category_control)

    # ------------------------------------------------------------ icon picker
    def _render_icon_field(self):
        glyph = self._form_icon_glyph
        if glyph and is_preset_glyph(glyph):
            option = icon_by_glyph(glyph)
            shown = render_glyph(glyph, self.ascii_only)
            text = f" {shown}  {option.label}   [Enter/Space: choose]"
        elif glyph:
            shown = render_glyph(glyph, self.ascii_only) or LEGACY_ASCII_FALLBACK
            text = f" {shown}  Legacy/custom   [Enter/Space: choose]"
        else:
            text = " (none)   [Enter/Space: choose]"
        return [("class:lm.row", text)]

    def _icon_picker_options(self) -> list[tuple[str, str]]:
        """Return a list of (token, rendered label) including any legacy item."""
        options: list[tuple[str, str]] = []
        if self._form_icon_is_legacy and self._form_icon_glyph:
            shown = render_glyph(self._form_icon_glyph, self.ascii_only) or LEGACY_ASCII_FALLBACK
            options.append(("legacy", f"{shown}  Legacy/custom (keep current)"))
        for option in preset_icons():
            shown = render_glyph(option.glyph, self.ascii_only)
            options.append((option.id, f"{shown}  {option.label}"))
        return options

    def _icon_picker_index_for(self, options: list[tuple[str, str]]) -> int:
        glyph = self._form_icon_glyph
        if self._form_icon_is_legacy:
            for index, (token, _) in enumerate(options):
                if token == "legacy":
                    return index
        if glyph:
            option = icon_by_glyph(glyph)
            if option is not None:
                for index, (token, _) in enumerate(options):
                    if token == option.id:
                        return index
        return 0

    def _build_icon_picker_root(self):
        options = self._icon_picker_options()
        self._icon_picker_cursor = clamp_cursor(
            self._icon_picker_index_for(options), len(options)
        )
        self._icon_control_picker = FormattedTextControl(
            self._render_icon_picker, focusable=True, show_cursor=False
        )
        return HSplit(
            [
                Window(
                    FormattedTextControl([("class:lm.header", " Select icon\n")]),
                    height=1,
                ),
                Window(content=self._icon_control_picker, wrap_lines=False),
            ]
        )

    def _render_icon_picker(self):
        options = self._icon_picker_options()
        lines = []
        for index, (_token, label) in enumerate(options):
            marker = "> " if index == self._icon_picker_cursor else "  "
            style = "class:lm.selected" if index == self._icon_picker_cursor else "class:lm.row"
            lines.append((style, f"{marker}{label}\n"))
        lines.append(
            (
                "class:lm.hint",
                " Up/Down or j/k: move   Enter/Space: choose   Esc: back to form",
            )
        )
        return lines

    def _open_icon_picker(self) -> None:
        self._cancel_confirmations()
        self._switch_mode(
            ICON_PICKER,
            self._build_icon_picker_root(),
            focus=self._icon_control_picker,
        )

    def _icon_picker_move(self, delta: int) -> None:
        options = self._icon_picker_options()
        self._icon_picker_cursor = clamp_cursor(
            self._icon_picker_cursor + delta, len(options)
        )
        self._invalidate()

    def _icon_picker_move_to(self, index: int) -> None:
        options = self._icon_picker_options()
        self._icon_picker_cursor = clamp_cursor(index, len(options))
        self._invalidate()

    def _icon_picker_confirm(self) -> None:
        options = self._icon_picker_options()
        if 0 <= self._icon_picker_cursor < len(options):
            token, _ = options[self._icon_picker_cursor]
            if token == "legacy":
                # Keep the existing legacy glyph untouched.
                pass
            else:
                option = next((o for o in preset_icons() if o.id == token), None)
                if option is not None:
                    self._form_icon_glyph = option.glyph
                    self._form_icon_is_legacy = False
        self._return_to_form()

    def _return_to_form(self) -> None:
        form_root = getattr(self, "_form_root", None)
        if form_root is None:
            self._return_to_categories()
            return
        self._switch_mode(FORM, form_root, focus=self._icon_control)

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
        icon = self._form_icon_glyph.strip()
        if not icon:
            self._set_notice("Icon is required; choose a preset icon", "error")
            return
        if _has_control_chars(icon) or len(icon) > 4:
            self._set_notice("Icon must be a single preset glyph", "error")
            return
        if not is_preset_glyph(icon):
            unchanged_legacy = (
                self._editing_rule is not None
                and self._form_icon_is_legacy
                and icon == self._form_icon_original
            )
            if not unchanged_legacy:
                self._set_notice("Choose an icon from the preset list", "error")
                return
        scheme = values.get("scheme", "").strip().rstrip(":").lower()
        if scheme and scheme not in ("http", "https"):
            self._set_notice("Scheme must be http or https", "error")
            return
        color = values.get("color", "").strip()
        if color and not is_valid_color(color):
            self._set_notice("Color must be a name or #RRGGBB", "error")
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
        # An empty value keeps the default (openable); only an explicit
        # false-ish value disables browser opening.
        open_in_browser = values.get("open_in_browser", "true").strip().lower() not in (
            "false",
            "no",
            "0",
        )
        rule_id = self._editing_rule.id if self._editing_rule else _slug_rule_id(name)
        if self._editing_rule is None and any(
            r.id == rule_id for r in self.user_rules
        ):
            self._set_notice(
                f"A category with id {rule_id!r} already exists; rename it", "error"
            )
            return
        preserved = _preserved_rule_fields(self._editing_rule)
        rule = CategoryRule(
            id=rule_id,
            name=name,
            color=color,
            icon=icon,
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
            # Collisions were rejected above, so appending cannot overwrite.
            new_rules = list(self.user_rules) + [rule]
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
        def _up(event) -> None:
            app._move(-1)

        @kb.add("down", filter=Condition(table_active))
        def _down(event) -> None:
            app._move(1)

        # Lower-case ``k`` on the main list means "force-stop": press twice.
        # Main-list Vim ``j``/``k`` navigation was removed so ``k`` is
        # unambiguous. Up/Down still move the cursor.
        @kb.add("k", filter=Condition(table_active))
        def _kill(event) -> None:
            app._handle_kill()

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
            app._cancel_confirmations()
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

        # Any unbound key cancels a pending delete confirmation, matching the
        # on-screen hint. Specific bindings above still take priority.
        @kb.add(Keys.Any, filter=Condition(category_active))
        def _cat_any(event) -> None:
            app._cancel_category_delete_confirm()

        # Icon picker mode (inside the category form). ``k``/``j`` navigate the
        # picker only; the mode condition keeps the main-list kill binding off.
        def icon_picker_active():
            return app.mode == ICON_PICKER

        @kb.add("up", filter=Condition(icon_picker_active))
        @kb.add("k", filter=Condition(icon_picker_active))
        def _icon_up(event) -> None:
            app._icon_picker_move(-1)

        @kb.add("down", filter=Condition(icon_picker_active))
        @kb.add("j", filter=Condition(icon_picker_active))
        def _icon_down(event) -> None:
            app._icon_picker_move(1)

        @kb.add("pageup", filter=Condition(icon_picker_active))
        def _icon_pageup(event) -> None:
            app._icon_picker_move(-5)

        @kb.add("pagedown", filter=Condition(icon_picker_active))
        def _icon_pagedown(event) -> None:
            app._icon_picker_move(5)

        @kb.add("home", filter=Condition(icon_picker_active))
        def _icon_home(event) -> None:
            app._icon_picker_move_to(0)

        @kb.add("end", filter=Condition(icon_picker_active))
        def _icon_end(event) -> None:
            app._icon_picker_move_to(len(app._icon_picker_options()) - 1)

        @kb.add("enter", filter=Condition(icon_picker_active))
        @kb.add("space", filter=Condition(icon_picker_active))
        def _icon_choose(event) -> None:
            app._icon_picker_confirm()

        @kb.add("escape", filter=Condition(icon_picker_active))
        @kb.add("c-c", filter=Condition(icon_picker_active))
        def _icon_cancel(event) -> None:
            app._return_to_form()

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
            # Validate bounds BEFORE expanding, so a huge range cannot allocate
            # millions of ints (or hang) before the error is raised.
            _check_port(lo)
            _check_port(hi)
            ports.extend(range(lo, hi + 1))
        else:
            port = int(part)
            _check_port(port)
            ports.append(port)
    return ports


def _check_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise ValueError(f"port out of range: {port}")


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
