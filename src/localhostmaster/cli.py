"""Command line interface and entry point."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .browser import default_opener
from .category_store import load_categories
from .classifier import Classifier, builtin_rules
from .clipboard import default_clipboard
from .config import (
    AppConfig,
    default_categories_path,
    default_config_path,
    default_log_dir,
    load_config,
)
from .docker_resolver import DockerResolver
from .process_resolver import ProcessResolver
from .refresh import build_snapshot
from .scanner import scan_connections
from .url_builder import is_openable, url_for_entry


def _refresh_ms(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}")
    if parsed < 250:
        raise argparse.ArgumentTypeError(
            f"refresh interval must be at least 250 ms (got {parsed})"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localhostmaster",
        description="Inspect local listening ports and the processes behind them.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--once", action="store_true", help="print the port table once and exit")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output and exit")
    parser.add_argument(
        "--show-established", action="store_true", help="include established TCP connections"
    )
    parser.add_argument("--no-docker", action="store_true", help="disable Docker enrichment")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--refresh-ms",
        type=_refresh_ms,
        default=None,
        help="auto refresh interval in milliseconds (minimum 250)",
    )
    parser.add_argument("--no-color", action="store_true", help="disable colours")
    parser.add_argument("--debug", action="store_true", help="enable file logging")
    return parser


def setup_logging(debug: bool) -> Optional[Path]:
    if not debug:
        return None
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "localhostmaster.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger("localhostmaster")
    root.setLevel(logging.DEBUG)
    root.handlers = [handler]
    root.propagate = False
    return log_path


def _json_record(entry) -> dict:
    record = {
        "protocol": entry.protocol.value,
        "family": entry.family.value,
        "address": entry.local_address,
        "port": entry.port,
        "state": entry.socket_state.value,
        "pid": entry.pid,
        "remote_address": entry.remote_address or None,
        "remote_port": entry.remote_port or None,
        "process": entry.process_name if entry.process is not None else None,
        "executable": entry.process.executable if entry.process is not None else None,
        "username": entry.process.username if entry.process is not None else None,
        "category": entry.category.name if entry.category else "unknown",
        "open_in_browser": bool(is_openable(entry)),
        "url": url_for_entry(entry) if is_openable(entry) else None,
    }
    if entry.container is not None:
        record["container"] = {
            "id": entry.container.short_id,
            "name": entry.container.name,
            "image": entry.container.image,
            "container_port": entry.container.container_port,
        }
    else:
        record["container"] = None
    return record


def render_json(entries) -> str:
    return json.dumps([_json_record(e) for e in entries], indent=2, ensure_ascii=False)


def render_text(entries) -> str:
    header = (
        f"{'PROTO':<6}{'ADDRESS':<30}{'PORT':>6}  {'PID':>7}  "
        f"{'PROCESS':<24}{'CATEGORY':<14}{'OPEN':<5}{'REMOTE':<30}CONTAINER"
    )
    lines = [header, "-" * len(header)]
    for entry in entries:
        container = entry.container.name if entry.container else ""
        remote = entry.remote_display if entry.has_remote else ""
        lines.append(
            f"{entry.protocol.value:<6}"
            f"{entry.local_address:<30}"
            f"{entry.port:>6}  "
            f"{(entry.pid if entry.pid is not None else '-'):>7}  "
            f"{entry.process_name:<24}"
            f"{(entry.category.name if entry.category else 'unknown'):<14}"
            f"{('yes' if is_openable(entry) else '-'):<5}"
            f"{remote:<30}"
            f"{container}"
        )
    return "\n".join(lines) + "\n"


def _load_classifier(categories_path) -> tuple[Classifier, list[str]]:
    warnings: list[str] = []
    user_rules, cat_warnings = load_categories(categories_path)
    warnings.extend(cat_warnings)
    classifier = Classifier(user_rules=user_rules, builtin=builtin_rules())
    return classifier, warnings


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"localhostmaster {__version__}")
        return 0

    log_path = setup_logging(args.debug)
    logger = logging.getLogger("localhostmaster")

    config_path = args.config or default_config_path()
    config, warnings = load_config(config_path)
    if args.show_established:
        config.show_established = True
    if args.no_docker:
        config.docker_enabled = False
    if args.refresh_ms is not None:
        config.refresh_ms = max(250, args.refresh_ms)

    categories_path = default_categories_path()
    if args.config is not None:
        try:
            categories_path = Path(args.config).with_name("categories.toml")
        except ValueError:
            parser.error(f"--config path {str(args.config)!r} does not name a file")
    classifier, cat_warnings = _load_classifier(categories_path)
    warnings.extend(cat_warnings)

    resolver = ProcessResolver()
    docker = DockerResolver(
        enabled=config.docker_enabled,
        ttl_s=config.docker_ttl_s,
        timeout_s=config.docker_timeout_s,
    )

    interactive = (
        not args.once
        and not args.json
        and sys.stdout.isatty()
        and sys.stdin.isatty()
    )

    if interactive:
        from .tui.application import LocalhostMasterApp

        app = LocalhostMasterApp(
            config=config,
            classifier=classifier,
            resolver=resolver,
            docker=docker,
            opener=default_opener(),
            clipboard=default_clipboard(),
            categories_path=categories_path,
            user_rules=classifier.user_rules,
            warnings=warnings,
            no_color=args.no_color,
        )
        if docker.enabled:
            docker.start()
        try:
            app.run()
        finally:
            app.shutdown()
            docker.stop()
        return 0

    # Non-interactive: one scan, plain text or JSON.
    if docker.enabled:
        docker.refresh_once()
    raw = scan_connections(include_established=config.show_established)
    snapshot = build_snapshot(
        raw,
        resolver,
        classifier,
        docker=docker if docker.enabled else None,
        generation=1,
    )
    if args.json:
        print(render_json(snapshot.entries))
    else:
        if warnings:
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
        sys.stdout.write(render_text(snapshot.entries))
    if args.debug and log_path:
        logger.debug("scan produced %d endpoints", len(snapshot.entries))
    return 0
