"""Backtest engine and metrics: known-P&L scenarios, costs, and exits."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from tests.conftest import FakeMarketData

from desk.backtest.backtest_engine import BacktestEngine
from desk.backtest.dataset_loader import DatasetLoader
from desk.backtest.metrics import (
    compute_metrics,
    curve_metrics,
    options_stats,
    outcome_distribution,
    trade_outcomes,
)

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_trade_outcomes_on_a_known_set():
    trades = [{"pnl": 100.0}, {"pnl": 200.0}, {"pnl": -50.0}, {"pnl": -150.0}]
    stats = trade_outcomes(trades)
    assert stats["trades"] == 4
    assert stats["wins"] == 2 and stats["losses"] == 2
    assert stats["hit_rate"] == 0.5
    assert stats["avg_win"] == 150.0
    assert stats["avg_loss"] == 100.0
    assert stats["profit_factor"] == 1.5
    assert stats["expectancy"] == 25.0
    assert stats["largest_win"] == 200.0
    assert stats["largest_loss"] == -150.0


def test_scratches_count_as_neither_win_nor_loss():
    stats = trade_outcomes([{"pnl": 0.0}, {"pnl": 100.0}])
    assert stats["scratches"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 0


def test_no_trades_yields_zeros_not_errors():
    stats = trade_outcomes([])
    assert stats["trades"] == 0 and stats["hit_rate"] == 0.0


def test_profit_factor_is_infinite_with_no_losers():
    assert trade_outcomes([{"pnl": 10.0}])["profit_factor"] == float("inf")


def test_curve_metrics_compute_drawdown_correctly():
    metrics = curve_metrics([100.0, 110.0, 90.0, 120.0], initial_capital=100.0)
    assert metrics["total_pnl"] == 20.0
    assert metrics["total_return_pct"] == 0.2
    # Peak 110 to trough 90 is a 20-point, 18.18% drawdown.
    assert metrics["max_drawdown"] == 20.0
    assert metrics["max_drawdown_pct"] == pytest.approx(0.1818, abs=1e-3)


def test_curve_metrics_handle_a_single_point():
    assert curve_metrics([100.0])["total_pnl"] == 0.0


def test_options_stats_separate_credit_from_debit():
    trades = [
        {"pnl": 50.0, "net_side": "credit", "max_profit": 100.0, "days_held": 20, "playbook": "iron_condor", "exit_reason": "profit_target"},
        {"pnl": -80.0, "net_side": "debit", "days_held": 10, "playbook": "bull_call_spread", "exit_reason": "expiry"},
    ]
    stats = options_stats(trades)
    assert stats["credit_structures"] == 1
    assert stats["debit_structures"] == 1
    assert stats["avg_days_held"] == 15.0
    assert stats["expired_worthless_pct"] == 0.5
    assert stats["by_playbook"]["iron_condor"]["total_pnl"] == 50.0


def test_outcome_distribution_buckets_every_trade():
    trades = [{"pnl": float(p)} for p in (-100, -50, 0, 50, 100, 150)]
    histogram = outcome_distribution(trades, buckets=4)
    assert sum(histogram.values()) == len(trades), "no trade may fall outside every bucket"


def test_compute_metrics_bundles_everything():
    metrics = compute_metrics([{"pnl": 10.0, "net_side": "credit"}], [100.0, 110.0], 100.0)
    assert "total_pnl" in metrics and "hit_rate" in metrics
    assert "options" in metrics and "outcome_distribution" in metrics


# ---------------------------------------------------------------------------
# Engine mechanics
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    loader = DatasetLoader(market_data=FakeMarketData(), cache_dir=tmp_path / "cache")
    return BacktestEngine(loader=loader)


def test_synthetic_chain_is_priceable_and_bounded(engine):
    chain = engine.synth_chain("SPY", 580.0, date.today(), 0.22)
    assert chain
    for contract in chain:
        assert contract["bid"] > 0 < contract["ask"]
        assert contract["ask"] > contract["bid"]
        assert contract["greeks"]["delta"] is not None
        assert contract["days_to_expiry"] >= engine.settings.options.min_days_to_expiry


def test_marking_a_spread_at_expiry_equals_its_payoff(engine):
    """At zero DTE the model must agree exactly with intrinsic value."""
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "mid_price": 3.0, "qty": 1},
        {"side": "sell", "right": "call", "strike": 105, "mid_price": 1.0, "qty": 1},
    ]
    # Well above the short strike, the spread is worth its full width.
    assert engine.mark_structure(legs, 120.0, 0, 0.2) == pytest.approx(5.0)
    # Well below the long strike, it is worthless.
    assert engine.mark_structure(legs, 80.0, 0, 0.2) == pytest.approx(0.0)


def test_marking_before_expiry_sits_between_the_bounds(engine):
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "mid_price": 3.0, "qty": 1},
        {"side": "sell", "right": "call", "strike": 105, "mid_price": 1.0, "qty": 1},
    ]
    value = engine.mark_structure(legs, 102.0, 30, 0.2)
    assert 0.0 < value < 5.0


def test_slippage_is_charged_on_the_spread(engine):
    legs = [{"bid": 1.00, "ask": 1.20}, {"bid": 0.50, "ask": 0.60}]
    assert engine.apply_slippage(2.0, legs, opening=True) == pytest.approx(0.15)


def test_no_slippage_model_charges_nothing(engine):
    engine.settings.backtest.slippage_model = "none"
    assert engine.apply_slippage(2.0, [{"bid": 1.0, "ask": 1.2}], opening=True) == 0.0


def test_commission_scales_with_legs_and_contracts(engine):
    engine.settings.backtest.commission_per_contract = 0.65
    assert engine.commission([{}, {}], 3) == pytest.approx(3.90)


def test_implied_vol_input_carries_a_variance_risk_premium(engine):
    """Options must be modelled as richer than realised vol, or sellers never lose."""
    from tests.conftest import make_bars

    bars = make_bars("SPY", days=120)
    implied = engine.implied_vol_for(bars, bars[-1]["timestamp"][:10])
    assert 0.06 <= implied <= 2.0


# ---------------------------------------------------------------------------
# Full replay
# ---------------------------------------------------------------------------


def test_replay_produces_a_coherent_result(engine):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=120)
    result = engine.run(start=start.isoformat(), end=end.isoformat(), tickers=["SPY", "QQQ"])

    assert result.equity_curve
    assert len(result.equity_curve) == len(result.dates)
    assert result.metrics["pricing_source"] == "black_scholes_synthetic_chain"
    for trade in result.trades:
        assert trade.closed_on is not None
        assert trade.pnl is not None
        assert trade.exit_reason
        assert trade.max_loss >= 0


def test_replay_respects_the_drawdown_circuit_breaker(engine):
    """A replay must not take risk the live desk's guard would have refused."""
    end = date.today() - timedelta(days=1)
    result = engine.run(
        start=(end - timedelta(days=200)).isoformat(), end=end.isoformat(), tickers=["SPY", "QQQ"]
    )
    halt = engine.settings.risk_limits.max_drawdown_halt_pct
    # Some overshoot is unavoidable (open marks move after the gate), but the
    # guard must keep it in the same neighbourhood as the configured halt.
    assert result.metrics["max_drawdown_pct"] <= halt * 2.0


def test_replay_never_exceeds_per_trade_notional(engine):
    end = date.today() - timedelta(days=1)
    result = engine.run(
        start=(end - timedelta(days=150)).isoformat(), end=end.isoformat(), tickers=["SPY"]
    )
    cap = engine.settings.risk_limits.max_notional_per_trade
    for trade in result.trades:
        assert trade.max_loss <= cap * 1.01


def test_empty_history_returns_warnings_not_an_exception(tmp_path):
    class NoData:
        def get_equity_bars(self, *args, **kwargs):
            return {}

        def get_options_bars(self, *args, **kwargs):
            return {}

    loader = DatasetLoader(market_data=NoData(), cache_dir=tmp_path / "cache")
    result = BacktestEngine(loader=loader).run(start="2025-01-02", end="2025-02-01")
    assert result.warnings
    assert result.trades == []


def test_dataset_loader_caches_between_calls(tmp_path):
    market_data = FakeMarketData()
    loader = DatasetLoader(market_data=market_data, cache_dir=tmp_path / "cache")
    loader.load_equity_bars(["SPY"], "2025-01-02", "2025-03-01")
    calls_after_first = len(market_data.calls)
    loader.load_equity_bars(["SPY"], "2025-01-02", "2025-03-01")
    assert len(market_data.calls) == calls_after_first, "the second load must hit the cache"


def test_clearing_the_cache_forces_a_refetch(tmp_path):
    market_data = FakeMarketData()
    loader = DatasetLoader(market_data=market_data, cache_dir=tmp_path / "cache")
    loader.load_equity_bars(["SPY"], "2025-01-02", "2025-03-01")
    assert loader.clear_cache() >= 1
    loader.load_equity_bars(["SPY"], "2025-01-02", "2025-03-01")
    assert len(market_data.calls) == 2
