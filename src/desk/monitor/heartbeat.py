"""Cycle heartbeat and staleness alerting.

A silent trading system is indistinguishable from a dead one. Every cycle writes
``monitor/heartbeat.json``; the dashboards and ``desk doctor`` read it back and
raise an alert when it goes stale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from desk.utils.config_loader import PROJECT_ROOT, get_settings
from desk.utils.logging import get_logger
from desk.utils.time_utils import seconds_since, utc_iso

logger = get_logger("monitor.heartbeat")


def heartbeat_path() -> Path:
    path = Path(get_settings().monitor.heartbeat_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def write_heartbeat(
    status: str = "ok", cycle_id: str = "", phase: str = "", **details: Any
) -> Path:
    """Write the heartbeat file. Never raises — a failed write must not stop a cycle."""
    path = heartbeat_path()
    payload = {
        "timestamp": utc_iso(),
        "status": status,
        "cycle_id": cycle_id,
        "phase": phase,
        "experiment_id": get_settings().experiment_id,
        "dry_run": get_settings().execution.dry_run,
        **details,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "heartbeat_write_failed",
            extra={"event": "heartbeat_write_failed", "error": str(exc)},
        )
    return path


def read_heartbeat() -> dict[str, Any] | None:
    """Read the last heartbeat, or ``None`` when it has never been written."""
    path = heartbeat_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "heartbeat_read_failed", extra={"event": "heartbeat_read_failed", "error": str(exc)}
        )
        return None


def check_heartbeat(stale_after_seconds: int | None = None) -> dict[str, Any]:
    """Report heartbeat health.

    Returns ``{"healthy": bool, "alert": str | None, ...}``. A missing heartbeat
    is unhealthy, not merely unknown — fail loud.
    """
    # `or` would swallow an explicit 0, which is a legitimate threshold in tests.
    threshold = (
        stale_after_seconds
        if stale_after_seconds is not None
        else get_settings().monitor.heartbeat_stale_seconds
    )
    beat = read_heartbeat()

    if beat is None:
        return {
            "healthy": False,
            "alert": "No heartbeat file found — the desk has never completed a cycle.",
            "age_seconds": None,
            "heartbeat": None,
        }

    try:
        age = seconds_since(beat["timestamp"])
    except (KeyError, ValueError):
        return {
            "healthy": False,
            "alert": "Heartbeat file has no readable timestamp.",
            "age_seconds": None,
            "heartbeat": beat,
        }

    if age > threshold:
        alert = (
            f"Heartbeat is {age / 60:.0f} minutes old "
            f"(threshold {threshold / 60:.0f}m). Last status: {beat.get('status')}."
        )
        logger.warning("heartbeat_stale", extra={"event": "heartbeat_stale", "age_seconds": age})
        return {"healthy": False, "alert": alert, "age_seconds": age, "heartbeat": beat}

    if beat.get("status") not in {"ok", "complete"}:
        return {
            "healthy": False,
            "alert": f"Last cycle reported status '{beat.get('status')}'.",
            "age_seconds": age,
            "heartbeat": beat,
        }

    return {"healthy": True, "alert": None, "age_seconds": age, "heartbeat": beat}
