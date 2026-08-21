"""Order construction, submission, and fill management.

Handles single-leg equity/option orders and multi-leg option structures
(``order_class="mleg"``). Every order carries a deterministic
``client_order_id`` so a retry cannot double-fill, and unfilled limit orders are
cancelled rather than left resting into the close.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from desk.alpaca.client import AlpacaClients, get_clients
from desk.utils.logging import get_logger
from desk.utils.math_utils import round_to_tick
from desk.utils.symbols import is_option_symbol, underlying_of
from desk.utils.time_utils import utc_iso

logger = get_logger("alpaca.execution")

TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}


class DryRunOrder(dict):
    """A simulated order returned when the desk is in dry-run mode."""


def make_client_order_id(trade_id: str, symbol: str, qty: float, cycle_id: str = "") -> str:
    """Deterministic, idempotent order id (Alpaca caps these at 48 characters).

    Re-submitting the identical trade inside the same cycle collides on the
    broker side instead of opening a second position.
    """
    digest = hashlib.sha1(
        f"{cycle_id}|{trade_id}|{symbol}|{qty}".encode()
    ).hexdigest()[:16]
    prefix = "".join(ch for ch in trade_id if ch.isalnum())[:16] or "trade"
    return f"mad-{prefix}-{digest}"


class ExecutionEngine:
    """Submits Risk-Guard-approved trades to the Alpaca paper account."""

    def __init__(self, clients: AlpacaClients | None = None, dry_run: bool | None = None) -> None:
        self.clients = clients or get_clients()
        self.settings = self.clients.settings
        self.dry_run = self.settings.execution.dry_run if dry_run is None else dry_run

    # -- account state -----------------------------------------------------

    def get_account_state(self) -> dict[str, Any]:
        """Cash, equity, buying power, and margin usage."""
        account = self.clients.account()

        def number(field: str) -> float:
            try:
                return float(getattr(account, field, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        equity = number("equity")
        last_equity = number("last_equity")
        return {
            "account_number": str(getattr(account, "account_number", "")),
            "status": str(getattr(account, "status", "")),
            "currency": str(getattr(account, "currency", "USD")),
            "cash": number("cash"),
            "equity": equity,
            "last_equity": last_equity,
            "buying_power": number("buying_power"),
            "options_buying_power": number("options_buying_power"),
            "regt_buying_power": number("regt_buying_power"),
            "initial_margin": number("initial_margin"),
            "maintenance_margin": number("maintenance_margin"),
            "portfolio_value": number("portfolio_value"),
            "daily_pnl": equity - last_equity if last_equity else 0.0,
            "daily_pnl_pct": (equity - last_equity) / last_equity if last_equity else 0.0,
            "options_trading_level": getattr(account, "options_trading_level", None),
            "trading_blocked": bool(getattr(account, "trading_blocked", False)),
            "pattern_day_trader": bool(getattr(account, "pattern_day_trader", False)),
            "as_of": utc_iso(),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        """Open positions across equities and options, normalised."""
        positions = self.clients.call(self.clients.trading.get_all_positions)
        result = []
        for position in positions or []:
            symbol = str(getattr(position, "symbol", ""))
            asset_class = str(getattr(position, "asset_class", "") or "")
            is_option = "option" in asset_class.lower() or is_option_symbol(symbol)

            def number(field: str, source: Any = position) -> float:
                try:
                    return float(getattr(source, field, 0) or 0)
                except (TypeError, ValueError):
                    return 0.0

            result.append(
                {
                    "symbol": symbol,
                    "underlying": underlying_of(symbol),
                    "asset_class": "option" if is_option else "us_equity",
                    "qty": number("qty"),
                    "side": str(getattr(position, "side", "")),
                    "avg_entry_price": number("avg_entry_price"),
                    "market_value": number("market_value"),
                    "cost_basis": number("cost_basis"),
                    "current_price": number("current_price"),
                    "unrealized_pl": number("unrealized_pl"),
                    "unrealized_plpc": number("unrealized_plpc"),
                    "unrealized_intraday_pl": number("unrealized_intraday_pl"),
                }
            )
        return result

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Currently open (non-terminal) orders."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self.clients.call(self.clients.trading.get_orders, filter=request)
        return [self._order_to_dict(order) for order in orders or []]

    # -- order construction -------------------------------------------------

    def _order_to_dict(self, order: Any) -> dict[str, Any]:
        """Normalise an Alpaca ``Order`` into a plain dict."""
        if isinstance(order, dict):
            return order

        def number(field: str) -> float | None:
            value = getattr(order, field, None)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        legs = getattr(order, "legs", None) or []
        return {
            "id": str(getattr(order, "id", "")),
            "client_order_id": str(getattr(order, "client_order_id", "")),
            "symbol": str(getattr(order, "symbol", "")),
            "asset_class": str(getattr(order, "asset_class", "") or ""),
            "side": str(getattr(order, "side", "")),
            "order_class": str(getattr(order, "order_class", "") or ""),
            "type": str(getattr(order, "order_type", getattr(order, "type", "")) or ""),
            "qty": number("qty"),
            "filled_qty": number("filled_qty"),
            "limit_price": number("limit_price"),
            "filled_avg_price": number("filled_avg_price"),
            "status": str(getattr(order, "status", "")).lower().replace("orderstatus.", ""),
            "time_in_force": str(getattr(order, "time_in_force", "")),
            "submitted_at": str(getattr(order, "submitted_at", "") or ""),
            "filled_at": str(getattr(order, "filled_at", "") or ""),
            "legs": [self._order_to_dict(leg) for leg in legs],
        }

    def _enums(self) -> dict[str, Any]:
        from alpaca.trading.enums import (
            OrderClass,
            OrderSide,
            PositionIntent,
            TimeInForce,
        )

        return {
            "OrderSide": OrderSide,
            "TimeInForce": TimeInForce,
            "OrderClass": OrderClass,
            "PositionIntent": PositionIntent,
        }

    def _time_in_force(self, value: str | None) -> Any:
        enums = self._enums()
        mapping = {
            "day": enums["TimeInForce"].DAY,
            "gtc": enums["TimeInForce"].GTC,
            "ioc": enums["TimeInForce"].IOC,
            "fok": enums["TimeInForce"].FOK,
        }
        key = (value or self.settings.execution.time_in_force or "day").lower()
        return mapping.get(key, enums["TimeInForce"].DAY)

    def build_order_request(self, spec: dict[str, Any], cycle_id: str = "") -> Any:
        """Turn a normalised trade spec into an Alpaca order request object.

        A spec with a ``legs`` list of two or more entries becomes a multi-leg
        (``mleg``) options order; anything else becomes a simple order.
        """
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            OptionLegRequest,
        )

        enums = self._enums()
        legs = spec.get("legs") or []
        qty = float(spec.get("qty", 1))
        order_type = str(spec.get("type", self.settings.execution.order_type)).lower()
        tif = self._time_in_force(spec.get("time_in_force"))

        if len(legs) >= 2:
            leg_requests = [
                OptionLegRequest(
                    symbol=leg["contract_symbol"],
                    ratio_qty=int(abs(float(leg.get("qty", 1)))),
                    side=(
                        enums["OrderSide"].BUY
                        if not str(leg.get("side", "buy")).lower().startswith("s")
                        else enums["OrderSide"].SELL
                    ),
                )
                for leg in legs
            ]
            client_order_id = make_client_order_id(
                str(spec.get("trade_id", "mleg")),
                "+".join(leg["contract_symbol"] for leg in legs),
                qty,
                cycle_id,
            )
            common = {
                "qty": qty,
                "order_class": enums["OrderClass"].MLEG,
                "time_in_force": tif,
                "legs": leg_requests,
                "client_order_id": client_order_id,
            }
            if order_type == "market":
                return MarketOrderRequest(**common)
            # A net credit is submitted as a negative limit price; a net debit
            # as positive. The desk's structures always supply a signed price.
            return LimitOrderRequest(
                limit_price=round_to_tick(
                    float(spec["limit_price"]), self.settings.execution.round_limit_to
                ),
                **common,
            )

        symbol = spec.get("symbol_or_contract") or spec.get("symbol") or (
            legs[0]["contract_symbol"] if legs else ""
        )
        side = (
            enums["OrderSide"].BUY
            if not str(spec.get("side", "buy")).lower().startswith("s")
            else enums["OrderSide"].SELL
        )
        common = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "time_in_force": tif,
            "client_order_id": make_client_order_id(
                str(spec.get("trade_id", "single")), symbol, qty, cycle_id
            ),
        }
        if order_type == "market" or spec.get("limit_price") is None:
            return MarketOrderRequest(**common)
        return LimitOrderRequest(
            limit_price=round_to_tick(
                float(spec["limit_price"]), self.settings.execution.round_limit_to
            ),
            **common,
        )

    def marketable_limit(self, mid: float, side: str, spread: float = 0.0) -> float:
        """Price a limit order at the mid, nudged toward the far side to fill.

        Buys pay slightly above the mid, sells accept slightly below — the
        desk's edge comes from structure selection, not from shaving the spread.
        """
        tick = self.settings.execution.round_limit_to
        pct = self.settings.execution.marketable_edge_pct
        # Floor the concession at one tick: a percentage of a narrow spread
        # rounds to zero and leaves a limit order sitting at the mid unfilled.
        edge = max(abs(spread) * pct, tick) if pct > 0 else 0.0
        adjusted = mid + edge if not str(side).lower().startswith("s") else mid - edge
        return round_to_tick(max(adjusted, tick), tick)

    # -- submission --------------------------------------------------------

    def submit_orders(
        self, orders: list[dict[str, Any]], cycle_id: str = "", wait_for_fill: bool = True
    ) -> list[dict[str, Any]]:
        """Submit a batch of orders and report the outcome of each.

        One failing order never blocks the rest — each result carries its own
        ``ok`` flag and error text.
        """
        results: list[dict[str, Any]] = []

        for spec in orders or []:
            trade_id = str(spec.get("trade_id", ""))
            try:
                request = self.build_order_request(spec, cycle_id)
            except Exception as exc:  # noqa: BLE001 - report, never abort the batch
                logger.error(
                    "order_build_failed",
                    extra={"event": "order_build_failed", "trade_id": trade_id, "error": str(exc)},
                )
                results.append(
                    {"ok": False, "trade_id": trade_id, "error": f"could not build order: {exc}"}
                )
                continue

            if self.dry_run:
                simulated = DryRunOrder(
                    {
                        "ok": True,
                        "dry_run": True,
                        "trade_id": trade_id,
                        "symbol": spec.get("symbol_or_contract")
                        or "+".join(leg["contract_symbol"] for leg in spec.get("legs", [])),
                        "qty": float(spec.get("qty", 1)),
                        "limit_price": spec.get("limit_price"),
                        "status": "simulated",
                        "client_order_id": getattr(request, "client_order_id", ""),
                        "submitted_at": utc_iso(),
                    }
                )
                logger.info(
                    "order_simulated",
                    extra={"event": "order_simulated", "trade_id": trade_id, "spec": dict(simulated)},
                )
                results.append(dict(simulated))
                continue

            try:
                order = self.clients.call(self.clients.trading.submit_order, order_data=request)
                record = self._order_to_dict(order)
                record.update({"ok": True, "dry_run": False, "trade_id": trade_id})
                logger.info(
                    "order_submitted",
                    extra={
                        "event": "order_submitted",
                        "trade_id": trade_id,
                        "order_id": record["id"],
                        "symbol": record["symbol"],
                        "qty": record["qty"],
                    },
                )
                if wait_for_fill:
                    record.update(self.await_fill(record["id"]))
                results.append(record)
            except Exception as exc:  # noqa: BLE001 - surface broker rejections verbatim
                logger.error(
                    "order_failed",
                    extra={"event": "order_failed", "trade_id": trade_id, "error": str(exc)},
                )
                results.append({"ok": False, "trade_id": trade_id, "error": str(exc)})

        return results

    def await_fill(self, order_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Poll an order to a terminal state, cancelling it if it never fills."""
        execution = self.settings.execution
        timeout = timeout or execution.fill_timeout_seconds
        deadline = time.monotonic() + timeout
        record: dict[str, Any] = {}

        while time.monotonic() < deadline:
            try:
                order = self.clients.call(self.clients.trading.get_order_by_id, order_id)
                record = self._order_to_dict(order)
            except Exception as exc:  # noqa: BLE001 - a poll failure is not fatal
                logger.warning(
                    "fill_poll_failed",
                    extra={"event": "fill_poll_failed", "order_id": order_id, "error": str(exc)[:200]},
                )
                break
            if record.get("status") in TERMINAL_STATUSES:
                return {"final_status": record["status"], "fill": record}
            time.sleep(execution.fill_poll_seconds)

        if execution.cancel_unfilled:
            cancelled = self.cancel_order(order_id)
            return {
                "final_status": "cancelled_unfilled",
                "fill": record,
                "cancel_ok": cancelled.get("ok", False),
            }
        return {"final_status": record.get("status", "pending"), "fill": record}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel one order by id."""
        if self.dry_run:
            return {"ok": True, "dry_run": True, "order_id": order_id}
        try:
            self.clients.call(self.clients.trading.cancel_order_by_id, order_id)
            logger.info("order_cancelled", extra={"event": "order_cancelled", "order_id": order_id})
            return {"ok": True, "order_id": order_id}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "order_id": order_id, "error": str(exc)}

    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel every open order — used by the end-of-day routine."""
        if self.dry_run:
            return {"ok": True, "dry_run": True, "cancelled": 0}
        try:
            responses = self.clients.call(self.clients.trading.cancel_orders)
            return {"ok": True, "cancelled": len(responses or [])}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def close_position(self, symbol: str, percentage: float | None = None) -> dict[str, Any]:
        """Close (or partially close) a position."""
        if self.dry_run:
            return {"ok": True, "dry_run": True, "symbol": symbol}
        try:
            from alpaca.trading.requests import ClosePositionRequest

            request = (
                ClosePositionRequest(percentage=str(percentage)) if percentage else None
            )
            order = self.clients.call(
                self.clients.trading.close_position, symbol, close_options=request
            )
            record = self._order_to_dict(order)
            record["ok"] = True
            logger.info("position_closed", extra={"event": "position_closed", "symbol": symbol})
            return record
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "symbol": symbol, "error": str(exc)}


def get_execution_engine(dry_run: bool | None = None) -> ExecutionEngine:
    """Convenience accessor used by the MCP tools and the orchestrator."""
    return ExecutionEngine(dry_run=dry_run)
