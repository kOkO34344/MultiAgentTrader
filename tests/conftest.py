"""Shared fixtures.

Everything here runs **offline**: no Alpaca credentials, no Anthropic key. The
fakes below stand in for the broker so the full chain — snapshot, research,
committee, risk gate, execution, journalling — is exercised in CI without a
network call. That is only possible because every LLM agent falls back to
deterministic mock reasoning when no key is present.
"""

from __future__ import annotations

import math
import os
import random
from datetime import date, timedelta
from typing import Any

import pytest

# Force offline mode before any desk module reads the environment.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_SECRET_KEY"] = ""
os.environ["DESK_DRY_RUN"] = "true"

from desk.utils.config_loader import load_settings  # noqa: E402
from desk.utils.math_utils import bs_greeks, bs_price  # noqa: E402
from desk.utils.symbols import build_occ_symbol  # noqa: E402
from desk.utils.time_utils import nearest_friday  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_repo_state(tmp_path, monkeypatch):
    """Keep every test out of tracked repo files.

    `run_journal` logs a live run to the experiment registry, and the registry
    writes to `config/experiments/experiments.json`. Without this guard a test
    run silently commits fabricated results into version control.
    """
    import desk.experiments.registry as registry_module
    from desk.experiments.registry import ExperimentRegistry

    isolated = ExperimentRegistry(tmp_path / "experiments.json")
    monkeypatch.setattr(registry_module, "_REGISTRY", isolated)
    monkeypatch.setattr(registry_module, "get_registry", lambda: isolated)
    monkeypatch.setattr(
        "desk.orchestrator.orchestrator_agent.get_registry", lambda: isolated
    )
    monkeypatch.setattr(
        "desk.monitor.heartbeat.heartbeat_path", lambda: tmp_path / "heartbeat.json"
    )
    yield isolated


@pytest.fixture
def settings():
    """Fresh settings, independent of the process-wide cache."""
    return load_settings()


@pytest.fixture
def store(tmp_path):
    """A state store backed by a temporary database file."""
    from desk.monitor.state_store import StateStore

    return StateStore(tmp_path / "state.db")


def make_bars(
    symbol: str,
    days: int = 200,
    start_price: float = 400.0,
    drift: float = 0.0004,
    vol: float = 0.011,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """Deterministic OHLCV series (seeded by symbol, so tests are reproducible)."""
    random.seed(abs(hash(symbol)) % 10_000)
    end = end or date.today()
    price = start_price
    bars: list[dict[str, Any]] = []
    day = end - timedelta(days=int(days * 1.5))

    while day <= end:
        if day.weekday() < 5:
            price *= math.exp(random.gauss(drift, vol))
            high = price * (1 + abs(random.gauss(0, 0.004)))
            low = price * (1 - abs(random.gauss(0, 0.004)))
            bars.append(
                {
                    "timestamp": f"{day.isoformat()}T20:00:00+00:00",
                    "open": price,
                    "high": high,
                    "low": low,
                    "close": price,
                    "volume": 1_000_000.0,
                    "vwap": price,
                    "trade_count": 5000,
                }
            )
        day += timedelta(days=1)
    return bars[-days:]


def make_trending_bars(symbol: str, days: int = 200, up: bool = True) -> list[dict[str, Any]]:
    """A clean, unambiguous trend — used to pin regime classification."""
    step = 0.6 if up else -0.6
    price, bars = 400.0, []
    day = date.today() - timedelta(days=int(days * 1.5))
    while len(bars) < days:
        if day.weekday() < 5:
            price += step
            bars.append(
                {
                    "timestamp": f"{day.isoformat()}T20:00:00+00:00",
                    "open": price, "high": price + 1.2, "low": price - 1.2,
                    "close": price, "volume": 1_000_000.0, "vwap": price, "trade_count": 5000,
                }
            )
        day += timedelta(days=1)
    return bars


def make_chain(
    underlying: str, spot: float, implied_vol: float = 0.22, dtes: tuple[int, ...] = (14, 30, 45)
) -> list[dict[str, Any]]:
    """A Black-Scholes-priced chain, shaped exactly like `MarketData` returns."""
    rate = 0.043
    contracts: list[dict[str, Any]] = []
    today = date.today()

    for target in dtes:
        expiry = nearest_friday(target, today)
        dte = (expiry - today).days
        if dte <= 0:
            continue
        years = dte / 365.0
        for step in range(-20, 21):
            strike = round(spot * (1 + step * 0.005), 0)
            if strike <= 0:
                continue
            for right in ("call", "put"):
                theo = bs_price(spot, strike, years, rate, implied_vol, right)
                if theo < 0.05:
                    continue
                half = max(0.01, min(theo * 0.02, 0.10))
                bid, ask = round(theo - half, 2), round(theo + half, 2)
                if bid <= 0:
                    continue
                contracts.append(
                    {
                        "symbol": build_occ_symbol(underlying, expiry, right, strike),
                        "underlying": underlying,
                        "expiration": expiry.isoformat(),
                        "days_to_expiry": dte,
                        "right": right,
                        "strike": float(strike),
                        "bid": bid,
                        "ask": ask,
                        "mid": round(theo, 2),
                        "spread_pct": (ask - bid) / theo,
                        "last_trade_price": round(theo, 2),
                        "open_interest": 2500,
                        "volume": 400.0,
                        "implied_volatility": implied_vol,
                        "greeks": bs_greeks(spot, strike, years, rate, implied_vol, right),
                        "greeks_source": "black_scholes",
                        "moneyness": spot / strike,
                        "spot": spot,
                    }
                )
    return contracts


class FakeMarketData:
    """Stands in for `desk.alpaca.market_data.MarketData`."""

    def __init__(self, tickers: dict[str, float] | None = None, trending: bool = False, implied_vol: float = 0.22):
        self.tickers = tickers or {"SPY": 580.0, "QQQ": 500.0, "IWM": 220.0}
        self.trending = trending
        self.implied_vol = implied_vol
        self.calls: list[str] = []
        # `compute_indicators` reads `self.settings`, so the fake carries one too.
        self.settings = load_settings()

    def get_equity_bars(self, symbols, timeframe="1D", start=None, end=None, limit=None):
        self.calls.append("get_equity_bars")
        if isinstance(symbols, str):
            symbols = [symbols]
        maker = make_trending_bars if self.trending else make_bars
        return {
            s: (maker(s) if self.trending else make_bars(s, start_price=self.tickers.get(s, 400.0)))
            for s in symbols
            if s in self.tickers
        }

    def compute_indicators(self, bars):
        from desk.alpaca.market_data import compute_indicators

        return compute_indicators(bars, self.settings.regime)

    def get_options_chain(self, ticker, **kwargs):
        self.calls.append("get_options_chain")
        return make_chain(ticker, self.tickers.get(ticker, 400.0), self.implied_vol)

    def get_options_bars(self, *args, **kwargs):
        return {}

    def get_latest_quotes(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        return {
            s: {"symbol": s, "bid": self.tickers[s] - 0.01, "ask": self.tickers[s] + 0.01,
                "mid": self.tickers[s], "spread_pct": 0.00003}
            for s in symbols
            if s in self.tickers
        }

    def get_last_price(self, symbol):
        return self.tickers.get(symbol)

    def iv_summary(self, ticker, chain=None):
        return {
            "underlying": ticker,
            "atm_iv": self.implied_vol,
            "iv_rank": 0.55,
            "iv_percentile": 0.55,
            "term_structure": "contango",
            "skew": "put_skewed",
            "contracts_sampled": len(chain or []),
        }


class FakeExecution:
    """Stands in for `desk.alpaca.execution.ExecutionEngine`."""

    def __init__(self, equity: float = 100_000.0, positions: list[dict[str, Any]] | None = None):
        self.equity = equity
        self._positions = positions or []
        self.submitted: list[dict[str, Any]] = []
        self.dry_run = True

    def get_account_state(self):
        return {
            "account_number": "PA_TEST_0001",
            "status": "ACTIVE",
            "cash": self.equity * 0.9,
            "equity": self.equity,
            "buying_power": self.equity * 2,
            "initial_margin": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "options_trading_level": 3,
            "trading_blocked": False,
            "as_of": "2026-08-21T14:00:00+00:00",
        }

    def get_positions(self):
        return list(self._positions)

    def get_open_orders(self):
        return []

    def submit_orders(self, orders, cycle_id="", wait_for_fill=True):
        self.submitted.extend(orders)
        return [
            {
                "ok": True,
                "dry_run": True,
                "trade_id": order.get("trade_id", ""),
                "symbol": "+".join(leg["contract_symbol"] for leg in order.get("legs", [])) or order.get("symbol_or_contract", ""),
                "qty": order.get("qty", 1),
                "limit_price": order.get("limit_price"),
                "status": "simulated",
                "client_order_id": f"test-{order.get('trade_id', '')}",
            }
            for order in orders
        ]


@pytest.fixture
def fake_market_data():
    return FakeMarketData()


@pytest.fixture
def fake_execution():
    return FakeExecution()


@pytest.fixture
def orchestrator(fake_market_data, fake_execution, store):
    """A fully wired orchestrator with fake broker plumbing."""
    from desk.orchestrator.orchestrator_agent import Orchestrator

    return Orchestrator(
        market_data=fake_market_data, execution=fake_execution, store=store, dry_run=True
    )
