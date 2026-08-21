"""Competition mode — the Aug 28 to Sep 4 run plan.

Encodes the daily schedule and the week's arc so the desk's behaviour over the
hackathon is a property of configuration rather than of whoever is at the
keyboard: explore small on days 1-2, concentrate on what worked on days 3-5,
then freeze prompts and settings and run consistently to the finish.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from desk.orchestrator.orchestrator_agent import CycleResult, Orchestrator
from desk.utils.config_loader import CompetitionPhase, Settings, get_settings
from desk.utils.logging import get_logger
from desk.utils.time_utils import (
    ET,
    is_trading_day,
    now_et,
    session_phase,
    today_et,
    trading_days_between,
)

logger = get_logger("orchestrator.competition")

PHASE_ORDER = ("premarket", "morning", "midday", "eod")


class CompetitionRunner:
    """Drives the desk through the competition's daily and weekly structure."""

    def __init__(self, orchestrator: Orchestrator | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.orchestrator = orchestrator or Orchestrator(settings=self.settings)
        self.competition = self.settings.competition

    # -- calendar ----------------------------------------------------------

    def competition_day(self, day: date | None = None) -> int:
        """1-indexed *trading* day of the competition. Zero when outside the window.

        Counting trading days rather than calendar days matters: the window
        opens on a Friday, so a calendar count would spend the exploration
        phase on a Saturday and reach the freeze phase a full session early.
        """
        day = day or today_et()
        try:
            start = date.fromisoformat(self.competition.start_date)
            end = date.fromisoformat(self.competition.end_date)
        except ValueError:
            return 0
        if not (start <= day <= end):
            return 0
        sessions = trading_days_between(start, day)
        if not sessions:
            return 0
        # A non-trading day inherits the number of the session before it, so
        # weekend maintenance runs report the phase they belong to.
        return len(sessions) if is_trading_day(day) else max(len(sessions), 1)

    def current_phase(self, moment: datetime | None = None) -> CompetitionPhase | None:
        """The week-arc phase (exploration / concentration / freeze) for today."""
        day = self.competition_day((moment or now_et()).date())
        return self.competition.phase_for_day(day) if day else self.competition.phases[-1] if self.competition.phases else None

    def scheduled_cycle(self, moment: datetime | None = None) -> str:
        """Which daily cycle is due now, by ET wall clock."""
        moment = moment or now_et()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ET)
        clock = moment.astimezone(ET).time()

        due = "closed"
        for name in PHASE_ORDER:
            entry = self.competition.schedule.get(name) or {}
            at = entry.get("at")
            if not at:
                continue
            try:
                hour, minute = (int(part) for part in str(at).split(":"))
            except ValueError:
                continue
            if (clock.hour, clock.minute) >= (hour, minute):
                due = name
        return due

    def limits_for_today(self, day: date | None = None) -> dict[str, Any]:
        """Effective per-day caps after applying the week-arc phase."""
        day = day or today_et()
        number = self.competition_day(day)
        phase = self.competition.phase_for_day(number) if number else None
        schedule_caps = {
            name: int((self.competition.schedule.get(name) or {}).get("max_new_trades", 0))
            for name in PHASE_ORDER
        }
        return {
            "competition_day": number,
            "phase": phase.name if phase else "unscheduled",
            "size_multiplier": phase.size_multiplier if phase else 1.0,
            "max_trades_per_day": phase.max_trades_per_day if phase else 0,
            "allowed_playbooks": phase.allowed_playbooks if phase else ["all"],
            "freeze_prompts": phase.freeze_prompts if phase else False,
            "per_cycle_caps": schedule_caps,
        }

    # -- execution ---------------------------------------------------------

    def run_scheduled_cycle(
        self,
        cycle: str | None = None,
        calendar: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run whichever cycle is due (or a named one), honouring today's caps."""
        cycle = cycle or self.scheduled_cycle()
        today = today_et()
        limits = self.limits_for_today(today)

        if not force and not is_trading_day(today):
            return {
                "ran": False,
                "cycle": cycle,
                "reason": f"{today.isoformat()} is not a US trading day.",
                "limits": limits,
            }
        if not force and cycle == "closed":
            return {
                "ran": False,
                "cycle": cycle,
                "reason": f"Market phase is '{session_phase()}'; no cycle is scheduled now.",
                "limits": limits,
            }

        if cycle == "eod":
            journal = self.orchestrator.run_journal()
            logger.info(
                "competition_journal",
                extra={"event": "competition_journal", "day": limits["competition_day"]},
            )
            return {"ran": True, "cycle": "eod", "limits": limits, "journal": journal}

        if cycle == "premarket":
            # Pre-market is analysis only; the schedule caps new trades at zero.
            result = self.orchestrator.run_cycle(
                phase="premarket", max_new_trades=0, calendar=calendar
            )
            return {"ran": True, "cycle": "premarket", "limits": limits, "result": result.model_dump()}

        cap = min(
            int(limits["per_cycle_caps"].get(cycle, 0)),
            int(limits["max_trades_per_day"]),
        )
        result: CycleResult = self.orchestrator.run_cycle(
            phase=cycle, max_new_trades=cap, calendar=calendar
        )
        logger.info(
            "competition_cycle",
            extra={
                "event": "competition_cycle",
                "day": limits["competition_day"],
                "phase_name": limits["phase"],
                "cycle": cycle,
                "cap": cap,
                "approved": result.trades_approved,
            },
        )
        return {"ran": True, "cycle": cycle, "limits": limits, "result": result.model_dump()}

    def plan(self) -> list[dict[str, Any]]:
        """Human-readable run plan for the whole competition window."""
        try:
            start = date.fromisoformat(self.competition.start_date)
            end = date.fromisoformat(self.competition.end_date)
        except ValueError:
            return []

        from datetime import timedelta

        rows, cursor = [], start
        while cursor <= end:
            limits = self.limits_for_today(cursor)
            rows.append(
                {
                    "date": cursor.isoformat(),
                    "weekday": cursor.strftime("%a"),
                    "trading_day": is_trading_day(cursor),
                    "competition_day": limits["competition_day"],
                    "phase": limits["phase"],
                    "max_trades": limits["max_trades_per_day"] if is_trading_day(cursor) else 0,
                    "size_multiplier": limits["size_multiplier"],
                    "prompts_frozen": limits["freeze_prompts"],
                }
            )
            cursor += timedelta(days=1)
        return rows
