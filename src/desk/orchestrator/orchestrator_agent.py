"""The main cycle: snapshot -> regime -> research -> critic -> risk -> execution.

The ordering here is the safety property of the whole system. Execution takes
its input *only* from the Risk Guard's approved list, at the quantity the guard
returned. There is no code path from a Critic decision to an order that does not
pass through :meth:`Orchestrator._gate_risk`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import AgentResult
from desk.agents.coach_agent import CoachAgent
from desk.agents.critic_committee import CriticCommittee
from desk.agents.regime_agent import RegimeAgent
from desk.agents.storyteller_agent import StorytellerAgent
from desk.agents.vol_options_strategist import VolOptionsStrategist
from desk.experiments.registry import get_registry
from desk.monitor.heartbeat import write_heartbeat
from desk.monitor.state_store import StateStore, get_state_store
from desk.orchestrator.routing import (
    SnapshotBuilder,
    build_portfolio,
    build_research_agents,
    run_fan_out,
)
from desk.risk.limits import CandidateTrade, RiskLimits, Verdict
from desk.risk.risk_guard import RiskGuard
from desk.utils.config_loader import Settings, get_settings, playbooks_for_regime
from desk.utils.logging import get_logger
from desk.utils.time_utils import today_et, utc_iso

logger = get_logger("orchestrator")


class CycleResult(BaseModel):
    """Everything one cycle produced — the object the CLI and dashboards read."""

    cycle_id: str
    phase: str = "morning"
    started_at: str = ""
    finished_at: str = ""
    dry_run: bool = True
    status: str = "complete"

    regime: str = "range"
    regime_detail: dict[str, Any] = Field(default_factory=dict)
    watchlist: list[str] = Field(default_factory=list)

    agents_consulted: list[str] = Field(default_factory=list)
    agents_abstained: list[str] = Field(default_factory=list)

    structures_built: int = 0
    proposals_received: int = 0
    trades_approved: int = 0
    trades_rejected: int = 0
    rejection_reasons: list[str] = Field(default_factory=list)

    risk_decision: dict[str, Any] = Field(default_factory=dict)
    orders_submitted: list[dict[str, Any]] = Field(default_factory=list)
    account: dict[str, Any] = Field(default_factory=dict)

    coach_review: dict[str, Any] = Field(default_factory=dict)
    social_post: dict[str, Any] = Field(default_factory=dict)

    errors: list[str] = Field(default_factory=list)
    summary: str = ""


class Orchestrator:
    """Coordinates the desk. Owns no market opinion of its own."""

    def __init__(
        self,
        settings: Settings | None = None,
        market_data: Any = None,
        execution: Any = None,
        store: StateStore | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._market_data = market_data
        self._execution = execution
        self.store = store or get_state_store()
        self.dry_run = self.settings.execution.dry_run if dry_run is None else dry_run

        self.regime_agent = RegimeAgent(settings=self.settings)
        self.critic = CriticCommittee(settings=self.settings)
        self.strategist = VolOptionsStrategist(settings=self.settings)
        self.coach = CoachAgent(settings=self.settings)
        self.storyteller = StorytellerAgent(settings=self.settings)
        self.risk_guard = RiskGuard(RiskLimits.from_settings(self.settings))

    # -- lazy broker wiring ------------------------------------------------

    @property
    def market_data(self) -> Any:
        if self._market_data is None:
            from desk.alpaca.market_data import MarketData

            self._market_data = MarketData()
        return self._market_data

    @property
    def execution(self) -> Any:
        if self._execution is None:
            from desk.alpaca.execution import ExecutionEngine

            self._execution = ExecutionEngine(dry_run=self.dry_run)
        return self._execution

    # -- main cycle --------------------------------------------------------

    def run_cycle(
        self,
        phase: str = "morning",
        max_new_trades: int | None = None,
        calendar: list[dict[str, Any]] | None = None,
    ) -> CycleResult:
        """Run one full decision cycle."""
        started = utc_iso()
        cycle_id = f"{today_et().isoformat()}-{phase}-{started[11:19].replace(':', '')}"
        result = CycleResult(
            cycle_id=cycle_id, phase=phase, started_at=started, dry_run=self.dry_run
        )
        self.store.start_cycle(cycle_id, phase, dry_run=self.dry_run)
        write_heartbeat("running", cycle_id=cycle_id, phase=phase)

        try:
            self._execute_cycle(result, calendar or [], max_new_trades)
        except Exception as exc:  # noqa: BLE001 - a cycle must always close cleanly
            logger.exception("cycle_failed", extra={"event": "cycle_failed", "cycle_id": cycle_id})
            result.status = "failed"
            result.errors.append(str(exc))
            result.summary = f"Cycle failed: {exc}"

        result.finished_at = utc_iso()
        self.store.finish_cycle(cycle_id, result.summary, result.status)
        self.store.record_trace(cycle_id, "cycle_result", result.model_dump())
        write_heartbeat(
            "ok" if result.status == "complete" else "error",
            cycle_id=cycle_id,
            phase=phase,
            regime=result.regime,
            trades_approved=result.trades_approved,
            orders=len(result.orders_submitted),
        )
        logger.info(
            "cycle_complete",
            extra={
                "event": "cycle_complete",
                "cycle_id": cycle_id,
                "regime": result.regime,
                "approved": result.trades_approved,
                "rejected": result.trades_rejected,
                "status": result.status,
            },
        )
        return result

    def _execute_cycle(
        self, result: CycleResult, calendar: list[dict[str, Any]], max_new_trades: int | None
    ) -> None:
        cycle_id = result.cycle_id

        # 1. Account and positions -----------------------------------------
        account = self.execution.get_account_state()
        positions = self.execution.get_positions()
        result.account = account
        self.store.record_account(account, cycle_id)
        self.store.record_positions(positions, cycle_id)
        self.store.record_trace(cycle_id, "account", {"account": account, "positions": positions})

        # 2. Market snapshot ------------------------------------------------
        builder = SnapshotBuilder(self.market_data, self.settings)
        snapshot = builder.build()
        result.watchlist = sorted(snapshot)
        if not snapshot:
            result.status = "no_data"
            result.summary = "No market data available for any watchlist ticker. Cycle aborted."
            return
        compact = builder.strip_chains(snapshot)
        self.store.record_trace(cycle_id, "snapshot", compact)

        # 3. Regime ----------------------------------------------------------
        benchmark = self.settings.regime.benchmark
        benchmark_data = snapshot.get(benchmark) or next(iter(snapshot.values()))
        days_to_event = min(
            (int(e.get("days_away", 999)) for e in calendar), default=None
        )
        regime_detail = self.regime_agent.classify(
            indicators=benchmark_data.get("indicators", {}),
            iv_summary=benchmark_data.get("vol_surface", {}),
            days_to_next_event=days_to_event,
            as_of=result.started_at,
        )
        result.regime = regime_detail["regime"]
        result.regime_detail = regime_detail
        self.store.record_trace(
            cycle_id, "regime", regime_detail, agent="regime_agent", mode=regime_detail.get("mode", "")
        )

        playbooks = playbooks_for_regime(result.regime)
        if not playbooks:
            result.summary = f"No playbooks defined for regime '{result.regime}'."
            return

        # 4. Parallel research ------------------------------------------------
        agents = build_research_agents(self.settings)
        agent_results = run_fan_out(
            agents,
            snapshot=compact,
            regime=result.regime,
            playbooks=[p["name"] for p in playbooks],
            calendar=calendar,
            positions=positions,
            vol_surfaces={t: d.get("vol_surface", {}) for t, d in snapshot.items()},
            as_of=result.started_at,
        )
        result.agents_consulted = sorted(agent_results)
        result.agents_abstained = sorted(n for n, r in agent_results.items() if r.abstained)
        for name, agent_result in agent_results.items():
            self.store.record_trace(
                cycle_id,
                "research",
                agent_result.model_dump(),
                agent=name,
                mode=agent_result.mode,
                ok=agent_result.ok,
                latency_ms=agent_result.latency_ms,
            )

        agent_views = {name: r.output for name, r in agent_results.items() if r.ok}

        # 5. Build concrete structures ----------------------------------------
        vol_view = agent_views.get("vol_options_strategist", {})
        structures = self.strategist.build_structures(
            snapshot,
            playbooks,
            selected=vol_view.get("selected_playbooks") or None,
            max_per_ticker=self.settings.critic.max_structures_per_ticker,
            positions=positions,
            days_to_event=days_to_event,
        )
        result.structures_built = len(structures)
        result.proposals_received = len(structures)
        self.store.record_trace(cycle_id, "structures", structures)

        if not structures:
            result.summary = (
                f"Regime '{result.regime}'. No structure in the chain satisfied the playbook, "
                "liquidity, and defined-risk requirements. No trades placed."
            )
            return

        # 6. Critic ------------------------------------------------------------
        critic_result: AgentResult = self.critic.run(
            regime=result.regime,
            regime_guidance=regime_detail.get("playbook_guidance", ""),
            structures=structures,
            agent_views=agent_views,
            portfolio={"account": account, "positions": positions},
            as_of=result.started_at,
        )
        self.store.record_trace(
            cycle_id, "critic", critic_result.model_dump(), agent="critic", mode=critic_result.mode
        )
        approved_by_critic = critic_result.output.get("approved_trades_summary", []) or []
        for decision in critic_result.output.get("decisions", []):
            if decision.get("decision") != "approve":
                result.rejection_reasons.append(
                    f"[critic/{decision.get('reason_code')}] {decision.get('reason')}"
                )

        if not approved_by_critic:
            result.trades_rejected = len(structures)
            result.summary = (
                f"Regime '{result.regime}'. The committee reviewed {len(structures)} structure(s) "
                "and approved none. Capital preserved."
            )
            return

        # 7. Risk Guard — the only gate to execution ---------------------------
        risk_decision, approved_specs = self._gate_risk(
            approved_by_critic, account, positions, snapshot, max_new_trades
        )
        result.risk_decision = risk_decision
        self.store.record_trace(cycle_id, "risk_guard", risk_decision)

        for verdict in risk_decision.get("trades", []):
            if verdict.get("verdict") == Verdict.REJECT:
                result.rejection_reasons.append(
                    f"[risk/{','.join(verdict.get('reason_codes', []))}] "
                    f"{'; '.join(verdict.get('reasons', []))}"
                )

        result.trades_approved = len(approved_specs)
        result.trades_rejected = len(approved_by_critic) - len(approved_specs)

        for trade in approved_by_critic:
            verdict = next(
                (v for v in risk_decision.get("trades", []) if v["trade_id"] == trade["trade_id"]),
                {},
            )
            self.store.record_trade(
                {**trade, "risk_reason_codes": verdict.get("reason_codes", [])},
                cycle_id,
                status="approved" if verdict.get("approved_qty", 0) > 0 else "rejected",
            )

        if not approved_specs:
            result.summary = (
                f"Regime '{result.regime}'. The committee approved "
                f"{len(approved_by_critic)} trade(s); the Risk Guard cleared none."
            )
            return

        # 8. Execute --------------------------------------------------------
        orders = self.execution.submit_orders(approved_specs, cycle_id=cycle_id)
        result.orders_submitted = orders
        for order in orders:
            self.store.record_order(order, cycle_id, order.get("trade_id", ""))
            if order.get("ok"):
                self.store.update_trade(order.get("trade_id", ""), status="submitted")
        self.store.record_trace(cycle_id, "execution", orders)

        filled = sum(1 for o in orders if o.get("ok"))
        result.summary = (
            f"Regime '{result.regime}'. {len(structures)} structure(s) built, "
            f"{len(approved_by_critic)} approved by the committee, {len(approved_specs)} cleared "
            f"by the Risk Guard, {filled} order(s) "
            f"{'simulated (dry run)' if self.dry_run else 'submitted'}."
        )

    # -- risk gate ----------------------------------------------------------

    def _gate_risk(
        self,
        trades: list[dict[str, Any]],
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        snapshot: dict[str, Any],
        max_new_trades: int | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the Risk Guard and emit execution specs for survivors only."""
        today = today_et().isoformat()
        todays_trades = [
            t for t in self.store.get_trades(limit=200) if str(t.get("created_at", "")).startswith(today)
        ]
        portfolio = build_portfolio(
            account,
            positions,
            snapshot,
            trades_today=len(todays_trades),
            tickers_traded_today=[t.get("ticker", "") for t in todays_trades],
            peak_equity=self._peak_equity(account),
        )

        candidates = [
            CandidateTrade(
                trade_id=trade["trade_id"],
                symbol_or_contract=(trade["legs"][0]["contract_symbol"] if trade.get("legs") else trade.get("ticker", "")),
                asset_class="option",
                side="buy" if trade.get("net_side") == "debit" else "sell",
                qty=1,
                estimated_notional=trade.get("estimated_notional"),
                delta=trade.get("net_delta"),
                gamma=trade.get("net_gamma"),
                vega=trade.get("net_vega"),
                theta=trade.get("net_theta"),
                underlying=trade.get("ticker"),
                days_to_expiry=trade.get("days_to_expiry"),
                max_loss=trade.get("max_loss"),
                max_profit=trade.get("max_profit"),
                playbook=trade.get("playbook"),
                legs=trade.get("legs", []),
            )
            for trade in trades
        ]

        decision = self.risk_guard.check(portfolio, candidates)
        payload = decision.model_dump()

        specs: list[dict[str, Any]] = []
        for verdict in decision.approved_trades:
            if max_new_trades is not None and len(specs) >= max_new_trades:
                break
            trade = next(t for t in trades if t["trade_id"] == verdict.trade_id)
            if not trade.get("legs"):
                continue
            specs.append(
                {
                    "trade_id": trade["trade_id"],
                    "qty": verdict.approved_qty,
                    "type": self.settings.execution.order_type,
                    "limit_price": self._signed_limit(trade),
                    "time_in_force": self.settings.execution.time_in_force,
                    "legs": trade["legs"],
                }
            )
        return payload, specs

    def _signed_limit(self, trade: dict[str, Any]) -> float:
        """Positive limit for a net debit, negative for a net credit."""
        from desk.utils.math_utils import round_to_tick

        price = float(trade.get("net_price", 0) or 0)
        edge = self.settings.execution.marketable_edge_pct
        tick = self.settings.execution.round_limit_to
        if trade.get("net_side") == "debit":
            return round_to_tick(price * (1 + edge), tick)
        return -round_to_tick(price * (1 - edge), tick)

    def _peak_equity(self, account: dict[str, Any]) -> float:
        """Highest equity ever recorded — the denominator for the drawdown halt."""
        curve = self.store.equity_curve(limit=1000)
        equities = [float(point["equity"]) for point in curve if point.get("equity")]
        equities.append(float(account.get("equity", 0) or 0))
        return max(equities) if equities else 0.0

    # -- end-of-day journalling ---------------------------------------------

    def run_journal(self, cycle_id: str | None = None, save_post: bool = True) -> dict[str, Any]:
        """Post-trade review plus the day's social post."""
        today = today_et().isoformat()
        trades = [
            t for t in self.store.get_trades(limit=200) if str(t.get("created_at", "")).startswith(today)
        ]
        account = self.store.latest_account() or {}
        traces = self.store.get_traces(cycle_id, limit=200) if cycle_id else []
        rejections = [
            trace["payload"]
            for trace in traces
            if trace.get("stage") == "risk_guard"
        ]
        risk_rejections = [
            verdict
            for payload in rejections
            for verdict in (payload.get("trades", []) if isinstance(payload, dict) else [])
            if verdict.get("verdict") == Verdict.REJECT
        ]

        metrics = {
            "equity": account.get("equity"),
            "day_pnl": account.get("daily_pnl"),
            "realised_pnl": sum(float(t.get("pnl") or 0) for t in trades),
            "unrealised_pnl": sum(
                float(p.get("unrealized_pl") or 0) for p in self.store.latest_positions()
            ),
            "trades": len(trades),
            "wins": sum(1 for t in trades if (t.get("pnl") or 0) > 0),
            "losses": sum(1 for t in trades if (t.get("pnl") or 0) < 0),
        }
        self.store.record_daily_metrics(metrics, today)

        coach_result = self.coach.run(
            period=today,
            trades=trades,
            traces=traces,
            metrics=metrics,
            risk_rejections=risk_rejections,
            account=account,
            positions=self.store.latest_positions(),
        )
        lessons = coach_result.output.get("lessons_for_tomorrow", [])

        critic_trace = next((t for t in traces if t.get("stage") == "critic"), None)
        disagreements = []
        if critic_trace and isinstance(critic_trace.get("payload"), dict):
            disagreements = (critic_trace["payload"].get("output") or {}).get(
                "notable_disagreements", []
            )

        story_result = self.storyteller.run(
            date=today,
            regime=(self.store.get_cycles(1) or [{}])[0].get("regime", ""),
            trades=trades,
            risk_decisions=rejections,
            disagreements=disagreements,
            coach_lessons=lessons,
            metrics=metrics,
        )

        post_path = None
        if save_post:
            post_path = str(self.storyteller.save_post(story_result.output, today))

        try:
            get_registry().log_live_run(self.settings.experiment_id, metrics, today)
        except Exception as exc:  # noqa: BLE001 - journalling must not fail the day
            logger.warning(
                "experiment_log_failed",
                extra={"event": "experiment_log_failed", "error": str(exc)},
            )

        if cycle_id:
            self.store.record_trace(cycle_id, "coach", coach_result.model_dump(), agent="coach")
            self.store.record_trace(cycle_id, "storyteller", story_result.model_dump(), agent="storyteller")

        return {
            "date": today,
            "metrics": metrics,
            "coach": coach_result.output,
            "story": story_result.output,
            "post_path": post_path,
        }
