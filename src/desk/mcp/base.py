"""Shared tool plumbing: the response envelope and the tool registry type."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from desk.utils.logging import get_logger
from desk.utils.time_utils import utc_iso

logger = get_logger("mcp")

TIMEFRAME_ENUM = ["1Min", "5Min", "15Min", "1H", "1D"]

#: Hard ceiling on rows in any single tool response. Option chains can run to
#: thousands of contracts and would otherwise blow up the caller's context.
MAX_ROWS = 400


@dataclass
class ToolSpec:
    """One MCP tool: its schema and its handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    annotations: dict[str, Any] = field(default_factory=dict)


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    """A successful tool response."""
    return {"ok": True, "data": data, "error": None, "as_of": utc_iso(), **meta}


def fail(message: str, error_type: str = "error", retryable: bool = False, **meta: Any) -> dict[str, Any]:
    """A failed tool response. Tools never raise across the MCP boundary."""
    return {
        "ok": False,
        "data": None,
        "error": message,
        "error_type": error_type,
        "retryable": retryable,
        "as_of": utc_iso(),
        **meta,
    }


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to ``(error_type, retryable)`` for the caller."""
    from desk.alpaca.client import (
        AlpacaNotConfiguredError,
        PaperOnlyError,
        is_retryable,
    )

    if isinstance(exc, PaperOnlyError):
        return "paper_only_violation", False
    if isinstance(exc, AlpacaNotConfiguredError):
        return "not_configured", False
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_input", False
    if is_retryable(exc):
        return "rate_limited_or_transient", True
    return "upstream_error", False


def safe_call(name: str, handler: Callable[[dict[str, Any]], dict[str, Any]], arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool handler, converting every failure into an envelope."""
    try:
        return handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 - the MCP boundary must never raise
        error_type, retryable = classify_error(exc)
        logger.error(
            "mcp_tool_failed",
            extra={"event": "mcp_tool_failed", "tool": name, "error_type": error_type, "error": str(exc)[:400]},
        )
        return fail(str(exc), error_type, retryable, tool=name)


def truncate(rows: list[Any], limit: int | None = None) -> tuple[list[Any], bool]:
    """Cap a result set, reporting whether anything was dropped."""
    cap = min(limit or MAX_ROWS, MAX_ROWS)
    if len(rows) <= cap:
        return rows, False
    return rows[:cap], True
