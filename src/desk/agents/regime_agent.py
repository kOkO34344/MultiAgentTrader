"""Regime classification — deterministic first, LLM second.

The Python classifier owns the authoritative label. The LLM adds narrative and
may override it only above a configured confidence threshold, and every override
is logged for the Coach to review. Indicators are stable but blind; the model is
perceptive but inconsistent. This split keeps the desk's regime stable by
default and flexible under genuine conviction.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from desk.agents.base import AgentResult, LLMAgent
from desk.utils.config_loader import Settings, get_settings
from desk.utils.logging import get_logger

logger = get_logger("agents.regime")

RegimeLabel = Literal["trend_up", "trend_down", "range", "high_vol_event"]

VALID_LABELS = ("trend_up", "trend_down", "range", "high_vol_event")


# ---------------------------------------------------------------------------
# Deterministic classifier
# ---------------------------------------------------------------------------


class RegimeSignals(BaseModel):
    """The raw inputs the deterministic classifier consumed."""

    adx: float | None = None
    ema_slope: float | None = None
    ema_fast_above_slow: bool | None = None
    bollinger_bandwidth: float | None = None
    atr_pct: float | None = None
    iv_rank: float | None = None
    days_to_next_event: int | None = None
    realised_vol_20d: float | None = None
    return_20d: float | None = None


class DeterministicRegime(BaseModel):
    """Output of the rules-based classifier."""

    label: str = "range"
    confidence: float = 0.5
    rationale: list[str] = Field(default_factory=list)
    signals: RegimeSignals = Field(default_factory=RegimeSignals)


def classify_regime_deterministic(
    indicators: dict[str, Any],
    iv_summary: dict[str, Any] | None = None,
    days_to_next_event: int | None = None,
    settings: Settings | None = None,
) -> DeterministicRegime:
    """Classify the market regime from computed signals only.

    ``high_vol_event`` deliberately outranks the directional labels: a trending
    market three days before CPI is an event market, and the playbooks that gate
    on this label need to know that first.
    """
    settings = settings or get_settings()
    thresholds = settings.regime.thresholds
    iv_summary = iv_summary or {}

    signals = RegimeSignals(
        adx=indicators.get("adx"),
        ema_slope=indicators.get("ema_slope"),
        ema_fast_above_slow=indicators.get("ema_fast_above_slow"),
        bollinger_bandwidth=indicators.get("bollinger_bandwidth"),
        atr_pct=indicators.get("atr_pct"),
        iv_rank=iv_summary.get("iv_rank"),
        days_to_next_event=days_to_next_event,
        realised_vol_20d=indicators.get("realised_vol_20d"),
        return_20d=indicators.get("return_20d"),
    )
    rationale: list[str] = []

    # 1. Event / high-volatility regime overrides everything else.
    event_reasons = []
    if signals.iv_rank is not None and signals.iv_rank >= thresholds.high_vol_iv_rank_min:
        event_reasons.append(
            f"IV rank {signals.iv_rank:.2f} >= {thresholds.high_vol_iv_rank_min:.2f}"
        )
    if signals.atr_pct is not None and signals.atr_pct >= thresholds.high_vol_atr_pct_min:
        event_reasons.append(
            f"ATR {signals.atr_pct:.2%} of spot >= {thresholds.high_vol_atr_pct_min:.2%}"
        )
    if (
        days_to_next_event is not None
        and 0 <= days_to_next_event <= thresholds.event_window_days
    ):
        event_reasons.append(f"binary event in {days_to_next_event}d")

    if event_reasons:
        return DeterministicRegime(
            label="high_vol_event",
            confidence=min(0.55 + 0.15 * len(event_reasons), 0.95),
            rationale=event_reasons,
            signals=signals,
        )

    # 2. Trend: strength (ADX) and direction (EMA slope) must agree.
    adx_value = signals.adx or 0.0
    slope = signals.ema_slope or 0.0
    trending = adx_value >= thresholds.trend_adx_min
    directional = abs(slope) >= thresholds.trend_ema_slope_min

    if trending and directional:
        up = slope > 0
        if signals.ema_fast_above_slow is not None:
            up = signals.ema_fast_above_slow and slope > 0
        rationale.append(f"ADX {adx_value:.1f} >= {thresholds.trend_adx_min:.1f}")
        rationale.append(f"EMA slope {slope:+.5f} confirms direction")
        strength = min((adx_value - thresholds.trend_adx_min) / 25.0, 1.0)
        return DeterministicRegime(
            label="trend_up" if up else "trend_down",
            confidence=round(min(0.60 + 0.30 * strength, 0.92), 3),
            rationale=rationale,
            signals=signals,
        )

    # 3. Range: compressed bandwidth and no directional strength.
    bandwidth = signals.bollinger_bandwidth
    if bandwidth is not None and bandwidth <= thresholds.range_bandwidth_max:
        rationale.append(
            f"Bollinger bandwidth {bandwidth:.4f} <= {thresholds.range_bandwidth_max:.4f}"
        )
        rationale.append(f"ADX {adx_value:.1f} shows no trend strength")
        return DeterministicRegime(
            label="range",
            confidence=round(min(0.60 + 0.25 * (1 - bandwidth / thresholds.range_bandwidth_max), 0.90), 3),
            rationale=rationale,
            signals=signals,
        )

    # 4. Indecisive tape. Default to `range` — the most conservative label,
    #    since its playbooks are defined-risk and delta-neutral.
    rationale.append(
        f"no threshold met (ADX {adx_value:.1f}, slope {slope:+.5f}, bandwidth {bandwidth})"
    )
    rationale.append("defaulting to 'range' as the conservative label")
    return DeterministicRegime(label="range", confidence=0.45, rationale=rationale, signals=signals)


# ---------------------------------------------------------------------------
# LLM overlay
# ---------------------------------------------------------------------------


class TransitionRisk(BaseModel):
    to_regime: str = "range"
    probability: float = 0.0
    trigger: str = ""


class RegimeOutput(BaseModel):
    """Schema the Regime Agent must return."""

    regime_summary: str = ""
    regime_label: str = "range"
    confidence: float = 0.5
    agrees_with_deterministic: bool = True
    override_reason: str | None = None
    supporting_metrics: RegimeSignals = Field(default_factory=RegimeSignals)
    macro_context: str = ""
    transition_risk: TransitionRisk = Field(default_factory=TransitionRisk)
    playbook_guidance: str = ""


class RegimeAgent(LLMAgent):
    """Hybrid regime classifier."""

    name = "regime_agent"
    prompt_name = "regime_agent"
    output_model = RegimeOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        indicators = kwargs.get("indicators") or {}
        iv_summary = kwargs.get("iv_summary") or {}
        days_to_next_event = kwargs.get("days_to_next_event")

        deterministic = classify_regime_deterministic(
            indicators, iv_summary, days_to_next_event, self.settings
        )
        return {
            "benchmark": self.settings.regime.benchmark,
            "as_of": kwargs.get("as_of"),
            "indicators": indicators,
            "iv_summary": iv_summary,
            "days_to_next_event": days_to_next_event,
            "deterministic_classification": deterministic.model_dump(),
            "thresholds": self.settings.regime.thresholds.model_dump(),
            "valid_labels": list(VALID_LABELS),
            "override_policy": (
                "The deterministic label stands unless your confidence exceeds "
                f"{self.settings.regime.llm_override_confidence:.2f} AND you give a concrete, "
                "falsifiable reason. Every override is logged and reviewed."
            ),
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Offline output: echo the deterministic classification as-is."""
        deterministic = context.get("deterministic_classification", {})
        label = deterministic.get("label", "range")
        rationale = deterministic.get("rationale", [])
        guidance = {
            "trend_up": "Bullish defined-risk: bull call spreads when IV is cheap, put credit spreads when rich.",
            "trend_down": "Bearish defined-risk: bear put spreads, call credit spreads, protective overlays.",
            "range": "Delta-neutral premium harvesting: iron condors and short vertical spreads at the band edges.",
            "high_vol_event": "Buy convexity or stand aside. Never sell undefined-risk premium into a binary event.",
        }
        return {
            "regime_summary": (
                f"Deterministic classifier reports '{label}' "
                f"(confidence {deterministic.get('confidence', 0.5):.2f}). "
                + ("Drivers: " + "; ".join(rationale) + "." if rationale else "")
                + " No Claude key configured — running the rules-based classifier alone."
            ),
            "regime_label": label,
            "confidence": deterministic.get("confidence", 0.5),
            "agrees_with_deterministic": True,
            "override_reason": None,
            "supporting_metrics": deterministic.get("signals", {}),
            "macro_context": "Offline mode: no macro narrative available.",
            "transition_risk": {
                "to_regime": "high_vol_event" if label != "high_vol_event" else "range",
                "probability": 0.25,
                "trigger": "An IV-rank or ATR expansion past the configured high-vol thresholds.",
            },
            "playbook_guidance": guidance.get(label, guidance["range"]),
        }

    def resolve(self, result: AgentResult, context: dict[str, Any]) -> dict[str, Any]:
        """Reconcile the LLM's label with the deterministic one.

        Returns the final regime decision, including whether an override was
        accepted and why. This is the only place the label can change.
        """
        deterministic = context.get("deterministic_classification", {})
        det_label = deterministic.get("label", "range")
        threshold = self.settings.regime.llm_override_confidence

        output = result.output or {}
        llm_label = output.get("regime_label", det_label)
        confidence = float(output.get("confidence", 0.0) or 0.0)
        reason = output.get("override_reason")

        final_label, override_accepted, note = det_label, False, ""

        if llm_label not in VALID_LABELS:
            note = f"LLM returned invalid label '{llm_label}'; keeping deterministic label."
        elif llm_label == det_label:
            note = "LLM agrees with the deterministic classifier."
        elif result.mode != "llm":
            note = "Fallback mode — deterministic label stands."
        elif confidence < threshold:
            note = (
                f"LLM proposed '{llm_label}' at confidence {confidence:.2f}, below the "
                f"{threshold:.2f} override threshold. Deterministic label stands."
            )
        elif not reason:
            note = f"LLM proposed '{llm_label}' without an override reason. Rejected."
        else:
            final_label, override_accepted = llm_label, True
            note = f"OVERRIDE ACCEPTED: '{det_label}' -> '{llm_label}' at confidence {confidence:.2f}."
            logger.warning(
                "regime_override",
                extra={
                    "event": "regime_override",
                    "from": det_label,
                    "to": llm_label,
                    "confidence": confidence,
                    "reason": reason,
                },
            )

        return {
            "regime": final_label,
            "deterministic_label": det_label,
            "deterministic_confidence": deterministic.get("confidence", 0.5),
            "llm_label": llm_label,
            "llm_confidence": confidence,
            "override_accepted": override_accepted,
            "override_reason": reason if override_accepted else None,
            "resolution_note": note,
            "summary": output.get("regime_summary", ""),
            "playbook_guidance": output.get("playbook_guidance", ""),
            "transition_risk": output.get("transition_risk", {}),
            "signals": deterministic.get("signals", {}),
            "rationale": deterministic.get("rationale", []),
            "mode": result.mode,
        }

    def classify(self, **kwargs: Any) -> dict[str, Any]:
        """Full classification: build context, run the agent, resolve the label."""
        context = self.build_context(**kwargs)
        result = self.run(**kwargs)
        return self.resolve(result, context)
