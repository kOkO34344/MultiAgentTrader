"""Regime classification: the deterministic rules and the LLM override gate."""

from __future__ import annotations

import pytest

from desk.agents.base import AgentResult
from desk.agents.regime_agent import (
    VALID_LABELS,
    RegimeAgent,
    classify_regime_deterministic,
)


def signals(**overrides):
    base = {
        "adx": 15.0,
        "ema_slope": 0.0001,
        "ema_fast_above_slow": True,
        "bollinger_bandwidth": 0.03,
        "atr_pct": 0.008,
        "last_close": 500.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Deterministic classifier
# ---------------------------------------------------------------------------


def test_strong_uptrend_is_trend_up():
    result = classify_regime_deterministic(
        signals(adx=35.0, ema_slope=0.004, ema_fast_above_slow=True, bollinger_bandwidth=0.09),
        {"iv_rank": 0.3},
    )
    assert result.label == "trend_up"
    assert result.confidence > 0.6


def test_strong_downtrend_is_trend_down():
    result = classify_regime_deterministic(
        signals(adx=30.0, ema_slope=-0.004, ema_fast_above_slow=False, bollinger_bandwidth=0.09),
        {"iv_rank": 0.3},
    )
    assert result.label == "trend_down"


def test_compressed_quiet_tape_is_range():
    result = classify_regime_deterministic(signals(adx=12.0, bollinger_bandwidth=0.02), {"iv_rank": 0.3})
    assert result.label == "range"


def test_high_iv_rank_overrides_a_trend():
    """An event regime outranks direction: playbooks gate on this label."""
    result = classify_regime_deterministic(
        signals(adx=35.0, ema_slope=0.004, bollinger_bandwidth=0.09), {"iv_rank": 0.80}
    )
    assert result.label == "high_vol_event"


def test_wide_atr_overrides_a_trend():
    result = classify_regime_deterministic(
        signals(adx=35.0, ema_slope=0.004, atr_pct=0.03), {"iv_rank": 0.3}
    )
    assert result.label == "high_vol_event"


def test_imminent_event_overrides_a_trend():
    result = classify_regime_deterministic(
        signals(adx=35.0, ema_slope=0.004), {"iv_rank": 0.3}, days_to_next_event=2
    )
    assert result.label == "high_vol_event"
    assert any("event" in reason for reason in result.rationale)


def test_distant_event_does_not_trigger_the_event_regime():
    result = classify_regime_deterministic(
        signals(adx=35.0, ema_slope=0.004, bollinger_bandwidth=0.09), {"iv_rank": 0.3},
        days_to_next_event=30,
    )
    assert result.label == "trend_up"


def test_indecisive_tape_defaults_to_the_conservative_label():
    """Range playbooks are defined-risk, so ambiguity resolves there."""
    result = classify_regime_deterministic(signals(adx=15.0, bollinger_bandwidth=0.07), {})
    assert result.label == "range"
    assert result.confidence < 0.5


def test_adx_threshold_boundary():
    below = classify_regime_deterministic(signals(adx=21.9, ema_slope=0.004, bollinger_bandwidth=0.09), {})
    at = classify_regime_deterministic(signals(adx=22.0, ema_slope=0.004, bollinger_bandwidth=0.09), {})
    assert below.label != "trend_up"
    assert at.label == "trend_up"


def test_missing_signals_do_not_raise():
    result = classify_regime_deterministic({}, {})
    assert result.label in VALID_LABELS


def test_every_label_is_valid():
    cases = [
        (signals(adx=35.0, ema_slope=0.004, bollinger_bandwidth=0.09), {"iv_rank": 0.3}, None),
        (signals(adx=35.0, ema_slope=-0.004, ema_fast_above_slow=False, bollinger_bandwidth=0.09), {}, None),
        (signals(adx=10.0, bollinger_bandwidth=0.01), {}, None),
        (signals(), {"iv_rank": 0.9}, None),
    ]
    for indicators, iv, event in cases:
        assert classify_regime_deterministic(indicators, iv, event).label in VALID_LABELS


# ---------------------------------------------------------------------------
# Override gate
# ---------------------------------------------------------------------------


@pytest.fixture
def agent():
    return RegimeAgent()


def context_for(agent, **kwargs):
    return agent.build_context(
        indicators=signals(adx=12.0, bollinger_bandwidth=0.02), iv_summary={"iv_rank": 0.3}, **kwargs
    )


def llm_result(label: str, confidence: float, reason: str | None) -> AgentResult:
    return AgentResult(
        agent="regime_agent",
        mode="llm",
        output={"regime_label": label, "confidence": confidence, "override_reason": reason},
    )


def test_agreement_keeps_the_label(agent):
    context = context_for(agent)
    resolved = agent.resolve(llm_result("range", 0.9, None), context)
    assert resolved["regime"] == "range"
    assert resolved["override_accepted"] is False


def test_low_confidence_override_is_refused(agent):
    context = context_for(agent)
    resolved = agent.resolve(llm_result("trend_up", 0.60, "momentum is building"), context)
    assert resolved["regime"] == "range"
    assert resolved["override_accepted"] is False
    assert "below the" in resolved["resolution_note"]


def test_high_confidence_override_with_a_reason_is_accepted(agent):
    context = context_for(agent)
    resolved = agent.resolve(llm_result("trend_up", 0.95, "breadth thrust confirmed by NYSE advancers"), context)
    assert resolved["regime"] == "trend_up"
    assert resolved["override_accepted"] is True
    assert resolved["override_reason"]


def test_override_without_a_reason_is_refused(agent):
    context = context_for(agent)
    resolved = agent.resolve(llm_result("trend_up", 0.99, None), context)
    assert resolved["regime"] == "range"
    assert resolved["override_accepted"] is False


def test_invalid_label_is_refused(agent):
    context = context_for(agent)
    resolved = agent.resolve(llm_result("euphoric", 0.99, "vibes"), context)
    assert resolved["regime"] == "range"
    assert "invalid label" in resolved["resolution_note"]


def test_mock_mode_can_never_override(agent):
    """Only a real model gets override authority; the fallback must not."""
    context = context_for(agent)
    mock = AgentResult(
        agent="regime_agent", mode="mock",
        output={"regime_label": "trend_up", "confidence": 0.99, "override_reason": "confident"},
    )
    resolved = agent.resolve(mock, context)
    assert resolved["regime"] == "range"
    assert resolved["override_accepted"] is False


def test_classify_runs_end_to_end_offline(agent):
    resolved = agent.classify(
        indicators=signals(adx=30.0, ema_slope=0.004, bollinger_bandwidth=0.09),
        iv_summary={"iv_rank": 0.3},
    )
    assert resolved["regime"] == "trend_up"
    assert resolved["mode"] == "mock"
    assert resolved["playbook_guidance"]
