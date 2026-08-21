"""MCP tools for account state, order submission, and the risk gate."""

from __future__ import annotations

from typing import Any

from desk.mcp.base import ToolSpec, fail, ok, truncate

EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SUBMIT_ORDERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "minItems": 1,
            "description": "Orders to submit to the Alpaca PAPER account.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol_or_contract": {
                        "type": "string",
                        "description": "Equity ticker or OCC option contract symbol.",
                    },
                    "asset_class": {
                        "type": "string",
                        "enum": ["us_equity", "option"],
                    },
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "qty": {"type": "number", "exclusiveMinimum": 0},
                    "type": {"type": "string", "enum": ["market", "limit"]},
                    "limit_price": {
                        "type": "number",
                        "description": "Required when `type` is \"limit\". Negative means a net credit for a multi-leg order.",
                    },
                    "time_in_force": {
                        "type": "string",
                        "enum": ["day", "gtc", "ioc", "fok"],
                    },
                },
                "required": ["symbol_or_contract", "asset_class", "side", "qty", "type"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["orders"],
    "additionalProperties": False,
}

RISK_GUARD_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "current_portfolio": {
            "type": "object",
            "description": "Account state and open positions.",
            "properties": {
                "cash": {"type": "number"},
                "equity": {"type": "number"},
                "buying_power": {"type": "number"},
                "initial_margin": {"type": "number"},
                "peak_equity": {"type": "number"},
                "daily_pnl": {"type": "number"},
                "trades_today": {"type": "integer", "minimum": 0},
                "tickers_traded_today": {"type": "array", "items": {"type": "string"}},
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "qty": {"type": "number"},
                            "asset_class": {"type": "string", "enum": ["us_equity", "option"]},
                            "market_value": {"type": "number"},
                            "cost_basis": {"type": "number"},
                            "unrealized_pl": {"type": "number"},
                            "delta": {"type": "number"},
                            "gamma": {"type": "number"},
                            "vega": {"type": "number"},
                            "theta": {"type": "number"},
                        },
                        "required": ["symbol", "qty"],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "candidate_trades": {
            "type": "array",
            "minItems": 1,
            "description": "Trades to evaluate. Greeks are position-level totals for the full requested quantity.",
            "items": {
                "type": "object",
                "properties": {
                    "trade_id": {"type": "string"},
                    "symbol_or_contract": {"type": "string"},
                    "asset_class": {"type": "string", "enum": ["us_equity", "option"]},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "qty": {"type": "number", "exclusiveMinimum": 0},
                    "estimated_notional": {"type": "number"},
                    "delta": {"type": "number"},
                    "gamma": {"type": "number"},
                    "vega": {"type": "number"},
                    "theta": {"type": "number"},
                    "underlying": {"type": "string"},
                    "days_to_expiry": {"type": "integer"},
                    "max_loss": {"type": "number"},
                    "max_profit": {"type": "number"},
                    "playbook": {"type": "string"},
                    "legs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "contract_symbol": {"type": "string"},
                                "side": {"type": "string", "enum": ["buy", "sell"]},
                                "right": {"type": "string", "enum": ["call", "put"]},
                                "strike": {"type": "number"},
                                "qty": {"type": "number"},
                                "limit_price": {"type": "number"},
                            },
                            "required": ["contract_symbol", "side"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["trade_id", "symbol_or_contract", "asset_class", "side", "qty"],
                "additionalProperties": False,
            },
        },
        "risk_limits": {
            "type": "object",
            "description": "Overrides for the configured limits. Omit to use config/settings.yaml.",
            "properties": {
                "max_notional_per_trade": {"type": "number"},
                "max_notional_total": {"type": "number"},
                "max_exposure_per_ticker": {"type": "number"},
                "max_contracts_per_ticker": {"type": "integer"},
                "max_delta_total": {"type": "number"},
                "max_gamma_total": {"type": "number"},
                "max_vega_total": {"type": "number"},
                "min_days_to_expiry": {"type": "integer"},
            },
            "additionalProperties": True,
        },
    },
    "required": ["current_portfolio", "candidate_trades"],
    "additionalProperties": False,
}


def get_account_state(arguments: dict[str, Any]) -> dict[str, Any]:
    """Cash, equity, buying power, and margin usage on the paper account."""
    from desk.alpaca.execution import ExecutionEngine

    return ok(ExecutionEngine().get_account_state())


def get_positions(arguments: dict[str, Any]) -> dict[str, Any]:
    """Open equity and option positions."""
    from desk.alpaca.execution import ExecutionEngine

    positions = ExecutionEngine().get_positions()
    rows, truncated = truncate(positions)
    return ok(
        {
            "positions": rows,
            "count": len(rows),
            "total_market_value": round(sum(abs(p.get("market_value", 0)) for p in positions), 2),
            "total_unrealized_pl": round(sum(p.get("unrealized_pl", 0) for p in positions), 2),
        },
        truncated=truncated,
    )


def get_open_orders(arguments: dict[str, Any]) -> dict[str, Any]:
    """Currently open (non-terminal) orders."""
    from desk.alpaca.execution import ExecutionEngine

    orders = ExecutionEngine().get_open_orders()
    rows, truncated = truncate(orders)
    return ok({"orders": rows, "count": len(rows)}, truncated=truncated)


def submit_orders(arguments: dict[str, Any]) -> dict[str, Any]:
    """Submit orders to the paper account.

    Refuses any batch that has not been cleared by ``risk_guard_check`` for the
    session — the tool re-runs the guard itself rather than trusting the caller,
    because "the model said it already checked" is not a control.
    """
    from desk.alpaca.execution import ExecutionEngine
    from desk.risk.risk_guard import check as risk_check

    orders = arguments.get("orders") or []
    if not orders:
        raise ValueError("`orders` must contain at least one order")

    engine = ExecutionEngine()
    account = engine.get_account_state()
    positions = engine.get_positions()

    candidates = [
        {
            "trade_id": f"mcp-{index:03d}",
            "symbol_or_contract": order["symbol_or_contract"],
            "asset_class": order.get("asset_class", "option"),
            "side": order.get("side", "buy"),
            "qty": float(order.get("qty", 1)),
            "estimated_notional": abs(float(order.get("limit_price") or 0))
            * float(order.get("qty", 1))
            * (100 if order.get("asset_class") == "option" else 1),
        }
        for index, order in enumerate(orders)
    ]

    portfolio = {
        "cash": account.get("cash", 0),
        "equity": account.get("equity", 0),
        "buying_power": account.get("buying_power", 0),
        "initial_margin": account.get("initial_margin", 0),
        "daily_pnl": account.get("daily_pnl", 0),
        "positions": [
            {
                "symbol": p["symbol"],
                "qty": p["qty"],
                "asset_class": p["asset_class"],
                "market_value": p["market_value"],
            }
            for p in positions
        ],
    }

    decision = risk_check(portfolio, candidates)
    approved_ids = {verdict.trade_id: verdict for verdict in decision.approved_trades}

    if not approved_ids:
        return fail(
            "Risk Guard rejected every order in this batch. Nothing was submitted.",
            "risk_rejected",
            retryable=False,
            risk_decision=decision.model_dump(),
        )

    to_submit = []
    for index, order in enumerate(orders):
        verdict = approved_ids.get(f"mcp-{index:03d}")
        if verdict is None:
            continue
        to_submit.append(
            {
                "trade_id": f"mcp-{index:03d}",
                "symbol_or_contract": order["symbol_or_contract"],
                "side": order.get("side", "buy"),
                "qty": verdict.approved_qty,
                "type": order.get("type", "limit"),
                "limit_price": order.get("limit_price"),
                "time_in_force": order.get("time_in_force", "day"),
            }
        )

    results = engine.submit_orders(to_submit, cycle_id="mcp")
    return ok(
        {
            "submitted": results,
            "submitted_count": sum(1 for r in results if r.get("ok")),
            "rejected_by_risk_guard": len(orders) - len(to_submit),
            "dry_run": engine.dry_run,
        },
        risk_decision=decision.model_dump(),
    )


def risk_guard_check(arguments: dict[str, Any]) -> dict[str, Any]:
    """Deterministic pre-trade risk evaluation."""
    from desk.risk.risk_guard import risk_guard_check as run_check

    portfolio = arguments.get("current_portfolio") or {}
    candidates = arguments.get("candidate_trades") or []
    if not candidates:
        raise ValueError("`candidate_trades` must contain at least one trade")

    return ok(run_check(portfolio, candidates, arguments.get("risk_limits")))


EXECUTION_TOOLS = [
    ToolSpec(
        name="get_account_state",
        description="Fetch Alpaca PAPER account state: cash, equity, buying power, and margin usage.",
        input_schema=EMPTY_SCHEMA,
        handler=get_account_state,
        annotations={"readOnlyHint": True},
    ),
    ToolSpec(
        name="get_positions",
        description="Fetch current open positions for equities and options.",
        input_schema=EMPTY_SCHEMA,
        handler=get_positions,
        annotations={"readOnlyHint": True},
    ),
    ToolSpec(
        name="get_open_orders",
        description="Fetch currently open orders.",
        input_schema=EMPTY_SCHEMA,
        handler=get_open_orders,
        annotations={"readOnlyHint": True},
    ),
    ToolSpec(
        name="submit_orders",
        description=(
            "Submit one or more Alpaca PAPER trading orders (equities or options). "
            "Every batch is re-checked by the deterministic Risk Guard before anything "
            "reaches the broker; orders the guard rejects are never sent, and orders it "
            "resizes are sent at the reduced quantity."
        ),
        input_schema=SUBMIT_ORDERS_SCHEMA,
        handler=submit_orders,
        annotations={"destructiveHint": True, "idempotentHint": True},
    ),
    ToolSpec(
        name="risk_guard_check",
        description=(
            "Deterministic risk check for candidate trades against the current portfolio "
            "and the configured limits. Returns APPROVE, RESIZE (with an approved quantity), "
            "or REJECT per trade, plus machine-readable reason codes. This check is "
            "mandatory before any order: treat a REJECT as final and never route around it."
        ),
        input_schema=RISK_GUARD_CHECK_SCHEMA,
        handler=risk_guard_check,
        annotations={"readOnlyHint": True},
    ),
]
