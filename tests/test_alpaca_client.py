"""Alpaca layer: the paper-only guard, retry policy, and order construction."""

from __future__ import annotations

import pytest

from desk.alpaca.client import (
    AlpacaClients,
    AlpacaNotConfiguredError,
    PaperOnlyError,
    assert_paper_endpoint,
    is_retryable,
    with_retry,
)
from desk.alpaca.execution import ExecutionEngine, make_client_order_id


class HttpError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


# ---------------------------------------------------------------------------
# Paper-only guard — the single most important safety property here
# ---------------------------------------------------------------------------


def test_paper_endpoint_is_allowed():
    assert_paper_endpoint("https://paper-api.alpaca.markets")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "https://API.ALPACA.MARKETS/v2",
        "https://broker-api.alpaca.markets",
        "https://evil.example.com",
        "",
    ],
)
def test_non_paper_endpoints_are_refused(url):
    """Allow-list, not deny-list: an unrecognised host is refused too."""
    with pytest.raises(PaperOnlyError):
        assert_paper_endpoint(url)


def test_disabling_paper_only_is_itself_refused():
    with pytest.raises(PaperOnlyError):
        assert_paper_endpoint("https://paper-api.alpaca.markets", paper_only=False)


def test_client_construction_refuses_a_live_endpoint(settings):
    settings.alpaca.base_url = "https://api.alpaca.markets"
    with pytest.raises(PaperOnlyError):
        AlpacaClients(settings=settings)


def test_client_requires_credentials(settings):
    clients = AlpacaClients(settings=settings)
    with pytest.raises(AlpacaNotConfiguredError):
        _ = clients.trading


def test_verify_paper_account_rejects_a_non_paper_account_number(settings, monkeypatch):
    clients = AlpacaClients(settings=settings)

    class Account:
        account_number = "123456789"
        status = "ACTIVE"
        equity = "100000"
        cash = "50000"
        buying_power = "200000"
        options_trading_level = 3
        trading_blocked = False

    monkeypatch.setattr(clients, "account", lambda: Account())
    # Blank the URL so the account-number branch of the check is what is tested.
    settings.alpaca.base_url = "https://mock.test"
    with pytest.raises(PaperOnlyError):
        clients.verify_paper_account()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "expected"), [(429, True), (500, True), (503, True), (400, False), (404, False)])
def test_error_classification(status, expected):
    assert is_retryable(HttpError(status)) is expected


def test_message_based_classification():
    assert is_retryable(Exception("Too Many Requests")) is True
    assert is_retryable(Exception("connection reset by peer")) is True
    assert is_retryable(Exception("invalid symbol")) is False


def test_retry_succeeds_after_transient_failures():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise HttpError(429)
        return "ok"

    assert with_retry(flaky, backoff_base=0.001) == "ok"
    assert len(attempts) == 3


def test_non_retryable_errors_propagate_immediately():
    attempts = []

    def broken():
        attempts.append(1)
        raise HttpError(400)

    with pytest.raises(HttpError):
        with_retry(broken, backoff_base=0.001)
    assert len(attempts) == 1, "a 400 must not be retried — it wastes rate limit"


def test_retries_are_bounded():
    attempts = []

    def always_rate_limited():
        attempts.append(1)
        raise HttpError(429)

    with pytest.raises(HttpError):
        with_retry(always_rate_limited, max_retries=2, backoff_base=0.001)
    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(settings):
    return ExecutionEngine(clients=AlpacaClients(settings=settings), dry_run=True)


def test_multi_leg_order_uses_the_mleg_order_class(engine):
    spec = {
        "trade_id": "t-001",
        "qty": 2,
        "type": "limit",
        "limit_price": -1.23,
        "legs": [
            {"contract_symbol": "SPY260918P00540000", "side": "sell", "qty": 1},
            {"contract_symbol": "SPY260918P00535000", "side": "buy", "qty": 1},
        ],
    }
    request = engine.build_order_request(spec, cycle_id="c1")
    assert str(request.order_class.value) == "mleg"
    assert len(request.legs) == 2
    assert request.qty == 2
    assert request.limit_price == -1.23, "a negative limit expresses a net credit"
    assert [leg.side.value for leg in request.legs] == ["sell", "buy"]


def test_single_leg_market_order(engine):
    request = engine.build_order_request(
        {"trade_id": "t-002", "symbol_or_contract": "SPY", "qty": 10, "side": "buy", "type": "market"},
        cycle_id="c1",
    )
    assert request.symbol == "SPY"
    assert request.qty == 10
    assert request.side.value == "buy"


def test_client_order_id_is_deterministic_and_bounded():
    first = make_client_order_id("t1", "SPY", 1, "cycle-a")
    assert first == make_client_order_id("t1", "SPY", 1, "cycle-a")
    assert first != make_client_order_id("t1", "SPY", 1, "cycle-b")
    assert first != make_client_order_id("t1", "SPY", 2, "cycle-a")
    assert len(first) <= 48, "Alpaca caps client_order_id at 48 characters"


def test_marketable_limit_crosses_toward_the_fill(engine):
    assert engine.marketable_limit(2.50, "buy", 0.40) > 2.50
    assert engine.marketable_limit(2.50, "sell", 0.40) < 2.50


def test_marketable_limit_moves_at_least_one_tick_on_a_narrow_spread(engine):
    """A percentage of a tight spread rounds to zero and never fills."""
    assert engine.marketable_limit(1.00, "buy", 0.02) >= 1.01


def test_dry_run_never_calls_the_broker(engine):
    results = engine.submit_orders(
        [{"trade_id": "t-1", "symbol_or_contract": "SPY", "qty": 1, "side": "buy", "type": "market"}]
    )
    assert results[0]["ok"] is True
    assert results[0]["dry_run"] is True
    assert results[0]["status"] == "simulated"


def test_a_bad_order_spec_fails_that_order_only(engine):
    results = engine.submit_orders(
        [
            {"trade_id": "bad", "legs": [{"side": "buy"}, {"side": "sell"}], "type": "limit"},
            {"trade_id": "good", "symbol_or_contract": "SPY", "qty": 1, "side": "buy", "type": "market"},
        ]
    )
    assert results[0]["ok"] is False
    assert results[1]["ok"] is True, "one malformed order must not abort the batch"


# ---------------------------------------------------------------------------
# `limit` means "the most recent N bars"
# ---------------------------------------------------------------------------


class _CapturingBars:
    """Stands in for the Alpaca stock-data client, recording the request."""

    def __init__(self, bars):
        self._bars = bars
        self.request = None

    def get_stock_bars(self, request):
        self.request = request

        class _Response:
            data = {"SPY": self._bars}

        return _Response()


def _bar(day: int):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    return SimpleNamespace(
        timestamp=datetime(2026, 8, day, tzinfo=UTC),
        open=1.0, high=2.0, low=0.5, close=float(day), volume=10, vwap=1.0, trade_count=1,
    )


def _market_data_with(monkeypatch, bars):
    from desk.alpaca.market_data import MarketData

    md = MarketData()
    stub = _CapturingBars(bars)
    monkeypatch.setattr(type(md.clients), "stock_data", property(lambda self: stub))
    monkeypatch.setattr(md.clients, "call", lambda fn, *a, **k: fn(*a, **k))
    return md, stub


def test_limit_without_start_returns_the_most_recent_bars(monkeypatch):
    """Regression: `limit` alone used to send start=None and return nothing.

    Alpaca counts `limit` forward from `start`, so the fix synthesises the
    default window and slices the tail — otherwise `limit=1` would report the
    *oldest* bar in the lookback as the latest price.
    """
    md, stub = _market_data_with(monkeypatch, [_bar(d) for d in (17, 18, 19, 20, 21)])

    bars = md.get_equity_bars(["SPY"], "1D", limit=2)["SPY"]

    assert [b["close"] for b in bars] == [20.0, 21.0]
    # The window must be requested from the API, with limit applied locally.
    assert stub.request.start is not None
    assert stub.request.limit is None


def test_explicit_start_still_delegates_limit_to_the_api(monkeypatch):
    """An explicit window keeps the API's own paging semantics."""
    md, stub = _market_data_with(monkeypatch, [_bar(d) for d in (17, 18)])

    md.get_equity_bars(["SPY"], "1D", start="2026-08-17", limit=2)

    assert stub.request.limit == 2
