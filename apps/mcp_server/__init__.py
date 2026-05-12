"""TradeBridge FastMCP server factory.

Creates a FastMCP instance and registers all tool modules.
Tool files import `mcp` from this package and use `@mcp.tool()` decorators.
"""

from __future__ import annotations

# Patch sync tools to run in thread pool (prevents event loop blocking).
# Must import before FastMCP or any tool modules.
from . import sync_tool_fix  # noqa: F401

from mcp.server.fastmcp import FastMCP

# Global MCP instance — tool files import this and register via @mcp.tool()
mcp = FastMCP(
    name="TradeBridge",
    instructions="MetaTrader 5 MCP server for AI-driven trading.",
)


def create_mcp_server() -> FastMCP:
    """Create and configure the TradeBridge FastMCP server.

    Imports all tool modules to trigger @mcp.tool() registration side effects,
    then returns the global mcp instance.
    """
    # Deferred imports to avoid circular dependency.
    # Each module's import triggers @mcp.tool() decorators at module level.
    from . import (
        tools_analysis,
        tools_context,
        tools_data,
        tools_ea_native,
        tools_fundamentals,
        tools_management,
        tools_market_data,
        tools_metacognition,
        tools_portfolio,
        tools_resources,
        tools_streaming,
        tools_trading,
        tools_vibe,
        tools_wait,
    )

    return mcp
