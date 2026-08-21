"""Performance metrics shared by the backtester and the live desk.

Both paths compute their numbers here so a backtest result and a live result are
directly comparable rather than two different definitions of "hit rate".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from desk.utils.math_utils import max_drawdown, safe_div, sharpe_ratio, sortino_ratio


def trade_outcomes(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Win/loss statistics over closed trades."""
    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed:
        return {
            "trades": 0, "wins": 0, "losses": 0, "scratches": 0,
            "hit_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "avg_rr": 0.0,
        }

    pnls = [float(t["pnl"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = safe_div(gross_profit, len(wins))
    avg_loss = safe_div(gross_loss, len(losses))
    hit_rate = safe_div(len(wins), len(closed))

    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "hit_rate": round(hit_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(max(pnls), 2),
        "largest_loss": round(min(pnls), 2),
        # Gross profit divided by gross loss: above 1.0 means the strategy makes
        # money. Infinite when there are no losers, which is a red flag for
        # sample size, not a result.
        "profit_factor": round(safe_div(gross_profit, gross_loss, float("inf") if gross_profit else 0.0), 3),
        "expectancy": round(hit_rate * avg_win - (1 - hit_rate) * avg_loss, 2),
        "avg_rr": round(safe_div(avg_win, avg_loss), 3),
    }


def options_stats(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Options-specific statistics the equity metrics cannot express."""
    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed:
        return {}

    credits = [t for t in closed if t.get("net_side") == "credit"]
    debits = [t for t in closed if t.get("net_side") == "debit"]
    held = [float(t["days_held"]) for t in closed if t.get("days_held") is not None]
    expired = [t for t in closed if t.get("exit_reason") == "expiry"]

    # How much of the credit received was actually kept, on credit structures.
    capture = [
        float(t["pnl"]) / (float(t["max_profit"]) or 1)
        for t in credits
        if t.get("max_profit")
    ]

    by_playbook: dict[str, list[float]] = {}
    for trade in closed:
        by_playbook.setdefault(str(trade.get("playbook", "unknown")), []).append(float(trade["pnl"]))

    return {
        "credit_structures": len(credits),
        "debit_structures": len(debits),
        "avg_days_held": round(safe_div(sum(held), len(held)), 1) if held else None,
        "expired_worthless_pct": round(safe_div(len(expired), len(closed)), 4),
        "avg_credit_capture": round(safe_div(sum(capture), len(capture)), 4) if capture else None,
        "by_playbook": {
            name: {
                "trades": len(pnls),
                "total_pnl": round(sum(pnls), 2),
                "hit_rate": round(safe_div(sum(1 for p in pnls if p > 0), len(pnls)), 3),
                "avg_pnl": round(safe_div(sum(pnls), len(pnls)), 2),
            }
            for name, pnls in sorted(by_playbook.items())
        },
    }


def curve_metrics(equity_curve: Sequence[float], initial_capital: float | None = None) -> dict[str, Any]:
    """Return, drawdown, and risk-adjusted statistics from an equity curve."""
    if len(equity_curve) < 2:
        return {
            "total_pnl": 0.0, "total_return_pct": 0.0,
            "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
            "sharpe": 0.0, "sortino": 0.0, "volatility_annualised": 0.0,
            "best_day": 0.0, "worst_day": 0.0, "periods": len(equity_curve),
        }

    start = initial_capital or equity_curve[0]
    end = equity_curve[-1]
    returns = [
        (equity_curve[i] / equity_curve[i - 1]) - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
    drawdown_abs, drawdown_pct = max_drawdown(equity_curve)

    volatility = 0.0
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        volatility = (variance**0.5) * (252**0.5)

    return {
        "starting_equity": round(start, 2),
        "ending_equity": round(end, 2),
        "total_pnl": round(end - start, 2),
        "total_return_pct": round(safe_div(end - start, start), 4),
        "max_drawdown": round(drawdown_abs, 2),
        "max_drawdown_pct": round(drawdown_pct, 4),
        "sharpe": round(sharpe_ratio(returns), 3),
        "sortino": round(sortino_ratio(returns), 3) if sortino_ratio(returns) != float("inf") else None,
        "volatility_annualised": round(volatility, 4),
        "best_day": round(max(returns), 4) if returns else 0.0,
        "worst_day": round(min(returns), 4) if returns else 0.0,
        "periods": len(equity_curve),
    }


def compute_metrics(
    trades: Sequence[dict[str, Any]],
    equity_curve: Sequence[float] | None = None,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """The desk's full metrics bundle."""
    metrics: dict[str, Any] = {
        **curve_metrics(equity_curve or [], initial_capital),
        **trade_outcomes(trades),
    }
    metrics["options"] = options_stats(trades)
    metrics["outcome_distribution"] = outcome_distribution(trades)
    return metrics


def outcome_distribution(trades: Sequence[dict[str, Any]], buckets: int = 8) -> dict[str, int]:
    """Histogram of trade P&L — reveals fat tails a mean would hide."""
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    if not pnls:
        return {}
    low, high = min(pnls), max(pnls)
    if high - low < 1e-9:
        return {f"{low:.0f}": len(pnls)}

    width = (high - low) / buckets
    histogram: dict[str, int] = {}
    for index in range(buckets):
        start = low + index * width
        end = start + width
        label = f"{start:,.0f} to {end:,.0f}"
        count = sum(1 for p in pnls if start <= p < end or (index == buckets - 1 and p == high))
        histogram[label] = count
    return histogram


def format_metrics(metrics: dict[str, Any]) -> str:
    """Compact human-readable summary for the CLI."""
    lines = [
        f"  Period P&L        ${metrics.get('total_pnl', 0):>12,.2f}  "
        f"({metrics.get('total_return_pct', 0):+.2%})",
        f"  Max drawdown      ${metrics.get('max_drawdown', 0):>12,.2f}  "
        f"({metrics.get('max_drawdown_pct', 0):.2%})",
        f"  Sharpe / Sortino  {metrics.get('sharpe', 0):>12.2f}  / {metrics.get('sortino') or 0:.2f}",
        f"  Trades            {metrics.get('trades', 0):>12}  "
        f"({metrics.get('wins', 0)}W / {metrics.get('losses', 0)}L)",
        f"  Hit rate          {metrics.get('hit_rate', 0):>12.1%}",
        f"  Avg win / loss    ${metrics.get('avg_win', 0):>12,.2f}  / ${metrics.get('avg_loss', 0):,.2f}",
        f"  Profit factor     {metrics.get('profit_factor', 0):>12.2f}",
        f"  Expectancy        ${metrics.get('expectancy', 0):>12,.2f}",
    ]
    return "\n".join(lines)
