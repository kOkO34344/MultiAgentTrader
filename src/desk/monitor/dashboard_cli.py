"""Rich terminal dashboard: P&L, positions, regime, and decision traces."""

from __future__ import annotations

from typing import Any

from desk.monitor.heartbeat import check_heartbeat
from desk.monitor.state_store import StateStore, get_state_store
from desk.utils.config_loader import get_settings
from desk.utils.math_utils import max_drawdown

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 60) -> str:
    """Unicode sparkline of an equity curve."""
    if len(values) < 2:
        return "—"
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return SPARK_CHARS[0] * len(values)
    return "".join(
        SPARK_CHARS[min(int((v - low) / (high - low) * (len(SPARK_CHARS) - 1)), len(SPARK_CHARS) - 1)]
        for v in values
    )


def _pnl_style(value: float) -> str:
    return "green" if value > 0 else "red" if value < 0 else "dim"


def collect(store: StateStore | None = None) -> dict[str, Any]:
    """Gather everything the dashboards render. Shared with the web view."""
    store = store or get_state_store()
    account = store.latest_account() or {}
    positions = store.latest_positions()
    trades = store.get_trades(limit=15)
    cycles = store.get_cycles(limit=5)
    curve_rows = store.equity_curve(limit=500)
    equity = [float(r["equity"]) for r in curve_rows if r.get("equity")]
    drawdown_abs, drawdown_pct = max_drawdown(equity)

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if float(t["pnl"]) > 0]

    return {
        "account": account,
        "positions": positions,
        "trades": trades,
        "cycles": cycles,
        "equity_curve": equity,
        "equity_dates": [r.get("captured_at") for r in curve_rows],
        "heartbeat": check_heartbeat(),
        "regime": (cycles[0].get("regime") if cycles else "") or "unknown",
        "counts": store.counts(),
        "summary": {
            "equity": account.get("equity", 0.0),
            "cash": account.get("cash", 0.0),
            "buying_power": account.get("buying_power", 0.0),
            "day_pnl": account.get("daily_pnl", 0.0),
            "day_pnl_pct": account.get("daily_pnl_pct", 0.0),
            "open_positions": len(positions),
            "unrealised_pnl": sum(float(p.get("unrealized_pl") or 0) for p in positions),
            "realised_pnl": sum(float(t["pnl"]) for t in closed),
            "max_drawdown": drawdown_abs,
            "max_drawdown_pct": drawdown_pct,
            "closed_trades": len(closed),
            "hit_rate": (len(wins) / len(closed)) if closed else 0.0,
            "dry_run": get_settings().execution.dry_run,
            "experiment_id": get_settings().experiment_id,
        },
    }


def render(store: StateStore | None = None) -> None:
    """Print the dashboard once."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    data = collect(store)
    summary = data["summary"]

    mode = "[yellow]DRY RUN[/yellow]" if summary["dry_run"] else "[red]LIVE PAPER ORDERS[/red]"
    console.print(
        Panel(
            f"[bold]Multi-Agent Options Desk[/bold]   {mode}\n"
            f"Experiment: [cyan]{summary['experiment_id']}[/cyan]    "
            f"Regime: [magenta]{data['regime']}[/magenta]",
            border_style="blue",
        )
    )

    # -- account ---------------------------------------------------------
    account_table = Table(title="Account", show_header=True, header_style="bold cyan")
    for column in ("Equity", "Cash", "Buying power", "Day P&L", "Unrealised", "Realised"):
        account_table.add_column(column, justify="right")
    account_table.add_row(
        f"${summary['equity']:,.2f}",
        f"${summary['cash']:,.2f}",
        f"${summary['buying_power']:,.2f}",
        f"[{_pnl_style(summary['day_pnl'])}]${summary['day_pnl']:,.2f} "
        f"({summary['day_pnl_pct']:+.2%})[/]",
        f"[{_pnl_style(summary['unrealised_pnl'])}]${summary['unrealised_pnl']:,.2f}[/]",
        f"[{_pnl_style(summary['realised_pnl'])}]${summary['realised_pnl']:,.2f}[/]",
    )
    console.print(account_table)

    # -- equity curve ----------------------------------------------------
    if data["equity_curve"]:
        console.print(
            Panel(
                f"[green]{sparkline(data['equity_curve'])}[/green]\n"
                f"Max drawdown: ${summary['max_drawdown']:,.2f} "
                f"({summary['max_drawdown_pct']:.2%})    "
                f"Closed trades: {summary['closed_trades']}    "
                f"Hit rate: {summary['hit_rate']:.0%}",
                title=f"Equity curve ({len(data['equity_curve'])} snapshots)",
                border_style="green",
            )
        )

    # -- positions -------------------------------------------------------
    positions_table = Table(title=f"Open positions ({len(data['positions'])})", header_style="bold cyan")
    for column, justify in (
        ("Symbol", "left"), ("Underlying", "left"), ("Class", "left"),
        ("Qty", "right"), ("Market value", "right"), ("Unrealised", "right"),
    ):
        positions_table.add_column(column, justify=justify)
    for position in data["positions"][:15]:
        unrealised = float(position.get("unrealized_pl") or 0)
        positions_table.add_row(
            str(position.get("symbol", "")),
            str(position.get("underlying", "")),
            str(position.get("asset_class", "")),
            f"{float(position.get('qty', 0)):g}",
            f"${float(position.get('market_value', 0)):,.2f}",
            f"[{_pnl_style(unrealised)}]${unrealised:,.2f}[/]",
        )
    if not data["positions"]:
        positions_table.add_row("[dim]— no open positions —[/dim]", "", "", "", "", "")
    console.print(positions_table)

    # -- trades ----------------------------------------------------------
    trades_table = Table(title="Recent trades", header_style="bold cyan")
    for column in ("Trade", "Ticker", "Playbook", "Status", "P&L", "Risk codes", "Thesis"):
        trades_table.add_column(column, overflow="ellipsis")
    for trade in data["trades"][:10]:
        pnl = trade.get("pnl")
        codes = trade.get("risk_reason_codes") or []
        trades_table.add_row(
            str(trade.get("trade_id", ""))[:22],
            str(trade.get("ticker", "")),
            str(trade.get("playbook", "")),
            str(trade.get("status", "")),
            f"[{_pnl_style(float(pnl or 0))}]${float(pnl):,.2f}[/]" if pnl is not None else "[dim]—[/dim]",
            ",".join(codes if isinstance(codes, list) else [])[:28],
            str(trade.get("thesis", ""))[:44],
        )
    if not data["trades"]:
        trades_table.add_row("[dim]— no trades recorded —[/dim]", "", "", "", "", "", "")
    console.print(trades_table)

    # -- health ------------------------------------------------------------
    heartbeat = data["heartbeat"]
    if heartbeat["healthy"]:
        age = heartbeat.get("age_seconds") or 0
        console.print(
            Panel(f"[green]Heartbeat OK[/green] — last cycle {age / 60:.0f} minute(s) ago.",
                  border_style="green")
        )
    else:
        console.print(Panel(f"[red]ALERT[/red] — {heartbeat['alert']}", border_style="red"))


def watch(interval: int = 10, store: StateStore | None = None) -> None:
    """Re-render on an interval until interrupted."""
    import time

    from rich.console import Console

    console = Console()
    try:
        while True:
            console.clear()
            render(store)
            console.print(f"[dim]Refreshing every {interval}s — Ctrl-C to exit.[/dim]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard stopped.[/dim]")
