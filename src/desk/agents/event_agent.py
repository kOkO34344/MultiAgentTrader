"""Event-Driven Agent — owns the calendar, and the right to stand aside."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent


class ProposedStructure(BaseModel):
    name: str = "stand_aside"
    target_dte: int = 0
    rationale: str = ""


class CalendarEvent(BaseModel):
    ticker: str = ""
    event_type: str = "earnings"
    date: str = ""
    days_away: int = 0
    confidence: str = "low"
    implied_move_pct: float = 0.0
    historical_avg_move_pct: float = 0.0
    implied_vs_historical: str = "fair"
    recommendation: str = "no_action"
    proposed_structure: ProposedStructure = Field(default_factory=ProposedStructure)


class PositionAtRisk(BaseModel):
    symbol: str = ""
    event: str = ""
    date: str = ""
    recommended_action: str = "hold_with_awareness"


class MacroWindowItem(BaseModel):
    release: str = ""
    date: str = ""
    desk_guidance: str = "normal"


class EventOutput(BaseModel):
    calendar_summary: str = ""
    events: list[CalendarEvent] = Field(default_factory=list)
    positions_at_event_risk: list[PositionAtRisk] = Field(default_factory=list)
    macro_window: list[MacroWindowItem] = Field(default_factory=list)


class EventAgent(LLMAgent):
    """Knows what is scheduled, and usually recommends avoiding it."""

    name = "event_agent"
    prompt_name = "event_agent"
    output_model = EventOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "regime": kwargs.get("regime"),
            "as_of": kwargs.get("as_of"),
            "tickers": list((kwargs.get("snapshot") or {}).keys()),
            "calendar": kwargs.get("calendar", []),
            "positions": kwargs.get("positions", []),
            "event_window_days": self.settings.regime.thresholds.event_window_days,
            "reminder": "Never fabricate a date. Mark confidence 'low' when unsure.",
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Offline: report only calendar entries actually supplied to the desk."""
        window = int(context.get("event_window_days", 3))
        calendar = context.get("calendar") or []
        events = []

        for entry in calendar:
            days_away = int(entry.get("days_away", 999))
            recommendation = "avoid_until_after" if 0 <= days_away <= window else "no_action"
            events.append(
                {
                    "ticker": str(entry.get("ticker", "")).upper(),
                    "event_type": str(entry.get("event_type", entry.get("type", "earnings"))),
                    "date": str(entry.get("date", "")),
                    "days_away": days_away,
                    "confidence": str(entry.get("confidence", "medium")),
                    "implied_move_pct": float(entry.get("implied_move_pct", 0.0) or 0.0),
                    "historical_avg_move_pct": float(entry.get("historical_avg_move_pct", 0.0) or 0.0),
                    "implied_vs_historical": "fair",
                    "recommendation": recommendation,
                    "proposed_structure": {
                        "name": "stand_aside" if recommendation == "avoid_until_after" else "",
                        "target_dte": 0,
                        "rationale": (
                            f"binary event in {days_away}d — capital preservation beats a coin flip"
                            if recommendation == "avoid_until_after"
                            else "event is outside the risk window"
                        ),
                    },
                }
            )

        at_risk = []
        near_tickers = {e["ticker"] for e in events if e["recommendation"] == "avoid_until_after"}
        for position in context.get("positions") or []:
            underlying = str(position.get("underlying", "")).upper()
            if underlying in near_tickers:
                event = next(e for e in events if e["ticker"] == underlying)
                at_risk.append(
                    {
                        "symbol": str(position.get("symbol", "")),
                        "event": event["event_type"],
                        "date": event["date"],
                        "recommended_action": "hedge",
                    }
                )

        return {
            "calendar_summary": (
                f"{len(events)} scheduled event(s) supplied; "
                f"{len(near_tickers)} inside the {window}-day risk window. "
                "Offline mode reports only events explicitly provided — none are inferred."
            ),
            "events": events,
            "positions_at_event_risk": at_risk,
            "macro_window": [],
        }
