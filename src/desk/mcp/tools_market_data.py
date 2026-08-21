"""MCP tools for equity market data."""

from __future__ import annotations

from typing import Any

from desk.mcp.base import TIMEFRAME_ENUM, ToolSpec, ok, truncate

GET_EQUITY_BARS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbols": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Equity symbols, e.g. [\"SPY\", \"AAPL\"].",
            "minItems": 1,
        },
        "timeframe": {
            "type": "string",
            "enum": TIMEFRAME_ENUM,
            "description": "Bar aggregation interval.",
        },
        "start": {
            "type": "string",
            "description": "ISO-8601 start datetime (inclusive). Optional.",
        },
        "end": {
            "type": "string",
            "description": "ISO-8601 end datetime (exclusive). Optional.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum bars per symbol. Optional.",
        },
    },
    "required": ["symbols", "timeframe"],
    "additionalProperties": False,
}


def get_equity_bars(arguments: dict[str, Any]) -> dict[str, Any]:
    """Historical OHLCV bars, with derived indicators for convenience."""
    from desk.alpaca.market_data import MarketData

    symbols = arguments["symbols"]
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        raise ValueError("`symbols` must contain at least one ticker")

    timeframe = arguments["timeframe"]
    if timeframe not in TIMEFRAME_ENUM:
        raise ValueError(f"`timeframe` must be one of {TIMEFRAME_ENUM}")

    market_data = MarketData()
    bars = market_data.get_equity_bars(
        symbols,
        timeframe,
        start=arguments.get("start"),
        end=arguments.get("end"),
        limit=arguments.get("limit"),
    )

    payload: dict[str, Any] = {}
    truncated_any = False
    for symbol, symbol_bars in bars.items():
        rows, truncated = truncate(symbol_bars, arguments.get("limit"))
        truncated_any = truncated_any or truncated
        payload[symbol] = {
            "bars": rows,
            "count": len(rows),
            "indicators": market_data.compute_indicators(symbol_bars),
        }

    missing = [s for s in symbols if s not in payload]
    return ok(
        payload,
        truncated=truncated_any,
        symbols_requested=symbols,
        symbols_missing=missing,
        timeframe=timeframe,
    )


MARKET_DATA_TOOLS = [
    ToolSpec(
        name="get_equity_bars",
        description=(
            "Get historical OHLCV bars for one or more equity symbols, plus derived "
            "technical indicators (ADX, EMA slope, ATR, Bollinger bandwidth, realised "
            "volatility) computed over the returned series."
        ),
        input_schema=GET_EQUITY_BARS_SCHEMA,
        handler=get_equity_bars,
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
]
