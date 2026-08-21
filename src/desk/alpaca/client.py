"""Alpaca client construction, the paper-only guard, and retry handling.

Every network call the desk makes goes through :func:`with_retry`, and every
client is built by :class:`AlpacaClients`, which refuses outright to construct
against a live brokerage endpoint. This is the single chokepoint that makes
"paper only" a property of the code rather than a promise in the README.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar

from desk.utils.config_loader import Settings, get_settings
from desk.utils.logging import get_logger

logger = get_logger("alpaca.client")

T = TypeVar("T")

LIVE_ENDPOINT_MARKERS = ("api.alpaca.markets",)
PAPER_ENDPOINT_MARKER = "paper-api.alpaca.markets"


class PaperOnlyError(RuntimeError):
    """Raised when configuration points at anything other than paper trading."""


class AlpacaNotConfiguredError(RuntimeError):
    """Raised when API credentials are missing."""


class RetryableError(RuntimeError):
    """A transient failure worth retrying (rate limit, 5xx, connection reset)."""


def is_retryable(exc: BaseException) -> bool:
    """Classify an exception as transient.

    Alpaca's SDK surfaces HTTP failures as ``APIError`` with a status code, and
    the underlying transport raises connection errors, so both are inspected.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    text = str(exc).lower()
    markers = (
        "too many requests",
        "rate limit",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    return any(marker in text for marker in markers)


def with_retry(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 4,
    backoff_base: float = 0.6,
    description: str = "",
    **kwargs: Any,
) -> T:
    """Call ``func`` with exponential backoff and jitter on transient failures.

    Non-retryable errors (400s other than 429) propagate immediately — retrying
    a malformed order just wastes the rate-limit budget.
    """
    label = description or getattr(func, "__name__", "alpaca_call")
    last_error: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            last_error = exc
            if not is_retryable(exc) or attempt == max_retries:
                raise
            delay = backoff_base * (2**attempt) + random.uniform(0, backoff_base)
            logger.warning(
                "alpaca_retry",
                extra={
                    "event": "alpaca_retry",
                    "call": label,
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "delay_seconds": round(delay, 2),
                    "error": str(exc)[:200],
                },
            )
            time.sleep(delay)

    raise RetryableError(f"{label} failed after {max_retries} retries") from last_error


def assert_paper_endpoint(base_url: str, paper_only: bool = True) -> None:
    """Refuse any endpoint that is not Alpaca paper trading.

    Deliberately allow-list based: an unrecognised host is rejected too, because
    an unknown endpoint is exactly the case where a mistake is most expensive.
    """
    if not paper_only:
        raise PaperOnlyError(
            "alpaca.paper_only was disabled. This desk is built for the hackathon's "
            "paper account and refuses to run against real money."
        )
    url = (base_url or "").strip().lower()
    if PAPER_ENDPOINT_MARKER in url:
        return
    for marker in LIVE_ENDPOINT_MARKERS:
        if marker in url:
            raise PaperOnlyError(
                f"Refusing to connect: '{base_url}' is a LIVE trading endpoint. "
                f"Set ALPACA_BASE_URL=https://{PAPER_ENDPOINT_MARKER}"
            )
    raise PaperOnlyError(
        f"Refusing to connect: '{base_url}' is not a recognised Alpaca paper endpoint. "
        f"Expected a URL containing '{PAPER_ENDPOINT_MARKER}'."
    )


@dataclass
class AlpacaClients:
    """Lazily constructed Alpaca SDK clients, guarded to paper trading."""

    settings: Settings

    _trading: Any = None
    _stock_data: Any = None
    _option_data: Any = None

    def __post_init__(self) -> None:
        assert_paper_endpoint(self.settings.alpaca.base_url, self.settings.alpaca.paper_only)

    # -- guards ------------------------------------------------------------

    def _require_credentials(self) -> tuple[str, str]:
        config = self.settings.alpaca
        if not config.configured:
            raise AlpacaNotConfiguredError(
                "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set. "
                "Copy .env.example to .env and fill in your PAPER keys."
            )
        return config.api_key_id, config.api_secret_key

    # -- clients -----------------------------------------------------------

    @property
    def trading(self) -> Any:
        """``alpaca.trading.TradingClient``, pinned to paper."""
        if self._trading is None:
            from alpaca.trading.client import TradingClient

            key, secret = self._require_credentials()
            self._trading = TradingClient(api_key=key, secret_key=secret, paper=True)
            logger.info(
                "alpaca_trading_client_ready",
                extra={"event": "alpaca_trading_client_ready", "paper": True},
            )
        return self._trading

    @property
    def stock_data(self) -> Any:
        """``alpaca.data.StockHistoricalDataClient``."""
        if self._stock_data is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            key, secret = self._require_credentials()
            self._stock_data = StockHistoricalDataClient(api_key=key, secret_key=secret)
        return self._stock_data

    @property
    def option_data(self) -> Any:
        """``alpaca.data.OptionHistoricalDataClient``."""
        if self._option_data is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient

            key, secret = self._require_credentials()
            self._option_data = OptionHistoricalDataClient(api_key=key, secret_key=secret)
        return self._option_data

    # -- helpers -----------------------------------------------------------

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Invoke an SDK method under this desk's retry policy."""
        config = self.settings.alpaca
        return with_retry(
            func,
            *args,
            max_retries=config.max_retries,
            backoff_base=config.backoff_base_seconds,
            description=getattr(func, "__name__", "alpaca_call"),
            **kwargs,
        )

    def account(self) -> Any:
        """Raw account object from the Trading API."""
        return self.call(self.trading.get_account)

    def verify_paper_account(self) -> dict[str, Any]:
        """Confirm the connected account really is a paper account.

        The endpoint guard covers configuration; this covers reality. Alpaca
        does not expose an explicit ``is_paper`` flag, so the paper account-number
        prefix and the trading-blocked flags are checked instead.
        """
        account = self.account()
        number = str(getattr(account, "account_number", "") or "")
        result = {
            "account_number": number,
            "status": str(getattr(account, "status", "")),
            "equity": float(getattr(account, "equity", 0) or 0),
            "cash": float(getattr(account, "cash", 0) or 0),
            "buying_power": float(getattr(account, "buying_power", 0) or 0),
            "options_trading_level": getattr(account, "options_trading_level", None),
            "trading_blocked": bool(getattr(account, "trading_blocked", False)),
            "endpoint": self.settings.alpaca.base_url,
            "looks_like_paper": number.startswith("PA") or "paper-api" in self.settings.alpaca.base_url,
        }
        if not result["looks_like_paper"]:
            raise PaperOnlyError(
                f"Connected account {number!r} does not look like a paper account. Halting."
            )
        return result


@lru_cache(maxsize=1)
def get_clients() -> AlpacaClients:
    """Process-wide Alpaca client bundle."""
    return AlpacaClients(settings=get_settings())


def reset_clients() -> None:
    """Drop the cached bundle (used by tests and after a config reload)."""
    get_clients.cache_clear()
