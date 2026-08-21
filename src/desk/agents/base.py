"""Base class for every LLM-backed agent on the desk.

Three properties matter here:

1. **Schema-validated output.** Every agent declares a pydantic output model and
   the response is parsed against it, so downstream code never guesses at shape.
2. **Offline fallback.** With no ``ANTHROPIC_API_KEY`` the agent produces a
   deterministic, feature-derived mock instead of failing. The whole pipeline —
   research, critic, risk gate, execution, journalling — runs end to end with no
   credentials, which is what makes the test suite meaningful.
3. **Graceful degradation.** An agent that errors, times out, or is refused
   returns an *abstention*. One broken specialist never takes down a cycle.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, Field

from desk.utils.config_loader import Settings, get_settings, load_prompt
from desk.utils.logging import get_logger
from desk.utils.time_utils import utc_iso

logger = get_logger("agents")


class AgentResult(BaseModel):
    """Uniform envelope returned by every agent."""

    agent: str
    ok: bool = True
    mode: str = "mock"  # "llm" | "mock" | "error"
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    tokens: dict[str, int] = Field(default_factory=dict)
    ran_at: str = ""

    @property
    def abstained(self) -> bool:
        return not self.ok or self.mode == "error"


class LLMAgent:
    """A persona-driven agent with a validated JSON output contract."""

    #: Registry key — also the config key under ``agents.enabled`` and ``llm.models``.
    name: str = "agent"
    #: File stem under ``config/prompts/``.
    prompt_name: str = "agent"
    #: Pydantic model the response must satisfy.
    output_model: type[BaseModel] | None = None

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._prompt: str | None = None

    # -- persona -----------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        """The agent's persona, loaded once from ``config/prompts``."""
        if self._prompt is None:
            try:
                self._prompt = load_prompt(self.prompt_name)
            except FileNotFoundError:
                logger.warning(
                    "prompt_missing",
                    extra={"event": "prompt_missing", "agent": self.name, "prompt": self.prompt_name},
                )
                self._prompt = f"You are the {self.name} on a multi-agent options trading desk."
        return self._prompt

    @property
    def model(self) -> str:
        return self.settings.llm.model_for(self.name)

    # -- Claude client -----------------------------------------------------

    @property
    def client(self) -> Any:
        """Lazily constructed Anthropic client; ``None`` when no key is set."""
        if self._client is None and self.settings.llm.enabled:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self.settings.llm.api_key,
                timeout=float(self.settings.llm.timeout_seconds),
                max_retries=2,
            )
        return self._client

    @property
    def llm_available(self) -> bool:
        return self.settings.llm.enabled and self.output_model is not None

    # -- subclass hooks ----------------------------------------------------

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        """Assemble the JSON payload handed to the model. Override in subclasses."""
        return dict(kwargs)

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Deterministic offline output derived from ``context``.

        Subclasses override this. It must be a pure function of the context —
        no randomness — so tests and demos are reproducible.
        """
        return {"note": f"{self.name} mock output", "context_keys": sorted(context)}

    def user_message(self, context: dict[str, Any]) -> str:
        """Render the context as the user turn."""
        return (
            "Analyse the following desk snapshot and respond with the JSON object "
            "your role specifies.\n\n```json\n"
            + json.dumps(context, indent=2, default=str, sort_keys=True)
            + "\n```"
        )

    # -- execution ---------------------------------------------------------

    def run(self, **kwargs: Any) -> AgentResult:
        """Run the agent, falling back to mock output and never raising."""
        started = time.monotonic()
        try:
            context = self.build_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a bad context is an abstention
            return self._error_result(f"context build failed: {exc}", started)

        if not self.llm_available:
            output = self.mock_output(context)
            return AgentResult(
                agent=self.name,
                ok=True,
                mode="mock",
                output=output,
                latency_ms=(time.monotonic() - started) * 1000,
                ran_at=utc_iso(),
            )

        try:
            return self._call_claude(context, started)
        except Exception as exc:  # noqa: BLE001 - degrade, do not crash the cycle
            logger.warning(
                "agent_llm_failed",
                extra={"event": "agent_llm_failed", "agent": self.name, "error": str(exc)[:300]},
            )
            if self.settings.agents.abstain_on_error:
                output = self.mock_output(context)
                return AgentResult(
                    agent=self.name,
                    ok=True,
                    mode="mock",
                    output=output,
                    error=f"LLM unavailable, used deterministic fallback: {exc}",
                    latency_ms=(time.monotonic() - started) * 1000,
                    ran_at=utc_iso(),
                )
            return self._error_result(str(exc), started)

    def _call_claude(self, context: dict[str, Any], started: float) -> AgentResult:
        """One structured-output call to the Messages API."""
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.llm.max_tokens,
            "system": self.system_prompt,
            "messages": [{"role": "user", "content": self.user_message(context)}],
            "output_format": self.output_model,
            "output_config": {"effort": self.settings.llm.effort},
        }
        if self.settings.llm.thinking:
            request["thinking"] = {"type": "adaptive"}

        response = self.client.messages.parse(**request)

        # Safety classifiers can decline a request with HTTP 200. Treat that as
        # an abstention rather than reading an empty content list.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(
                f"model refused the request (category={getattr(details, 'category', None)})"
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ValueError("response contained no parsable structured output")

        usage = getattr(response, "usage", None)
        return AgentResult(
            agent=self.name,
            ok=True,
            mode="llm",
            output=parsed.model_dump() if isinstance(parsed, BaseModel) else dict(parsed),
            model=self.model,
            latency_ms=(time.monotonic() - started) * 1000,
            tokens={
                "input": int(getattr(usage, "input_tokens", 0) or 0),
                "output": int(getattr(usage, "output_tokens", 0) or 0),
            }
            if usage
            else {},
            ran_at=utc_iso(),
        )

    def _error_result(self, message: str, started: float) -> AgentResult:
        logger.error("agent_error", extra={"event": "agent_error", "agent": self.name, "error": message})
        return AgentResult(
            agent=self.name,
            ok=False,
            mode="error",
            error=message,
            latency_ms=(time.monotonic() - started) * 1000,
            ran_at=utc_iso(),
        )

    async def arun(self, **kwargs: Any) -> AgentResult:
        """Async wrapper so the orchestrator can fan agents out concurrently."""
        timeout = self.settings.agents.timeout_seconds
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.run, **kwargs), timeout=timeout)
        except TimeoutError:
            return AgentResult(
                agent=self.name,
                ok=False,
                mode="error",
                error=f"timed out after {timeout}s — abstaining",
                ran_at=utc_iso(),
            )
