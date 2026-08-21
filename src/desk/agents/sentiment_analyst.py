"""Sentiment & News Analyst — narrative, positioning, and headline risk."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent


class SentimentEvent(BaseModel):
    type: str = ""
    date: str = ""
    days_away: int = 0
    expected_move_pct: float = 0.0


class TickerSentiment(BaseModel):
    ticker: str
    score: float = 0.0
    coverage: str = "none"
    stance: str = "mixed"
    crowding: str = "normal"
    events: list[SentimentEvent] = Field(default_factory=list)
    binary_event_risk: bool = False
    commentary: str = ""
    impact_on_options_idea: str = "confirm"
    impact_reason: str = ""


class SentimentOutput(BaseModel):
    sentiment_regime: str = ""
    per_ticker_sentiment: list[TickerSentiment] = Field(default_factory=list)
    desk_wide_warnings: list[str] = Field(default_factory=list)


class SentimentAnalyst(LLMAgent):
    """Holds veto power over trade *timing*. Its best output is usually 'not yet'."""

    name = "sentiment_analyst"
    prompt_name = "sentiment_analyst"
    output_model = SentimentOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "regime": kwargs.get("regime"),
            "as_of": kwargs.get("as_of"),
            "tickers": list((kwargs.get("snapshot") or {}).keys()),
            "calendar": kwargs.get("calendar", []),
            "news": kwargs.get("news", []),
            "proposals_to_review": kwargs.get("proposals", []),
            "reminder": "Never invent a headline. Report coverage 'none' when you have no data.",
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Offline: score 0.0 with coverage 'none', but still honour the calendar.

        Event risk comes from the structured calendar rather than from news, so
        the binary-event veto keeps working without a news feed.
        """
        calendar = context.get("calendar") or []
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for event in calendar:
            by_ticker.setdefault(str(event.get("ticker", "")).upper(), []).append(event)

        window = self.settings.regime.thresholds.event_window_days
        sentiments = []
        for ticker in sorted(context.get("tickers") or []):
            events = by_ticker.get(ticker, [])
            near = [e for e in events if 0 <= int(e.get("days_away", 999)) <= window]
            sentiments.append(
                {
                    "ticker": ticker,
                    "score": 0.0,
                    "coverage": "none",
                    "stance": "mixed",
                    "crowding": "normal",
                    "events": [
                        {
                            "type": str(e.get("event_type", e.get("type", "unknown"))),
                            "date": str(e.get("date", "")),
                            "days_away": int(e.get("days_away", 0)),
                            "expected_move_pct": float(e.get("implied_move_pct", 0.0) or 0.0),
                        }
                        for e in events
                    ],
                    "binary_event_risk": bool(near),
                    "commentary": (
                        "No news feed configured — sentiment withheld rather than invented."
                    ),
                    "impact_on_options_idea": "delay" if near else "confirm",
                    "impact_reason": (
                        f"binary event within {window}d — defer new premium selling"
                        if near
                        else "no scheduled event inside the window"
                    ),
                }
            )

        flagged = [s["ticker"] for s in sentiments if s["binary_event_risk"]]
        return {
            "sentiment_regime": (
                "Offline mode: no narrative data. Event risk still enforced from the calendar."
            ),
            "per_ticker_sentiment": sentiments,
            "desk_wide_warnings": (
                [f"Binary event risk inside {window}d: {', '.join(flagged)}"] if flagged else []
            ),
        }
