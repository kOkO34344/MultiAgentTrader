"""Technical / Momentum Analyst — price structure to options structure."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent


class Levels(BaseModel):
    support: float = 0.0
    resistance: float = 0.0
    entry_zone: list[float] = Field(default_factory=list)
    invalidation: float = 0.0
    target: float = 0.0


class Indicators(BaseModel):
    trend: str = ""
    momentum: str = ""
    volatility: str = ""
    volume: str = ""


class SuggestedStructure(BaseModel):
    name: str = ""
    target_dte: int = 30
    rationale: str = ""


class TickerAnalysis(BaseModel):
    ticker: str
    setup_type: str = "no_setup"
    direction: str = "neutral"
    conviction: float = 0.0
    levels: Levels = Field(default_factory=Levels)
    indicators: Indicators = Field(default_factory=Indicators)
    suggested_structure: SuggestedStructure = Field(default_factory=SuggestedStructure)
    commentary: str = ""


class TechnicalOutput(BaseModel):
    technical_regime: str = ""
    per_ticker_analysis: list[TickerAnalysis] = Field(default_factory=list)


class TechnicalAnalyst(LLMAgent):
    """Reads trend quality, levels, and momentum persistence."""

    name = "technical_analyst"
    prompt_name = "technical_analyst"
    output_model = TechnicalOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "regime": kwargs.get("regime"),
            "as_of": kwargs.get("as_of"),
            "max_candidates": self.settings.agents.max_candidates_per_agent,
            "playbooks_available": kwargs.get("playbooks", []),
            "tickers": kwargs.get("snapshot", {}),
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Deterministic technical read derived from the computed indicators."""
        thresholds = self.settings.regime.thresholds
        analyses: list[dict[str, Any]] = []

        for ticker, data in sorted((context.get("tickers") or {}).items()):
            indicators = data.get("indicators") or {}
            price = indicators.get("last_close") or 0.0
            adx_value = indicators.get("adx") or 0.0
            slope = indicators.get("ema_slope") or 0.0
            atr_value = indicators.get("atr") or (price * 0.01)
            bandwidth = indicators.get("bollinger_bandwidth") or 0.0
            range_position = indicators.get("pct_of_52w_range")

            if adx_value >= thresholds.trend_adx_min and slope > thresholds.trend_ema_slope_min:
                setup, direction = "trend_continuation", "bullish"
                structure = "bull_call_spread"
            elif adx_value >= thresholds.trend_adx_min and slope < -thresholds.trend_ema_slope_min:
                setup, direction = "trend_continuation", "bearish"
                structure = "bear_put_spread"
            elif bandwidth and bandwidth <= thresholds.range_bandwidth_max:
                setup, direction = "range_fade", "neutral"
                structure = "iron_condor"
            else:
                setup, direction, structure = "no_setup", "neutral", ""

            conviction = 0.0
            if setup != "no_setup":
                strength = min(adx_value / 40.0, 1.0)
                conviction = round(min(0.35 + 0.45 * strength, 0.85), 3)

            analyses.append(
                {
                    "ticker": ticker,
                    "setup_type": setup,
                    "direction": direction,
                    "conviction": conviction,
                    "levels": {
                        "support": round(price - 2 * atr_value, 2),
                        "resistance": round(price + 2 * atr_value, 2),
                        "entry_zone": [round(price - 0.4 * atr_value, 2), round(price + 0.4 * atr_value, 2)],
                        "invalidation": round(
                            price - 1.5 * atr_value if direction != "bearish" else price + 1.5 * atr_value,
                            2,
                        ),
                        "target": round(
                            price + 3 * atr_value if direction != "bearish" else price - 3 * atr_value,
                            2,
                        ),
                    },
                    "indicators": {
                        "trend": f"ADX {adx_value:.1f}, EMA slope {slope:+.5f}",
                        "momentum": f"20d return {indicators.get('return_20d') or 0:+.2%}",
                        "volatility": f"ATR {atr_value:.2f} ({(indicators.get('atr_pct') or 0):.2%} of spot)",
                        "volume": "not evaluated in offline mode",
                    },
                    "suggested_structure": {
                        "name": structure,
                        "target_dte": self.settings.options.target_days_to_expiry,
                        "rationale": f"{setup} in a {context.get('regime')} regime",
                    },
                    "commentary": (
                        f"{ticker} at {price:.2f}, "
                        f"{(range_position or 0.5):.0%} of its 52-week range. "
                        f"Rules-based read: {setup}."
                    ),
                }
            )

        trending = sum(1 for a in analyses if a["setup_type"] == "trend_continuation")
        return {
            "technical_regime": (
                f"{trending}/{len(analyses)} names show confirmed directional structure "
                f"under a '{context.get('regime')}' regime (deterministic offline read)."
            ),
            "per_ticker_analysis": analyses,
        }
