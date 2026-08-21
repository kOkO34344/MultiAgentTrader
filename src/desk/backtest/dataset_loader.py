"""Historical data loading with an on-disk cache.

Backtests are re-run constantly while tuning; hitting Alpaca for the same bars
each time is slow and burns rate limit. Everything is cached as JSON keyed by
symbol, timeframe, and window.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from desk.utils.config_loader import PROJECT_ROOT, get_settings
from desk.utils.logging import get_logger

logger = get_logger("backtest.dataset")


class DatasetLoader:
    """Cached access to historical equity and option bars."""

    def __init__(self, market_data: Any = None, cache_dir: str | Path | None = None) -> None:
        settings = get_settings()
        self._market_data = market_data
        path = Path(cache_dir or settings.backtest.cache_dir)
        self.cache_dir = path if path.is_absolute() else PROJECT_ROOT / path
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def market_data(self) -> Any:
        if self._market_data is None:
            from desk.alpaca.market_data import MarketData

            self._market_data = MarketData()
        return self._market_data

    def _cache_path(self, kind: str, key: str) -> Path:
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"{kind}-{digest}.json"

    def load_equity_bars(
        self,
        symbols: list[str],
        start: str | date,
        end: str | date,
        timeframe: str = "1D",
        refresh: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Daily (or intraday) bars for the backtest window, cached on disk."""
        start_str = start.isoformat() if isinstance(start, (date, datetime)) else str(start)
        end_str = end.isoformat() if isinstance(end, (date, datetime)) else str(end)
        key = f"{','.join(sorted(symbols))}|{timeframe}|{start_str}|{end_str}"
        path = self._cache_path("equity", key)

        if path.exists() and not refresh:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                logger.info(
                    "dataset_cache_hit",
                    extra={"event": "dataset_cache_hit", "kind": "equity", "symbols": symbols},
                )
                return data
            except (OSError, json.JSONDecodeError):
                logger.warning("dataset_cache_corrupt", extra={"event": "dataset_cache_corrupt", "path": str(path)})

        bars = self.market_data.get_equity_bars(symbols, timeframe, start=start_str, end=end_str)
        try:
            path.write_text(json.dumps(bars, default=str), encoding="utf-8")
        except OSError as exc:
            logger.warning("dataset_cache_write_failed", extra={"event": "dataset_cache_write_failed", "error": str(exc)})
        return bars

    def load_option_bars(
        self,
        contract_symbols: list[str],
        start: str | date,
        end: str | date,
        timeframe: str = "1D",
        refresh: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Real option bars where Alpaca has them; ``{}`` when it does not."""
        start_str = start.isoformat() if isinstance(start, (date, datetime)) else str(start)
        end_str = end.isoformat() if isinstance(end, (date, datetime)) else str(end)
        key = f"{','.join(sorted(contract_symbols))}|{timeframe}|{start_str}|{end_str}"
        path = self._cache_path("option", key)

        if path.exists() and not refresh:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        try:
            bars = self.market_data.get_options_bars(contract_symbols, timeframe, start=start_str, end=end_str)
        except Exception as exc:  # noqa: BLE001 - the engine falls back to model pricing
            logger.warning(
                "option_bars_unavailable",
                extra={"event": "option_bars_unavailable", "error": str(exc)[:200]},
            )
            return {}

        try:
            path.write_text(json.dumps(bars, default=str), encoding="utf-8")
        except OSError:
            pass
        return bars

    def clear_cache(self) -> int:
        """Delete every cached file. Returns how many were removed."""
        removed = 0
        for path in self.cache_dir.glob("*.json"):
            path.unlink()
            removed += 1
        return removed
