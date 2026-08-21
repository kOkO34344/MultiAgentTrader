"""MCP server exposing the desk's market data, execution, and risk tools.

Nested under ``desk`` deliberately — a top-level ``mcp`` package here would
shadow the installed MCP SDK this module builds on.
"""

from desk.mcp.tools_execution import EXECUTION_TOOLS
from desk.mcp.tools_market_data import MARKET_DATA_TOOLS
from desk.mcp.tools_options import OPTIONS_TOOLS

ALL_TOOLS = [*MARKET_DATA_TOOLS, *OPTIONS_TOOLS, *EXECUTION_TOOLS]

__all__ = ["ALL_TOOLS", "MARKET_DATA_TOOLS", "OPTIONS_TOOLS", "EXECUTION_TOOLS"]
