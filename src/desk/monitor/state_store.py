"""SQLite-backed store for account snapshots, trades, and decision traces.

The decision traces are the point of this module. Every agent output, critic
verdict, and Risk Guard reason code is persisted with the cycle that produced
it, so any trade can be explained after the fact — by the dashboards, by the
Coach, and by a judge reading the repo.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from desk.utils.config_loader import PROJECT_ROOT, get_settings
from desk.utils.logging import get_logger
from desk.utils.time_utils import today_et, utc_iso

logger = get_logger("monitor.state_store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id      TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    phase         TEXT,
    regime        TEXT,
    experiment_id TEXT,
    dry_run       INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'running',
    summary       TEXT,
    payload       TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at    TEXT NOT NULL,
    cycle_id       TEXT,
    equity         REAL, cash REAL, buying_power REAL,
    initial_margin REAL, daily_pnl REAL, daily_pnl_pct REAL,
    payload        TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    cycle_id     TEXT,
    symbol       TEXT NOT NULL,
    underlying   TEXT,
    asset_class  TEXT,
    qty          REAL,
    market_value REAL,
    unrealized_pl REAL,
    payload      TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id          TEXT PRIMARY KEY,
    cycle_id          TEXT,
    created_at        TEXT NOT NULL,
    closed_at         TEXT,
    ticker            TEXT,
    playbook          TEXT,
    net_side          TEXT,
    qty               REAL,
    intended_price    REAL,
    fill_price        REAL,
    max_loss          REAL,
    max_profit        REAL,
    days_to_expiry    INTEGER,
    status            TEXT DEFAULT 'proposed',
    pnl               REAL,
    exit_reason       TEXT,
    thesis            TEXT,
    risk_reason_codes TEXT,
    experiment_id     TEXT,
    payload           TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at     TEXT NOT NULL,
    cycle_id         TEXT,
    trade_id         TEXT,
    broker_order_id  TEXT,
    client_order_id  TEXT,
    symbol           TEXT,
    qty              REAL,
    limit_price      REAL,
    status           TEXT,
    dry_run          INTEGER DEFAULT 1,
    payload          TEXT
);

CREATE TABLE IF NOT EXISTS decision_traces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stage      TEXT NOT NULL,
    agent      TEXT,
    mode       TEXT,
    ok         INTEGER DEFAULT 1,
    latency_ms REAL,
    payload    TEXT
);

CREATE TABLE IF NOT EXISTS metrics_daily (
    trade_date    TEXT PRIMARY KEY,
    equity        REAL, day_pnl REAL, realised_pnl REAL, unrealised_pnl REAL,
    trades        INTEGER, wins INTEGER, losses INTEGER,
    experiment_id TEXT, payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_traces_cycle    ON decision_traces(cycle_id);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created  ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_time  ON account_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_positions_cycle ON positions(cycle_id);
"""


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    for key in ("payload", "risk_reason_codes"):
        if isinstance(record.get(key), str) and record[key]:
            try:
                record[key] = json.loads(record[key])
            except json.JSONDecodeError:
                pass
    return record


class StateStore:
    """Typed access to the desk's SQLite state."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        settings = get_settings()
        raw = str(db_path) if db_path else settings.monitor.db_path
        # Check the sentinel *before* resolving, or `PROJECT_ROOT / ":memory:"`
        # turns an in-memory database into a file literally named ":memory:".
        self.in_memory = raw == ":memory:"

        if self.in_memory:
            self.path = Path(raw)
        else:
            path = Path(raw)
            self.path = path if path.is_absolute() else PROJECT_ROOT / path
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._memory_conn: sqlite3.Connection | None = None
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with row access by name and foreign keys on."""
        if self.in_memory:
            # An in-memory database dies with its connection, so tests reuse one.
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:")
                self._memory_conn.row_factory = sqlite3.Row
            yield self._memory_conn
            self._memory_conn.commit()
            return

        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- cycles ------------------------------------------------------------

    def start_cycle(
        self, cycle_id: str, phase: str, regime: str = "", dry_run: bool = True, **extra: Any
    ) -> str:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cycles
                   (cycle_id, started_at, phase, regime, experiment_id, dry_run, status, payload)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    cycle_id,
                    utc_iso(),
                    phase,
                    regime,
                    get_settings().experiment_id,
                    int(dry_run),
                    "running",
                    _json(extra),
                ),
            )
        return cycle_id

    def finish_cycle(self, cycle_id: str, summary: str = "", status: str = "complete", **extra: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE cycles SET finished_at=?, status=?, summary=?, payload=? WHERE cycle_id=?",
                (utc_iso(), status, summary, _json(extra), cycle_id),
            )

    def get_cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- account & positions -----------------------------------------------

    def record_account(self, account: dict[str, Any], cycle_id: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO account_snapshots
                   (captured_at, cycle_id, equity, cash, buying_power, initial_margin,
                    daily_pnl, daily_pnl_pct, payload)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    account.get("as_of") or utc_iso(),
                    cycle_id,
                    float(account.get("equity", 0) or 0),
                    float(account.get("cash", 0) or 0),
                    float(account.get("buying_power", 0) or 0),
                    float(account.get("initial_margin", 0) or 0),
                    float(account.get("daily_pnl", 0) or 0),
                    float(account.get("daily_pnl_pct", 0) or 0),
                    _json(account),
                ),
            )

    def record_positions(self, positions: list[dict[str, Any]], cycle_id: str = "") -> None:
        captured = utc_iso()
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO positions
                   (captured_at, cycle_id, symbol, underlying, asset_class, qty,
                    market_value, unrealized_pl, payload)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        captured,
                        cycle_id,
                        p.get("symbol", ""),
                        p.get("underlying", ""),
                        p.get("asset_class", ""),
                        float(p.get("qty", 0) or 0),
                        float(p.get("market_value", 0) or 0),
                        float(p.get("unrealized_pl", 0) or 0),
                        _json(p),
                    )
                    for p in positions
                ],
            )

    def latest_account(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return _row_to_dict(row) if row else None

    def latest_positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(captured_at) AS t FROM positions").fetchone()
            if not row or not row["t"]:
                return []
            rows = conn.execute(
                "SELECT * FROM positions WHERE captured_at = ?", (row["t"],)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def equity_curve(self, limit: int = 500) -> list[dict[str, Any]]:
        """Chronological equity points for the P&L and drawdown charts."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT captured_at, equity, daily_pnl FROM account_snapshots "
                "ORDER BY captured_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -- trades & orders ---------------------------------------------------

    def record_trade(self, trade: dict[str, Any], cycle_id: str = "", status: str = "proposed") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO trades
                   (trade_id, cycle_id, created_at, ticker, playbook, net_side, qty,
                    intended_price, max_loss, max_profit, days_to_expiry, status,
                    thesis, risk_reason_codes, experiment_id, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(trade.get("trade_id", "")),
                    cycle_id,
                    utc_iso(),
                    trade.get("ticker", ""),
                    trade.get("playbook", ""),
                    trade.get("net_side", ""),
                    float(trade.get("qty", 1) or 1),
                    float(trade.get("net_price", 0) or 0),
                    float(trade.get("max_loss", 0) or 0),
                    trade.get("max_profit"),
                    int(trade.get("days_to_expiry", 0) or 0),
                    status,
                    trade.get("thesis", ""),
                    _json(trade.get("risk_reason_codes", [])),
                    get_settings().experiment_id,
                    _json(trade),
                ),
            )

    def update_trade(self, trade_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "closed_at", "fill_price", "status", "pnl", "exit_reason", "qty", "risk_reason_codes",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "risk_reason_codes" in updates:
            updates["risk_reason_codes"] = _json(updates["risk_reason_codes"])
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE trades SET {assignments} WHERE trade_id=?",
                (*updates.values(), trade_id),
            )

    def record_order(self, order: dict[str, Any], cycle_id: str = "", trade_id: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO orders
                   (submitted_at, cycle_id, trade_id, broker_order_id, client_order_id,
                    symbol, qty, limit_price, status, dry_run, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.get("submitted_at") or utc_iso(),
                    cycle_id,
                    trade_id or order.get("trade_id", ""),
                    order.get("id", ""),
                    order.get("client_order_id", ""),
                    order.get("symbol", ""),
                    float(order.get("qty", 0) or 0),
                    order.get("limit_price"),
                    order.get("status", ""),
                    int(bool(order.get("dry_run", True))),
                    _json(order),
                ),
            )

    def get_trades(
        self, limit: int = 50, status: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trades"
        conditions, params = [], []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY submitted_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- decision traces ---------------------------------------------------

    def record_trace(
        self,
        cycle_id: str,
        stage: str,
        payload: Any,
        agent: str = "",
        mode: str = "",
        ok: bool = True,
        latency_ms: float = 0.0,
    ) -> None:
        """Persist one step of the decision chain."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO decision_traces
                   (cycle_id, created_at, stage, agent, mode, ok, latency_ms, payload)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (cycle_id, utc_iso(), stage, agent, mode, int(ok), latency_ms, _json(payload)),
            )

    def get_traces(self, cycle_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if cycle_id:
                rows = conn.execute(
                    "SELECT * FROM decision_traces WHERE cycle_id=? ORDER BY id ASC LIMIT ?",
                    (cycle_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decision_traces ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- daily metrics -----------------------------------------------------

    def record_daily_metrics(self, metrics: dict[str, Any], trade_date: str | None = None) -> None:
        trade_date = trade_date or today_et().isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO metrics_daily
                   (trade_date, equity, day_pnl, realised_pnl, unrealised_pnl,
                    trades, wins, losses, experiment_id, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_date,
                    float(metrics.get("equity", 0) or 0),
                    float(metrics.get("day_pnl", 0) or 0),
                    float(metrics.get("realised_pnl", 0) or 0),
                    float(metrics.get("unrealised_pnl", 0) or 0),
                    int(metrics.get("trades", 0) or 0),
                    int(metrics.get("wins", 0) or 0),
                    int(metrics.get("losses", 0) or 0),
                    get_settings().experiment_id,
                    _json(metrics),
                ),
            )

    def get_daily_metrics(self, limit: int = 60) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM metrics_daily ORDER BY trade_date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]

    # -- housekeeping ------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Row counts per table — used by ``desk doctor``."""
        tables = ["cycles", "account_snapshots", "positions", "trades", "orders", "decision_traces"]
        with self.connect() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in tables
            }

    def prune(self, retain_days: int | None = None) -> int:
        """Drop traces older than the retention window."""
        from datetime import timedelta

        from desk.utils.time_utils import now_utc

        # `or` would swallow an explicit 0, which means "prune everything".
        days = (
            retain_days
            if retain_days is not None
            else get_settings().monitor.retain_traces_days
        )
        cutoff = utc_iso(now_utc() - timedelta(days=days))
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM decision_traces WHERE created_at < ?", (cutoff,))
            return cursor.rowcount


_STORE: StateStore | None = None


def get_state_store(db_path: str | Path | None = None) -> StateStore:
    """Process-wide state store."""
    global _STORE
    if _STORE is None or db_path is not None:
        _STORE = StateStore(db_path)
    return _STORE
