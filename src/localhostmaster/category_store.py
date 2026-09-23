"""Load and persist user categories in ``categories.toml``.

``tomllib`` is read-only, so a small, well-tested TOML serializer lives here.
Writes are atomic: temp file in the same directory, flush + fsync, then
``os.replace``. A failed write leaves the previous file untouched.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Optional

from .classifier import validate_pattern
from .models import CategoryRule

HEADER = (
    "# LocalhostMaster user categories.\n"
    "# This file may be rewritten by the application; comments are not preserved.\n"
    "# Builtin categories live in code and cannot be edited here.\n"
)

_ALLOWED_SCHEMES = {"http", "https"}


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _validate_port_bounds(rule: CategoryRule) -> None:
    for port in rule.ports:
        if not (1 <= port <= 65535):
            raise ValueError(f"port out of range: {port}")
    for port in rule.exclude_ports:
        if not (1 <= port <= 65535):
            raise ValueError(f"exclude_port out of range: {port}")
    for lo, hi in rule.port_ranges:
        if not (1 <= lo <= 65535) or not (1 <= hi <= 65535):
            raise ValueError(f"port range out of range: {lo}-{hi}")


# ---------------------------------------------------------------------------
# TOML serialization
# ---------------------------------------------------------------------------
def toml_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot serialize {type(value)!r} to TOML")


def _format_port_ranges(ranges: list[tuple[int, int]]) -> list[str]:
    out = []
    for lo, hi in ranges:
        out.append(f"{lo}-{hi}" if lo != hi else str(lo))
    return out


def dumps_categories(rules: list[CategoryRule]) -> str:
    chunks: list[str] = [HEADER]
    for rule in rules:
        lines = ["[[category]]"]
        lines.append(f"id = {_toml_value(rule.id)}")
        lines.append(f"name = {_toml_value(rule.name)}")
        if rule.color:
            lines.append(f"color = {_toml_value(rule.color)}")
        if rule.icon:
            lines.append(f"icon = {_toml_value(rule.icon)}")
        lines.append(f"priority = {_toml_value(rule.priority)}")
        lines.append(f"open_in_browser = {_toml_value(rule.open_in_browser)}")
        if rule.scheme:
            lines.append(f"scheme = {_toml_value(rule.scheme)}")
        for attr, key in (
            ("process_globs", "process_globs"),
            ("executable_globs", "executable_globs"),
            ("command_line_globs", "command_line_globs"),
            ("address_globs", "address_globs"),
            ("container_name_globs", "container_name_globs"),
            ("container_image_globs", "container_image_globs"),
            ("exclude_process_globs", "exclude_process_globs"),
            ("exclude_executable_globs", "exclude_executable_globs"),
            ("exclude_command_line_globs", "exclude_command_line_globs"),
        ):
            value = getattr(rule, attr)
            if value:
                lines.append(f"{key} = {_toml_value(list(value))}")
        if rule.ports:
            lines.append(f"ports = {_toml_value(sorted(rule.ports))}")
        if rule.port_ranges:
            lines.append(
                f"port_ranges = {_toml_value(_format_port_ranges(rule.port_ranges))}"
            )
        if rule.protocols:
            lines.append(f"protocols = {_toml_value(list(rule.protocols))}")
        if rule.exclude_ports:
            lines.append(f"exclude_ports = {_toml_value(sorted(rule.exclude_ports))}")
        if rule.match_any_container:
            lines.append("match_any_container = true")
        chunks.append("\n".join(lines) + "\n")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------
def _as_str_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise ValueError("expected a string or list of strings")


def _as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (int, float, str)):
        value = [value]
    out: list[int] = []
    for item in value:
        out.append(int(item))
    return out


def _as_port_ranges(value: Any) -> list[tuple[int, int]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    out: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            lo, hi = int(item[0]), int(item[1])
        elif isinstance(item, str) and "-" in item:
            lo_s, _, hi_s = item.partition("-")
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(item)
        if lo > hi:
            lo, hi = hi, lo
        out.append((lo, hi))
    return out


def parse_category(data: dict, index: int, warnings: list[str]) -> Optional[CategoryRule]:
    if not isinstance(data, dict):
        warnings.append(f"category #{index}: not a table, skipped")
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"category #{index}: missing name, skipped")
        return None
    name = name.strip()
    if _has_control_chars(name):
        warnings.append(f"category #{index}: name contains control characters, skipped")
        return None
    rule_id = data.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        rule_id = f"user.{name.lower().replace(' ', '-')}.{index}"

    rule = CategoryRule(id=rule_id, name=name, source="user")

    try:
        rule.color = str(data.get("color", "") or "")
        rule.icon = str(data.get("icon", "") or "")
        rule.priority = int(data.get("priority", 0))
        rule.scheme = str(data.get("scheme", "") or "")
        rule.match_any_container = bool(data.get("match_any_container", False))
        rule.open_in_browser = bool(data.get("open_in_browser", True))
        rule.process_globs = _as_str_list(data.get("process_globs"))
        rule.executable_globs = _as_str_list(data.get("executable_globs"))
        rule.command_line_globs = _as_str_list(data.get("command_line_globs"))
        rule.address_globs = _as_str_list(data.get("address_globs"))
        rule.container_name_globs = _as_str_list(data.get("container_name_globs"))
        rule.container_image_globs = _as_str_list(data.get("container_image_globs"))
        rule.exclude_process_globs = _as_str_list(data.get("exclude_process_globs"))
        rule.exclude_executable_globs = _as_str_list(data.get("exclude_executable_globs"))
        rule.exclude_command_line_globs = _as_str_list(
            data.get("exclude_command_line_globs")
        )
        rule.ports = _as_int_list(data.get("ports"))
        rule.port_ranges = _as_port_ranges(data.get("port_ranges"))
        rule.protocols = [p.upper() for p in _as_str_list(data.get("protocols"))]
        rule.exclude_ports = _as_int_list(data.get("exclude_ports"))
        _validate_port_bounds(rule)
    except (TypeError, ValueError) as exc:
        warnings.append(f"category {name!r}: invalid field ({exc}), skipped")
        return None

    if rule.scheme:
        candidate = rule.scheme.strip().rstrip(":").lower()
        if candidate in _ALLOWED_SCHEMES:
            rule.scheme = candidate
        else:
            warnings.append(
                f"category {name!r}: unsupported scheme {rule.scheme!r}, using http"
            )
            rule.scheme = ""

    all_patterns = (
        rule.process_globs
        + rule.executable_globs
        + rule.command_line_globs
        + rule.address_globs
        + rule.container_name_globs
        + rule.container_image_globs
        + rule.exclude_process_globs
        + rule.exclude_executable_globs
        + rule.exclude_command_line_globs
    )
    for pattern in all_patterns:
        problem = validate_pattern(pattern)
        if problem:
            warnings.append(f"category {name!r}: {problem}")

    return rule


def loads_categories(text: str) -> tuple[list[CategoryRule], list[str]]:
    warnings: list[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"categories file is not valid TOML: {exc}"]
    raw = data.get("category", [])
    if isinstance(raw, dict):
        raw = [raw]
    rules: list[CategoryRule] = []
    for index, item in enumerate(raw or []):
        rule = parse_category(item, index, warnings)
        if rule is not None:
            rules.append(rule)
    return rules, warnings


def load_categories(path: Path) -> tuple[list[CategoryRule], list[str]]:
    if not path.exists():
        return [], []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"could not read categories {path}: {exc}"]
    return loads_categories(text)


# ---------------------------------------------------------------------------
# Atomic save
# ---------------------------------------------------------------------------
def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = str(path.parent)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=directory,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_name = handle.name
    try:
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(tmp_name, path)
    except BaseException:
        # Any failure (write/flush/fsync or os.replace) must not leave a
        # ``*.tmp`` file behind. The previous file is still untouched.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_categories(path: Path, rules: list[CategoryRule]) -> None:
    atomic_write_text(path, dumps_categories(rules))
