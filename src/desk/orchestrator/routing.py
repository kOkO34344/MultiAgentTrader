"""Agent registry, parallel fan-out, and market snapshot assembly."""

from __future__ import annotations

import asyncio
from typing import Any

from desk.agents.base import AgentResult, LLMAgent
from desk.agents.event_agent import EventAgent
from desk.agents.fundamental_analyst import FundamentalAnalyst
from desk.agents.sentiment_analyst import SentimentAnalyst
from desk.agents.technical_analyst import TechnicalAnalyst
from desk.agents.vol_options_strategist import VolOptionsStrategist
from desk.risk.limits import Portfolio, Position
from desk.utils.config_loader import Settings, get_settings
from desk.utils.logging import get_logger

logger = get_logger("orchestrator.routing")

#: Agents that fan out in parallel during the research stage.
RESEARCH_AGENTS: dict[str, type[LLMAgent]] = {
    "fundamental_analyst": FundamentalAnalyst,
    "technical_analyst": TechnicalAnalyst,
    "sentiment_analyst": SentimentAnalyst,
    "vol_options_strategist": VolOptionsStrategist,
    "event_agent": EventAgent,
}


def build_research_agents(settings: Settings | None = None) -> dict[str, LLMAgent]:
    """Instantiate every research agent that is enabled in config."""
    settings = settings or get_settings()
    return {
        name: agent_class(settings=settings)
        for name, agent_class in RESEARCH_AGENTS.items()
        if settings.agents.is_enabled(name)
    }


async def fan_out(agents: dict[str, LLMAgent], **kwargs: Any) -> dict[str, AgentResult]:
    """Run every agent concurrently on the same snapshot.

    Each agent gets its own timeout and its own failure boundary: a dead
    specialist abstains, it never takes down the cycle.
    """
    if not agents:
        return {}

    names = list(agents)
    results = await asyncio.gather(
        *(agents[name].arun(**kwargs) for name in names), return_exceptions=True
    )

    collected: dict[str, AgentResult] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "agent_crashed",
                extra={"event": "agent_crashed", "agent": name, "error": str(result)[:300]},
            )
            collected[name] = AgentResult(
                agent=name, ok=False, mode="error", error=f"unhandled exception: {result}"
            )
        else:
            collected[name] = result

    abstained = [name for name, r in collected.items() if r.abstained]
    logger.info(
        "research_fanout_complete",
        extra={
            "event": "research_fanout_complete",
            "agents": names,
            "abstained": abstained,
            "modes": {name: r.mode for name, r in collected.items()},
        },
    )
    return collected


def run_fan_out(agents: dict[str, LLMAgent], **kwargs: Any) -> dict[str, AgentResult]:
    """Synchronous wrapper around :func:`fan_out`."""
    return asyncio.run(fan_out(agents, **kwargs))


class SnapshotBuilder:
    """Assembles the shared market snapshot every agent reasons against."""

    def __init__(self, market_data: Any, settings: Settings | None = None) -> None:
        self.market_data = market_data
        self.settings = settings or get_settings()

    def select_watchlist(self, universe: list[str] | None = None) -> list[str]:
        """Core tickers first, then satellites, capped at ``max_active_tickers``."""
        config = self.settings.universe
        tickers = universe or config.all_tickers
        return tickers[: config.max_active_tickers]

    def build(
        self, tickers: list[str] | None = None, include_chains: bool = True
    ) -> dict[str, dict[str, Any]]:
        """Bars, indicators, spot, chain, and vol surface for each ticker."""
        watchlist = self.select_watchlist(tickers)
        if not watchlist:
            return {}

        try:
            bars = self.market_data.get_equity_bars(watchlist, "1D")
        except Exception as exc:  # noqa: BLE001 - a data outage must be visible, not fatal
            logger.error(
                "snapshot_bars_failed", extra={"event": "snapshot_bars_failed", "error": str(exc)}
            )
            return {}

        snapshot: dict[str, dict[str, Any]] = {}
        for ticker in watchlist:
            ticker_bars = bars.get(ticker) or []
            if not ticker_bars:
                logger.warning(
                    "snapshot_no_bars", extra={"event": "snapshot_no_bars", "ticker": ticker}
                )
                continue

            indicators = self.market_data.compute_indicators(ticker_bars)
            entry: dict[str, Any] = {
                "indicators": indicators,
                "spot": indicators.get("last_close"),
                "bars": len(ticker_bars),
            }

            if include_chains:
                try:
                    chain = self.market_data.get_options_chain(ticker)
                    entry["chain"] = chain
                    entry["chain_size"] = len(chain)
                    entry["vol_surface"] = self.market_data.iv_summary(ticker, chain)
                except Exception as exc:  # noqa: BLE001 - trade the names that do have chains
                    logger.warning(
                        "snapshot_chain_failed",
                        extra={"event": "snapshot_chain_failed", "ticker": ticker, "error": str(exc)[:200]},
                    )
                    entry["chain"] = []
                    entry["chain_size"] = 0
                    entry["vol_surface"] = {}

            snapshot[ticker] = entry

        return snapshot

    @staticmethod
    def strip_chains(snapshot: dict[str, Any]) -> dict[str, Any]:
        """A compact snapshot for LLM context — full chains are far too large."""
        return {
            ticker: {
                "indicators": data.get("indicators", {}),
                "spot": data.get("spot"),
                "chain_size": data.get("chain_size", 0),
                "vol_surface": data.get("vol_surface", {}),
            }
            for ticker, data in snapshot.items()
        }


def build_portfolio(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    trades_today: int = 0,
    tickers_traded_today: list[str] | None = None,
    peak_equity: float | None = None,
) -> Portfolio:
    """Assemble the :class:`Portfolio` the Risk Guard evaluates.

    Alpaca does not return Greeks on positions, so option positions are enriched
    from the chain snapshot where one is available. Positions whose Greeks
    cannot be resolved contribute zero, which understates portfolio risk — so
    the notional and contract-count limits, which never depend on Greeks, are
    the binding constraints in that case.
    """
    greeks_by_symbol: dict[str, dict[str, Any]] = {}
    for data in (snapshot or {}).values():
        for contract in data.get("chain") or []:
            greeks_by_symbol[contract["symbol"]] = contract.get("greeks") or {}

    enriched: list[Position] = []
    for raw in positions or []:
        symbol = raw.get("symbol", "")
        qty = float(raw.get("qty", 0) or 0)
        is_option = str(raw.get("asset_class", "")).lower() == "option"
        greeks = greeks_by_symbol.get(symbol, {})
        multiplier = 100.0 if is_option else 1.0

        enriched.append(
            Position(
                symbol=symbol,
                qty=qty,
                asset_class=raw.get("asset_class", "option" if is_option else "us_equity"),
                market_value=float(raw.get("market_value", 0) or 0),
                cost_basis=float(raw.get("cost_basis", 0) or 0),
                unrealized_pl=float(raw.get("unrealized_pl", 0) or 0),
                delta=float(greeks.get("delta") or 0) * qty * multiplier
                if is_option
                else qty,
                gamma=float(greeks.get("gamma") or 0) * qty * multiplier if is_option else 0.0,
                vega=float(greeks.get("vega") or 0) * qty * multiplier if is_option else 0.0,
                theta=float(greeks.get("theta") or 0) * qty * multiplier if is_option else 0.0,
            )
        )

    equity = float(account.get("equity", 0) or 0)
    return Portfolio(
        cash=float(account.get("cash", 0) or 0),
        equity=equity,
        buying_power=float(account.get("buying_power", 0) or 0),
        initial_margin=float(account.get("initial_margin", 0) or 0),
        peak_equity=peak_equity or equity,
        daily_pnl=float(account.get("daily_pnl", 0) or 0),
        positions=enriched,
        trades_today=trades_today,
        tickers_traded_today=tickers_traded_today or [],
    )
