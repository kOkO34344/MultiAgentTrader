"""Fundamental Analyst — business quality and catalysts as defined-risk options."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent


class StructureHint(BaseModel):
    name: str = ""
    rationale: str = ""
    target_dte: int = 30
    notes: str = ""


class FundamentalCandidate(BaseModel):
    ticker: str
    direction: str = "neutral"
    conviction: float = 0.0
    thesis: str = ""
    horizon_days: int = 30
    data_points: list[str] = Field(default_factory=list)
    options_structure: StructureHint = Field(default_factory=StructureHint)
    risks: list[str] = Field(default_factory=list)
    invalidation_level: float = 0.0


class FundamentalOutput(BaseModel):
    universe_summary: str = ""
    candidates: list[FundamentalCandidate] = Field(default_factory=list)
    abstentions: list[str] = Field(default_factory=list)


class FundamentalAnalyst(LLMAgent):
    """The desk's slowest-moving voice: weeks, not minutes."""

    name = "fundamental_analyst"
    prompt_name = "fundamental_analyst"
    output_model = FundamentalOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "regime": kwargs.get("regime"),
            "as_of": kwargs.get("as_of"),
            "max_candidates": self.settings.agents.max_candidates_per_agent,
            "horizon_guidance": "Do not express a multi-quarter thesis in a 7-DTE structure.",
            "tickers": kwargs.get("snapshot", {}),
            "calendar": kwargs.get("calendar", []),
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Offline mode has no fundamentals feed, so it abstains honestly.

        The persona forbids inventing data points; a mock that fabricated
        valuation metrics would be worse than no opinion at all.
        """
        tickers = sorted((context.get("tickers") or {}).keys())
        return {
            "universe_summary": (
                "No fundamentals data source is configured (offline mode). "
                "This agent abstains rather than fabricate valuation or earnings data."
            ),
            "candidates": [],
            "abstentions": [
                f"{ticker}: no fundamentals coverage available offline" for ticker in tickers
            ],
        }
