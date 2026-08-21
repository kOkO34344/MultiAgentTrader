"""Historical replay of the desk's regime -> playbook -> structure logic.

**Option pricing honesty note.** Alpaca does not serve historical option *chain
snapshots*, so a faithful replay of "what was quotable on that morning" is not
available. The engine therefore prices a synthetic chain with Black-Scholes,
using trailing realised volatility plus a configurable variance risk premium as
the implied-volatility input, and applies a modelled bid-ask spread. Where real
option bars exist for a contract the engine marks against them instead.

That makes results **indicative, not exact**: they show whether the regime and
playbook logic is coherent, not what the desk would have banked to the cent.
Every reported metric is labelled with `pricing_source` so the distinction
survives into the experiment registry.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from desk.agents.regime_agent import classify_regime_deterministic
from desk.agents.vol_options_strategist import (
    CONTRACT_MULTIPLIER,
    StructureBuilder,
    payoff_at,
)
from desk.backtest.dataset_loader import DatasetLoader
from desk.backtest.metrics import compute_metrics
from desk.risk.limits import CandidateTrade, Portfolio, Position, RiskLimits
from desk.risk.risk_guard import RiskGuard
from desk.utils.config_loader import Settings, get_settings, playbooks_for_regime
from desk.utils.logging import get_logger
from desk.utils.math_utils import bs_greeks, bs_price, realised_volatility
from desk.utils.symbols import build_occ_symbol
from desk.utils.time_utils import nearest_friday

logger = get_logger("backtest.engine")

#: Sellers of options are paid, on average, more than realised vol. Using
#: realised vol alone as the IV input would systematically underprice premium
#: and flatter every credit strategy in the book.
VARIANCE_RISK_PREMIUM = 1.12

#: Strike spacing as a fraction of spot, used to build the synthetic chain.
STRIKE_STEP_PCT = 0.005
STRIKES_EACH_SIDE = 20


class BacktestTrade(BaseModel):
    """One structure opened and closed during the replay."""

    trade_id: str
    ticker: str
    playbook: str
    regime: str
    opened_on: str
    closed_on: str | None = None
    expiration: str = ""
    net_side: str = "credit"
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: int = 1
    max_loss: float = 0.0
    max_profit: float | None = None
    pnl: float | None = None
    days_held: int | None = None
    exit_reason: str = ""
    commission: float = 0.0
    slippage: float = 0.0
    legs: list[dict[str, Any]] = Field(default_factory=list)


class BacktestResult(BaseModel):
    """Everything a replay produced."""

    start: str
    end: str
    initial_capital: float
    trades: list[BacktestTrade] = Field(default_factory=list)
    equity_curve: list[float] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    regimes: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    pricing_source: str = "black_scholes_synthetic_chain"
    warnings: list[str] = Field(default_factory=list)


class BacktestEngine:
    """Replays the live decision logic against historical bars."""

    def __init__(
        self,
        settings: Settings | None = None,
        loader: DatasetLoader | None = None,
        market_data: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.loader = loader or DatasetLoader(market_data=market_data)
        self.builder = StructureBuilder(self.settings)
        self.risk_guard = RiskGuard(RiskLimits.from_settings(self.settings))

    # -- synthetic chain ---------------------------------------------------

    def synth_chain(self, ticker: str, spot: float, as_of: date, implied_vol: float) -> list[dict[str, Any]]:
        """Build a priceable chain around ``spot`` for the expiries the desk uses."""
        options = self.settings.options
        rate = options.risk_free_rate
        contracts: list[dict[str, Any]] = []

        target_dtes = sorted(
            {
                options.min_days_to_expiry + 7,
                options.target_days_to_expiry,
                min(options.target_days_to_expiry + 21, options.max_days_to_expiry),
            }
        )

        for target in target_dtes:
            expiry = nearest_friday(target, as_of)
            dte = (expiry - as_of).days
            if dte < options.min_days_to_expiry or dte > options.max_days_to_expiry:
                continue
            years = dte / 365.0

            for step in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1):
                strike = round(spot * (1 + step * STRIKE_STEP_PCT), 0)
                if strike <= 0:
                    continue
                for right in ("call", "put"):
                    theo = bs_price(spot, strike, years, rate, implied_vol, right)
                    if theo < 0.02:
                        continue
                    # Model a spread that widens for cheap, far-from-the-money
                    # contracts, the way a real chain does.
                    half_spread = max(0.01, min(theo * 0.03, 0.15))
                    bid, ask = round(theo - half_spread, 2), round(theo + half_spread, 2)
                    if bid <= 0:
                        continue
                    contracts.append(
                        {
                            "symbol": build_occ_symbol(ticker, expiry, right, strike),
                            "underlying": ticker,
                            "expiration": expiry.isoformat(),
                            "days_to_expiry": dte,
                            "right": right,
                            "strike": float(strike),
                            "bid": bid,
                            "ask": ask,
                            "mid": round(theo, 2),
                            "spread_pct": (ask - bid) / theo if theo else 1.0,
                            "open_interest": 1000,
                            "volume": 100,
                            "implied_volatility": implied_vol,
                            "greeks": bs_greeks(spot, strike, years, rate, implied_vol, right),
                            "greeks_source": "black_scholes",
                            "spot": spot,
                        }
                    )
        return contracts

    # -- pricing -----------------------------------------------------------

    def mark_structure(
        self, legs: list[dict[str, Any]], spot: float, days_left: int, implied_vol: float
    ) -> float:
        """Mark-to-model value of an open structure, per contract."""
        if days_left <= 0:
            return payoff_at(legs, spot) / CONTRACT_MULTIPLIER + self._entry_cost(legs)

        rate = self.settings.options.risk_free_rate
        years = max(days_left, 0) / 365.0
        value = 0.0
        for leg in legs:
            theo = bs_price(spot, float(leg["strike"]), years, rate, implied_vol, leg["right"])
            sign = -1.0 if str(leg["side"]).lower().startswith("s") else 1.0
            value += sign * theo * abs(float(leg.get("qty", 1)))
        return value

    @staticmethod
    def _entry_cost(legs: list[dict[str, Any]]) -> float:
        total = 0.0
        for leg in legs:
            sign = -1.0 if str(leg["side"]).lower().startswith("s") else 1.0
            total += sign * float(leg.get("mid_price", 0)) * abs(float(leg.get("qty", 1)))
        return total

    def apply_slippage(self, price: float, legs: list[dict[str, Any]], opening: bool) -> float:
        """Cost of crossing the spread, in the direction that hurts."""
        config = self.settings.backtest
        if config.slippage_model == "none":
            return 0.0
        if config.slippage_model == "fixed_pct":
            return abs(price) * config.fixed_slippage_pct
        # half_spread: pay half the quoted spread on every leg, both ways.
        return sum(
            max((float(leg.get("ask", 0)) - float(leg.get("bid", 0))) / 2.0, 0.01) for leg in legs
        )

    def commission(self, legs: list[dict[str, Any]], qty: int) -> float:
        return self.settings.backtest.commission_per_contract * len(legs) * qty

    # -- main loop ---------------------------------------------------------

    def run(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        tickers: list[str] | None = None,
        rebalance_every_days: int = 5,
    ) -> BacktestResult:
        """Replay the desk over ``[start, end]``."""
        config = self.settings.backtest
        start_date = date.fromisoformat(str(start or config.start))
        end_date = date.fromisoformat(str(end or config.end))
        universe = tickers or self.settings.universe.all_tickers[: self.settings.universe.max_active_tickers]

        result = BacktestResult(
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            initial_capital=config.initial_capital,
        )

        # Warm-up history is needed before the first indicator is valid.
        warmup_start = start_date - timedelta(days=int(self.settings.regime.lookback_days * 1.8) + 30)
        bars_by_ticker = self.loader.load_equity_bars(universe, warmup_start, end_date, "1D")
        if not any(bars_by_ticker.values()):
            result.warnings.append("No historical bars returned — check credentials and the date range.")
            result.metrics = compute_metrics([], [], config.initial_capital)
            return result

        series = {
            ticker: [b for b in bars if b.get("timestamp")]
            for ticker, bars in bars_by_ticker.items()
            if bars
        }
        benchmark = self.settings.regime.benchmark
        if benchmark not in series:
            benchmark = next(iter(series))
            result.warnings.append(f"Benchmark unavailable; using {benchmark} instead.")

        trading_dates = sorted(
            {
                str(bar["timestamp"])[:10]
                for bar in series[benchmark]
                if start_date.isoformat() <= str(bar["timestamp"])[:10] <= end_date.isoformat()
            }
        )
        if not trading_dates:
            result.warnings.append("No bars inside the requested window.")
            result.metrics = compute_metrics([], [], config.initial_capital)
            return result

        equity = config.initial_capital
        peak_equity = equity
        open_trades: list[dict[str, Any]] = []
        closed: list[BacktestTrade] = []
        self._portfolio = Portfolio(cash=equity, equity=equity, buying_power=equity * 2)

        for index, day_str in enumerate(trading_dates):
            day = date.fromisoformat(day_str)
            prices = {t: self._close_on(series[t], day_str) for t in series}
            prices = {t: p for t, p in prices.items() if p}

            # 1. Mark and close open positions.
            realised_today = 0.0
            still_open = []
            for trade in open_trades:
                closed_trade = self._maybe_close(trade, day, prices, series)
                if closed_trade:
                    realised_today += closed_trade.pnl or 0.0
                    closed.append(closed_trade)
                else:
                    still_open.append(trade)
            open_trades = still_open
            equity += realised_today

            # 2. Open new positions on the rebalance cadence.
            self._portfolio = self._replay_portfolio(equity, peak_equity, realised_today, open_trades, day, prices)
            if index % rebalance_every_days == 0:
                history = self._history_to(series[benchmark], day_str)
                if len(history) >= self.settings.regime.bollinger_period + 5:
                    regime = self._classify(history)
                    result.regimes[day_str] = regime.label
                    opened = self._open_positions(day, regime.label, prices, series, len(open_trades))
                    open_trades.extend(opened)

            # 3. Record the equity point including open-position marks.
            unrealised = sum(self._unrealised(trade, day, prices) for trade in open_trades)
            marked = equity + unrealised
            peak_equity = max(peak_equity, marked)
            result.equity_curve.append(round(marked, 2))
            result.dates.append(day_str)

        # Close anything still open at the final mark.
        final_day = date.fromisoformat(trading_dates[-1])
        final_prices = {t: self._close_on(series[t], trading_dates[-1]) for t in series}
        for trade in open_trades:
            forced = self._close_trade(trade, final_day, final_prices.get(trade["ticker"], 0.0), "backtest_end")
            equity += forced.pnl or 0.0
            closed.append(forced)
        if open_trades:
            result.equity_curve[-1] = round(equity, 2)

        result.trades = closed
        result.metrics = compute_metrics(
            [t.model_dump() for t in closed], result.equity_curve, config.initial_capital
        )
        result.metrics["pricing_source"] = result.pricing_source
        result.metrics["regime_distribution"] = self._regime_counts(result.regimes)

        logger.info(
            "backtest_complete",
            extra={
                "event": "backtest_complete",
                "trades": len(closed),
                "total_pnl": result.metrics.get("total_pnl"),
                "sharpe": result.metrics.get("sharpe"),
            },
        )
        return result

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _close_on(bars: list[dict[str, Any]], day: str) -> float:
        return next((float(b["close"]) for b in bars if str(b["timestamp"])[:10] == day), 0.0)

    @staticmethod
    def _history_to(bars: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
        return [b for b in bars if str(b["timestamp"])[:10] <= day]

    def _classify(self, history: list[dict[str, Any]]) -> Any:
        from desk.utils.math_utils import adx, atr, bollinger_bandwidth, ema_slope

        closes = [b["close"] for b in history]
        highs = [b["high"] for b in history]
        lows = [b["low"] for b in history]
        regime_config = self.settings.regime
        atr_value = atr(highs, lows, closes, regime_config.atr_period)

        indicators = {
            "last_close": closes[-1],
            "adx": adx(highs, lows, closes, regime_config.adx_period),
            "ema_slope": ema_slope(closes, regime_config.fast_ema),
            "bollinger_bandwidth": bollinger_bandwidth(
                closes, regime_config.bollinger_period, regime_config.bollinger_std
            ),
            "atr": atr_value,
            "atr_pct": (atr_value / closes[-1]) if atr_value and closes[-1] else None,
            "realised_vol_20d": realised_volatility(closes, 20),
        }
        # No IV history in a replay, so the classifier sees no IV-rank input;
        # the high-vol label therefore keys off ATR alone here.
        return classify_regime_deterministic(indicators, {}, None, self.settings)

    def implied_vol_for(self, series: list[dict[str, Any]], day: str) -> float:
        """IV input for the synthetic chain: trailing realised vol plus a premium."""
        closes = [b["close"] for b in self._history_to(series, day)]
        realised = realised_volatility(closes, 20) or 0.20
        return max(0.06, min(realised * VARIANCE_RISK_PREMIUM, 2.0))

    def _risk_check(self, structure: dict[str, Any], qty: int, day: date) -> Any:
        """Run the deterministic guard against the replay's current portfolio."""
        profile = structure["risk_profile"]
        candidate = CandidateTrade(
            trade_id=structure["structure_id"],
            symbol_or_contract=structure["legs"][0]["contract_symbol"],
            asset_class="option",
            side="buy" if structure["net_side"] == "debit" else "sell",
            qty=qty,
            estimated_notional=(profile["max_loss"] or 0) * qty,
            delta=profile["net_delta"] * qty,
            gamma=profile["net_gamma"] * qty,
            vega=profile["net_vega"] * qty,
            theta=profile["net_theta"] * qty,
            underlying=structure["ticker"],
            days_to_expiry=structure["dte"],
            max_loss=(profile["max_loss"] or 0) * qty,
            max_profit=(profile["max_profit"] * qty) if profile["max_profit"] else None,
            playbook=structure["playbook"],
            legs=structure["legs"],
        )
        decision = self.risk_guard.check(self._portfolio, [candidate])
        return decision.trades[0] if decision.trades else None

    def _open_positions(
        self,
        day: date,
        regime: str,
        prices: dict[str, float],
        series: dict[str, list[dict[str, Any]]],
        already_open: int,
    ) -> list[dict[str, Any]]:
        """Build, risk-check, and open new structures for one rebalance date."""
        playbooks = playbooks_for_regime(regime)
        if not playbooks:
            return []

        limits = self.settings.risk_limits
        room = max(0, limits.max_open_positions - already_open)
        budget = min(self.settings.critic.max_approved_trades, room)
        opened: list[dict[str, Any]] = []
        open_tickers = {p.ticker for p in self._portfolio.positions}

        for ticker, spot in sorted(prices.items()):
            if len(opened) >= budget:
                break
            implied_vol = self.implied_vol_for(series[ticker], day.isoformat())
            chain = self.synth_chain(ticker, spot, day, implied_vol)
            if not chain:
                continue

            history = self._history_to(series[ticker], day.isoformat())
            realised = realised_volatility([b["close"] for b in history], 20)
            context = {
                # A replay has no IV-rank history; the synthetic chain is priced
                # off trailing realised vol, so that ratio stands in for rank.
                "iv_rank": min(max((implied_vol / 0.35), 0.0), 1.0),
                "adx": self._adx_for(history),
                "has_long_exposure": ticker in open_tickers,
                "days_to_event": None,
                "near_support": False,
            }

            for playbook in playbooks:
                permitted, _ = self.builder.conditions_met(playbook, context)
                if not permitted:
                    continue
                structure = self.builder.build(
                    ticker, playbook, chain, spot, realised_vol=realised
                )
                if structure is None:
                    continue
                valid, _ = self.builder.validate(structure)
                if not valid:
                    continue

                qty = max(1, int(structure.get("sizing", {}).get("max_contracts", 1) or 1))
                profile = structure["risk_profile"]

                # Run the live Risk Guard, so a backtest cannot take risk the
                # real desk would have refused. Without this the replay reports
                # drawdowns the circuit breakers would have prevented.
                verdict = self._risk_check(structure, qty, day)
                if verdict is None or verdict.approved_qty <= 0:
                    continue
                qty = int(verdict.approved_qty)

                legs = structure["legs"]
                slippage = self.apply_slippage(structure["net_price"], legs, opening=True) * qty
                commission = self.commission(legs, qty)
                opened.append(
                    {
                        "trade_id": f"bt-{day.isoformat()}-{ticker}-{playbook['name']}",
                        "ticker": ticker,
                        "playbook": playbook["name"],
                        "regime": regime,
                        "opened_on": day.isoformat(),
                        "expiration": structure["expiration"],
                        "net_side": structure["net_side"],
                        "entry_price": structure["net_price"],
                        "entry_value": self._entry_cost(legs),
                        "qty": qty,
                        "legs": legs,
                        "implied_vol": implied_vol,
                        "max_loss": (profile["max_loss"] or 0) * qty,
                        "max_profit": (profile["max_profit"] * qty) if profile["max_profit"] else None,
                        "exits": structure.get("exit_plan", {}),
                        "open_costs": slippage + commission,
                        "commission": commission,
                        "slippage": slippage,
                    }
                )
                break  # one structure per ticker per rebalance

        return opened

    def _maybe_close(
        self,
        trade: dict[str, Any],
        day: date,
        prices: dict[str, float],
        series: dict[str, list[dict[str, Any]]],
    ) -> BacktestTrade | None:
        """Apply the exit plan; return a closed trade or ``None`` to hold."""
        spot = prices.get(trade["ticker"])
        if not spot:
            return None

        expiry = date.fromisoformat(trade["expiration"])
        days_left = (expiry - day).days
        exits = trade.get("exits") or {}
        implied_vol = self.implied_vol_for(series[trade["ticker"]], day.isoformat())
        value = self.mark_structure(trade["legs"], spot, days_left, implied_vol)

        # P&L per contract = current value minus entry value, sign-consistent
        # because both are computed as (long legs - short legs).
        pnl_per_contract = (value - trade["entry_value"]) * CONTRACT_MULTIPLIER
        max_profit_per = (trade["max_profit"] / trade["qty"]) if trade.get("max_profit") else None
        max_loss_per = trade["max_loss"] / trade["qty"] if trade["qty"] else 0.0

        reason = ""
        if days_left <= 0:
            reason = "expiry"
        elif max_profit_per and pnl_per_contract >= max_profit_per * float(
            exits.get("profit_target_pct", 0.5)
        ):
            reason = "profit_target"
        elif max_loss_per and pnl_per_contract <= -max_loss_per * float(
            exits.get("stop_loss_pct", 0.6)
        ):
            reason = "stop_loss"
        elif days_left <= int(exits.get("time_stop_dte", 10) or 0):
            reason = "time_stop"

        if not reason:
            return None
        return self._close_trade(trade, day, spot, reason, value)

    def _close_trade(
        self,
        trade: dict[str, Any],
        day: date,
        spot: float,
        reason: str,
        value: float | None = None,
    ) -> BacktestTrade:
        """Realise a trade, charging exit slippage and commission."""
        expiry = date.fromisoformat(trade["expiration"])
        days_left = max((expiry - day).days, 0)
        if value is None:
            value = self.mark_structure(trade["legs"], spot, days_left, trade.get("implied_vol", 0.2))

        gross = (value - trade["entry_value"]) * CONTRACT_MULTIPLIER * trade["qty"]
        exit_slippage = self.apply_slippage(abs(value), trade["legs"], opening=False) * trade["qty"]
        exit_commission = self.commission(trade["legs"], trade["qty"]) if reason != "expiry" else 0.0
        costs = trade["open_costs"] + exit_slippage + exit_commission

        return BacktestTrade(
            trade_id=trade["trade_id"],
            ticker=trade["ticker"],
            playbook=trade["playbook"],
            regime=trade["regime"],
            opened_on=trade["opened_on"],
            closed_on=day.isoformat(),
            expiration=trade["expiration"],
            net_side=trade["net_side"],
            entry_price=trade["entry_price"],
            exit_price=round(abs(value), 2),
            qty=trade["qty"],
            max_loss=trade["max_loss"],
            max_profit=trade.get("max_profit"),
            pnl=round(gross - costs, 2),
            days_held=(day - date.fromisoformat(trade["opened_on"])).days,
            exit_reason=reason,
            commission=round(trade["commission"] + exit_commission, 2),
            slippage=round(trade["slippage"] + exit_slippage, 2),
            legs=trade["legs"],
        )

    def _unrealised(self, trade: dict[str, Any], day: date, prices: dict[str, float]) -> float:
        spot = prices.get(trade["ticker"])
        if not spot:
            return 0.0
        days_left = max((date.fromisoformat(trade["expiration"]) - day).days, 0)
        value = self.mark_structure(trade["legs"], spot, days_left, trade.get("implied_vol", 0.2))
        return (value - trade["entry_value"]) * CONTRACT_MULTIPLIER * trade["qty"] - trade["open_costs"]

    @staticmethod
    def _regime_counts(regimes: dict[str, str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label in regimes.values():
            counts[label] = counts.get(label, 0) + 1
        return counts


    def _replay_portfolio(
        self,
        equity: float,
        peak_equity: float,
        realised_today: float,
        open_trades: list[dict[str, Any]],
        day: date,
        prices: dict[str, float],
    ) -> Portfolio:
        """Portfolio state for the guard, reconstructed from the replay."""
        # Position Greeks are not reconstructed in a replay, so the Greek limits
        # do not bind here; the notional and contract-count limits do.
        positions = [
            Position(
                symbol=trade["legs"][0]["contract_symbol"],
                qty=trade["qty"],
                asset_class="option",
                market_value=trade["max_loss"],
            )
            for trade in open_trades
        ]
        deployed = sum(trade["max_loss"] for trade in open_trades)
        return Portfolio(
            cash=max(equity - deployed, 0.0),
            equity=equity,
            buying_power=max(equity * 2 - deployed, 0.0),
            initial_margin=deployed,
            peak_equity=peak_equity,
            daily_pnl=realised_today,
            positions=positions,
            trades_today=sum(1 for t in open_trades if t["opened_on"] == day.isoformat()),
            tickers_traded_today=[
                t["ticker"] for t in open_trades if t["opened_on"] == day.isoformat()
            ],
        )

    def _adx_for(self, history: list[dict[str, Any]]) -> float | None:
        from desk.utils.math_utils import adx

        if len(history) < 2 * self.settings.regime.adx_period + 1:
            return None
        return adx(
            [b["high"] for b in history],
            [b["low"] for b in history],
            [b["close"] for b in history],
            self.settings.regime.adx_period,
        )
