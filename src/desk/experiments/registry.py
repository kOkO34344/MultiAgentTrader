"""Registry of desk configurations and their results.

Every backtest and every live run is logged against an experiment id, together
with a hash of the prompts and config that produced it. Without that, "the
iron condors worked better this week" is an anecdote rather than a finding.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from desk.utils.config_loader import (
    EXPERIMENTS_PATH,
    PROMPTS_DIR,
    Settings,
    get_settings,
)
from desk.utils.logging import get_logger
from desk.utils.time_utils import utc_iso

logger = get_logger("experiments")


def prompt_fingerprints() -> dict[str, str]:
    """Short content hash of every persona, so a prompt edit is visible in results."""
    fingerprints: dict[str, str] = {}
    if not PROMPTS_DIR.exists():
        return fingerprints
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        fingerprints[path.stem] = digest
    return fingerprints


class ExperimentRegistry:
    """Read/write access to ``config/experiments/experiments.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or EXPERIMENTS_PATH
        self._data: dict[str, Any] | None = None

    # -- persistence -------------------------------------------------------

    def load(self) -> dict[str, Any]:
        if self._data is None:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._data = {
                    "schema_version": "1.0.0",
                    "updated_at": None,
                    "active_experiment_id": None,
                    "experiments": [],
                }
        return self._data

    def save(self) -> None:
        data = self.load()
        data["updated_at"] = utc_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")

    # -- queries -----------------------------------------------------------

    def list_experiments(self) -> list[dict[str, Any]]:
        return list(self.load().get("experiments", []))

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        return next(
            (e for e in self.load().get("experiments", []) if e.get("id") == experiment_id), None
        )

    def active(self) -> dict[str, Any] | None:
        data = self.load()
        return self.get(data.get("active_experiment_id") or "")

    def set_active(self, experiment_id: str) -> None:
        if not self.get(experiment_id):
            raise KeyError(f"Unknown experiment '{experiment_id}'")
        self.load()["active_experiment_id"] = experiment_id
        self.save()

    # -- mutation ----------------------------------------------------------

    def create(
        self,
        experiment_id: str,
        description: str,
        settings: Settings | None = None,
        notes: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register a new experiment, capturing the config that defines it."""
        settings = settings or get_settings()
        data = self.load()

        if self.get(experiment_id):
            if not overwrite:
                raise ValueError(f"Experiment '{experiment_id}' already exists")
            data["experiments"] = [e for e in data["experiments"] if e["id"] != experiment_id]

        entry = {
            "id": experiment_id,
            "description": description,
            "created_at": utc_iso(),
            "config_version": settings.config_version,
            "prompt_versions": prompt_fingerprints(),
            "universe": settings.universe.all_tickers,
            "risk_limits": settings.risk_limits.model_dump(),
            "regimes_used": list(settings.regime.labels),
            "playbooks_used": [],
            "notes": notes,
            "backtest_results": None,
            "live_results": None,
            "runs": [],
        }
        data["experiments"].append(entry)
        if not data.get("active_experiment_id"):
            data["active_experiment_id"] = experiment_id
        self.save()
        logger.info(
            "experiment_created",
            extra={"event": "experiment_created", "experiment_id": experiment_id},
        )
        return entry

    def _ensure(self, experiment_id: str) -> dict[str, Any]:
        entry = self.get(experiment_id)
        if entry is None:
            entry = self.create(experiment_id, f"Auto-created for run at {utc_iso()}")
        return entry

    def log_backtest_run(
        self,
        experiment_id: str,
        metrics: dict[str, Any],
        period: dict[str, str] | None = None,
        curve_path: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Attach backtest metrics to an experiment."""
        entry = self._ensure(experiment_id)
        run = {
            "run_id": f"bt-{len(entry.get('runs', [])) + 1:03d}",
            "type": "backtest",
            "logged_at": utc_iso(),
            "period": period or {},
            "metrics": metrics,
            "curve_path": curve_path,
            **extra,
        }
        entry.setdefault("runs", []).append(run)
        entry["backtest_results"] = metrics
        self.save()
        logger.info(
            "backtest_logged",
            extra={"event": "backtest_logged", "experiment_id": experiment_id, "run_id": run["run_id"]},
        )
        return run

    def log_live_run(
        self,
        experiment_id: str,
        metrics: dict[str, Any],
        trade_date: str | None = None,
        curve_path: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Attach a competition-mode (live paper) session to an experiment."""
        entry = self._ensure(experiment_id)
        run = {
            "run_id": f"live-{len(entry.get('runs', [])) + 1:03d}",
            "type": "live_paper",
            "logged_at": utc_iso(),
            "trade_date": trade_date,
            "metrics": metrics,
            "curve_path": curve_path,
            **extra,
        }
        entry.setdefault("runs", []).append(run)

        # Live results accumulate across days rather than being replaced.
        live = entry.get("live_results") or {"sessions": 0, "cumulative_pnl": 0.0, "history": []}
        live["sessions"] = int(live.get("sessions", 0)) + 1
        live["cumulative_pnl"] = round(
            float(live.get("cumulative_pnl", 0.0)) + float(metrics.get("day_pnl", 0.0) or 0.0), 2
        )
        live["history"] = [*live.get("history", []), {"date": trade_date, **metrics}][-60:]
        live["latest"] = metrics
        entry["live_results"] = live
        self.save()
        return run

    def compare(self) -> list[dict[str, Any]]:
        """Flat comparison table across experiments — used by the dashboards."""
        rows = []
        for entry in self.list_experiments():
            backtest = entry.get("backtest_results") or {}
            live = entry.get("live_results") or {}
            rows.append(
                {
                    "id": entry["id"],
                    "description": entry.get("description", "")[:80],
                    "runs": len(entry.get("runs", [])),
                    "backtest_pnl": backtest.get("total_pnl"),
                    "backtest_sharpe": backtest.get("sharpe"),
                    "backtest_max_dd": backtest.get("max_drawdown_pct"),
                    "live_sessions": live.get("sessions", 0),
                    "live_cumulative_pnl": live.get("cumulative_pnl"),
                }
            )
        return rows


_REGISTRY: ExperimentRegistry | None = None


def get_registry() -> ExperimentRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ExperimentRegistry()
    return _REGISTRY
