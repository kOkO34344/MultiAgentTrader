"""Agent behaviour: output contracts, offline determinism, and degradation."""

from __future__ import annotations

import asyncio

import pytest
from tests.conftest import make_chain

from desk.agents.base import LLMAgent
from desk.agents.coach_agent import CoachAgent
from desk.agents.critic_committee import CriticCommittee
from desk.agents.event_agent import EventAgent
from desk.agents.fundamental_analyst import FundamentalAnalyst
from desk.agents.sentiment_analyst import SentimentAnalyst
from desk.agents.storyteller_agent import StorytellerAgent
from desk.agents.technical_analyst import TechnicalAnalyst
from desk.agents.vol_options_strategist import (
    StructureBuilder,
    VolOptionsStrategist,
    net_greeks,
    payoff_at,
    risk_profile,
)
from desk.utils.config_loader import playbooks_for_regime

SNAPSHOT = {
    "SPY": {
        "indicators": {
            "last_close": 580.0, "adx": 30.0, "ema_slope": 0.003, "atr": 5.0,
            "atr_pct": 0.0086, "bollinger_bandwidth": 0.05, "return_20d": 0.03,
            "pct_of_52w_range": 0.88, "realised_vol_20d": 0.15,
        },
        "spot": 580.0,
        "vol_surface": {"iv_rank": 0.55, "atm_iv": 0.22, "term_structure": "contango"},
    },
    "QQQ": {
        "indicators": {
            "last_close": 500.0, "adx": 12.0, "ema_slope": 0.0001, "atr": 6.0,
            "atr_pct": 0.012, "bollinger_bandwidth": 0.02, "return_20d": 0.001,
            "pct_of_52w_range": 0.55, "realised_vol_20d": 0.18,
        },
        "spot": 500.0,
        "vol_surface": {"iv_rank": 0.35, "atm_iv": 0.20, "term_structure": "flat"},
    },
}


# ---------------------------------------------------------------------------
# Base behaviour
# ---------------------------------------------------------------------------


def test_agents_run_offline_without_an_api_key():
    result = TechnicalAnalyst().run(snapshot=SNAPSHOT, regime="trend_up")
    assert result.ok is True
    assert result.mode == "mock"
    assert result.abstained is False


def test_mock_output_is_deterministic():
    """Reproducibility is what makes the offline pipeline testable at all."""
    first = TechnicalAnalyst().run(snapshot=SNAPSHOT, regime="trend_up")
    second = TechnicalAnalyst().run(snapshot=SNAPSHOT, regime="trend_up")
    assert first.output == second.output


def test_a_broken_context_becomes_an_abstention_not_a_crash():
    class Broken(LLMAgent):
        name = "broken"

        def build_context(self, **kwargs):
            raise RuntimeError("upstream data is malformed")

    result = Broken().run()
    assert result.ok is False
    assert result.mode == "error"
    assert result.abstained is True
    assert "malformed" in result.error


def test_a_refusal_falls_back_rather_than_raising(settings, monkeypatch):
    """A model refusal is an abstention, never an exception into the cycle."""
    settings.llm.api_key = "test-key"

    class RefusingMessages:
        def parse(self, **kwargs):
            class Response:
                stop_reason = "refusal"
                stop_details = type("D", (), {"category": "cyber"})()
            return Response()

    class RefusingClient:
        messages = RefusingMessages()

    agent = TechnicalAnalyst(settings=settings, client=RefusingClient())
    result = agent.run(snapshot=SNAPSHOT, regime="trend_up")
    assert result.ok is True
    assert result.mode == "mock"
    assert "refus" in (result.error or "").lower()


def test_async_timeout_yields_an_abstention(settings):
    import time

    settings.agents.timeout_seconds = 0

    class Slow(LLMAgent):
        name = "slow"

        def mock_output(self, context):
            time.sleep(0.2)
            return {}

    result = asyncio.run(Slow(settings=settings).arun())
    assert result.ok is False
    assert "timed out" in result.error


# ---------------------------------------------------------------------------
# Individual agents honour their contracts
# ---------------------------------------------------------------------------


def test_technical_analyst_produces_a_setup_per_ticker():
    output = TechnicalAnalyst().run(snapshot=SNAPSHOT, regime="trend_up").output
    tickers = {a["ticker"] for a in output["per_ticker_analysis"]}
    assert tickers == {"SPY", "QQQ"}
    trending = next(a for a in output["per_ticker_analysis"] if a["ticker"] == "SPY")
    assert trending["setup_type"] == "trend_continuation"
    assert trending["direction"] == "bullish"
    assert trending["levels"]["invalidation"] < trending["levels"]["support"] + 100


def test_fundamental_analyst_abstains_rather_than_inventing_data():
    """Its persona forbids fabricated metrics; the offline path must comply."""
    output = FundamentalAnalyst().run(snapshot=SNAPSHOT, regime="trend_up").output
    assert output["candidates"] == []
    assert len(output["abstentions"]) == 2


def test_sentiment_analyst_reports_no_coverage_but_still_honours_the_calendar():
    calendar = [{"ticker": "SPY", "event_type": "earnings", "date": "2026-08-24", "days_away": 2}]
    output = SentimentAnalyst().run(snapshot=SNAPSHOT, calendar=calendar, regime="range").output
    spy = next(s for s in output["per_ticker_sentiment"] if s["ticker"] == "SPY")
    assert spy["coverage"] == "none"
    assert spy["score"] == 0.0, "no feed means no opinion, not a made-up one"
    assert spy["binary_event_risk"] is True
    assert spy["impact_on_options_idea"] == "delay"
    assert output["desk_wide_warnings"]


def test_event_agent_recommends_standing_aside_near_a_binary_event():
    calendar = [{"ticker": "SPY", "event_type": "earnings", "date": "2026-08-24", "days_away": 1}]
    output = EventAgent().run(snapshot=SNAPSHOT, calendar=calendar, positions=[]).output
    assert output["events"][0]["recommendation"] == "avoid_until_after"
    assert output["events"][0]["proposed_structure"]["name"] == "stand_aside"


def test_event_agent_invents_nothing_without_a_calendar():
    output = EventAgent().run(snapshot=SNAPSHOT, calendar=[], positions=[]).output
    assert output["events"] == []


def test_event_agent_flags_positions_exposed_to_an_event():
    calendar = [{"ticker": "SPY", "event_type": "earnings", "date": "2026-08-24", "days_away": 1}]
    positions = [{"symbol": "SPY260918C00600000", "underlying": "SPY"}]
    output = EventAgent().run(snapshot=SNAPSHOT, calendar=calendar, positions=positions).output
    assert output["positions_at_event_risk"][0]["recommended_action"] == "hedge"


# ---------------------------------------------------------------------------
# Structure construction
# ---------------------------------------------------------------------------


def test_vertical_spread_risk_profile_matches_the_textbook():
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "mid_price": 3.0, "qty": 1},
        {"side": "sell", "right": "call", "strike": 105, "mid_price": 1.0, "qty": 1},
    ]
    profile = risk_profile(legs, 100)
    assert profile["max_loss"] == 200.0
    assert profile["max_profit"] == 300.0
    assert profile["breakevens"] == [102.0]
    assert profile["unbounded_loss"] is False


def test_iron_condor_risk_profile():
    legs = [
        {"side": "buy", "right": "put", "strike": 90, "mid_price": 0.5, "qty": 1},
        {"side": "sell", "right": "put", "strike": 95, "mid_price": 1.5, "qty": 1},
        {"side": "sell", "right": "call", "strike": 105, "mid_price": 1.5, "qty": 1},
        {"side": "buy", "right": "call", "strike": 110, "mid_price": 0.5, "qty": 1},
    ]
    profile = risk_profile(legs, 100)
    assert profile["max_profit"] == 200.0
    assert profile["max_loss"] == 300.0
    assert profile["breakevens"] == [93.0, 107.0]


def test_naked_short_call_is_detected_as_unbounded():
    """The payoff curve proves it, rather than a label claiming it."""
    profile = risk_profile([{"side": "sell", "right": "call", "strike": 105, "mid_price": 1.5, "qty": 1}], 100)
    assert profile["unbounded_loss"] is True


def test_long_straddle_has_bounded_loss_and_unbounded_profit():
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "mid_price": 3.0, "qty": 1},
        {"side": "buy", "right": "put", "strike": 100, "mid_price": 3.0, "qty": 1},
    ]
    profile = risk_profile(legs, 100)
    assert profile["max_loss"] == 600.0
    assert profile["max_profit"] is None
    assert profile["unbounded_profit"] is True
    assert profile["unbounded_loss"] is False


def test_payoff_at_expiry_is_exact():
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "mid_price": 3.0, "qty": 1},
        {"side": "sell", "right": "call", "strike": 105, "mid_price": 1.0, "qty": 1},
    ]
    assert payoff_at(legs, 95) == pytest.approx(-200.0)
    assert payoff_at(legs, 110) == pytest.approx(300.0)
    assert payoff_at(legs, 102) == pytest.approx(0.0)


def test_net_greeks_respect_leg_direction():
    legs = [
        {"side": "buy", "right": "call", "strike": 100, "qty": 1, "greeks": {"delta": 0.55}},
        {"side": "sell", "right": "call", "strike": 105, "qty": 1, "greeks": {"delta": 0.30}},
    ]
    assert net_greeks(legs)["delta"] == pytest.approx(25.0)


def test_builder_selects_strikes_by_delta():
    chain = make_chain("SPY", 580.0)
    playbook = next(p for p in playbooks_for_regime("range") if p["name"] == "iron_condor")
    structure = StructureBuilder().build("SPY", playbook, chain, 580.0)
    assert structure is not None
    assert len(structure["legs"]) == 4
    short_put = next(leg for leg in structure["legs"] if leg["side"] == "sell" and leg["right"] == "put")
    assert abs(abs(short_put["delta"]) - 0.16) < 0.06


def test_builder_rejects_a_structure_it_cannot_price():
    chain = make_chain("SPY", 580.0, dtes=(2,))  # inside the minimum-DTE window
    playbook = next(p for p in playbooks_for_regime("range") if p["name"] == "iron_condor")
    assert StructureBuilder().build("SPY", playbook, chain, 580.0) is None


def test_playbook_conditions_gate_construction():
    """A protective put needs long exposure to protect."""
    builder = StructureBuilder()
    playbook = next(
        p for p in playbooks_for_regime("trend_down") if p["name"] == "protective_put_overlay"
    )
    permitted, unmet = builder.conditions_met(playbook, {"has_long_exposure": False})
    assert permitted is False and unmet

    permitted, _ = builder.conditions_met(playbook, {"has_long_exposure": True})
    assert permitted is True


def test_iv_rank_conditions_are_evaluated():
    builder = StructureBuilder()
    playbook = {"conditions": {"iv_rank_min": 0.5}}
    assert builder.conditions_met(playbook, {"iv_rank": 0.6})[0] is True
    assert builder.conditions_met(playbook, {"iv_rank": 0.3})[0] is False


def test_strategist_builds_valid_structures_from_a_chain():
    structures = build_structures_for("range")
    assert structures
    for structure in structures:
        assert structure["valid"] is True
        assert structure["risk_profile"]["max_loss"] > 0
        assert structure["risk_profile"]["unbounded_loss"] is False
        assert structure["limit_price"] != 0


def test_credit_structures_price_as_a_negative_limit():
    structures = build_structures_for("range")
    for structure in structures:
        if structure["net_side"] == "credit":
            assert structure["limit_price"] < 0, "a net credit is submitted as a negative limit"


# ---------------------------------------------------------------------------
# Critic
# ---------------------------------------------------------------------------


#: A quiet, range-bound snapshot. Range playbooks declare `adx_max: 22`, so a
#: trending snapshot would (correctly) fail their conditions.
RANGE_SNAPSHOT = {
    "SPY": {
        "indicators": {
            "last_close": 580.0, "adx": 14.0, "ema_slope": 0.0001, "atr": 5.0,
            "atr_pct": 0.0086, "bollinger_bandwidth": 0.02, "pct_of_52w_range": 0.5,
            "realised_vol_20d": 0.15,
        },
        "spot": 580.0,
        "vol_surface": {"iv_rank": 0.55, "atm_iv": 0.22, "term_structure": "contango"},
    }
}


def build_structures_for(regime: str = "range"):
    snapshot = {"SPY": {**RANGE_SNAPSHOT["SPY"], "chain": make_chain("SPY", 580.0)}}
    return VolOptionsStrategist().build_structures(snapshot, playbooks_for_regime(regime))


def test_critic_approves_a_regime_aligned_structure():
    output = CriticCommittee().run(
        regime="range",
        structures=build_structures_for("range"),
        agent_views={"technical_analyst": {"per_ticker_analysis": [
            {"ticker": "SPY", "setup_type": "range_fade", "conviction": 0.7}
        ]}},
    ).output
    assert output["approved_trades_summary"]
    trade = output["approved_trades_summary"][0]
    assert trade["max_loss"] > 0
    assert trade["thesis"]


def test_critic_vetoes_short_premium_into_a_binary_event():
    output = CriticCommittee().run(
        regime="range",
        structures=build_structures_for("range"),
        agent_views={"sentiment_analyst": {"per_ticker_sentiment": [
            {"ticker": "SPY", "binary_event_risk": True, "impact_on_options_idea": "veto"}
        ]}},
    ).output
    assert output["approved_trades_summary"] == []
    assert any(d["reason_code"] == "BINARY_EVENT" for d in output["decisions"])


def test_critic_rejects_a_regime_mismatch():
    """A short-premium condor has no business in an event regime."""
    output = CriticCommittee().run(
        regime="high_vol_event", structures=build_structures_for("range"), agent_views={}
    ).output
    assert output["approved_trades_summary"] == []
    assert any(d["reason_code"] == "REGIME_MISMATCH" for d in output["decisions"])


def test_critic_respects_the_per_cycle_cap(settings):
    settings.critic.max_approved_trades = 1
    structures = []
    for ticker, spot in (("SPY", 580.0), ("QQQ", 500.0), ("IWM", 220.0)):
        structures.extend(
            VolOptionsStrategist(settings=settings).build_structures(
                {ticker: {"spot": spot,
                          "indicators": {"last_close": spot, "realised_vol_20d": 0.15},
                          "chain": make_chain(ticker, spot),
                          "vol_surface": {"iv_rank": 0.55}}},
                playbooks_for_regime("range"),
            )
        )
    output = CriticCommittee(settings=settings).run(
        regime="range", structures=structures,
        agent_views={"technical_analyst": {"per_ticker_analysis": [
            {"ticker": t, "setup_type": "range_fade", "conviction": 0.8} for t in ("SPY", "QQQ", "IWM")
        ]}},
    ).output
    assert len(output["approved_trades_summary"]) <= 1
    assert any(d["reason_code"] == "CONCENTRATION" for d in output["decisions"])


def test_critic_records_every_rejection_with_a_reason():
    output = CriticCommittee().run(
        regime="high_vol_event", structures=build_structures_for("range"), agent_views={}
    ).output
    for decision in output["decisions"]:
        assert decision["reason_code"]
        assert decision["reason"]


# ---------------------------------------------------------------------------
# Coach and Storyteller
# ---------------------------------------------------------------------------


def test_coach_scores_process_separately_from_outcome():
    trades = [
        {"trade_id": "t1", "ticker": "SPY", "playbook": "iron_condor", "status": "closed",
         "pnl": 120.0, "exit_reason": "profit_target", "risk_reason_codes": ["APPROVED"]},
        {"trade_id": "t2", "ticker": "QQQ", "playbook": "iron_condor", "status": "closed",
         "pnl": -260.0, "exit_reason": "stop_loss", "risk_reason_codes": ["RESIZED"]},
    ]
    output = CoachAgent().run(trades=trades, risk_rejections=[], metrics={}).output
    assert output["review_report"]["trades_reviewed"] == 2
    assert output["review_report"]["pnl_realised"] == -140.0
    assert output["review_report"]["hit_rate"] == 0.5
    losing = next(s for s in output["trade_scores"] if s["trade_id"] == "t2")
    assert losing["verdict"] == "good_process_bad_outcome", "honouring a stop is good process"
    assert losing["rule_compliance"] == "resized"


def test_coach_flags_small_samples_as_such():
    trades = [{"trade_id": "t1", "playbook": "iron_condor", "status": "closed", "pnl": 10.0}]
    output = CoachAgent().run(trades=trades, risk_rejections=[], metrics={}).output
    assert any("not a pattern" in p for p in output["patterns"])


def test_coach_surfaces_risk_rejections_as_a_lesson():
    output = CoachAgent().run(
        trades=[], risk_rejections=[{"reason_codes": ["NOTIONAL_PER_TRADE"]}], metrics={}
    ).output
    assert any("NOTIONAL_PER_TRADE" in lesson for lesson in output["lessons_for_tomorrow"])


def test_storyteller_respects_the_character_limit(settings):
    output = StorytellerAgent(settings=settings).run(
        date="2026-08-28", regime="range", trades=[], risk_decisions=[],
        metrics={"day_pnl": -140.0, "equity": 99_860.0},
    ).output
    assert len(output["post_text_x"]) <= settings.social.max_chars_x
    assert output["post_text_linkedin"]


def test_storyteller_uses_only_supplied_numbers():
    """The persona forbids invented figures; the offline path must comply."""
    output = StorytellerAgent().run(date="2026-08-28", regime="range", trades=[], metrics={}).output
    labels = {n["label"] for n in output["key_numbers"]}
    assert "Day P&L (paper)" not in labels, "no P&L was supplied, so none may be reported"


def test_storyteller_leads_with_a_risk_guard_save_when_one_happened():
    output = StorytellerAgent().run(
        date="2026-08-28", regime="range", trades=[],
        risk_decisions=[{"verdict": "REJECT"}], metrics={"day_pnl": 0.0},
    ).output
    assert output["story_angle"] == "risk_guard_save"


def test_storyteller_writes_the_post_to_disk(tmp_path, settings):
    settings.social.output_dir = str(tmp_path)
    agent = StorytellerAgent(settings=settings)
    output = agent.run(date="2026-08-28", regime="range", trades=[], metrics={}).output
    path = agent.save_post(output, "2026-08-28")
    assert path.exists()
    content = path.read_text()
    assert "X / Twitter" in content and "LinkedIn" in content


def test_range_playbooks_are_gated_by_trend_strength():
    """An iron condor declares `adx_max: 22`; a trending tape must not get one."""
    trending = {"SPY": {**SNAPSHOT["SPY"], "chain": make_chain("SPY", 580.0)}}
    assert SNAPSHOT["SPY"]["indicators"]["adx"] > 22
    assert VolOptionsStrategist().build_structures(trending, playbooks_for_regime("range")) == []


def test_strategist_run_accepts_the_orchestrators_playbook_shape():
    """Regression: the orchestrator fans out playbook *names*, not dicts.

    ``build_context`` used to call ``.get("name")`` on each entry, so every live
    cycle raised ``'str' object has no attribute 'get'`` and the strategist
    abstained. Agents never raise, so this surfaced only as a silent abstention
    while the desk quietly lost its IV-rank playbook selection.
    """
    names = [p["name"] for p in playbooks_for_regime("range")]
    result = VolOptionsStrategist().run(
        regime="range",
        playbooks=names,
        snapshot=RANGE_SNAPSHOT,
        vol_surfaces={"SPY": RANGE_SNAPSHOT["SPY"]["vol_surface"]},
        as_of="2026-08-22T11:29:22+00:00",
    )

    assert result.ok, result.error
    assert result.mode != "error"
    assert set(result.output["selected_playbooks"]) <= set(names)


def test_strategist_run_still_accepts_full_playbook_dicts():
    """The dict shape stays valid so either caller contract works."""
    playbooks = playbooks_for_regime("range")
    result = VolOptionsStrategist().run(
        regime="range",
        playbooks=playbooks,
        snapshot=RANGE_SNAPSHOT,
        vol_surfaces={"SPY": RANGE_SNAPSHOT["SPY"]["vol_surface"]},
        as_of="2026-08-22T11:29:22+00:00",
    )

    assert result.ok, result.error
    assert set(result.output["selected_playbooks"]) <= {p["name"] for p in playbooks}


def _select(regime: str, iv_rank: float) -> list[str]:
    strategist = VolOptionsStrategist()
    context = strategist.build_context(
        regime=regime,
        playbooks=[p["name"] for p in playbooks_for_regime(regime)],
        vol_surfaces={"SPY": {"iv_rank": iv_rank, "atm_iv": 0.2}},
        snapshot={"SPY": {"indicators": {}}},
    )
    return strategist.mock_output(context)["selected_playbooks"]


def test_premium_selling_is_gated_on_iv_rank_not_playbook_spelling():
    """`short_put_spread_at_support` is a vertical_credit despite its name.

    The offline selector used to sniff the name for "credit"/"condor"/"butterfly",
    so this playbook read as premium-*buying* and got picked when IV was cheap —
    selling cheap premium, the exact inverse of the desk's stated edge.
    """
    cheap = _select("range", iv_rank=0.05)
    rich = _select("range", iv_rank=0.85)

    assert "short_put_spread_at_support" in rich
    assert "short_put_spread_at_support" not in cheap
    assert {"iron_condor", "iron_butterfly"} <= set(rich)


def test_stand_aside_is_never_offered_as_a_structure():
    """`no_trade` is the absence of a view, so it must not be 'selected'."""
    for rank in (0.05, 0.5, 0.95):
        assert "stand_aside" not in _select("high_vol_event", iv_rank=rank)
