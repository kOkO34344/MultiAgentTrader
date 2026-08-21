"""The Risk Guard — deterministic, non-negotiable pre-trade risk control.

This module contains **no LLM reasoning and no network calls**. It takes the
portfolio, the candidate trades, and the configured limits, and returns a
verdict. Agents may propose anything they like; nothing reaches the broker that
this function did not approve.

Design notes
------------
* **Fail closed.** Malformed input, a missing max loss, an unparseable symbol —
  every ambiguity resolves to ``REJECT``.
* **Resize before reject.** A trade that breaches only a *size* cap is approved
  at the largest quantity that fits. A trade that breaches a *structural*
  constraint is rejected outright; it cannot be sized into compliance.
* **Hedges are always allowed to reduce risk.** A trade that moves a portfolio
  Greek back toward zero passes even while the portfolio is over its limit.
* **Circuit breakers reject the whole batch.** A daily-loss or drawdown breach
  stops all new risk, irrespective of individual trade merit.

Greek convention: ``delta``/``gamma``/``vega``/``theta`` on a candidate trade are
**position-level totals for the full requested quantity**, already multiplied by
the contract multiplier. Resized trades scale them pro-rata.
"""

from __future__ import annotations

import math
from typing import Any

from desk.risk.limits import (
    CandidateTrade,
    Portfolio,
    ReasonCode,
    RiskDecision,
    RiskLimits,
    TradeVerdict,
    Verdict,
)
from desk.utils.logging import get_logger
from desk.utils.symbols import parse_occ_symbol
from desk.utils.time_utils import days_to_expiry, utc_iso

logger = get_logger("risk_guard")

INFINITY = float("inf")


def _floor_qty(value: float) -> int:
    """Floor to a whole contract, never below zero."""
    if value is None or math.isnan(value):
        return 0
    if value == INFINITY:
        return 1_000_000
    return max(0, int(math.floor(value + 1e-9)))


def _max_qty_for_greek(current: float, per_unit: float, limit: float) -> float:
    """Largest quantity keeping ``|current + per_unit * q|`` inside ``limit``.

    Naturally permits hedges: when ``per_unit`` moves the total toward zero, the
    allowance is measured to the *far* boundary, so a portfolio already over its
    limit can still reduce exposure.
    """
    if abs(per_unit) < 1e-12:
        return INFINITY
    if limit <= 0:
        return 0.0
    if per_unit > 0:
        return (limit - current) / per_unit
    return (limit + current) / (-per_unit)


class RiskGuard:
    """Stateless deterministic gate between the Critic and the broker."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    # -- circuit breakers --------------------------------------------------

    def _circuit_breakers(self, portfolio: Portfolio) -> list[str]:
        """Portfolio-wide halts. Any hit rejects every candidate in the batch."""
        breakers: list[str] = []
        if (
            self.limits.max_daily_loss_pct > 0
            and portfolio.daily_pnl_pct <= -abs(self.limits.max_daily_loss_pct)
        ):
            breakers.append(ReasonCode.CIRCUIT_DAILY_LOSS)
        if (
            self.limits.max_drawdown_halt_pct > 0
            and portfolio.drawdown_pct >= abs(self.limits.max_drawdown_halt_pct)
        ):
            breakers.append(ReasonCode.CIRCUIT_DRAWDOWN)
        return breakers

    # -- structure classification -----------------------------------------

    def _classify_structure(
        self, trade: CandidateTrade, portfolio: Portfolio
    ) -> tuple[str, list[tuple[str, str]]]:
        """Return ``(risk_class, blocking_issues)``.

        ``risk_class`` is one of ``defined``, ``covered``, ``undefined``.
        """
        issues: list[tuple[str, str]] = []

        if trade.playbook and trade.playbook in self.limits.forbidden_structures:
            issues.append(
                (ReasonCode.FORBIDDEN_STRUCTURE, f"'{trade.playbook}' is on the forbidden list")
            )
            return "undefined", issues

        # Equities: long is defined risk; short stock is unbounded.
        if not trade.is_option:
            if trade.is_short and not self.limits.allow_undefined_risk:
                issues.append(
                    (ReasonCode.UNDEFINED_RISK, "short equity has unbounded loss")
                )
                return "undefined", issues
            return "defined", issues

        # Multi-leg structures: every short leg must be covered by a long leg
        # of the same right, or the structure is net short and undefined.
        if trade.legs:
            for right in ("call", "put"):
                short_qty = sum(
                    abs(leg.qty) for leg in trade.legs if leg.is_short and leg.resolved_right == right
                )
                long_qty = sum(
                    abs(leg.qty)
                    for leg in trade.legs
                    if not leg.is_short and leg.resolved_right == right
                )
                if short_qty > long_qty + 1e-9:
                    code = (
                        ReasonCode.NAKED_SHORT_CALL if right == "call" else ReasonCode.NAKED_SHORT_PUT
                    )
                    issues.append(
                        (
                            code,
                            f"net short {short_qty:g} {right}(s) against {long_qty:g} long — "
                            "undefined risk",
                        )
                    )
                    return "undefined", issues
            return "defined", issues

        # Single-leg option.
        if not trade.is_short:
            return "defined", issues

        parsed = parse_occ_symbol(trade.symbol_or_contract)
        right = parsed.right if parsed else ""

        if right == "call":
            shares_needed = abs(trade.qty) * 100
            if portfolio.equity_shares(trade.ticker) >= shares_needed:
                return "covered", issues
            if self.limits.allow_naked_short_calls or self.limits.allow_undefined_risk:
                return "undefined", issues
            issues.append(
                (
                    ReasonCode.NAKED_SHORT_CALL,
                    f"short call needs {shares_needed:g} shares of {trade.ticker} to be covered",
                )
            )
            return "undefined", issues

        if self.limits.allow_undefined_risk:
            return "undefined", issues
        issues.append(
            (ReasonCode.NAKED_SHORT_PUT, "single-leg short put is not a defined-risk structure")
        )
        return "undefined", issues

    # -- per-trade static gates -------------------------------------------

    def _static_checks(
        self, trade: CandidateTrade, portfolio: Portfolio, seen_keys: set[str]
    ) -> list[tuple[str, str]]:
        """Checks that cannot be resolved by trading smaller."""
        issues: list[tuple[str, str]] = []

        if trade.qty is None or trade.qty <= 0:
            issues.append((ReasonCode.INVALID_TRADE, "quantity must be positive"))
        if not trade.symbol_or_contract and not trade.legs:
            issues.append((ReasonCode.INVALID_TRADE, "no symbol or legs supplied"))
        if trade.side.lower() not in {"buy", "sell"}:
            issues.append((ReasonCode.INVALID_TRADE, f"unknown side '{trade.side}'"))

        ticker = trade.ticker
        if not ticker:
            issues.append((ReasonCode.UNKNOWN_SYMBOL, "could not derive an underlying ticker"))
        elif self.limits.universe and ticker not in {t.upper() for t in self.limits.universe}:
            issues.append(
                (ReasonCode.UNIVERSE_NOT_WHITELISTED, f"{ticker} is outside the trading universe")
            )

        if trade.is_option:
            dte = self._resolve_dte(trade)
            if dte is None:
                issues.append(
                    (ReasonCode.UNKNOWN_SYMBOL, "could not determine days to expiry")
                )
            else:
                if dte < self.limits.min_days_to_expiry:
                    issues.append(
                        (
                            ReasonCode.DTE_TOO_SHORT,
                            f"{dte}d to expiry is below the {self.limits.min_days_to_expiry}d floor",
                        )
                    )
                if dte > self.limits.max_days_to_expiry:
                    issues.append(
                        (
                            ReasonCode.DTE_TOO_LONG,
                            f"{dte}d to expiry exceeds the {self.limits.max_days_to_expiry}d cap",
                        )
                    )

        risk_class, structure_issues = self._classify_structure(trade, portfolio)
        issues.extend(structure_issues)

        has_short_leg = trade.is_short or any(leg.is_short for leg in trade.legs)
        if (
            self.limits.require_defined_max_loss
            and trade.is_option
            and has_short_leg
            and risk_class != "covered"
        ):
            if trade.max_loss is None or not math.isfinite(trade.max_loss) or trade.max_loss <= 0:
                issues.append(
                    (
                        ReasonCode.MAX_LOSS_NOT_DEFINED,
                        "a structure with short legs must declare a finite max_loss",
                    )
                )

        key = f"{ticker}:{trade.playbook or trade.symbol_or_contract}"
        if key in seen_keys:
            issues.append(
                (ReasonCode.DUPLICATE_TRADE, f"duplicate of an earlier candidate ({key})")
            )

        return issues

    @staticmethod
    def _resolve_dte(trade: CandidateTrade) -> int | None:
        """Days to expiry from the explicit field, the legs, or the OCC symbol."""
        if trade.days_to_expiry is not None:
            return int(trade.days_to_expiry)
        symbols = [leg.contract_symbol for leg in trade.legs if leg.contract_symbol]
        symbols.append(trade.symbol_or_contract)
        expiries = [
            parse_occ_symbol(symbol).expiration
            for symbol in symbols
            if symbol and parse_occ_symbol(symbol)
        ]
        if not expiries:
            return None
        # The nearest expiry is the binding one for a multi-leg structure.
        return days_to_expiry(min(expiries))

    # -- sizing ------------------------------------------------------------

    def _max_allowed_qty(
        self, trade: CandidateTrade, state: dict[str, Any]
    ) -> tuple[int, list[tuple[str, str]]]:
        """Largest quantity that fits every size cap, with the binding reasons."""
        requested = abs(trade.qty)
        binding: list[tuple[str, str]] = []
        allowed = float(requested)

        def bind(cap: float, code: str, message: str) -> None:
            nonlocal allowed
            if cap < allowed - 1e-9:
                allowed = max(cap, 0.0)
                binding.append((code, message))

        limits = self.limits
        ticker = trade.ticker

        # --- notional -----------------------------------------------------
        notional_per_unit = (
            abs(trade.estimated_notional) / requested if trade.estimated_notional else 0.0
        )
        if notional_per_unit > 0:
            bind(
                limits.max_notional_per_trade / notional_per_unit,
                ReasonCode.NOTIONAL_PER_TRADE,
                f"${limits.max_notional_per_trade:,.0f} per-trade notional cap",
            )
            bind(
                (limits.max_notional_total - state["total_notional"]) / notional_per_unit,
                ReasonCode.NOTIONAL_TOTAL,
                f"${limits.max_notional_total:,.0f} portfolio notional cap",
            )
            bind(
                (limits.max_exposure_per_ticker - state["exposure"].get(ticker, 0.0))
                / notional_per_unit,
                ReasonCode.EXPOSURE_PER_TICKER,
                f"${limits.max_exposure_per_ticker:,.0f} exposure cap on {ticker}",
            )

        # --- contract counts ----------------------------------------------
        contracts_per_unit = trade.contract_count / requested if requested else 1.0
        if trade.is_option and contracts_per_unit > 0:
            bind(
                limits.max_contracts_per_trade / contracts_per_unit,
                ReasonCode.CONTRACTS_PER_TRADE,
                f"{limits.max_contracts_per_trade} contracts per trade",
            )
            bind(
                (limits.max_contracts_per_ticker - state["contracts"].get(ticker, 0.0))
                / contracts_per_unit,
                ReasonCode.CONTRACTS_PER_TICKER,
                f"{limits.max_contracts_per_ticker} contracts per ticker on {ticker}",
            )

        # --- portfolio Greeks ----------------------------------------------
        for greek, limit, code in (
            ("delta", limits.max_delta_total, ReasonCode.DELTA_LIMIT),
            ("gamma", limits.max_gamma_total, ReasonCode.GAMMA_LIMIT),
            ("vega", limits.max_vega_total, ReasonCode.VEGA_LIMIT),
            ("theta", limits.max_theta_total, ReasonCode.THETA_LIMIT),
        ):
            value = getattr(trade, greek)
            if value is None:
                continue
            per_unit = value / requested
            bind(
                _max_qty_for_greek(state["greeks"][greek], per_unit, limit),
                code,
                f"portfolio net {greek} limit of {limit:g}",
            )

        # --- capital ---------------------------------------------------------
        capital_per_unit = 0.0
        if trade.max_loss and math.isfinite(trade.max_loss):
            capital_per_unit = abs(trade.max_loss) / requested
        elif trade.estimated_notional:
            capital_per_unit = notional_per_unit

        if capital_per_unit > 0:
            buying_power_budget = (
                state["buying_power"] * limits.max_buying_power_utilisation - state["capital_used"]
            )
            bind(
                buying_power_budget / capital_per_unit,
                ReasonCode.BUYING_POWER,
                f"{limits.max_buying_power_utilisation:.0%} buying-power utilisation cap",
            )
            cash_budget = (
                state["cash"]
                - state["capital_used"]
                - state["equity"] * limits.min_cash_buffer_pct
            )
            bind(
                cash_budget / capital_per_unit,
                ReasonCode.CASH_BUFFER,
                f"{limits.min_cash_buffer_pct:.0%} minimum cash buffer",
            )

        return _floor_qty(allowed), binding

    # -- throttles ---------------------------------------------------------

    def _throttle_checks(
        self, trade: CandidateTrade, state: dict[str, Any]
    ) -> list[tuple[str, str]]:
        limits, issues = self.limits, []
        if state["trades_today"] >= limits.max_trades_per_day:
            issues.append(
                (
                    ReasonCode.MAX_TRADES_PER_DAY,
                    f"{limits.max_trades_per_day} trades already placed today",
                )
            )
        if state["open_positions"] >= limits.max_open_positions:
            issues.append(
                (
                    ReasonCode.MAX_OPEN_POSITIONS,
                    f"{limits.max_open_positions} open positions is the ceiling",
                )
            )
        ticker = trade.ticker
        if ticker not in state["tickers_today"] and len(
            state["tickers_today"]
        ) >= limits.max_new_tickers_per_day:
            issues.append(
                (
                    ReasonCode.MAX_NEW_TICKERS_PER_DAY,
                    f"{limits.max_new_tickers_per_day} new tickers already opened today",
                )
            )
        return issues

    # -- main entry point --------------------------------------------------

    def check(
        self, portfolio: Portfolio, candidates: list[CandidateTrade]
    ) -> RiskDecision:
        """Evaluate ``candidates`` against ``portfolio`` and the configured limits."""
        decision = RiskDecision(
            checked_at=utc_iso(),
            limits_applied=self.limits.model_dump(exclude={"universe", "forbidden_structures"}),
            portfolio_before={
                "equity": portfolio.equity,
                "cash": portfolio.cash,
                "buying_power": portfolio.buying_power,
                "open_positions": portfolio.open_position_count,
                "total_notional": portfolio.total_notional(),
                "net_greeks": portfolio.net_greeks(),
                "daily_pnl_pct": portfolio.daily_pnl_pct,
                "drawdown_pct": portfolio.drawdown_pct,
            },
        )

        breakers = self._circuit_breakers(portfolio)
        if breakers:
            decision.circuit_breakers = breakers
            decision.verdict = Verdict.REJECT
            decision.trades = [
                TradeVerdict(
                    trade_id=trade.trade_id,
                    verdict=Verdict.REJECT,
                    requested_qty=abs(trade.qty),
                    reason_codes=list(breakers),
                    reasons=["circuit breaker tripped — no new risk permitted"],
                )
                for trade in candidates
            ]
            decision.summary = (
                f"HALTED by circuit breaker(s): {', '.join(breakers)}. "
                f"All {len(candidates)} candidate trade(s) rejected."
            )
            decision.portfolio_after = dict(decision.portfolio_before)
            logger.warning(
                "risk_guard_halt",
                extra={"event": "risk_guard_halt", "breakers": breakers, "candidates": len(candidates)},
            )
            return decision

        state: dict[str, Any] = {
            "total_notional": portfolio.total_notional(),
            "exposure": {},
            "contracts": {},
            "greeks": portfolio.net_greeks(),
            "cash": portfolio.cash,
            "equity": portfolio.equity,
            "buying_power": portfolio.buying_power or portfolio.equity,
            "capital_used": portfolio.initial_margin,
            "open_positions": portfolio.open_position_count,
            "trades_today": portfolio.trades_today,
            "tickers_today": {t.upper() for t in portfolio.tickers_traded_today},
        }
        for position in portfolio.positions:
            state["exposure"][position.ticker] = state["exposure"].get(
                position.ticker, 0.0
            ) + abs(position.market_value)
            if position.is_option:
                state["contracts"][position.ticker] = state["contracts"].get(
                    position.ticker, 0.0
                ) + abs(position.qty)

        seen_keys: set[str] = set()

        for trade in candidates:
            requested = abs(trade.qty or 0.0)
            verdict = TradeVerdict(trade_id=trade.trade_id, requested_qty=requested)

            blocking = self._static_checks(trade, portfolio, seen_keys)
            blocking.extend(self._throttle_checks(trade, state))

            if blocking:
                verdict.verdict = Verdict.REJECT
                verdict.approved_qty = 0.0
                verdict.reason_codes = [code for code, _ in blocking]
                verdict.reasons = [message for _, message in blocking]
                decision.trades.append(verdict)
                continue

            allowed, binding = self._max_allowed_qty(trade, state)

            if allowed <= 0:
                verdict.verdict = Verdict.REJECT
                verdict.approved_qty = 0.0
                verdict.reason_codes = [code for code, _ in binding] or [ReasonCode.INVALID_TRADE]
                verdict.reasons = [message for _, message in binding] or [
                    "no quantity fits the configured limits"
                ]
                decision.trades.append(verdict)
                continue

            approved = min(allowed, requested)
            scale = approved / requested if requested else 0.0

            if approved < requested:
                verdict.verdict = Verdict.RESIZE
                verdict.reason_codes = [ReasonCode.RESIZED, *[code for code, _ in binding]]
                verdict.reasons = [
                    f"resized {requested:g} -> {approved:g} contracts",
                    *[message for _, message in binding],
                ]
            else:
                verdict.verdict = Verdict.APPROVE
                verdict.reason_codes = [ReasonCode.APPROVED]
                verdict.reasons = ["passes all risk checks at the requested size"]

            verdict.approved_qty = float(approved)
            decision.trades.append(verdict)

            # Commit the approved trade to the running state so later
            # candidates in the same batch see the capital it consumed.
            ticker = trade.ticker
            notional = abs(trade.estimated_notional or 0.0) * scale
            state["total_notional"] += notional
            state["exposure"][ticker] = state["exposure"].get(ticker, 0.0) + notional
            if trade.is_option:
                state["contracts"][ticker] = (
                    state["contracts"].get(ticker, 0.0) + trade.contract_count * scale
                )
            for greek in ("delta", "gamma", "vega", "theta"):
                value = getattr(trade, greek)
                if value is not None:
                    state["greeks"][greek] += value * scale
            capital = (
                abs(trade.max_loss) * scale
                if trade.max_loss and math.isfinite(trade.max_loss)
                else notional
            )
            state["capital_used"] += capital
            state["open_positions"] += 1
            state["trades_today"] += 1
            state["tickers_today"].add(ticker)
            seen_keys.add(f"{ticker}:{trade.playbook or trade.symbol_or_contract}")

        approved_count = len(decision.approved_trades)
        decision.verdict = Verdict.APPROVE if approved_count else Verdict.REJECT
        decision.portfolio_after = {
            "total_notional": state["total_notional"],
            "net_greeks": state["greeks"],
            "open_positions": state["open_positions"],
            "capital_used": state["capital_used"],
            "trades_today": state["trades_today"],
        }
        resized = sum(1 for t in decision.trades if t.verdict == Verdict.RESIZE)
        rejected = sum(1 for t in decision.trades if t.verdict == Verdict.REJECT)
        decision.summary = (
            f"{approved_count}/{len(candidates)} trade(s) cleared "
            f"({resized} resized, {rejected} rejected)."
        )

        logger.info(
            "risk_guard_check",
            extra={
                "event": "risk_guard_check",
                "verdict": decision.verdict,
                "approved": approved_count,
                "resized": resized,
                "rejected": rejected,
            },
        )
        return decision


# ---------------------------------------------------------------------------
# Functional entry points
# ---------------------------------------------------------------------------


def check(
    current_portfolio: dict[str, Any] | Portfolio,
    candidate_trades: list[dict[str, Any]] | list[CandidateTrade],
    risk_limits: dict[str, Any] | RiskLimits | None = None,
) -> RiskDecision:
    """Validate loose input and run the guard. Never raises on bad input."""
    portfolio = (
        current_portfolio
        if isinstance(current_portfolio, Portfolio)
        else Portfolio.model_validate(current_portfolio or {})
    )

    if isinstance(risk_limits, RiskLimits):
        limits = risk_limits
    elif risk_limits:
        limits = RiskLimits.model_validate(risk_limits)
    else:
        from desk.utils.config_loader import get_settings

        limits = RiskLimits.from_settings(get_settings())

    trades: list[CandidateTrade] = []
    malformed: list[TradeVerdict] = []
    for index, raw in enumerate(candidate_trades or []):
        if isinstance(raw, CandidateTrade):
            trades.append(raw)
            continue
        try:
            trades.append(CandidateTrade.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 - fail closed on any bad shape
            malformed.append(
                TradeVerdict(
                    trade_id=str((raw or {}).get("trade_id", f"malformed-{index}")),
                    verdict=Verdict.REJECT,
                    reason_codes=[ReasonCode.INVALID_TRADE],
                    reasons=[f"could not parse trade: {exc}"],
                )
            )

    decision = RiskGuard(limits).check(portfolio, trades)
    if malformed:
        decision.trades.extend(malformed)
        if not decision.approved_trades:
            decision.verdict = Verdict.REJECT
        decision.summary += f" {len(malformed)} malformed trade(s) rejected."
    return decision


def risk_guard_check(
    current_portfolio: dict[str, Any],
    candidate_trades: list[dict[str, Any]],
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MCP-facing wrapper matching the ``risk_guard_check`` tool schema."""
    return check(current_portfolio, candidate_trades, risk_limits).model_dump()
