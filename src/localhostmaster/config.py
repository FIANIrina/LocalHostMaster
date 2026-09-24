"""Application configuration (``%APPDATA%\\LocalhostMaster\\config.toml``)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional

APP_DIR_NAME = "LocalhostMaster"
VALID_COLOR_MODES = ("auto", "truecolor", "256", "16", "none")


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def local_appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def default_config_path() -> Path:
    return appdata_dir() / "config.toml"


def default_categories_path() -> Path:
    return appdata_dir() / "categories.toml"


def default_log_dir() -> Path:
    return local_appdata_dir() / "logs"


@dataclass
class AppConfig:
    refresh_ms: int = 3000
    show_established: bool = False
    color_mode: str = "auto"
    docker_enabled: bool = True
    docker_ttl_s: float = 15.0
    docker_timeout_s: float = 2.0
    double_enter_ms: int = 2500
    arm_guard_ms: int = 150

    def normalized(self) -> "AppConfig":
        cfg = AppConfig(**self.__dict__)
        try:
            cfg.refresh_ms = max(250, int(cfg.refresh_ms))
        except (TypeError, ValueError):
            cfg.refresh_ms = 3000
        try:
            cfg.double_enter_ms = max(300, int(cfg.double_enter_ms))
        except (TypeError, ValueError):
            cfg.double_enter_ms = 2500
        try:
            cfg.arm_guard_ms = max(0, int(cfg.arm_guard_ms))
        except (TypeError, ValueError):
            cfg.arm_guard_ms = 150
        # A guard window that is not shorter than the confirmation window would
        # make double-Enter (and double-k) impossible: every second press would
        # fall inside the auto-repeat guard and be ignored. Clamp it to at most
        # half the confirmation window so a usable confirmation period remains.
        max_guard = cfg.double_enter_ms // 2
        if cfg.arm_guard_ms > max_guard:
            cfg.arm_guard_ms = max(0, max_guard)
        try:
            cfg.docker_ttl_s = max(1.0, float(cfg.docker_ttl_s))
        except (TypeError, ValueError):
            cfg.docker_ttl_s = 15.0
        try:
            cfg.docker_timeout_s = max(0.1, float(cfg.docker_timeout_s))
        except (TypeError, ValueError):
            cfg.docker_timeout_s = 2.0
        if cfg.color_mode not in VALID_COLOR_MODES:
            cfg.color_mode = "auto"
        return cfg


_ALLOWED_KEYS = {f.name for f in fields(AppConfig)}


def load_config(path: Optional[Path] = None) -> tuple[AppConfig, list[str]]:
    """Load config, tolerating missing or corrupt files.

    Returns the config plus a list of human-readable warnings. Never raises
    for expected file/format problems.
    """
    warnings: list[str] = []
    path = path or default_config_path()
    if not path.exists():
        return AppConfig(), warnings

    try:
        raw = path.read_bytes()
    except OSError as exc:
        warnings.append(f"Could not read config {path}: {exc}")
        return AppConfig(), warnings

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        warnings.append(f"Config {path} is not valid TOML, using defaults: {exc}")
        return AppConfig(), warnings

    if not isinstance(data, dict):
        warnings.append(f"Config {path} has unexpected structure, using defaults")
        return AppConfig(), warnings

    section = data.get("general", data)
    if not isinstance(section, dict):
        warnings.append(f"Config {path} [general] is not a table, using defaults")
        return AppConfig(), warnings

    cfg = AppConfig()
    for key, value in section.items():
        if key not in _ALLOWED_KEYS:
            warnings.append(f"Config key ignored (unknown): {key}")
            continue
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ValueError("expected boolean")
                setattr(cfg, key, value)
            elif isinstance(current, int):
                setattr(cfg, key, int(value))
            elif isinstance(current, float):
                setattr(cfg, key, float(value))
            elif isinstance(current, str):
                if not isinstance(value, str):
                    raise ValueError("expected string")
                setattr(cfg, key, value)
            else:
                setattr(cfg, key, value)
        except (TypeError, ValueError) as exc:
            warnings.append(f"Config key {key!r} invalid ({exc}); using default")

    normalized = cfg.normalized()
    if (
        isinstance(cfg.refresh_ms, int)
        and cfg.refresh_ms != normalized.refresh_ms
    ):
        warnings.append(
            f"Config refresh_ms {cfg.refresh_ms} is below the minimum "
            f"{normalized.refresh_ms} ms; using {normalized.refresh_ms}"
        )
    if (
        isinstance(cfg.arm_guard_ms, int)
        and cfg.arm_guard_ms != normalized.arm_guard_ms
    ):
        warnings.append(
            f"Config arm_guard_ms {cfg.arm_guard_ms} must be at most half of "
            f"double_enter_ms {normalized.double_enter_ms}; using "
            f"{normalized.arm_guard_ms}"
        )
    if (
        isinstance(cfg.color_mode, str)
        and cfg.color_mode not in VALID_COLOR_MODES
    ):
        warnings.append(
            f"Config color_mode {cfg.color_mode!r} is not one of "
            f"{', '.join(VALID_COLOR_MODES)}; using auto"
        )
    if (
        isinstance(cfg.docker_ttl_s, (int, float))
        and not isinstance(cfg.docker_ttl_s, bool)
        and cfg.docker_ttl_s < 1.0
    ):
        warnings.append(
            f"Config docker_ttl_s {cfg.docker_ttl_s} is too small; "
            f"using {normalized.docker_ttl_s}"
        )
    if (
        isinstance(cfg.docker_timeout_s, (int, float))
        and not isinstance(cfg.docker_timeout_s, bool)
        and cfg.docker_timeout_s <= 0
    ):
        warnings.append(
            f"Config docker_timeout_s {cfg.docker_timeout_s} is too small; "
            f"using {normalized.docker_timeout_s}"
        )
    return normalized, warnings
