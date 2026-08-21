"""OCC option symbol parsing and construction.

OCC format: ``ROOT`` + ``YYMMDD`` + ``C|P`` + 8-digit strike (in thousandths).
Example: ``SPY260918P00540000`` -> SPY, 2026-09-18, put, strike 540.00.

The Risk Guard needs to derive an underlying ticker and an expiry from a
contract symbol without reaching for the broker, so this lives in ``utils``
with no dependency on the Alpaca layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

OCC_PATTERN = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class OptionContract:
    """A parsed OCC contract symbol."""

    symbol: str
    underlying: str
    expiration: date
    right: str  # "call" | "put"
    strike: float

    @property
    def is_call(self) -> bool:
        return self.right == "call"

    @property
    def is_put(self) -> bool:
        return self.right == "put"


def parse_occ_symbol(symbol: str) -> OptionContract | None:
    """Parse an OCC symbol, or return ``None`` if it is not one."""
    match = OCC_PATTERN.match((symbol or "").strip().upper())
    if not match:
        return None
    parts = match.groupdict()
    try:
        expiration = date(2000 + int(parts["yy"]), int(parts["mm"]), int(parts["dd"]))
    except ValueError:
        return None
    return OptionContract(
        symbol=symbol.strip().upper(),
        underlying=parts["root"],
        expiration=expiration,
        right="call" if parts["right"] == "C" else "put",
        strike=int(parts["strike"]) / 1000.0,
    )


def build_occ_symbol(underlying: str, expiration: date, right: str, strike: float) -> str:
    """Build an OCC symbol from its components."""
    letter = "C" if right.lower().startswith("c") else "P"
    return (
        f"{underlying.upper()}"
        f"{expiration.strftime('%y%m%d')}"
        f"{letter}"
        f"{int(round(strike * 1000)):08d}"
    )


def is_option_symbol(symbol: str) -> bool:
    """True when ``symbol`` is a valid OCC contract symbol."""
    return parse_occ_symbol(symbol) is not None


def underlying_of(symbol: str) -> str:
    """Underlying ticker for an option symbol; the symbol itself for equities."""
    parsed = parse_occ_symbol(symbol)
    return parsed.underlying if parsed else (symbol or "").strip().upper()
