"""Category classification: builtin rules + user rules.

Resolution order (first match wins):

1. user rules (priority desc, then file order)
2. builtin rules (priority desc, then declaration order)
3. the ``unknown`` fallback
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Iterable, Optional

from .models import (
    AddressFamily,
    CategoryAssignment,
    CategoryRule,
    ContainerInfo,
    PortEntry,
    Protocol,
    SocketState,
)

UNKNOWN_NAME = "unknown"
UNKNOWN_COLOR = "#9CA3AF"

_CI = os.name == "nt"


def builtin_rules() -> list[CategoryRule]:
    return [
        CategoryRule(
            id="builtin.llama.cpp",
            name="llama.cpp",
            color="#7C3AED",
            icon="\u25c6",
            priority=100,
            process_globs=["llama-server*", "llama-cli*", "llamafile*"],
            scheme="http",
            source="builtin",
        ),
        CategoryRule(
            id="builtin.docker",
            name="docker",
            color="#2563EB",
            icon="\u25a3",
            priority=95,
            process_globs=[
                "com.docker.backend*",
                "wslrelay*",
                "dockerd*",
                "docker*",
            ],
            scheme="http",
            source="builtin",
        ),
        CategoryRule(
            id="builtin.docker.container",
            name="docker",
            color="#2563EB",
            icon="\u25a3",
            priority=94,
            match_any_container=True,
            scheme="http",
            source="builtin",
        ),
        CategoryRule(
            id="builtin.database",
            name="database",
            color="#EA580C",
            icon="\u25a4",
            priority=60,
            process_globs=[
                "postgres*",
                "mysqld*",
                "mariadbd*",
                "mongod*",
                "redis-server*",
            ],
            scheme="http",
            source="builtin",
        ),
        CategoryRule(
            id="builtin.dev-server",
            name="dev-server",
            color="#16A34A",
            icon="\u25b8",
            priority=50,
            process_globs=[
                "node*",
                "deno*",
                "bun*",
                "python*",
                "uvicorn*",
                "vite*",
                "webpack*",
                "next*",
            ],
            scheme="http",
            source="builtin",
        ),
        CategoryRule(
            id="builtin.system",
            name="system",
            color="#6B7280",
            icon="\u2022",
            priority=10,
            process_globs=[
                "svchost*",
                "services*",
                "wininit*",
                "lsass*",
                "System",
                "System Idle Process",
            ],
            scheme="http",
            source="builtin",
        ),
    ]


class RuleError(Exception):
    pass


def _is_regex(pattern: str) -> bool:
    return pattern.startswith("re:")


def glob_match(value: Optional[str], pattern: str) -> bool:
    if value is None:
        return False
    if _is_regex(pattern):
        expr = pattern[3:]
        try:
            flags = re.IGNORECASE if _CI else 0
            return re.search(expr, value, flags) is not None
        except re.error:
            return False
    if _CI:
        return fnmatch.fnmatchcase(value.lower(), pattern.lower())
    return fnmatch.fnmatchcase(value, pattern)


def validate_pattern(pattern: str) -> Optional[str]:
    if _is_regex(pattern):
        try:
            re.compile(pattern[3:])
        except re.error as exc:
            return f"invalid regex {pattern!r}: {exc}"
    return None


def _any_glob(values: Iterable[Optional[str]], patterns: list[str]) -> bool:
    values = [v for v in values if v]
    for pattern in patterns:
        for value in values:
            try:
                if glob_match(value, pattern):
                    return True
            except Exception:
                continue
    return False


class Classifier:
    def __init__(
        self,
        user_rules: Optional[list[CategoryRule]] = None,
        builtin: Optional[list[CategoryRule]] = None,
    ) -> None:
        self._user_rules = list(user_rules or [])
        self._builtin_rules = list(builtin if builtin is not None else builtin_rules())
        self.warnings: list[str] = []
        self._ordered = self._build_order()

    def _build_order(self) -> list[CategoryRule]:
        ordered = _sort_source(self._user_rules)
        ordered += _sort_source(self._builtin_rules)
        return ordered

    @property
    def user_rules(self) -> tuple[CategoryRule, ...]:
        """Read-only snapshot of the current user rules."""
        return tuple(self._user_rules)

    @property
    def builtin_rules(self) -> tuple[CategoryRule, ...]:
        """Read-only snapshot of the built-in rules."""
        return tuple(self._builtin_rules)

    def replace_user_rules(self, rules: Iterable[CategoryRule]) -> None:
        """Atomically replace all user rules.

        Builds the new ordered rule list before publishing it, so a concurrent
        :meth:`classify` audience sees either the complete old list or the
        complete new list — never a half-updated one.
        """
        new_rules = list(rules)
        new_ordered = _sort_source(new_rules) + _sort_source(self._builtin_rules)
        self._user_rules = new_rules
        self.warnings = []
        self._ordered = new_ordered

    # Backwards-compatible alias. Prefer :meth:`replace_user_rules`.
    def set_user_rules(self, rules: list[CategoryRule]) -> None:
        self.replace_user_rules(rules)

    def classify(self, entry: PortEntry) -> CategoryAssignment:
        for rule in self._ordered:
            if not rule.has_positive_condition():
                continue
            try:
                if self._matches(rule, entry):
                    return _assignment_from(rule)
            except Exception as exc:  # pragma: no cover - defensive
                self.warnings.append(f"rule {rule.id!r} failed: {exc}")
        return CategoryAssignment(
            name=UNKNOWN_NAME,
            color=UNKNOWN_COLOR,
            icon="\u00b7",
            scheme="",
            open_in_browser=True,
            source="fallback",
            rule_id="",
        )

    def _matches(self, rule: CategoryRule, entry: PortEntry) -> bool:
        process = entry.process
        name = process.name if process else None
        exe = process.executable if process else None
        cmdline = process.command_line if process else None
        container = entry.container

        if rule.protocols:
            allowed = {p.strip().upper() for p in rule.protocols}
            if entry.protocol.value.upper() not in allowed:
                return False

        if rule.ports and entry.port not in rule.ports:
            return False

        if rule.port_ranges:
            if not any(lo <= entry.port <= hi for lo, hi in rule.port_ranges):
                return False

        if rule.address_globs and not _any_glob([entry.local_address], rule.address_globs):
            return False

        if rule.process_globs and not _any_glob([name], rule.process_globs):
            return False

        if rule.executable_globs and not _any_glob([exe], rule.executable_globs):
            return False

        # A rule that needs the command line never matches when it is not
        # readable (non-admin users cannot read other users' command lines).
        if rule.command_line_globs:
            if not cmdline:
                return False
            if not _any_glob([cmdline], rule.command_line_globs):
                return False

        if rule.match_any_container and container is None:
            return False

        if rule.container_name_globs:
            if container is None or not _any_glob([container.name], rule.container_name_globs):
                return False

        if rule.container_image_globs:
            if container is None or not _any_glob([container.image], rule.container_image_globs):
                return False

        if self._excluded(rule, entry, name, exe, cmdline):
            return False

        return True

    def _excluded(
        self,
        rule: CategoryRule,
        entry: PortEntry,
        name: Optional[str],
        exe: Optional[str],
        cmdline: Optional[str],
    ) -> bool:
        if rule.exclude_ports and entry.port in rule.exclude_ports:
            return True
        if rule.exclude_process_globs and _any_glob([name], rule.exclude_process_globs):
            return True
        if rule.exclude_executable_globs and _any_glob([exe], rule.exclude_executable_globs):
            return True
        if rule.exclude_command_line_globs and cmdline and _any_glob(
            [cmdline], rule.exclude_command_line_globs
        ):
            return True
        return False


def _sort_source(rules: list[CategoryRule]) -> list[CategoryRule]:
    indexed = list(enumerate(rules))
    indexed.sort(key=lambda item: (-item[1].priority, item[0]))
    return [rule for _, rule in indexed]


def _assignment_from(rule: CategoryRule) -> CategoryAssignment:
    return CategoryAssignment(
        name=rule.name,
        color=rule.color,
        icon=rule.icon,
        scheme=rule.scheme,
        open_in_browser=rule.open_in_browser,
        source=rule.source,
        rule_id=rule.id,
    )
