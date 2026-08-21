"""Stdio MCP server exposing the desk's eight tools to Claude.

Register with Claude Code:

    claude mcp add options-desk -- desk mcp-server
"""

from __future__ import annotations

import json
from typing import Any

from desk.mcp import ALL_TOOLS
from desk.mcp.base import ToolSpec, safe_call
from desk.utils.logging import get_logger, setup_logging

logger = get_logger("mcp.server")

SERVER_NAME = "multi-agent-options-desk"
SERVER_INSTRUCTIONS = """\
Tools for a multi-agent options trading desk running on an Alpaca PAPER account.

Rules of engagement:
  * `risk_guard_check` is mandatory before any order. Submit only trades it returns
    as APPROVE, at exactly the quantity it returns. A REJECT is final for this cycle.
  * `submit_orders` re-runs the guard itself and will refuse a batch the guard rejects.
  * Every response is an envelope: {ok, data, error, error_type, retryable}. Check `ok`
    before reading `data`. When `retryable` is true, back off and retry once.
  * Large chains are capped; check the `truncated` flag and narrow the filters instead
    of assuming you received everything.
  * This is a PAPER account. The client refuses to connect to a live endpoint.
"""


def build_tool_list() -> list[Any]:
    """Convert the desk's tool specs into MCP ``Tool`` objects."""
    from mcp.types import Tool

    return [
        Tool(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            annotations=spec.annotations or None,
        )
        for spec in ALL_TOOLS
    ]


def find_tool(name: str) -> ToolSpec | None:
    return next((spec for spec in ALL_TOOLS if spec.name == name), None)


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to its handler. Transport-independent, so it is testable."""
    spec = find_tool(name)
    if spec is None:
        from desk.mcp.base import fail

        return fail(f"Unknown tool '{name}'", "unknown_tool")
    logger.info("mcp_tool_call", extra={"event": "mcp_tool_call", "tool": name})
    return safe_call(name, spec.handler, arguments)


def build_server() -> Any:
    """Construct the MCP server with its two request handlers."""
    from mcp import types
    from mcp.server.lowlevel import Server

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=build_tool_list())

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        result = dispatch(params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
            isError=not result.get("ok", False),
        )

    return Server(
        SERVER_NAME,
        version="0.1.0",
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_stdio() -> None:
    """Serve over stdio until the client disconnects."""
    from mcp.server.stdio import stdio_server

    server = build_server()
    logger.info(
        "mcp_server_start",
        extra={"event": "mcp_server_start", "tools": [t.name for t in ALL_TOOLS]},
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Entry point for ``desk mcp-server``."""
    import asyncio

    # Logs must go to a file, never stdout — stdout is the MCP transport.
    setup_logging(console=False, filename="mcp.jsonl")
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
