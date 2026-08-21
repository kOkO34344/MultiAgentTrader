"""Market clock, session phases, and expiry helpers. All market times are ET."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = UTC

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)

# US market holidays covering the hackathon window and the backtest period.
MARKET_HOLIDAYS: set[date] = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}


def now_et() -> datetime:
    """Current time in US/Eastern."""
    return datetime.now(ET)


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(UTC)


def utc_iso(moment: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp — the canonical format for every stored record."""
    moment = moment or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def today_et() -> date:
    return now_et().date()


def is_trading_day(day: date | None = None) -> bool:
    """Weekday and not a listed holiday."""
    day = day or today_et()
    return day.weekday() < 5 and day not in MARKET_HOLIDAYS


def is_market_open(moment: datetime | None = None) -> bool:
    """Regular-hours check (does not model early closes)."""
    moment = moment or now_et()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ET)
    moment = moment.astimezone(ET)
    if not is_trading_day(moment.date()):
        return False
    return MARKET_OPEN <= moment.time() < MARKET_CLOSE


def session_phase(moment: datetime | None = None) -> str:
    """Map a moment to the desk's cycle phase.

    Returns one of ``premarket``, ``morning``, ``midday``, ``eod``, ``closed``.
    """
    moment = moment or now_et()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ET)
    moment = moment.astimezone(ET)

    if not is_trading_day(moment.date()):
        return "closed"

    clock = moment.time()
    if PREMARKET_OPEN <= clock < MARKET_OPEN:
        return "premarket"
    if MARKET_OPEN <= clock < time(12, 0):
        return "morning"
    if time(12, 0) <= clock < time(15, 30):
        return "midday"
    if time(15, 30) <= clock < MARKET_CLOSE:
        return "eod"
    return "closed"


def next_trading_day(day: date | None = None) -> date:
    """The next weekday that is not a holiday."""
    day = (day or today_et()) + timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def trading_days_between(start: date, end: date) -> list[date]:
    """Inclusive list of trading days in ``[start, end]``."""
    days, cursor = [], start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def days_to_expiry(expiration: date | datetime | str, reference: date | None = None) -> int:
    """Calendar days until expiry. Negative once expired."""
    if isinstance(expiration, str):
        expiration = datetime.fromisoformat(expiration.replace("Z", "+00:00")).date()
    elif isinstance(expiration, datetime):
        expiration = expiration.date()
    return (expiration - (reference or today_et())).days


def trading_days_to_expiry(expiration: date | str, reference: date | None = None) -> int:
    """Trading days until expiry — the right denominator for theta decay."""
    if isinstance(expiration, str):
        expiration = datetime.fromisoformat(expiration.replace("Z", "+00:00")).date()
    reference = reference or today_et()
    if expiration < reference:
        return 0
    return len(trading_days_between(reference, expiration))


def years_to_expiry(expiration: date | str, reference: date | None = None) -> float:
    """Time to expiry in years, floored just above zero for Black-Scholes."""
    return max(days_to_expiry(expiration, reference), 0) / 365.0 or 1e-6


def nearest_friday(target_dte: int, reference: date | None = None) -> date:
    """The Friday closest to ``target_dte`` days out — standard option expiry."""
    reference = reference or today_et()
    target = reference + timedelta(days=target_dte)
    offset = (4 - target.weekday()) % 7
    friday = target + timedelta(days=offset)
    if offset > 3:  # the previous Friday is closer to the target
        friday -= timedelta(days=7)
    return friday


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string, tolerating a trailing ``Z``."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seconds_since(timestamp: str | datetime) -> float:
    """Seconds elapsed since ``timestamp`` (used for heartbeat staleness)."""
    moment = parse_iso(timestamp) if isinstance(timestamp, str) else timestamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now_utc() - moment).total_seconds()
