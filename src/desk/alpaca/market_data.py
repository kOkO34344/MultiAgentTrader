"""Equity and options market data helpers.

Wraps Alpaca's historical data clients and normalises every response into plain
dicts, so agents, the backtester, and the MCP tools all consume the same shapes
regardless of SDK version.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from desk.alpaca.client import AlpacaClients, get_clients
from desk.utils.logging import get_logger
from desk.utils.math_utils import (
    adx,
    atr,
    bollinger_bandwidth,
    ema_slope,
    iv_rank,
    mid_price,
    percentile_rank,
    realised_volatility,
    spread_pct,
)
from desk.utils.symbols import parse_occ_symbol
from desk.utils.time_utils import days_to_expiry, utc_iso

logger = get_logger("alpaca.market_data")

TIMEFRAMES = ("1Min", "5Min", "15Min", "1H", "1D")


def _timeframe(name: str) -> Any:
    """Map the desk's timeframe strings onto Alpaca ``TimeFrame`` objects."""
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    mapping = {
        "1min": TimeFrame(1, TimeFrameUnit.Minute),
        "5min": TimeFrame(5, TimeFrameUnit.Minute),
        "15min": TimeFrame(15, TimeFrameUnit.Minute),
        "1h": TimeFrame(1, TimeFrameUnit.Hour),
        "1hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1d": TimeFrame(1, TimeFrameUnit.Day),
        "1day": TimeFrame(1, TimeFrameUnit.Day),
    }
    key = str(name).lower().replace(" ", "")
    if key not in mapping:
        raise ValueError(f"Unsupported timeframe {name!r}. Use one of {TIMEFRAMES}.")
    return mapping[key]


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _bar_to_dict(bar: Any) -> dict[str, Any]:
    return {
        "timestamp": _to_iso(getattr(bar, "timestamp", None)),
        "open": float(getattr(bar, "open", 0) or 0),
        "high": float(getattr(bar, "high", 0) or 0),
        "low": float(getattr(bar, "low", 0) or 0),
        "close": float(getattr(bar, "close", 0) or 0),
        "volume": float(getattr(bar, "volume", 0) or 0),
        "vwap": float(getattr(bar, "vwap", 0) or 0) if getattr(bar, "vwap", None) else None,
        "trade_count": int(getattr(bar, "trade_count", 0) or 0),
    }


def _ema_cross(closes: list[float], fast: int, slow: int) -> bool | None:
    from desk.utils.math_utils import ema

    fast_value, slow_value = ema(closes, fast), ema(closes, slow)
    if fast_value is None or slow_value is None:
        return None
    return fast_value > slow_value


def _range_position(closes: list[float]) -> float | None:
    """Where the last close sits in its trailing 52-week range, in ``[0, 1]``."""
    window = closes[-252:] if len(closes) > 252 else closes
    if len(window) < 20:
        return None
    low, high = min(window), max(window)
    return (closes[-1] - low) / (high - low) if high > low else 0.5


def compute_indicators(bars: list[dict[str, Any]], regime: Any) -> dict[str, Any]:
    """Technical features from a bar series.

    Deliberately a module-level function rather than a client method: indicator
    maths has no dependency on the network, so the backtester and the tests can
    call it without constructing a broker client.
    """
    if not bars:
        return {}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    last = closes[-1]

    atr_value = atr(highs, lows, closes, regime.atr_period)
    return {
        "last_close": last,
        "bars": len(bars),
        "adx": adx(highs, lows, closes, regime.adx_period),
        "ema_slope": ema_slope(closes, regime.fast_ema),
        "ema_fast_above_slow": _ema_cross(closes, regime.fast_ema, regime.slow_ema),
        "bollinger_bandwidth": bollinger_bandwidth(
            closes, regime.bollinger_period, regime.bollinger_std
        ),
        "atr": atr_value,
        "atr_pct": (atr_value / last) if atr_value and last else None,
        "realised_vol_20d": realised_volatility(closes, 20),
        "return_5d": (closes[-1] / closes[-6] - 1.0) if len(closes) > 5 else None,
        "return_20d": (closes[-1] / closes[-21] - 1.0) if len(closes) > 20 else None,
        "pct_of_52w_range": _range_position(closes),
    }


class MarketData:
    """Normalised market data access for equities and options."""

    def __init__(self, clients: AlpacaClients | None = None) -> None:
        self.clients = clients or get_clients()
        self.settings = self.clients.settings

    # -- equities ----------------------------------------------------------

    def get_equity_bars(
        self,
        symbols: list[str] | str,
        timeframe: str = "1D",
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Historical OHLCV bars keyed by symbol.

        Defaults to a lookback long enough to seed every indicator the regime
        classifier needs when no explicit window is given.
        """
        from alpaca.data.requests import StockBarsRequest

        symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
        if not symbol_list:
            return {}

        # Alpaca counts `limit` *forward* from `start`, but every caller means it
        # as "the most recent N bars". With no `start` the API returned nothing at
        # all, so `limit=1` silently yielded zero bars — which is what broke
        # `get_last_price`'s close fallback. Synthesise the window, then slice the
        # tail locally so `limit` keeps its intended meaning.
        tail: int | None = None
        if start is None:
            lookback = self.settings.regime.lookback_days
            start = datetime.utcnow() - timedelta(days=int(lookback * 1.8) + 10)
            tail, limit = limit, None

        request = StockBarsRequest(
            symbol_or_symbols=symbol_list,
            timeframe=_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
            feed=self.settings.alpaca.data_feed,
        )
        response = self.clients.call(self.clients.stock_data.get_stock_bars, request)

        raw = getattr(response, "data", response) or {}
        result = {symbol: [_bar_to_dict(bar) for bar in bars] for symbol, bars in raw.items()}
        if tail:
            result = {symbol: bars[-tail:] for symbol, bars in result.items()}
        logger.info(
            "equity_bars_fetched",
            extra={
                "event": "equity_bars_fetched",
                "symbols": symbol_list,
                "timeframe": timeframe,
                "bars": {s: len(b) for s, b in result.items()},
            },
        )
        return result

    def get_latest_quotes(self, symbols: list[str] | str) -> dict[str, dict[str, Any]]:
        """Latest NBBO quote per equity symbol."""
        from alpaca.data.requests import StockLatestQuoteRequest

        symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
        if not symbol_list:
            return {}

        request = StockLatestQuoteRequest(
            symbol_or_symbols=symbol_list, feed=self.settings.alpaca.data_feed
        )
        response = self.clients.call(self.clients.stock_data.get_stock_latest_quote, request)

        quotes: dict[str, dict[str, Any]] = {}
        for symbol, quote in (response or {}).items():
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            quotes[symbol] = {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "bid_size": float(getattr(quote, "bid_size", 0) or 0),
                "ask_size": float(getattr(quote, "ask_size", 0) or 0),
                "mid": mid_price(bid, ask),
                "spread_pct": spread_pct(bid, ask),
                "timestamp": _to_iso(getattr(quote, "timestamp", None)),
            }
        return quotes

    def get_last_price(self, symbol: str) -> float | None:
        """Best available spot price: quote mid, else the last daily close."""
        try:
            quote = self.get_latest_quotes(symbol).get(symbol)
            if quote and quote.get("mid"):
                return float(quote["mid"])
        except Exception as exc:  # noqa: BLE001 - fall through to bars
            logger.warning(
                "quote_unavailable",
                extra={"event": "quote_unavailable", "symbol": symbol, "error": str(exc)[:200]},
            )
        bars = self.get_equity_bars(symbol, "1D", limit=1).get(symbol) or []
        return bars[-1]["close"] if bars else None

    # -- indicators --------------------------------------------------------

    def compute_indicators(self, bars: list[dict[str, Any]]) -> dict[str, Any]:
        """Derive the technical features the regime classifier consumes."""
        return compute_indicators(bars, self.settings.regime)

    # -- options -----------------------------------------------------------

    def get_options_chain(
        self,
        underlying_symbol: str,
        expiration_date_gte: str | date | None = None,
        expiration_date_lte: str | date | None = None,
        min_iv: float | None = None,
        max_iv: float | None = None,
        limit: int | None = None,
        apply_liquidity_filter: bool = True,
    ) -> list[dict[str, Any]]:
        """Latest option chain snapshots for ``underlying_symbol``.

        Alpaca filters server-side by expiry only, so IV bands, liquidity, and
        spread limits are applied here. Returns a list sorted by expiry then
        strike, with greeks backfilled from Black-Scholes where the feed omits
        them — the desk must never hold a contract whose risk it cannot compute.
        """
        from alpaca.data.requests import OptionChainRequest

        options = self.settings.options
        if expiration_date_gte is None:
            expiration_date_gte = date.today() + timedelta(days=options.min_days_to_expiry)
        if expiration_date_lte is None:
            expiration_date_lte = date.today() + timedelta(days=options.max_days_to_expiry)

        request = OptionChainRequest(
            underlying_symbol=underlying_symbol.upper(),
            expiration_date_gte=expiration_date_gte,
            expiration_date_lte=expiration_date_lte,
        )
        response = self.clients.call(self.clients.option_data.get_option_chain, request)

        spot = self.get_last_price(underlying_symbol)
        contracts: list[dict[str, Any]] = []

        for symbol, snapshot in (response or {}).items():
            contract = self._snapshot_to_dict(symbol, snapshot, spot)
            if contract is None:
                continue
            iv = contract.get("implied_volatility")
            if min_iv is not None and (iv is None or iv < min_iv):
                continue
            if max_iv is not None and (iv is None or iv > max_iv):
                continue
            if apply_liquidity_filter and not self.passes_liquidity(contract):
                continue
            contracts.append(contract)

        contracts.sort(key=lambda c: (c["expiration"] or "", c["strike"]))
        if limit:
            contracts = contracts[:limit]

        logger.info(
            "options_chain_fetched",
            extra={
                "event": "options_chain_fetched",
                "underlying": underlying_symbol,
                "contracts": len(contracts),
                "spot": spot,
            },
        )
        return contracts

    def _snapshot_to_dict(
        self, symbol: str, snapshot: Any, spot: float | None
    ) -> dict[str, Any] | None:
        """Normalise an ``OptionsSnapshot``, backfilling greeks when missing."""
        parsed = parse_occ_symbol(symbol)
        if parsed is None:
            return None

        quote = getattr(snapshot, "latest_quote", None)
        trade = getattr(snapshot, "latest_trade", None)
        bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
        ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
        mid = mid_price(bid, ask)

        iv = getattr(snapshot, "implied_volatility", None)
        iv = float(iv) if iv else None

        greeks_obj = getattr(snapshot, "greeks", None)
        greeks = {
            "delta": float(getattr(greeks_obj, "delta", 0) or 0) if greeks_obj else None,
            "gamma": float(getattr(greeks_obj, "gamma", 0) or 0) if greeks_obj else None,
            "vega": float(getattr(greeks_obj, "vega", 0) or 0) if greeks_obj else None,
            "theta": float(getattr(greeks_obj, "theta", 0) or 0) if greeks_obj else None,
            "rho": float(getattr(greeks_obj, "rho", 0) or 0) if greeks_obj else None,
        }
        greeks_source = "alpaca"

        dte = days_to_expiry(parsed.expiration)
        if greeks["delta"] is None and spot and mid:
            greeks, iv, greeks_source = self._backfill_greeks(parsed, spot, mid, dte, iv)

        return {
            "symbol": symbol,
            "underlying": parsed.underlying,
            "expiration": parsed.expiration.isoformat(),
            "days_to_expiry": dte,
            "right": parsed.right,
            "strike": parsed.strike,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct(bid, ask),
            "last_trade_price": float(getattr(trade, "price", 0) or 0) if trade else None,
            "open_interest": int(getattr(snapshot, "open_interest", 0) or 0),
            "volume": float(getattr(trade, "size", 0) or 0) if trade else 0.0,
            "implied_volatility": iv,
            "greeks": greeks,
            "greeks_source": greeks_source,
            "moneyness": (spot / parsed.strike) if spot and parsed.strike else None,
            "spot": spot,
        }

    def _backfill_greeks(
        self, parsed: Any, spot: float, mid: float, dte: int, iv: float | None
    ) -> tuple[dict[str, float | None], float | None, str]:
        """Solve for IV from the mid, then compute greeks with Black-Scholes."""
        from desk.utils.math_utils import bs_greeks, implied_volatility

        rate = self.settings.options.risk_free_rate
        years = max(dte, 0) / 365.0 or 1e-6
        solved = iv or implied_volatility(mid, spot, parsed.strike, years, rate, parsed.right)
        if not solved:
            return {k: None for k in ("delta", "gamma", "vega", "theta", "rho")}, iv, "unavailable"
        return (
            bs_greeks(spot, parsed.strike, years, rate, solved, parsed.right),
            solved,
            "black_scholes",
        )

    def passes_liquidity(self, contract: dict[str, Any]) -> bool:
        """Reject contracts the desk could not exit at a reasonable price."""
        options = self.settings.options
        bid, ask, mid = contract.get("bid"), contract.get("ask"), contract.get("mid")

        if not bid or not ask or not mid or mid <= 0:
            return False
        if (ask - bid) > options.max_spread_abs:
            return False
        if contract.get("spread_pct", float("inf")) > options.max_bid_ask_spread_pct:
            return False
        open_interest = contract.get("open_interest") or 0
        # Alpaca snapshots do not always carry open interest; only enforce it
        # when the feed actually reports a value.
        if open_interest and open_interest < options.min_open_interest:
            return False
        return True

    def get_options_bars(
        self,
        contract_symbols: list[str] | str,
        timeframe: str = "1D",
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Historical OHLCV bars for option contracts, keyed by contract symbol."""
        from alpaca.data.requests import OptionBarsRequest

        symbol_list = [contract_symbols] if isinstance(contract_symbols, str) else list(contract_symbols)
        if not symbol_list:
            return {}

        if start is None and limit is None:
            start = datetime.utcnow() - timedelta(days=30)

        request = OptionBarsRequest(
            symbol_or_symbols=symbol_list,
            timeframe=_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
        )
        response = self.clients.call(self.clients.option_data.get_option_bars, request)
        raw = getattr(response, "data", response) or {}
        return {symbol: [_bar_to_dict(bar) for bar in bars] for symbol, bars in raw.items()}

    # -- derived vol metrics -----------------------------------------------

    def iv_summary(self, underlying_symbol: str, chain: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """ATM implied vol, IV rank/percentile, term structure, and skew.

        IV rank is computed against the *cross-sectional* surface rather than a
        historical IV series, since Alpaca does not expose historical IV. It is
        a proxy — labelled as such — not a substitute for a true IV rank.
        """
        chain = chain if chain is not None else self.get_options_chain(underlying_symbol)
        ivs = [c["implied_volatility"] for c in chain if c.get("implied_volatility")]
        if not ivs:
            return {"underlying": underlying_symbol, "atm_iv": None, "iv_rank": None}

        spot = chain[0].get("spot")
        atm = min(
            (c for c in chain if c.get("implied_volatility")),
            key=lambda c: abs((c["strike"] or 0) - (spot or c["strike"])),
            default=None,
        )
        atm_iv = atm["implied_volatility"] if atm else None

        near = [c for c in chain if c["days_to_expiry"] <= 30 and c.get("implied_volatility")]
        far = [c for c in chain if c["days_to_expiry"] > 30 and c.get("implied_volatility")]
        near_iv = sum(c["implied_volatility"] for c in near) / len(near) if near else None
        far_iv = sum(c["implied_volatility"] for c in far) / len(far) if far else None

        term_structure = "flat"
        if near_iv and far_iv:
            if far_iv > near_iv * 1.03:
                term_structure = "contango"
            elif near_iv > far_iv * 1.03:
                term_structure = "backwardation"

        put_ivs = [c["implied_volatility"] for c in chain if c["right"] == "put" and c.get("implied_volatility")]
        call_ivs = [c["implied_volatility"] for c in chain if c["right"] == "call" and c.get("implied_volatility")]
        skew = "flat"
        if put_ivs and call_ivs:
            put_avg = sum(put_ivs) / len(put_ivs)
            call_avg = sum(call_ivs) / len(call_ivs)
            if put_avg > call_avg * 1.05:
                skew = "put_skewed"
            elif call_avg > put_avg * 1.05:
                skew = "call_skewed"

        return {
            "underlying": underlying_symbol,
            "atm_iv": atm_iv,
            "iv_rank": iv_rank(atm_iv, ivs) if atm_iv else None,
            "iv_percentile": percentile_rank(atm_iv, ivs) if atm_iv else None,
            "iv_rank_basis": "cross_sectional_surface_proxy",
            "term_structure": term_structure,
            "skew": skew,
            "near_iv": near_iv,
            "far_iv": far_iv,
            "contracts_sampled": len(ivs),
        }

    def find_by_delta(
        self,
        chain: list[dict[str, Any]],
        right: str,
        target_delta: float,
        expiration: str | None = None,
    ) -> dict[str, Any] | None:
        """Closest contract to a target delta — the desk's strike-selection rule."""
        candidates = [
            c
            for c in chain
            if c["right"] == right
            and c["greeks"].get("delta") is not None
            and (expiration is None or c["expiration"] == expiration)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(abs(c["greeks"]["delta"]) - abs(target_delta)))

    def expirations_near(self, chain: list[dict[str, Any]], target_dte: int) -> str | None:
        """The listed expiry closest to ``target_dte``."""
        expiries = {c["expiration"]: c["days_to_expiry"] for c in chain}
        if not expiries:
            return None
        return min(expiries.items(), key=lambda kv: abs(kv[1] - target_dte))[0]


def get_market_data() -> MarketData:
    """Convenience accessor used by the MCP tools and agents."""
    return MarketData()
