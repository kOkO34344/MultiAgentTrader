"""MCP tools for options chains and option contract bars."""

from __future__ import annotations

from typing import Any

from desk.mcp.base import TIMEFRAME_ENUM, ToolSpec, ok, truncate

GET_OPTIONS_CHAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "underlying_symbol": {
            "type": "string",
            "description": "Underlying equity symbol, e.g. \"SPY\".",
        },
        "expiration_date_gte": {
            "type": "string",
            "description": "Earliest expiration, YYYY-MM-DD. Optional.",
        },
        "expiration_date_lte": {
            "type": "string",
            "description": "Latest expiration, YYYY-MM-DD. Optional.",
        },
        "min_iv": {
            "type": "number",
            "minimum": 0,
            "description": "Minimum implied volatility, as a decimal (0.25 = 25%). Optional.",
        },
        "max_iv": {
            "type": "number",
            "minimum": 0,
            "description": "Maximum implied volatility, as a decimal. Optional.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum contracts to return. Optional.",
        },
    },
    "required": ["underlying_symbol"],
    "additionalProperties": False,
}

GET_OPTIONS_BARS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contract_symbols": {
            "type": "array",
            "items": {"type": "string"},
            "description": "OCC contract symbols, e.g. [\"SPY260918P00540000\"].",
            "minItems": 1,
        },
        "timeframe": {
            "type": "string",
            "enum": TIMEFRAME_ENUM,
            "description": "Bar aggregation interval.",
        },
        "start": {"type": "string", "description": "ISO-8601 start datetime. Optional."},
        "end": {"type": "string", "description": "ISO-8601 end datetime. Optional."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum bars per contract. Optional.",
        },
    },
    "required": ["contract_symbols", "timeframe"],
    "additionalProperties": False,
}


def get_options_chain(arguments: dict[str, Any]) -> dict[str, Any]:
    """Latest chain snapshots with bid/ask, IV, and greeks."""
    from desk.alpaca.market_data import MarketData

    underlying = str(arguments["underlying_symbol"]).upper().strip()
    if not underlying:
        raise ValueError("`underlying_symbol` is required")

    min_iv, max_iv = arguments.get("min_iv"), arguments.get("max_iv")
    if min_iv is not None and max_iv is not None and min_iv > max_iv:
        raise ValueError("`min_iv` cannot exceed `max_iv`")

    market_data = MarketData()
    chain = market_data.get_options_chain(
        underlying,
        expiration_date_gte=arguments.get("expiration_date_gte"),
        expiration_date_lte=arguments.get("expiration_date_lte"),
        min_iv=min_iv,
        max_iv=max_iv,
    )
    rows, truncated = truncate(chain, arguments.get("limit"))

    return ok(
        {
            "underlying_symbol": underlying,
            "contracts": rows,
            "count": len(rows),
            "total_available": len(chain),
            "vol_surface": market_data.iv_summary(underlying, chain),
        },
        truncated=truncated,
    )


def get_options_bars(arguments: dict[str, Any]) -> dict[str, Any]:
    """Historical OHLCV bars for specific option contracts."""
    from desk.alpaca.market_data import MarketData

    symbols = arguments["contract_symbols"]
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        raise ValueError("`contract_symbols` must contain at least one contract")

    timeframe = arguments["timeframe"]
    if timeframe not in TIMEFRAME_ENUM:
        raise ValueError(f"`timeframe` must be one of {TIMEFRAME_ENUM}")

    bars = MarketData().get_options_bars(
        symbols,
        timeframe,
        start=arguments.get("start"),
        end=arguments.get("end"),
        limit=arguments.get("limit"),
    )

    payload, truncated_any = {}, False
    for symbol, symbol_bars in bars.items():
        rows, truncated = truncate(symbol_bars, arguments.get("limit"))
        truncated_any = truncated_any or truncated
        payload[symbol] = {"bars": rows, "count": len(rows)}

    return ok(
        payload,
        truncated=truncated_any,
        symbols_requested=symbols,
        symbols_missing=[s for s in symbols if s not in payload],
        timeframe=timeframe,
    )


OPTIONS_TOOLS = [
    ToolSpec(
        name="get_options_chain",
        description=(
            "Get latest option chain snapshots (bid/ask, implied volatility, greeks) for "
            "an underlying symbol. Greeks are taken from the feed where available and "
            "solved with Black-Scholes where the feed omits them, so every returned "
            "contract has a computable risk profile. Illiquid contracts breaching the "
            "configured spread and open-interest limits are filtered out."
        ),
        input_schema=GET_OPTIONS_CHAIN_SCHEMA,
        handler=get_options_chain,
        annotations={"readOnlyHint": True, "openWorldHint": True},
    ),
    ToolSpec(
        name="get_options_bars",
        description="Get historical OHLCV bars for the specified option contract symbols.",
        input_schema=GET_OPTIONS_BARS_SCHEMA,
        handler=get_options_bars,
        annotations={"readOnlyHint": True, "openWorldHint": True},
    ),
]
