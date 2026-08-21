"""Post-Trade Coach — reviews process, not just outcomes.

Judges whether the desk followed its own reasoning, and proposes concrete,
addressable adjustments. It is explicitly forbidden from proposing looser risk
limits: performance problems are solved upstream of the guard, never by moving
the guard.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent
from desk.utils.time_utils import today_et


class ReviewReport(BaseModel):
    period: str = ""
    trades_reviewed: int = 0
    pnl_realised: float = 0.0
    pnl_unrealised: float = 0.0
    hit_rate: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    process_score: float = 0.0
    summary: str = ""


class TradeScore(BaseModel):
    trade_id: str = ""
    ticker: str = ""
    playbook: str = ""
    pnl: float = 0.0
    thesis_accuracy: float = 0.0
    structure_fit: float = 0.0
    execution_quality: float = 0.0
    exit_discipline: float = 0.0
    rule_compliance: str = "clean"
    verdict: str = "good_process_good_outcome"
    comment: str = ""


class AgentCalibration(BaseModel):
    agent: str = ""
    calls: int = 0
    hit_rate: float = 0.0
    note: str = ""


class ProposedAdjustment(BaseModel):
    target: str = ""
    key: str = ""
    from_value: str = ""
    to_value: str = ""
    rationale: str = ""


class CoachOutput(BaseModel):
    review_report: ReviewReport = Field(default_factory=ReviewReport)
    trade_scores: list[TradeScore] = Field(default_factory=list)
    agent_calibration: list[AgentCalibration] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    lessons_for_tomorrow: list[str] = Field(default_factory=list)
    proposed_adjustments: list[ProposedAdjustment] = Field(default_factory=list)


class CoachAgent(LLMAgent):
    """Daily and weekly post-trade review."""

    name = "coach"
    prompt_name = "coach_agent"
    output_model = CoachOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "period": kwargs.get("period", today_et().isoformat()),
            "trades": kwargs.get("trades", []),
            "decision_traces": kwargs.get("traces", []),
            "metrics": kwargs.get("metrics", {}),
            "risk_rejections": kwargs.get("risk_rejections", []),
            "account": kwargs.get("account", {}),
            "positions": kwargs.get("positions", []),
            "constraint": (
                "Never propose loosening a risk limit. Fix the proposal upstream instead."
            ),
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Deterministic review computed from the logged record."""
        trades = context.get("trades") or []
        rejections = context.get("risk_rejections") or []
        metrics = context.get("metrics") or {}

        closed = [t for t in trades if t.get("status") in {"closed", "filled"} and t.get("pnl") is not None]
        wins = [t for t in closed if float(t.get("pnl", 0)) > 0]
        losses = [t for t in closed if float(t.get("pnl", 0)) <= 0]

        realised = sum(float(t.get("pnl", 0) or 0) for t in closed)
        hit_rate = len(wins) / len(closed) if closed else 0.0
        avg_win = sum(float(t["pnl"]) for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(float(t["pnl"]) for t in losses) / len(losses) if losses else 0.0

        scores = []
        for trade in closed:
            pnl = float(trade.get("pnl", 0) or 0)
            resized = "RESIZED" in (trade.get("risk_reason_codes") or [])
            followed_plan = bool(trade.get("exit_reason")) and trade.get("exit_reason") != "manual"
            scores.append(
                {
                    "trade_id": str(trade.get("trade_id", "")),
                    "ticker": str(trade.get("ticker", "")),
                    "playbook": str(trade.get("playbook", "")),
                    "pnl": round(pnl, 2),
                    # Offline scoring is process-only: thesis accuracy needs
                    # judgement the rules engine does not have.
                    "thesis_accuracy": 0.0,
                    "structure_fit": 0.6 if trade.get("playbook") else 0.0,
                    "execution_quality": self._execution_score(trade),
                    "exit_discipline": 0.8 if followed_plan else 0.3,
                    "rule_compliance": "resized" if resized else "clean",
                    "verdict": (
                        "good_process_good_outcome"
                        if pnl > 0 and followed_plan
                        else "good_process_bad_outcome"
                        if followed_plan
                        else "bad_process_bad_outcome"
                        if pnl <= 0
                        else "bad_process_good_outcome"
                    ),
                    "comment": (
                        f"{'Win' if pnl > 0 else 'Loss'} of ${pnl:,.2f}; "
                        f"exit plan {'honoured' if followed_plan else 'not honoured'}."
                    ),
                }
            )

        process_score = (
            sum(s["exit_discipline"] for s in scores) / len(scores) if scores else 0.0
        )

        patterns: list[str] = []
        by_playbook: dict[str, list[float]] = {}
        for trade in closed:
            by_playbook.setdefault(str(trade.get("playbook", "unknown")), []).append(
                float(trade.get("pnl", 0) or 0)
            )
        for playbook, pnls in sorted(by_playbook.items()):
            total = sum(pnls)
            note = "one observation, not a pattern" if len(pnls) < 3 else "sample large enough to act on"
            patterns.append(f"{playbook}: {len(pnls)} trade(s), net ${total:,.2f} ({note})")

        lessons: list[str] = []
        if rejections:
            codes = sorted({code for r in rejections for code in (r.get("reason_codes") or [])})
            lessons.append(
                f"Risk Guard rejected {len(rejections)} proposal(s) ({', '.join(codes)}). "
                "Check whether the proposals were oversized or the sizing rule is wrong."
            )
        if losses and avg_loss and avg_win and abs(avg_loss) > avg_win:
            lessons.append(
                f"Average loser (${abs(avg_loss):,.2f}) exceeds average winner (${avg_win:,.2f}) — "
                "tighten stop discipline before adding size."
            )
        if not closed:
            lessons.append("No closed trades to learn from. Judge the process, not the P&L.")

        return {
            "review_report": {
                "period": str(context.get("period", "")),
                "trades_reviewed": len(closed),
                "pnl_realised": round(realised, 2),
                "pnl_unrealised": round(float(metrics.get("unrealised_pnl", 0.0) or 0.0), 2),
                "hit_rate": round(hit_rate, 3),
                "avg_winner": round(avg_win, 2),
                "avg_loser": round(avg_loss, 2),
                "process_score": round(process_score, 3),
                "summary": (
                    f"{len(closed)} closed trade(s), {len(rejections)} risk rejection(s). "
                    f"Realised P&L ${realised:,.2f}, hit rate {hit_rate:.0%}. "
                    "Deterministic offline review — process metrics only, no thesis scoring."
                ),
            },
            "trade_scores": scores,
            "agent_calibration": [],
            "patterns": patterns,
            "lessons_for_tomorrow": lessons[:5],
            "proposed_adjustments": [],
        }

    @staticmethod
    def _execution_score(trade: dict[str, Any]) -> float:
        """Fill quality versus the mid at decision time."""
        intended = trade.get("intended_price")
        filled = trade.get("fill_price")
        if not intended or not filled:
            return 0.5
        slippage = abs(float(filled) - float(intended)) / max(abs(float(intended)), 0.01)
        return round(max(0.0, 1.0 - min(slippage * 4, 1.0)), 3)
