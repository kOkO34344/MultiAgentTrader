"""Critic / Investment Committee — selects, reshapes, or kills every proposal.

Rewarded for rejecting. A cycle that approves nothing and explains why clearly
is a good cycle; the Risk Guard downstream is a backstop, not the first line of
defence.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent
from desk.utils.logging import get_logger

logger = get_logger("agents.critic")

REGIME_BIAS = {
    "trend_up": "bullish",
    "trend_down": "bearish",
    "range": "neutral",
    "high_vol_event": "volatility",
}

# Which playbook families are coherent with which regime.
REGIME_PLAYBOOK_FAMILIES = {
    "trend_up": {"vertical_debit", "vertical_credit", "diagonal"},
    "trend_down": {"vertical_debit", "vertical_credit", "diagonal", "hedge"},
    "range": {"iron_condor", "iron_butterfly", "vertical_credit"},
    "high_vol_event": {"straddle", "strangle", "butterfly", "hedge", "no_trade"},
}


class Disagreement(BaseModel):
    topic: str = ""
    bull_case: str = ""
    bear_case: str = ""
    resolution: str = ""


class Decision(BaseModel):
    structure_id: str = ""
    decision: str = "reject"
    reason_code: str = "LOW_CONVICTION"
    reason: str = ""
    conviction: float = 0.0
    supporting_agents: list[str] = Field(default_factory=list)
    dissenting_agents: list[str] = Field(default_factory=list)


class ApprovedLeg(BaseModel):
    contract_symbol: str = ""
    side: str = "buy"
    qty: float = 1.0
    limit_price: float | None = None


class ExitPlan(BaseModel):
    profit_target_pct: float = 0.5
    stop_loss_multiple: float = 2.0
    time_stop_dte: int = 10


class ApprovedTrade(BaseModel):
    trade_id: str = ""
    ticker: str = ""
    playbook: str = ""
    legs: list[ApprovedLeg] = Field(default_factory=list)
    net_price: float = 0.0
    net_side: str = "credit"
    estimated_notional: float = 0.0
    max_loss: float = 0.0
    max_profit: float | None = None
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    net_theta: float = 0.0
    days_to_expiry: int = 30
    thesis: str = ""
    exit_plan: ExitPlan = Field(default_factory=ExitPlan)


class CriticOutput(BaseModel):
    committee_view: str = ""
    regime_context: str = "range"
    notable_disagreements: list[Disagreement] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    approved_trades_summary: list[ApprovedTrade] = Field(default_factory=list)


class CriticCommittee(LLMAgent):
    """The adult in the room."""

    name = "critic"
    prompt_name = "critic"
    output_model = CriticOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "regime": kwargs.get("regime"),
            "regime_guidance": kwargs.get("regime_guidance", ""),
            "as_of": kwargs.get("as_of"),
            "structures": kwargs.get("structures", []),
            "agent_views": kwargs.get("agent_views", {}),
            "portfolio": kwargs.get("portfolio", {}),
            "constraints": {
                "max_approved_trades": self.settings.critic.max_approved_trades,
                "min_conviction": self.settings.critic.min_conviction,
                "max_structures_per_ticker": self.settings.critic.max_structures_per_ticker,
                "reject_if_regime_mismatch": self.settings.critic.reject_if_regime_mismatch,
            },
        }

    # -- deterministic scoring (also used to sanity-check LLM output) -------

    def score_structure(
        self, structure: dict[str, Any], agent_views: dict[str, Any], regime: str
    ) -> tuple[float, list[str], list[str], list[tuple[str, str]]]:
        """Score a structure on corroboration, expectancy, and regime fit.

        Returns ``(conviction, supporters, dissenters, problems)`` where each
        problem is a ``(reason_code, message)`` pair. Codes are assigned at the
        point the problem is detected rather than inferred from the message —
        inferring them once matched "event" inside "high_vol_event" and
        mislabelled every regime mismatch as a binary-event veto.
        """
        ticker = structure.get("ticker", "")
        supporters: list[str] = []
        dissenters: list[str] = []
        problems: list[tuple[str, str]] = []
        score = 0.35  # base credit for being a valid, priceable structure

        # --- regime alignment ---------------------------------------------
        family = structure.get("playbook_type", "")
        allowed = REGIME_PLAYBOOK_FAMILIES.get(regime, set())
        if allowed and family and family not in allowed:
            problems.append(
                ("REGIME_MISMATCH", f"'{family}' structures do not belong in a '{regime}' regime")
            )
        else:
            score += 0.10

        # --- technical corroboration --------------------------------------
        technical = (agent_views.get("technical_analyst") or {}).get("per_ticker_analysis", [])
        for analysis in technical:
            if analysis.get("ticker") != ticker:
                continue
            if analysis.get("conviction", 0) >= 0.5 and analysis.get("setup_type") != "no_setup":
                supporters.append("technical_analyst")
                score += 0.20 * float(analysis["conviction"])
            elif analysis.get("setup_type") == "no_setup":
                dissenters.append("technical_analyst")
                score -= 0.05

        # --- fundamental corroboration ------------------------------------
        for candidate in (agent_views.get("fundamental_analyst") or {}).get("candidates", []):
            if candidate.get("ticker") == ticker and candidate.get("conviction", 0) >= 0.5:
                supporters.append("fundamental_analyst")
                score += 0.15

        # --- sentiment / event veto ---------------------------------------
        for sentiment in (agent_views.get("sentiment_analyst") or {}).get("per_ticker_sentiment", []):
            if sentiment.get("ticker") != ticker:
                continue
            if sentiment.get("binary_event_risk") and structure.get("net_side") == "credit":
                dissenters.append("sentiment_analyst")
                problems.append(
                    ("BINARY_EVENT", "binary event risk inside a short-premium structure")
                )
            elif sentiment.get("impact_on_options_idea") == "veto":
                dissenters.append("sentiment_analyst")
                problems.append(
                    ("BINARY_EVENT", f"sentiment veto: {sentiment.get('impact_reason', '')}")
                )

        for event in (agent_views.get("event_agent") or {}).get("events", []):
            if event.get("ticker") == ticker and event.get("recommendation") == "avoid_until_after":
                dissenters.append("event_agent")
                problems.append(
                    ("BINARY_EVENT", f"event agent advises standing aside ({event.get('date')})")
                )

        # --- expectancy ------------------------------------------------------
        # Judge risk/reward *against win probability*, never on its own. A
        # 0.25 R:R iron condor is a good trade at an 80% win rate and a bad one
        # at 50%; a flat R:R floor would reject the desk's core strategy.
        profile = structure.get("risk_profile", {})
        risk_reward = profile.get("risk_reward")
        pop = profile.get("probability_of_profit")
        edge = profile.get("expectancy")

        if profile.get("unbounded_loss"):
            problems.append(("UNDEFINED_RISK", "unbounded loss"))

        if edge is not None:
            max_loss = profile.get("max_loss") or 1.0
            if edge <= 0:
                problems.append(
                    (
                        "NEGATIVE_EXPECTANCY",
                        f"negative expectancy (${edge:,.0f} at a {pop:.0%} win rate)"
                        if pop is not None
                        else f"negative expectancy (${edge:,.0f})",
                    )
                )
            else:
                # Reward edge relative to capital at risk, capped so a single
                # rich-looking structure cannot dominate the ranking.
                score += min(edge / max_loss, 0.25)
        elif risk_reward is not None and risk_reward < 0.4:
            # No probability estimate available — fall back to raw risk/reward.
            problems.append(
                ("POOR_RR", f"risk/reward {risk_reward:.2f} is too thin and no win rate was computable")
            )
        elif profile.get("unbounded_profit"):
            score += 0.10  # convexity trades have no finite max profit by design

        # --- complexity penalty -------------------------------------------
        if len(structure.get("legs", [])) > 4:
            problems.append(("TOO_COMPLEX", "more than four legs — excess complexity and slippage"))

        return round(max(0.0, min(score, 1.0)), 3), sorted(set(supporters)), sorted(set(dissenters)), problems

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Deterministic committee: score, veto, deduplicate, and cap."""
        regime = context.get("regime", "range")
        constraints = context.get("constraints", {})
        min_conviction = float(constraints.get("min_conviction", 0.55))
        max_trades = int(constraints.get("max_approved_trades", 3))
        max_per_ticker = int(constraints.get("max_structures_per_ticker", 1))
        agent_views = context.get("agent_views", {})

        scored = []
        for structure in context.get("structures", []):
            conviction, supporters, dissenters, problems = self.score_structure(
                structure, agent_views, regime
            )
            scored.append((conviction, supporters, dissenters, problems, structure))
        scored.sort(key=lambda row: row[0], reverse=True)

        decisions: list[dict[str, Any]] = []
        approved: list[dict[str, Any]] = []
        per_ticker: dict[str, int] = {}

        for conviction, supporters, dissenters, problems, structure in scored:
            ticker = structure.get("ticker", "")
            structure_id = structure.get("structure_id", "")

            if problems:
                code, message = problems[0]
                decisions.append(
                    self._decision(
                        structure_id, "reject", code, message,
                        conviction, supporters, dissenters,
                    )
                )
                continue
            if conviction < min_conviction:
                decisions.append(
                    self._decision(
                        structure_id, "reject", "LOW_CONVICTION",
                        f"conviction {conviction:.2f} below the {min_conviction:.2f} floor",
                        conviction, supporters, dissenters,
                    )
                )
                continue
            if per_ticker.get(ticker, 0) >= max_per_ticker:
                decisions.append(
                    self._decision(
                        structure_id, "reject", "REDUNDANT",
                        f"{ticker} already has a structure this cycle",
                        conviction, supporters, dissenters,
                    )
                )
                continue
            if len(approved) >= max_trades:
                decisions.append(
                    self._decision(
                        structure_id, "reject", "CONCENTRATION",
                        f"cycle cap of {max_trades} approved trades reached",
                        conviction, supporters, dissenters,
                    )
                )
                continue

            decisions.append(
                self._decision(
                    structure_id, "approve", "APPROVED",
                    f"regime-aligned, corroborated by {len(supporters)} agent(s)",
                    conviction, supporters, dissenters,
                )
            )
            approved.append(self._to_trade(structure, conviction, supporters))
            per_ticker[ticker] = per_ticker.get(ticker, 0) + 1

        disagreements = [
            {
                "topic": f"{structure.get('ticker')} {structure.get('playbook')}",
                "bull_case": f"supported by {', '.join(supporters)}",
                "bear_case": f"opposed by {', '.join(dissenters)}",
                "resolution": problems[0][1] if problems else "approved despite dissent",
            }
            for conviction, supporters, dissenters, problems, structure in scored
            if supporters and dissenters
        ]

        return {
            "committee_view": (
                f"Reviewed {len(scored)} structure(s) in a '{regime}' regime; approved "
                f"{len(approved)}. Deterministic scoring on regime fit, cross-agent "
                "corroboration, risk/reward, and complexity."
            ),
            "regime_context": regime,
            "notable_disagreements": disagreements,
            "decisions": decisions,
            "approved_trades_summary": approved,
        }

    @staticmethod
    def _decision(
        structure_id: str,
        decision: str,
        code: str,
        reason: str,
        conviction: float,
        supporters: list[str],
        dissenters: list[str],
    ) -> dict[str, Any]:
        return {
            "structure_id": structure_id,
            "decision": decision,
            "reason_code": code,
            "reason": reason,
            "conviction": conviction,
            "supporting_agents": supporters,
            "dissenting_agents": dissenters,
        }

    def _to_trade(
        self, structure: dict[str, Any], conviction: float, supporters: list[str]
    ) -> dict[str, Any]:
        """Normalise an approved structure into an execution-ready trade spec."""
        profile = structure.get("risk_profile", {})
        exits = structure.get("exit_plan", {}) or {}
        qty = max(1, int(structure.get("sizing", {}).get("max_contracts", 1) or 1))

        return {
            "trade_id": structure.get("structure_id", ""),
            "ticker": structure.get("ticker", ""),
            "playbook": structure.get("playbook", ""),
            "legs": [
                {
                    "contract_symbol": leg["contract_symbol"],
                    "side": leg["side"],
                    "qty": float(leg.get("qty", 1)),
                    "limit_price": leg.get("mid_price"),
                }
                for leg in structure.get("legs", [])
            ],
            "net_price": structure.get("net_price", 0.0),
            "net_side": structure.get("net_side", "credit"),
            "estimated_notional": round((profile.get("max_loss") or 0.0) * qty, 2),
            "max_loss": round((profile.get("max_loss") or 0.0) * qty, 2),
            "max_profit": (
                round(profile["max_profit"] * qty, 2) if profile.get("max_profit") else None
            ),
            "net_delta": profile.get("net_delta", 0.0) * qty,
            "net_gamma": profile.get("net_gamma", 0.0) * qty,
            "net_vega": profile.get("net_vega", 0.0) * qty,
            "net_theta": profile.get("net_theta", 0.0) * qty,
            "days_to_expiry": structure.get("dte", 30),
            "thesis": (
                f"{structure.get('playbook')} on {structure.get('ticker')}; "
                f"conviction {conviction:.2f}; supported by {', '.join(supporters) or 'structure quality alone'}"
            ),
            "exit_plan": {
                "profit_target_pct": exits.get("profit_target_pct", 0.5),
                "stop_loss_multiple": exits.get("stop_loss_multiple", 2.0),
                "time_stop_dte": exits.get("time_stop_dte", 10),
            },
        }
