"""State store, heartbeat, experiment registry, dashboards, MCP, and the full cycle."""

from __future__ import annotations

import json

import pytest

from desk.experiments.registry import ExperimentRegistry, prompt_fingerprints
from desk.mcp import ALL_TOOLS
from desk.mcp.server import build_tool_list, dispatch, find_tool
from desk.monitor.dashboard_cli import collect, sparkline
from desk.monitor.heartbeat import check_heartbeat, read_heartbeat, write_heartbeat

# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------


def test_cycle_round_trip(store):
    store.start_cycle("c1", "morning", "range", dry_run=True)
    store.finish_cycle("c1", "all done", "complete")
    cycles = store.get_cycles()
    assert cycles[0]["cycle_id"] == "c1"
    assert cycles[0]["status"] == "complete"
    assert cycles[0]["summary"] == "all done"


def test_account_and_position_snapshots(store):
    store.record_account({"equity": 100_000, "cash": 90_000, "buying_power": 180_000, "daily_pnl": -140}, "c1")
    store.record_positions(
        [{"symbol": "SPY260918P00540000", "underlying": "SPY", "asset_class": "option",
          "qty": -1, "market_value": -210, "unrealized_pl": 15}],
        "c1",
    )
    assert store.latest_account()["equity"] == 100_000
    assert len(store.latest_positions()) == 1
    assert store.latest_positions()[0]["underlying"] == "SPY"


def test_trade_lifecycle(store):
    store.record_trade(
        {"trade_id": "t1", "ticker": "SPY", "playbook": "iron_condor", "net_side": "credit",
         "net_price": 2.0, "max_loss": 300, "days_to_expiry": 30, "thesis": "range fade",
         "risk_reason_codes": ["APPROVED"]},
        "c1", status="approved",
    )
    store.update_trade("t1", status="closed", pnl=120.0, exit_reason="profit_target")
    trade = store.get_trades()[0]
    assert trade["status"] == "closed"
    assert trade["pnl"] == 120.0
    assert trade["risk_reason_codes"] == ["APPROVED"], "JSON columns must round-trip as lists"


def test_update_trade_ignores_unknown_columns(store):
    """A typo'd field must not become a SQL injection vector or a silent no-op crash."""
    store.record_trade({"trade_id": "t1", "ticker": "SPY"}, "c1")
    store.update_trade("t1", status="closed", bogus_column="'; DROP TABLE trades; --")
    assert store.get_trades()[0]["status"] == "closed"
    assert store.counts()["trades"] == 1


def test_decision_traces_are_ordered_and_filterable(store):
    for stage in ("snapshot", "regime", "research", "critic", "risk_guard"):
        store.record_trace("c1", stage, {"stage": stage}, agent=stage, mode="mock")
    store.record_trace("c2", "snapshot", {}, mode="mock")

    traces = store.get_traces("c1")
    assert [t["stage"] for t in traces] == ["snapshot", "regime", "research", "critic", "risk_guard"]
    assert len(store.get_traces("c2")) == 1


def test_traces_store_structured_payloads(store):
    store.record_trace("c1", "risk_guard", {"verdict": "REJECT", "codes": ["DTE_TOO_SHORT"]})
    payload = store.get_traces("c1")[0]["payload"]
    assert payload["verdict"] == "REJECT"
    assert payload["codes"] == ["DTE_TOO_SHORT"]


def test_equity_curve_is_chronological(store):
    for equity in (100_000, 100_500, 99_800):
        store.record_account({"equity": equity}, "c1")
    curve = store.equity_curve()
    assert [point["equity"] for point in curve] == [100_000, 100_500, 99_800]


def test_daily_metrics_upsert(store):
    store.record_daily_metrics({"equity": 100_000, "day_pnl": -140, "trades": 2}, "2026-08-21")
    store.record_daily_metrics({"equity": 100_100, "day_pnl": 100, "trades": 3}, "2026-08-21")
    metrics = store.get_daily_metrics()
    assert len(metrics) == 1, "the same date must update, not duplicate"
    assert metrics[0]["day_pnl"] == 100


def test_in_memory_store_creates_no_file():
    """`:memory:` must not be resolved into a file literally named ":memory:"."""
    from desk.monitor.state_store import StateStore
    from desk.utils.config_loader import PROJECT_ROOT

    memory_store = StateStore(":memory:")
    assert memory_store.in_memory is True
    memory_store.start_cycle("c1", "morning")
    assert memory_store.get_cycles()[0]["cycle_id"] == "c1"
    assert not (PROJECT_ROOT / ":memory:").exists()


def test_counts_reports_every_table(store):
    counts = store.counts()
    assert set(counts) == {"cycles", "account_snapshots", "positions", "trades", "orders", "decision_traces"}


def test_pruning_removes_only_old_traces(store):
    store.record_trace("c1", "snapshot", {})
    assert store.prune(retain_days=3650) == 0
    assert store.prune(retain_days=0) == 1


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_round_trip(tmp_path, settings, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: path)
    write_heartbeat("ok", cycle_id="c1", phase="morning", trades=2)
    beat = read_heartbeat()
    assert beat["status"] == "ok"
    assert beat["cycle_id"] == "c1"
    assert beat["trades"] == 2
    assert check_heartbeat()["healthy"] is True


def test_a_missing_heartbeat_is_unhealthy_not_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: tmp_path / "absent.json")
    result = check_heartbeat()
    assert result["healthy"] is False
    assert "never completed a cycle" in result["alert"]


def test_a_stale_heartbeat_raises_an_alert(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: path)
    write_heartbeat("ok")
    result = check_heartbeat(stale_after_seconds=0)
    assert result["healthy"] is False, "an explicit zero threshold must not fall back to the default"
    assert "old" in result["alert"]


def test_a_failed_cycle_status_is_unhealthy(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: path)
    write_heartbeat("error", cycle_id="c1")
    assert check_heartbeat()["healthy"] is False


def test_a_corrupt_heartbeat_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text("{ not json")
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: path)
    assert read_heartbeat() is None
    assert check_heartbeat()["healthy"] is False


# ---------------------------------------------------------------------------
# Experiment registry
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return ExperimentRegistry(tmp_path / "experiments.json")


def test_create_and_activate(registry):
    registry.create("exp-1", "First experiment")
    assert registry.get("exp-1")["description"] == "First experiment"
    assert registry.active()["id"] == "exp-1"

    registry.create("exp-2", "Second")
    registry.set_active("exp-2")
    assert registry.active()["id"] == "exp-2"


def test_duplicate_creation_is_refused(registry):
    registry.create("exp-1", "First")
    with pytest.raises(ValueError):
        registry.create("exp-1", "Again")
    registry.create("exp-1", "Overwritten", overwrite=True)
    assert registry.get("exp-1")["description"] == "Overwritten"


def test_activating_an_unknown_experiment_raises(registry):
    with pytest.raises(KeyError):
        registry.set_active("nope")


def test_backtest_runs_replace_and_live_runs_accumulate(registry):
    registry.create("exp-1", "Test")
    registry.log_backtest_run("exp-1", {"total_pnl": 1000.0, "sharpe": 1.1})
    registry.log_backtest_run("exp-1", {"total_pnl": 1200.0, "sharpe": 1.3})
    assert registry.get("exp-1")["backtest_results"]["total_pnl"] == 1200.0

    registry.log_live_run("exp-1", {"day_pnl": -140.0}, "2026-08-28")
    registry.log_live_run("exp-1", {"day_pnl": 320.0}, "2026-08-31")
    live = registry.get("exp-1")["live_results"]
    assert live["sessions"] == 2
    assert live["cumulative_pnl"] == 180.0, "live P&L accumulates across sessions"


def test_logging_against_an_unknown_experiment_creates_it(registry):
    registry.log_backtest_run("auto-created", {"total_pnl": 1.0})
    assert registry.get("auto-created") is not None


def test_prompt_fingerprints_cover_every_persona():
    fingerprints = prompt_fingerprints()
    assert len(fingerprints) >= 11
    assert "critic" in fingerprints and "risk_guard" in fingerprints


def test_compare_produces_one_row_per_experiment(registry):
    registry.create("a", "A")
    registry.create("b", "B")
    assert {row["id"] for row in registry.compare()} == {"a", "b"}


def test_registry_persists_to_disk(tmp_path):
    path = tmp_path / "experiments.json"
    ExperimentRegistry(path).create("exp-1", "Persisted")
    assert ExperimentRegistry(path).get("exp-1") is not None


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------


def test_sparkline_handles_every_input_length():
    assert sparkline([]) == "—"
    assert sparkline([1.0]) == "—"
    assert len(sparkline([1.0, 2.0, 3.0])) == 3
    assert len(sparkline(list(range(500)), width=40)) == 40
    assert sparkline([5.0, 5.0, 5.0]) == "▁▁▁", "a flat curve must not divide by zero"


def test_collect_returns_the_full_dashboard_payload(store):
    store.record_account({"equity": 100_000, "cash": 90_000, "daily_pnl": 250}, "c1")
    payload = collect(store)
    assert set(payload) >= {"account", "positions", "trades", "equity_curve", "summary", "heartbeat"}
    assert payload["summary"]["equity"] == 100_000


def test_web_endpoints_all_respond():
    from fastapi.testclient import TestClient

    from desk.monitor.dashboard_web import create_app

    client = TestClient(create_app())
    for path in ("/", "/api/state", "/api/account", "/api/positions", "/api/pnl",
                 "/api/trades", "/api/traces", "/api/regime", "/api/experiments"):
        assert client.get(path).status_code == 200, path
    assert client.get("/health").status_code in (200, 503)


def test_the_web_page_is_self_contained():
    """A strict-offline demo must not depend on a CDN."""
    from desk.monitor.dashboard_web import PAGE

    assert "http://" not in PAGE.replace("http://127.0.0.1", "")
    assert "cdn." not in PAGE
    assert "<script>" in PAGE and "src=" not in PAGE


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_all_eight_tools_are_registered():
    names = {spec.name for spec in ALL_TOOLS}
    assert names == {
        "get_equity_bars", "get_options_chain", "get_options_bars", "get_account_state",
        "get_positions", "get_open_orders", "submit_orders", "risk_guard_check",
    }


def test_every_schema_is_a_closed_object():
    """`additionalProperties: false` is what makes a tool contract enforceable."""
    for spec in ALL_TOOLS:
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema.get("additionalProperties") is False
        assert spec.description


def test_required_fields_match_the_specification():
    expected = {
        "get_equity_bars": ["symbols", "timeframe"],
        "get_options_chain": ["underlying_symbol"],
        "get_options_bars": ["contract_symbols", "timeframe"],
        "get_account_state": [],
        "get_positions": [],
        "get_open_orders": [],
        "submit_orders": ["orders"],
        "risk_guard_check": ["current_portfolio", "candidate_trades"],
    }
    for name, required in expected.items():
        assert find_tool(name).input_schema.get("required", []) == required


def test_timeframe_enums_are_exact():
    for name in ("get_equity_bars", "get_options_bars"):
        schema = find_tool(name).input_schema
        assert schema["properties"]["timeframe"]["enum"] == ["1Min", "5Min", "15Min", "1H", "1D"]


def test_tool_objects_build_for_the_mcp_sdk():
    tools = build_tool_list()
    assert len(tools) == 8
    assert "inputSchema" in tools[0].model_dump(by_alias=True)


def test_an_unknown_tool_returns_an_envelope_not_an_exception():
    result = dispatch("no_such_tool", {})
    assert result["ok"] is False
    assert result["error_type"] == "unknown_tool"


def test_missing_credentials_surface_as_a_typed_error():
    result = dispatch("get_account_state", {})
    assert result["ok"] is False
    assert result["error_type"] == "not_configured"
    assert result["retryable"] is False


def test_risk_guard_tool_rejects_an_oversized_trade():
    result = dispatch(
        "risk_guard_check",
        {
            "current_portfolio": {"cash": 100_000, "equity": 100_000, "buying_power": 200_000, "positions": []},
            "candidate_trades": [
                {"trade_id": "t1", "symbol_or_contract": "SPY", "asset_class": "us_equity",
                 "side": "buy", "qty": 10, "estimated_notional": 999_999}
            ],
        },
    )
    assert result["ok"] is True
    assert result["data"]["verdict"] == "REJECT"
    assert "NOTIONAL_PER_TRADE" in result["data"]["trades"][0]["reason_codes"]


def test_risk_guard_tool_validates_its_input():
    result = dispatch("risk_guard_check", {"current_portfolio": {}, "candidate_trades": []})
    assert result["ok"] is False
    assert result["error_type"] == "invalid_input"


def test_every_tool_response_is_json_serialisable():
    for spec in ALL_TOOLS:
        result = dispatch(spec.name, {})
        json.dumps(result, default=str)
        assert "ok" in result


# ---------------------------------------------------------------------------
# Full orchestrated cycle, offline
# ---------------------------------------------------------------------------


def test_a_full_cycle_runs_end_to_end(orchestrator):
    """Snapshot -> regime -> research -> critic -> risk gate -> execution -> trace."""
    result = orchestrator.run_cycle(phase="morning")

    assert result.status == "complete"
    assert result.regime in {"trend_up", "trend_down", "range", "high_vol_event"}
    assert result.watchlist
    assert set(result.agents_consulted) >= {
        "technical_analyst", "sentiment_analyst", "vol_options_strategist", "event_agent"
    }
    assert result.summary
    # "Consulted" counts agents that errored and abstained, so assert the stronger
    # property too: a healthy cycle has no agent failing outright. Without this a
    # crashing agent degrades the desk silently and every assertion above still holds.
    assert result.agents_abstained == []


def test_a_cycle_writes_a_complete_decision_trace(orchestrator, store):
    result = orchestrator.run_cycle(phase="morning")
    stages = {trace["stage"] for trace in store.get_traces(result.cycle_id, limit=200)}
    assert {"account", "snapshot", "regime", "research", "cycle_result"} <= stages


def test_a_cycle_updates_the_heartbeat(orchestrator, tmp_path, monkeypatch):
    monkeypatch.setattr("desk.monitor.heartbeat.heartbeat_path", lambda: tmp_path / "hb.json")
    result = orchestrator.run_cycle(phase="morning")
    beat = read_heartbeat()
    assert beat["cycle_id"] == result.cycle_id
    assert beat["status"] == "ok"


def test_execution_only_ever_receives_risk_approved_trades(orchestrator, fake_execution):
    """The core safety property: nothing reaches the broker unapproved."""
    result = orchestrator.run_cycle(phase="morning")
    approved_ids = {
        verdict["trade_id"]
        for verdict in result.risk_decision.get("trades", [])
        if verdict.get("approved_qty", 0) > 0
    }
    for order in fake_execution.submitted:
        assert order["trade_id"] in approved_ids
        verdict = next(v for v in result.risk_decision["trades"] if v["trade_id"] == order["trade_id"])
        assert order["qty"] == verdict["approved_qty"], "orders must use the guard's quantity"


def test_a_tripped_circuit_breaker_stops_all_execution(orchestrator, fake_execution):
    fake_execution.equity = 100_000.0

    def losing_account():
        state = dict(fake_execution.get_account_state.__wrapped__(fake_execution)) if hasattr(
            fake_execution.get_account_state, "__wrapped__"
        ) else {
            "account_number": "PA_TEST", "status": "ACTIVE", "cash": 90_000.0,
            "equity": 95_000.0, "buying_power": 200_000.0, "initial_margin": 0.0,
            "daily_pnl": -5_000.0, "daily_pnl_pct": -0.05, "trading_blocked": False,
            "as_of": "2026-08-21T14:00:00+00:00",
        }
        return state

    fake_execution.get_account_state = losing_account
    result = orchestrator.run_cycle(phase="morning")
    assert fake_execution.submitted == []
    if result.risk_decision:
        assert result.risk_decision.get("circuit_breakers")


def test_a_data_outage_ends_the_cycle_cleanly(orchestrator):
    orchestrator._market_data.tickers = {}
    result = orchestrator.run_cycle(phase="morning")
    assert result.status == "no_data"
    assert "No market data" in result.summary


def test_the_journal_produces_a_review_and_a_post(orchestrator, tmp_path, settings):
    orchestrator.settings.social.output_dir = str(tmp_path)
    orchestrator.storyteller.settings.social.output_dir = str(tmp_path)
    orchestrator.run_cycle(phase="morning")
    journal = orchestrator.run_journal(save_post=True)

    assert "review_report" in journal["coach"]
    assert journal["story"]["post_text_x"]
    assert journal["post_path"]
