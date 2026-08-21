"""Structured logging: JSON lines to disk, human-readable to the console."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CONFIGURED = False
_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JSONFormatter(logging.Formatter):
    """One JSON object per line — greppable, and directly loadable by the Coach."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = "logs",
    console: bool = True,
    filename: str = "desk.jsonl",
) -> logging.Logger:
    """Configure root logging once. Safe to call repeatedly."""
    global _CONFIGURED
    root = logging.getLogger("desk")
    if _CONFIGURED:
        return root

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    directory = Path(log_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(directory / filename, encoding="utf-8")
        file_handler.setFormatter(JSONFormatter())
        root.addHandler(file_handler)
    except OSError:
        # A read-only filesystem must not stop the desk from running.
        pass

    if console:
        try:
            from rich.logging import RichHandler

            handler: logging.Handler = RichHandler(
                rich_tracebacks=True, show_path=False, markup=False
            )
            handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
        except ImportError:  # pragma: no cover
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        root.addHandler(handler)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.LoggerAdapter | logging.Logger:
    """Get a namespaced child of the ``desk`` logger."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"desk.{name}")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured event. Fields land as top-level JSON keys."""
    logger.info(event, extra={"event": event, **fields})
