"""Configuration loading and validation.

Reads ``config/settings.yaml``, interpolates ``${VAR}`` / ``${VAR:-default}``
from the environment (``.env`` is loaded first), and validates the result into
pydantic models. A typo in the YAML becomes a startup error rather than a
surprising trade.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
PROMPTS_DIR = CONFIG_DIR / "prompts"
PLAYBOOKS_PATH = CONFIG_DIR / "strategies" / "playbooks.yaml"
EXPERIMENTS_PATH = CONFIG_DIR / "experiments" / "experiments.json"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def load_dotenv_once() -> None:
    """Load ``.env`` from the project root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional at runtime
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def interpolate_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` in loaded YAML."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name) or (default if default is not None else "")

        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v) for v in value]
    return value


def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce env-interpolated strings like ``"true"`` into real booleans."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AlpacaConfig(BaseModel):
    api_key_id: str = ""
    api_secret_key: str = ""
    base_url: str = "https://paper-api.alpaca.markets"
    data_feed: str = "iex"
    paper_only: bool = True
    max_retries: int = 4
    backoff_base_seconds: float = 0.6
    request_timeout_seconds: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.api_key_id and self.api_secret_key)

    @property
    def is_paper_endpoint(self) -> bool:
        return "paper-api" in self.base_url


class LLMConfig(BaseModel):
    api_key: str = ""
    default_model: str = "claude-opus-5"
    max_tokens: int = 8000
    effort: str = "high"
    thinking: bool = True
    timeout_seconds: int = 120
    models: dict[str, str] = Field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        """True when a real Claude key is present; otherwise agents run mocked."""
        return bool(self.api_key.strip())

    def model_for(self, agent: str) -> str:
        return self.models.get(agent, self.default_model)


class UniverseConfig(BaseModel):
    core: list[str] = Field(default_factory=list)
    satellites: list[str] = Field(default_factory=list)
    max_active_tickers: int = 6
    min_underlying_price: float = 20.0
    min_avg_dollar_volume: float = 50_000_000.0

    @property
    def all_tickers(self) -> list[str]:
        seen: list[str] = []
        for ticker in [*self.core, *self.satellites]:
            if ticker not in seen:
                seen.append(ticker)
        return seen


class OptionsConfig(BaseModel):
    min_days_to_expiry: int = 7
    max_days_to_expiry: int = 60
    target_days_to_expiry: int = 30
    min_open_interest: int = 250
    min_volume: int = 10
    max_bid_ask_spread_pct: float = 0.12
    max_spread_abs: float = 0.60
    contract_multiplier: int = 100
    strike_selection: str = "delta"
    risk_free_rate: float = 0.043


class RiskLimitsConfig(BaseModel):
    max_notional_per_trade: float = 2500.0
    max_notional_total: float = 20000.0
    max_exposure_per_ticker: float = 5000.0
    max_contracts_per_ticker: int = 10
    max_contracts_per_trade: int = 5
    max_delta_total: float = 250.0
    max_gamma_total: float = 25.0
    max_vega_total: float = 800.0
    max_theta_total: float = 400.0
    min_days_to_expiry: int = 7
    max_open_positions: int = 12
    max_trades_per_day: int = 6
    max_new_tickers_per_day: int = 3
    allow_undefined_risk: bool = False
    allow_naked_short_calls: bool = False
    require_defined_max_loss: bool = True
    min_cash_buffer_pct: float = 0.30
    max_buying_power_utilisation: float = 0.50
    max_daily_loss_pct: float = 0.03
    max_drawdown_halt_pct: float = 0.10


class RegimeThresholds(BaseModel):
    trend_adx_min: float = 22.0
    trend_ema_slope_min: float = 0.0008
    range_bandwidth_max: float = 0.045
    high_vol_iv_rank_min: float = 0.65
    high_vol_atr_pct_min: float = 0.020
    event_window_days: int = 3


class RegimeConfig(BaseModel):
    benchmark: str = "SPY"
    lookback_days: int = 120
    fast_ema: int = 20
    slow_ema: int = 50
    adx_period: int = 14
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    thresholds: RegimeThresholds = Field(default_factory=RegimeThresholds)
    llm_override_confidence: float = 0.80
    labels: list[str] = Field(
        default_factory=lambda: ["trend_up", "trend_down", "range", "high_vol_event"]
    )


class AgentsConfig(BaseModel):
    timeout_seconds: int = 90
    max_candidates_per_agent: int = 3
    abstain_on_error: bool = True
    enabled: dict[str, bool] = Field(default_factory=dict)

    def is_enabled(self, name: str) -> bool:
        return self.enabled.get(name, True)


class CriticConfig(BaseModel):
    max_approved_trades: int = 3
    min_conviction: float = 0.55
    max_structures_per_ticker: int = 1
    reject_if_regime_mismatch: bool = True


class ExecutionConfig(BaseModel):
    dry_run: bool = True
    order_type: str = "limit"
    limit_price_mode: str = "mid"
    marketable_edge_pct: float = 0.02
    time_in_force: str = "day"
    fill_poll_seconds: int = 3
    fill_timeout_seconds: int = 45
    cancel_unfilled: bool = True
    round_limit_to: float = 0.01

    @field_validator("dry_run", mode="before")
    @classmethod
    def _coerce_dry_run(cls, v: Any) -> bool:
        # Defaults to True: an unparseable value must never mean "trade live".
        return as_bool(v, default=True)


class CompetitionPhase(BaseModel):
    days: list[int]
    name: str
    size_multiplier: float = 1.0
    max_trades_per_day: int = 3
    allowed_playbooks: list[str] = Field(default_factory=lambda: ["all"])
    freeze_prompts: bool = False


class CompetitionConfig(BaseModel):
    enabled: bool = True
    timezone: str = "America/New_York"
    start_date: str = ""
    end_date: str = ""
    schedule: dict[str, dict[str, Any]] = Field(default_factory=dict)
    phases: list[CompetitionPhase] = Field(default_factory=list)

    def phase_for_day(self, day: int) -> CompetitionPhase | None:
        for phase in self.phases:
            if day in phase.days:
                return phase
        return self.phases[-1] if self.phases else None


class BacktestConfig(BaseModel):
    start: str = "2025-01-02"
    end: str = "2025-06-30"
    initial_capital: float = 100_000.0
    slippage_model: str = "half_spread"
    fixed_slippage_pct: float = 0.01
    commission_per_contract: float = 0.65
    fill_probability: float = 0.95
    cache_dir: str = "data/cache"


class MonitorConfig(BaseModel):
    db_path: str = "monitor/state.db"
    heartbeat_path: str = "monitor/heartbeat.json"
    heartbeat_stale_seconds: int = 3600
    log_dir: str = "logs"
    log_level: str = "INFO"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    retain_traces_days: int = 90

    @field_validator("dashboard_port", mode="before")
    @classmethod
    def _port(cls, v: Any) -> int:
        try:
            return int(str(v).strip() or 8787)
        except ValueError:
            return 8787

    @field_validator("log_level", mode="before")
    @classmethod
    def _level(cls, v: Any) -> str:
        return (str(v).strip() or "INFO").upper()


class SocialConfig(BaseModel):
    output_dir: str = "social/daily_posts"
    platforms: list[str] = Field(default_factory=lambda: ["x", "linkedin"])
    max_chars_x: int = 280
    hashtags: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    """Fully validated desk configuration."""

    config_version: str = "1.0.0"
    experiment_id: str = "baseline-v1"
    alpaca: AlpacaConfig = Field(default_factory=AlpacaConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    risk_limits: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    critic: CriticConfig = Field(default_factory=CriticConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    competition: CompetitionConfig = Field(default_factory=CompetitionConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file with environment interpolation applied."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return interpolate_env(raw)


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate settings (uncached — prefer :func:`get_settings`)."""
    load_dotenv_once()
    return Settings.model_validate(load_yaml(path or SETTINGS_PATH))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return load_settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read from disk (used by tests and the CLI)."""
    get_settings.cache_clear()
    return get_settings()


@lru_cache(maxsize=1)
def load_playbooks() -> dict[str, Any]:
    """Load the regime -> options playbook library."""
    return load_yaml(PLAYBOOKS_PATH)


def playbooks_for_regime(regime: str) -> list[dict[str, Any]]:
    """Return the playbooks legal for ``regime`` (empty list if unknown)."""
    regimes = load_playbooks().get("regimes", {})
    return list(regimes.get(regime, {}).get("playbooks", []))


def forbidden_structures() -> list[str]:
    """Structure names that are rejected unconditionally."""
    return list(load_playbooks().get("forbidden", []))


def load_prompt(name: str) -> str:
    """Load an agent persona from ``config/prompts/<name>.md``."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")
