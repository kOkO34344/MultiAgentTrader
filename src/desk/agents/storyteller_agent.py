"""Storyteller — turns a day of machine decisions into an honest build-log post.

Hard rule enforced in code as well as in the persona: every number in a post
comes from the logs. Offline mode composes from real recorded values only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent
from desk.utils.config_loader import PROJECT_ROOT
from desk.utils.logging import get_logger
from desk.utils.time_utils import today_et

logger = get_logger("agents.storyteller")


class KeyNumber(BaseModel):
    label: str = ""
    value: str = ""


class StoryOutput(BaseModel):
    date: str = ""
    headline: str = ""
    post_text_x: str = ""
    post_text_linkedin: str = ""
    key_numbers: list[KeyNumber] = Field(default_factory=list)
    story_angle: str = "infrastructure"
    visuals: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    notes_for_human: str = ""


class StorytellerAgent(LLMAgent):
    """Generates the daily X/LinkedIn post from the day's record."""

    name = "storyteller"
    prompt_name = "storyteller_agent"
    output_model = StoryOutput

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "date": kwargs.get("date", today_et().isoformat()),
            "regime": kwargs.get("regime", ""),
            "cycle_summary": kwargs.get("cycle_summary", {}),
            "trades": kwargs.get("trades", []),
            "risk_decisions": kwargs.get("risk_decisions", []),
            "disagreements": kwargs.get("disagreements", []),
            "coach_lessons": kwargs.get("coach_lessons", []),
            "metrics": kwargs.get("metrics", {}),
            "constraints": {
                "max_chars_x": self.settings.social.max_chars_x,
                "hashtags": self.settings.social.hashtags,
                "rules": [
                    "Never invent a number that is not in this payload.",
                    "Always say 'paper' when referring to P&L.",
                    "No hype. Understatement is more credible.",
                ],
            },
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Compose a factual post from recorded values only."""
        metrics = context.get("metrics") or {}
        trades = context.get("trades") or []
        rejections = [
            r for r in (context.get("risk_decisions") or []) if r.get("verdict") == "REJECT"
        ]
        disagreements = context.get("disagreements") or []
        regime = context.get("regime", "unknown")
        date = context.get("date", today_et().isoformat())

        day_pnl = metrics.get("day_pnl")
        equity = metrics.get("equity")

        if disagreements:
            angle = "agent_disagreement"
            headline = "Six analysts, one committee, and a disagreement worth reading"
        elif rejections:
            angle = "risk_guard_save"
            headline = f"The Risk Guard blocked {len(rejections)} proposal(s) today"
        elif trades:
            angle = "regime_flip" if regime == "high_vol_event" else "honest_loss"
            headline = f"{len(trades)} defined-risk structure(s) into a '{regime}' regime"
        else:
            angle = "infrastructure"
            headline = "A quiet day: the desk chose not to trade"

        key_numbers = [{"label": "Regime", "value": str(regime)}]
        if day_pnl is not None:
            key_numbers.append({"label": "Day P&L (paper)", "value": f"${float(day_pnl):,.2f}"})
        if equity is not None:
            key_numbers.append({"label": "Equity (paper)", "value": f"${float(equity):,.2f}"})
        key_numbers.append({"label": "Trades placed", "value": str(len(trades))})
        key_numbers.append({"label": "Risk Guard rejections", "value": str(len(rejections))})

        hashtags = list(self.settings.social.hashtags)
        x_body = (
            f"Day {date}: regime '{regime}'. "
            f"{len(trades)} defined-risk options structure(s) placed on paper, "
            f"{len(rejections)} blocked by the deterministic risk guard."
        )
        tags = " ".join(hashtags[:2])
        limit = self.settings.social.max_chars_x
        available = limit - len(tags) - 1
        post_x = f"{x_body[:available].rstrip()} {tags}".strip()

        linkedin_paragraphs = [
            f"**{headline}**",
            (
                f"Today the Multi-Agent Options Desk classified the market as '{regime}'. "
                "Six research agents ran in parallel — fundamental, technical, sentiment, "
                "volatility, regime, and event — and an investment-committee critic selected "
                f"{len(trades)} structure(s) from their proposals."
            ),
            (
                f"Every candidate then passed through a deterministic Python risk guard. "
                f"It rejected {len(rejections)} of them. That guard cannot be argued with by "
                "any agent, which is the point: the most dangerous failure mode of an LLM "
                "trading system is a persuasive argument for an oversized position."
            ),
        ]
        if day_pnl is not None:
            linkedin_paragraphs.append(f"Paper P&L for the day: ${float(day_pnl):,.2f}.")
        if context.get("coach_lessons"):
            linkedin_paragraphs.append(
                "Coach's lesson for tomorrow: " + str(context["coach_lessons"][0])
            )
        linkedin_paragraphs.append(" ".join(hashtags))

        return {
            "date": date,
            "headline": headline,
            "post_text_x": post_x[:limit],
            "post_text_linkedin": "\n\n".join(linkedin_paragraphs),
            "key_numbers": key_numbers,
            "story_angle": angle,
            "visuals": [
                "P&L curve from the web dashboard (`desk dashboard --web`)",
                "Decision trace for the day's highest-conviction trade",
            ],
            "hashtags": hashtags,
            "notes_for_human": (
                "Generated offline from logged values only — no figure here was invented. "
                "Verify the P&L against the Alpaca paper dashboard before posting."
            ),
        }

    def save_post(self, output: dict[str, Any], date: str | None = None) -> Path:
        """Write the post to ``social/daily_posts/YYYY-MM-DD.md``."""
        date = date or output.get("date") or today_et().isoformat()
        directory = PROJECT_ROOT / self.settings.social.output_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{date}.md"

        numbers = "\n".join(
            f"| {n.get('label', '')} | {n.get('value', '')} |" for n in output.get("key_numbers", [])
        )
        visuals = "\n".join(f"- {v}" for v in output.get("visuals", []))

        path.write_text(
            f"""# {date} — {output.get('headline', '')}

*Story angle: `{output.get('story_angle', '')}`*

## X / Twitter

```text
{output.get('post_text_x', '')}
```

*({len(output.get('post_text_x', ''))} / {self.settings.social.max_chars_x} characters)*

## LinkedIn

{output.get('post_text_linkedin', '')}

## Key numbers

| Metric | Value |
|---|---|
{numbers}

## Suggested visuals

{visuals}

---

> {output.get('notes_for_human', '')}
""",
            encoding="utf-8",
        )
        logger.info("post_saved", extra={"event": "post_saved", "path": str(path)})
        return path
