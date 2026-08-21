"""Options pricing, Greeks, technical indicators, and small numeric helpers.

Black-Scholes here is a *fallback*: Alpaca's option snapshots carry greeks and
implied volatility, and those are preferred whenever present. This module fills
the gaps (illiquid contracts, backtests, sanity checks) so the desk never has a
structure whose risk profile it cannot compute.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

SQRT_2PI = math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Normal distribution
# ---------------------------------------------------------------------------


def norm_pdf(x: float) -> float:
    """Standard normal probability density."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------


def _d1_d2(
    spot: float, strike: float, time_years: float, rate: float, vol: float, dividend: float
) -> tuple[float, float]:
    time_years = max(time_years, 1e-9)
    vol = max(vol, 1e-9)
    d1 = (
        math.log(max(spot, 1e-9) / max(strike, 1e-9))
        + (rate - dividend + 0.5 * vol * vol) * time_years
    ) / (vol * math.sqrt(time_years))
    return d1, d1 - vol * math.sqrt(time_years)


def bs_price(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    vol: float,
    right: str = "call",
    dividend: float = 0.0,
) -> float:
    """Black-Scholes price of a European option."""
    if time_years <= 0:
        intrinsic = spot - strike if right.lower().startswith("c") else strike - spot
        return max(intrinsic, 0.0)

    d1, d2 = _d1_d2(spot, strike, time_years, rate, vol, dividend)
    discount, carry = math.exp(-rate * time_years), math.exp(-dividend * time_years)

    if right.lower().startswith("c"):
        return spot * carry * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    return strike * discount * norm_cdf(-d2) - spot * carry * norm_cdf(-d1)


def bs_greeks(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    vol: float,
    right: str = "call",
    dividend: float = 0.0,
) -> dict[str, float]:
    """Per-share Greeks. Multiply by the contract multiplier for position risk.

    ``theta`` is per calendar day; ``vega`` is per 1 volatility point (1%).
    """
    is_call = right.lower().startswith("c")

    if time_years <= 0 or vol <= 0:
        if is_call:
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2(spot, strike, time_years, rate, vol, dividend)
    sqrt_t = math.sqrt(time_years)
    discount, carry = math.exp(-rate * time_years), math.exp(-dividend * time_years)
    pdf_d1 = norm_pdf(d1)

    gamma = carry * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * carry * pdf_d1 * sqrt_t / 100.0
    common_theta = -(spot * carry * pdf_d1 * vol) / (2.0 * sqrt_t)

    if is_call:
        delta = carry * norm_cdf(d1)
        theta = (
            common_theta
            - rate * strike * discount * norm_cdf(d2)
            + dividend * spot * carry * norm_cdf(d1)
        ) / 365.0
        rho = strike * time_years * discount * norm_cdf(d2) / 100.0
    else:
        delta = -carry * norm_cdf(-d1)
        theta = (
            common_theta
            + rate * strike * discount * norm_cdf(-d2)
            - dividend * spot * carry * norm_cdf(-d1)
        ) / 365.0
        rho = -strike * time_years * discount * norm_cdf(-d2) / 100.0

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho}


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    right: str = "call",
    dividend: float = 0.0,
    tolerance: float = 1e-6,
    max_iterations: int = 100,
) -> float | None:
    """Solve for implied vol by bisection (slower than Newton, never diverges)."""
    if price <= 0 or time_years <= 0:
        return None

    intrinsic = max(
        (spot - strike) if right.lower().startswith("c") else (strike - spot), 0.0
    ) * math.exp(-rate * time_years)
    if price < intrinsic - tolerance:
        return None

    low, high = 1e-6, 5.0
    for _ in range(max_iterations):
        mid = 0.5 * (low + high)
        diff = bs_price(spot, strike, time_years, rate, mid, right, dividend) - price
        if abs(diff) < tolerance:
            return mid
        if diff > 0:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


def probability_itm(
    spot: float, strike: float, time_years: float, rate: float, vol: float, right: str = "call"
) -> float:
    """Risk-neutral probability of finishing in the money (N(d2) / N(-d2))."""
    if time_years <= 0 or vol <= 0:
        if right.lower().startswith("c"):
            return 1.0 if spot > strike else 0.0
        return 1.0 if spot < strike else 0.0
    _, d2 = _d1_d2(spot, strike, time_years, rate, vol, 0.0)
    return norm_cdf(d2) if right.lower().startswith("c") else norm_cdf(-d2)


# ---------------------------------------------------------------------------
# Quote helpers
# ---------------------------------------------------------------------------


def mid_price(bid: float | None, ask: float | None) -> float | None:
    """NBBO mid, or the single valid side when only one is quoted."""
    valid_bid = bid if bid and bid > 0 else None
    valid_ask = ask if ask and ask > 0 else None
    if valid_bid and valid_ask:
        return (valid_bid + valid_ask) / 2.0
    return valid_ask or valid_bid


def spread_pct(bid: float | None, ask: float | None) -> float:
    """Relative bid-ask spread. Returns ``inf`` when unquotable."""
    mid = mid_price(bid, ask)
    if not mid or not bid or not ask or bid <= 0 or ask <= 0:
        return float("inf")
    return (ask - bid) / mid


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round a limit price to a valid tick."""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


# ---------------------------------------------------------------------------
# Technical indicators (pure-Python; inputs are plain sequences of floats)
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average series, seeded with the first SMA."""
    if len(values) < period or period <= 0:
        return []
    multiplier = 2.0 / (period + 1.0)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def ema_slope(values: Sequence[float], period: int, lookback: int = 5) -> float:
    """Normalised EMA slope: change per bar divided by level.

    Scale-free, so the same threshold works for a $30 stock and a $600 ETF.
    """
    series = ema_series(values, period)
    if len(series) < lookback + 1 or series[-1] == 0:
        return 0.0
    return (series[-1] - series[-1 - lookback]) / (abs(series[-1]) * lookback)


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> float | None:
    """Wilder's Average True Range."""
    if min(len(highs), len(lows), len(closes)) < period + 1:
        return None
    ranges = [true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, len(closes))]
    if len(ranges) < period:
        return None
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> float | None:
    """Wilder's Average Directional Index — trend *strength*, not direction."""
    n = min(len(highs), len(lows), len(closes))
    if n < 2 * period + 1:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    def wilder(series: Sequence[float]) -> list[float]:
        smoothed = [sum(series[:period])]
        for value in series[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + value)
        return smoothed

    tr_s, plus_s, minus_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)

    dx_values = []
    for tr_v, plus_v, minus_v in zip(tr_s, plus_s, minus_s, strict=False):
        if tr_v == 0:
            continue
        plus_di = 100.0 * plus_v / tr_v
        minus_di = 100.0 * minus_v / tr_v
        total = plus_di + minus_di
        if total:
            dx_values.append(100.0 * abs(plus_di - minus_di) / total)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else None

    value = sum(dx_values[:period]) / period
    for dx_value in dx_values[period:]:
        value = (value * (period - 1) + dx_value) / period
    return value


def bollinger_bandwidth(values: Sequence[float], period: int = 20, num_std: float = 2.0) -> float | None:
    """(upper - lower) / middle. Low bandwidth means compression, i.e. a range."""
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    if mean == 0:
        return None
    variance = sum((v - mean) ** 2 for v in window) / period
    return (2.0 * num_std * math.sqrt(variance)) / abs(mean)


def realised_volatility(closes: Sequence[float], period: int = 20, annualise: bool = True) -> float | None:
    """Close-to-close realised volatility."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    returns = [
        math.log(window[i] / window[i - 1])
        for i in range(1, len(window))
        if window[i] > 0 and window[i - 1] > 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    vol = math.sqrt(variance)
    return vol * math.sqrt(252) if annualise else vol


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """Fraction of ``history`` at or below ``value`` — used for IV rank/percentile."""
    if not history:
        return 0.5
    return sum(1 for h in history if h <= value) / len(history)


def iv_rank(current_iv: float, iv_history: Sequence[float]) -> float:
    """Position of current IV within its historical range, in ``[0, 1]``."""
    if not iv_history:
        return 0.5
    low, high = min(iv_history), max(iv_history)
    if high - low < 1e-9:
        return 0.5
    return clamp((current_iv - low) / (high - low), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Performance statistics
# ---------------------------------------------------------------------------


def max_drawdown(equity_curve: Sequence[float]) -> tuple[float, float]:
    """Return ``(absolute_drawdown, fractional_drawdown)`` — both positive."""
    if not equity_curve:
        return 0.0, 0.0
    peak, worst_abs, worst_pct = equity_curve[0], 0.0, 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > worst_abs:
            worst_abs = drawdown
        if peak > 0 and drawdown / peak > worst_pct:
            worst_pct = drawdown / peak
    return worst_abs, worst_pct


def sharpe_ratio(returns: Sequence[float], risk_free: float = 0.0, periods: int = 252) -> float:
    """Annualised Sharpe ratio from per-period returns."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free / periods for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance)
    return 0.0 if std == 0 else (mean / std) * math.sqrt(periods)


def sortino_ratio(returns: Sequence[float], risk_free: float = 0.0, periods: int = 252) -> float:
    """Sharpe's downside-only cousin — penalises losses, not volatility."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free / periods for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return float("inf") if mean > 0 else 0.0
    deviation = math.sqrt(sum(r * r for r in downside) / len(downside))
    return 0.0 if deviation == 0 else (mean / deviation) * math.sqrt(periods)
