"""Risk limit definitions, reason codes, and the data shapes the guard consumes.

Everything here is plain data. The guard's inputs arrive as untrusted JSON from
an MCP tool call, so every model parses leniently and fails closed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.utils.symbols import parse_occ_symbol, underlying_of


class ReasonCode:
    """Machine-readable verdict reasons. Stable strings — the Coach greps these."""

    APPROVED = "APPROVED"
    RESIZED = "RESIZED"

    # Circuit breakers (reject the entire batch)
    CIRCUIT_DAILY_LOSS = "CIRCUIT_DAILY_LOSS"
    CIRCUIT_DRAWDOWN = "CIRCUIT_DRAWDOWN"

    # Input validity
    INVALID_TRADE = "INVALID_TRADE"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"

    # Eligibility
    UNIVERSE_NOT_WHITELISTED = "UNIVERSE_NOT_WHITELISTED"
    DTE_TOO_SHORT = "DTE_TOO_SHORT"
    DTE_TOO_LONG = "DTE_TOO_LONG"

    # Structure
    UNDEFINED_RISK = "UNDEFINED_RISK"
    NAKED_SHORT_CALL = "NAKED_SHORT_CALL"
    NAKED_SHORT_PUT = "NAKED_SHORT_PUT"
    MAX_LOSS_NOT_DEFINED = "MAX_LOSS_NOT_DEFINED"
    FORBIDDEN_STRUCTURE = "FORBIDDEN_STRUCTURE"

    # Size / notional
    NOTIONAL_PER_TRADE = "NOTIONAL_PER_TRADE"
    NOTIONAL_TOTAL = "NOTIONAL_TOTAL"
    EXPOSURE_PER_TICKER = "EXPOSURE_PER_TICKER"
    CONTRACTS_PER_TRADE = "CONTRACTS_PER_TRADE"
    CONTRACTS_PER_TICKER = "CONTRACTS_PER_TICKER"

    # Greeks
    DELTA_LIMIT = "DELTA_LIMIT"
    GAMMA_LIMIT = "GAMMA_LIMIT"
    VEGA_LIMIT = "VEGA_LIMIT"
    THETA_LIMIT = "THETA_LIMIT"

    # Capital
    CASH_BUFFER = "CASH_BUFFER"
    BUYING_POWER = "BUYING_POWER"

    # Throttles / hygiene
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
    MAX_NEW_TICKERS_PER_DAY = "MAX_NEW_TICKERS_PER_DAY"
    DUPLICATE_TRADE = "DUPLICATE_TRADE"


class Verdict:
    APPROVE = "APPROVE"
    RESIZE = "RESIZE"
    REJECT = "REJECT"


class RiskLimits(BaseModel):
    """Hard constraints. Mirrors ``risk_limits`` in ``config/settings.yaml``."""

    max_notional_per_trade: float = 2500.0
    max_notional_total: float = 20000.0
    max_exposure_per_ticker: float = 5000.0
    max_contracts_per_ticker: int = 10
    max_contracts_per_trade: int = 5

    max_delta_total: float = 250.0
    max_gamma_total: float = 25.0
    max_vega_total: float = 800.0
    max_theta_total: float = 400.0

    min_days_to_expiry: int = 7
    max_days_to_expiry: int = 400
    max_open_positions: int = 12
    max_trades_per_day: int = 6
    max_new_tickers_per_day: int = 3

    allow_undefined_risk: bool = False
    allow_naked_short_calls: bool = False
    require_defined_max_loss: bool = True

    min_cash_buffer_pct: float = 0.30
    max_buying_power_utilisation: float = 0.50
    max_daily_loss_pct: float = 0.03
    max_drawdown_halt_pct: float = 0.10

    universe: list[str] = Field(default_factory=list)
    forbidden_structures: list[str] = Field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Any) -> RiskLimits:
        """Build limits from a validated :class:`~desk.utils.config_loader.Settings`."""
        from desk.utils.config_loader import forbidden_structures

        data = settings.risk_limits.model_dump()
        data["universe"] = settings.universe.all_tickers
        data["max_days_to_expiry"] = max(settings.options.max_days_to_expiry, 400)
        try:
            data["forbidden_structures"] = forbidden_structures()
        except (FileNotFoundError, OSError):
            data["forbidden_structures"] = []
        return cls.model_validate(data)


class TradeLeg(BaseModel):
    """One leg of a multi-leg structure."""

    contract_symbol: str = ""
    side: str = "buy"
    right: str = ""
    strike: float = 0.0
    qty: float = 1.0
    limit_price: float | None = None

    @property
    def is_short(self) -> bool:
        return self.side.lower().startswith("s")

    @property
    def resolved_right(self) -> str:
        if self.right:
            return "call" if self.right.lower().startswith("c") else "put"
        parsed = parse_occ_symbol(self.contract_symbol)
        return parsed.right if parsed else ""

    @property
    def resolved_strike(self) -> float:
        if self.strike:
            return self.strike
        parsed = parse_occ_symbol(self.contract_symbol)
        return parsed.strike if parsed else 0.0


class CandidateTrade(BaseModel):
    """A trade the desk wants to place, as handed to ``risk_guard_check``."""

    trade_id: str
    symbol_or_contract: str
    asset_class: str = "option"
    side: str = "buy"
    qty: float = 1.0

    estimated_notional: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None

    # Optional structure context — supplied by the desk, absent for raw MCP calls.
    underlying: str | None = None
    days_to_expiry: int | None = None
    max_loss: float | None = None
    max_profit: float | None = None
    playbook: str | None = None
    legs: list[TradeLeg] = Field(default_factory=list)
    limit_price: float | None = None

    @property
    def is_option(self) -> bool:
        return self.asset_class.lower() in {"option", "us_option", "options"}

    @property
    def is_short(self) -> bool:
        return self.side.lower().startswith("s")

    @property
    def ticker(self) -> str:
        """Underlying ticker, derived from the contract symbol when not supplied."""
        if self.underlying:
            return self.underlying.upper()
        if self.legs:
            return underlying_of(self.legs[0].contract_symbol)
        return underlying_of(self.symbol_or_contract)

    @property
    def contract_count(self) -> float:
        """Total contracts across every leg (or ``qty`` for a single-leg trade)."""
        if self.legs:
            return sum(abs(leg.qty) for leg in self.legs) * abs(self.qty)
        return abs(self.qty)


class Position(BaseModel):
    """An open position as reported by the broker."""

    symbol: str
    qty: float = 0.0
    asset_class: str = "option"
    market_value: float = 0.0
    cost_basis: float = 0.0
    unrealized_pl: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0

    @property
    def is_option(self) -> bool:
        return self.asset_class.lower() in {"option", "us_option", "options"}

    @property
    def ticker(self) -> str:
        return underlying_of(self.symbol)


class Portfolio(BaseModel):
    """Account state the guard reasons against."""

    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    initial_margin: float = 0.0
    peak_equity: float | None = None
    daily_pnl: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    trades_today: int = 0
    tickers_traded_today: list[str] = Field(default_factory=list)

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions if abs(p.qty) > 0)

    @property
    def daily_pnl_pct(self) -> float:
        base = self.equity - self.daily_pnl
        return self.daily_pnl / base if base > 0 else 0.0

    @property
    def drawdown_pct(self) -> float:
        peak = self.peak_equity or self.equity
        return (peak - self.equity) / peak if peak > 0 else 0.0

    def exposure_for(self, ticker: str) -> float:
        return sum(abs(p.market_value) for p in self.positions if p.ticker == ticker.upper())

    def contracts_for(self, ticker: str) -> float:
        return sum(
            abs(p.qty) for p in self.positions if p.is_option and p.ticker == ticker.upper()
        )

    def total_notional(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)

    def net_greeks(self) -> dict[str, float]:
        return {
            "delta": sum(p.delta for p in self.positions),
            "gamma": sum(p.gamma for p in self.positions),
            "vega": sum(p.vega for p in self.positions),
            "theta": sum(p.theta for p in self.positions),
        }

    def equity_shares(self, ticker: str) -> float:
        """Long shares of ``ticker`` — used to recognise covered calls."""
        return sum(
            p.qty for p in self.positions if not p.is_option and p.ticker == ticker.upper()
        )


class TradeVerdict(BaseModel):
    """The guard's decision on one candidate trade."""

    trade_id: str
    verdict: str = Verdict.REJECT
    approved_qty: float = 0.0
    requested_qty: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict in {Verdict.APPROVE, Verdict.RESIZE} and self.approved_qty > 0


class RiskDecision(BaseModel):
    """The full response of a ``risk_guard_check`` call."""

    verdict: str = Verdict.REJECT
    checked_at: str = ""
    trades: list[TradeVerdict] = Field(default_factory=list)
    circuit_breakers: list[str] = Field(default_factory=list)
    portfolio_before: dict[str, Any] = Field(default_factory=dict)
    portfolio_after: dict[str, Any] = Field(default_factory=dict)
    limits_applied: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""

    @property
    def approved_trades(self) -> list[TradeVerdict]:
        return [t for t in self.trades if t.approved]
