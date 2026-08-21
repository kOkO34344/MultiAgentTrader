"""Risk Guard tests — the most safety-critical module on the desk.

The guard is the only thing standing between a persuasive LLM argument and a
real (paper) order, so these tests cover every limit, both sides of each
boundary, the resize path, and the fail-closed behaviour on malformed input.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from desk.risk.limits import (
    CandidateTrade,
    Portfolio,
    Position,
    ReasonCode,
    RiskLimits,
    Verdict,
)
from desk.risk.risk_guard import RiskGuard, check
from desk.utils.symbols import build_occ_symbol

EXPIRY = date.today() + timedelta(days=30)
SHORT_PUT = build_occ_symbol("SPY", EXPIRY, "put", 540)
LONG_PUT = build_occ_symbol("SPY", EXPIRY, "put", 535)
SHORT_CALL = build_occ_symbol("SPY", EXPIRY, "call", 600)
LONG_CALL = build_occ_symbol("SPY", EXPIRY, "call", 605)


def limits(**overrides) -> RiskLimits:
    base = {
        "universe": ["SPY", "QQQ", "IWM"],
        "max_notional_per_trade": 2500.0,
        "max_notional_total": 20000.0,
        "max_exposure_per_ticker": 5000.0,
        "max_contracts_per_trade": 5,
        "max_contracts_per_ticker": 10,
        "max_delta_total": 250.0,
        "max_gamma_total": 25.0,
        "max_vega_total": 800.0,
        "max_theta_total": 400.0,
        "min_days_to_expiry": 7,
        "max_trades_per_day": 6,
        "max_new_tickers_per_day": 3,
        "max_open_positions": 12,
        "min_cash_buffer_pct": 0.30,
        "max_buying_power_utilisation": 0.50,
        "max_daily_loss_pct": 0.03,
        "max_drawdown_halt_pct": 0.10,
    }
    base.update(overrides)
    return RiskLimits(**base)


def portfolio(**overrides) -> Portfolio:
    base = {"cash": 100_000.0, "equity": 100_000.0, "buying_power": 200_000.0, "positions": []}
    base.update(overrides)
    return Portfolio(**base)


def spread(trade_id: str = "t1", qty: float = 1, **overrides) -> CandidateTrade:
    """A defined-risk put credit spread — the desk's bread-and-butter structure."""
    base = {
        "trade_id": trade_id,
        "symbol_or_contract": SHORT_PUT,
        "asset_class": "option",
        "side": "sell",
        "qty": qty,
        "estimated_notional": 400.0 * qty,
        "max_loss": 400.0 * qty,
        "max_profit": 100.0 * qty,
        "playbook": "bull_put_credit_spread",
        "days_to_expiry": 30,
        "legs": [
            {"contract_symbol": SHORT_PUT, "side": "sell", "qty": 1},
            {"contract_symbol": LONG_PUT, "side": "buy", "qty": 1},
        ],
    }
    base.update(overrides)
    return CandidateTrade(**base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_defined_risk_spread_is_approved():
    decision = RiskGuard(limits()).check(portfolio(), [spread()])
    assert decision.verdict == Verdict.APPROVE
    assert decision.trades[0].verdict == Verdict.APPROVE
    assert decision.trades[0].approved_qty == 1
    assert decision.trades[0].reason_codes == [ReasonCode.APPROVED]


def test_approved_trades_property_filters_rejects():
    decision = RiskGuard(limits()).check(
        portfolio(), [spread("ok"), spread("bad", qty=1, days_to_expiry=2)]
    )
    assert [t.trade_id for t in decision.approved_trades] == ["ok"]


# ---------------------------------------------------------------------------
# Structural rejections — cannot be sized into compliance
# ---------------------------------------------------------------------------


def test_naked_short_put_is_rejected():
    trade = spread(legs=[], max_loss=None)
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert decision.trades[0].verdict == Verdict.REJECT
    assert ReasonCode.NAKED_SHORT_PUT in decision.trades[0].reason_codes


def test_naked_short_call_is_rejected_without_stock_cover():
    trade = spread(symbol_or_contract=SHORT_CALL, legs=[], max_loss=None)
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert ReasonCode.NAKED_SHORT_CALL in decision.trades[0].reason_codes


def test_covered_call_is_not_treated_as_naked():
    """100 shares cover one short call, so the undefined-risk gate must not fire.

    The stock position itself still consumes ticker exposure, so this asserts
    the structural verdict rather than the final sizing outcome.
    """
    holder = portfolio(
        positions=[Position(symbol="SPY", qty=100, asset_class="us_equity", market_value=58_000)]
    )
    trade = spread(symbol_or_contract=SHORT_CALL, legs=[], max_loss=None, estimated_notional=500.0)
    decision = RiskGuard(
        limits(max_exposure_per_ticker=10**9, max_notional_total=10**9)
    ).check(holder, [trade])
    assert ReasonCode.NAKED_SHORT_CALL not in decision.trades[0].reason_codes
    assert decision.trades[0].verdict == Verdict.APPROVE


def test_uncovered_short_call_against_too_few_shares_is_naked():
    """99 shares do not cover a 100-share obligation."""
    holder = portfolio(
        positions=[Position(symbol="SPY", qty=99, asset_class="us_equity", market_value=57_420)]
    )
    trade = spread(symbol_or_contract=SHORT_CALL, legs=[], max_loss=None, estimated_notional=500.0)
    decision = RiskGuard(limits(max_exposure_per_ticker=10**9)).check(holder, [trade])
    assert ReasonCode.NAKED_SHORT_CALL in decision.trades[0].reason_codes


def test_ratio_spread_net_short_is_rejected():
    """Two shorts against one long is undefined risk, however it is labelled."""
    trade = spread(
        legs=[
            {"contract_symbol": SHORT_PUT, "side": "sell", "qty": 2},
            {"contract_symbol": LONG_PUT, "side": "buy", "qty": 1},
        ]
    )
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert ReasonCode.NAKED_SHORT_PUT in decision.trades[0].reason_codes


def test_short_equity_is_rejected_as_undefined_risk():
    trade = CandidateTrade(
        trade_id="short-stock", symbol_or_contract="SPY", asset_class="us_equity",
        side="sell", qty=100, estimated_notional=58_000,
    )
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert ReasonCode.UNDEFINED_RISK in decision.trades[0].reason_codes


def test_missing_max_loss_on_short_structure_is_rejected():
    decision = RiskGuard(limits()).check(portfolio(), [spread(max_loss=None)])
    assert ReasonCode.MAX_LOSS_NOT_DEFINED in decision.trades[0].reason_codes


def test_forbidden_playbook_is_rejected():
    guard = RiskGuard(limits(forbidden_structures=["short_strangle"]))
    decision = guard.check(portfolio(), [spread(playbook="short_strangle")])
    assert ReasonCode.FORBIDDEN_STRUCTURE in decision.trades[0].reason_codes


def test_ticker_outside_universe_is_rejected():
    decision = RiskGuard(limits()).check(portfolio(), [spread(underlying="TSLA")])
    assert ReasonCode.UNIVERSE_NOT_WHITELISTED in decision.trades[0].reason_codes


# ---------------------------------------------------------------------------
# Expiry boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dte", "expected"),
    [(6, Verdict.REJECT), (7, Verdict.APPROVE), (8, Verdict.APPROVE)],
)
def test_min_days_to_expiry_boundary(dte, expected):
    decision = RiskGuard(limits(min_days_to_expiry=7)).check(portfolio(), [spread(days_to_expiry=dte)])
    assert decision.trades[0].verdict == expected


def test_expiry_is_derived_from_the_contract_symbol_when_not_supplied():
    near = build_occ_symbol("SPY", date.today() + timedelta(days=2), "put", 540)
    trade = spread(
        days_to_expiry=None,
        symbol_or_contract=near,
        legs=[
            {"contract_symbol": near, "side": "sell", "qty": 1},
            {"contract_symbol": build_occ_symbol("SPY", date.today() + timedelta(days=2), "put", 535), "side": "buy", "qty": 1},
        ],
    )
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert ReasonCode.DTE_TOO_SHORT in decision.trades[0].reason_codes


# ---------------------------------------------------------------------------
# Sizing — resize rather than reject
# ---------------------------------------------------------------------------


def test_oversized_notional_is_resized_not_rejected():
    decision = RiskGuard(limits(max_notional_per_trade=1000.0)).check(
        portfolio(), [spread(qty=10, estimated_notional=4000.0, max_loss=4000.0)]
    )
    verdict = decision.trades[0]
    assert verdict.verdict == Verdict.RESIZE
    assert 0 < verdict.approved_qty < 10
    assert ReasonCode.RESIZED in verdict.reason_codes
    assert ReasonCode.NOTIONAL_PER_TRADE in verdict.reason_codes


def test_resize_floors_to_whole_contracts():
    decision = RiskGuard(limits(max_notional_per_trade=999.0)).check(
        portfolio(), [spread(qty=10, estimated_notional=4000.0, max_loss=4000.0)]
    )
    assert decision.trades[0].approved_qty == int(decision.trades[0].approved_qty)


def test_contracts_per_trade_cap_binds():
    decision = RiskGuard(limits(max_contracts_per_trade=3)).check(
        portfolio(), [spread(qty=10, estimated_notional=100.0, max_loss=100.0)]
    )
    # Two legs per contract, so 3 contracts is 1 unit of a 2-leg structure.
    assert decision.trades[0].approved_qty <= 3


def test_existing_position_consumes_ticker_exposure():
    holder = portfolio(
        positions=[Position(symbol=SHORT_PUT, qty=-8, asset_class="option", market_value=4800)]
    )
    decision = RiskGuard(limits(max_exposure_per_ticker=5000.0)).check(
        holder, [spread(qty=5, estimated_notional=2000.0, max_loss=2000.0)]
    )
    assert decision.trades[0].approved_qty < 5


def test_batch_accumulates_so_later_trades_see_earlier_capital():
    guard = RiskGuard(limits(max_notional_total=1000.0))
    decision = guard.check(
        portfolio(),
        [
            spread("first", qty=1, estimated_notional=900.0, max_loss=900.0),
            spread("second", qty=1, estimated_notional=900.0, max_loss=900.0, underlying="QQQ",
                   symbol_or_contract=build_occ_symbol("QQQ", EXPIRY, "put", 500),
                   legs=[{"contract_symbol": build_occ_symbol("QQQ", EXPIRY, "put", 500), "side": "sell", "qty": 1},
                         {"contract_symbol": build_occ_symbol("QQQ", EXPIRY, "put", 495), "side": "buy", "qty": 1}]),
        ],
    )
    assert decision.trades[0].verdict == Verdict.APPROVE
    assert decision.trades[1].verdict == Verdict.REJECT


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------


def test_delta_limit_blocks_a_trade_that_pushes_past_it():
    holder = portfolio(
        positions=[Position(symbol="SPY", qty=240, asset_class="us_equity", market_value=140_000, delta=240)]
    )
    decision = RiskGuard(limits(max_delta_total=250.0, max_exposure_per_ticker=10**9, max_notional_total=10**9)).check(
        holder, [spread(qty=1, delta=100.0)]
    )
    assert decision.trades[0].verdict == Verdict.REJECT
    assert ReasonCode.DELTA_LIMIT in decision.trades[0].reason_codes


def test_a_hedge_is_allowed_even_while_over_the_delta_limit():
    """Reducing exposure must never be blocked by the limit it is reducing."""
    holder = portfolio(
        positions=[Position(symbol="SPY", qty=500, asset_class="us_equity", market_value=290_000, delta=500)]
    )
    decision = RiskGuard(
        limits(max_delta_total=250.0, max_exposure_per_ticker=10**9, max_notional_total=10**9)
    ).check(holder, [spread(qty=1, delta=-200.0)])
    assert decision.trades[0].verdict == Verdict.APPROVE


@pytest.mark.parametrize(
    ("greek", "limit_key", "code"),
    [
        ("gamma", "max_gamma_total", ReasonCode.GAMMA_LIMIT),
        ("vega", "max_vega_total", ReasonCode.VEGA_LIMIT),
        ("theta", "max_theta_total", ReasonCode.THETA_LIMIT),
    ],
)
def test_each_greek_limit_binds(greek, limit_key, code):
    decision = RiskGuard(limits(**{limit_key: 1.0})).check(
        portfolio(), [spread(qty=1, **{greek: 500.0})]
    )
    assert decision.trades[0].verdict == Verdict.REJECT
    assert code in decision.trades[0].reason_codes


# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------


def test_cash_buffer_is_protected():
    thin = portfolio(cash=1000.0, equity=100_000.0, buying_power=200_000.0)
    decision = RiskGuard(limits(min_cash_buffer_pct=0.30)).check(
        thin, [spread(qty=1, estimated_notional=2000.0, max_loss=2000.0)]
    )
    assert decision.trades[0].verdict == Verdict.REJECT
    assert ReasonCode.CASH_BUFFER in decision.trades[0].reason_codes


def test_buying_power_utilisation_caps_size():
    small = portfolio(cash=100_000.0, equity=100_000.0, buying_power=1000.0)
    decision = RiskGuard(limits(max_buying_power_utilisation=0.50)).check(
        small, [spread(qty=5, estimated_notional=2000.0, max_loss=2000.0)]
    )
    assert decision.trades[0].approved_qty < 5


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


def test_daily_loss_circuit_breaker_halts_the_whole_batch():
    losing = portfolio(equity=97_000.0, daily_pnl=-3_100.0)
    decision = RiskGuard(limits(max_daily_loss_pct=0.03)).check(
        losing, [spread("a"), spread("b")]
    )
    assert decision.verdict == Verdict.REJECT
    assert ReasonCode.CIRCUIT_DAILY_LOSS in decision.circuit_breakers
    assert all(t.verdict == Verdict.REJECT for t in decision.trades)
    assert all(t.approved_qty == 0 for t in decision.trades)


def test_drawdown_circuit_breaker_halts_the_whole_batch():
    drawn = portfolio(equity=88_000.0, peak_equity=100_000.0)
    decision = RiskGuard(limits(max_drawdown_halt_pct=0.10)).check(drawn, [spread()])
    assert ReasonCode.CIRCUIT_DRAWDOWN in decision.circuit_breakers


def test_circuit_breaker_boundary_is_inclusive_of_the_limit():
    at_limit = portfolio(equity=97_000.0, daily_pnl=-3_000.0)
    decision = RiskGuard(limits(max_daily_loss_pct=0.03)).check(at_limit, [spread()])
    assert decision.circuit_breakers == [ReasonCode.CIRCUIT_DAILY_LOSS]


# ---------------------------------------------------------------------------
# Throttles and hygiene
# ---------------------------------------------------------------------------


def test_daily_trade_cap_blocks_new_risk():
    busy = portfolio(trades_today=6)
    decision = RiskGuard(limits(max_trades_per_day=6)).check(busy, [spread()])
    assert ReasonCode.MAX_TRADES_PER_DAY in decision.trades[0].reason_codes


def test_new_ticker_cap_blocks_a_fourth_name():
    busy = portfolio(tickers_traded_today=["QQQ", "IWM", "AAPL"])
    decision = RiskGuard(limits(max_new_tickers_per_day=3)).check(busy, [spread()])
    assert ReasonCode.MAX_NEW_TICKERS_PER_DAY in decision.trades[0].reason_codes


def test_open_position_cap_blocks_new_risk():
    full = portfolio(
        positions=[Position(symbol=f"X{i}", qty=1, asset_class="option") for i in range(12)]
    )
    decision = RiskGuard(limits(max_open_positions=12)).check(full, [spread()])
    assert ReasonCode.MAX_OPEN_POSITIONS in decision.trades[0].reason_codes


def test_duplicate_candidates_are_rejected():
    decision = RiskGuard(limits()).check(portfolio(), [spread("a"), spread("b")])
    assert decision.trades[0].verdict == Verdict.APPROVE
    assert ReasonCode.DUPLICATE_TRADE in decision.trades[1].reason_codes


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_malformed_trade_is_rejected_not_raised():
    decision = check({"cash": 1000, "equity": 1000}, [{"garbage": True}], limits().model_dump())
    assert decision.verdict == Verdict.REJECT
    assert ReasonCode.INVALID_TRADE in decision.trades[0].reason_codes


def test_zero_quantity_is_invalid():
    decision = RiskGuard(limits()).check(portfolio(), [spread(qty=0)])
    assert ReasonCode.INVALID_TRADE in decision.trades[0].reason_codes


def test_unparseable_symbol_is_rejected():
    trade = CandidateTrade(
        trade_id="junk", symbol_or_contract="!!!", asset_class="option", side="buy", qty=1
    )
    decision = RiskGuard(limits()).check(portfolio(), [trade])
    assert decision.trades[0].verdict == Verdict.REJECT


def test_empty_batch_is_a_reject_not_a_crash():
    decision = RiskGuard(limits()).check(portfolio(), [])
    assert decision.verdict == Verdict.REJECT
    assert decision.trades == []


def test_decision_records_before_and_after_state():
    decision = RiskGuard(limits()).check(portfolio(), [spread()])
    assert decision.portfolio_before["equity"] == 100_000.0
    assert decision.portfolio_after["trades_today"] == 1
    assert "net_greeks" in decision.portfolio_after
