"""
Standalone MCP Analysis Server for MetaTraderBridge.
Connects to the TCP bridge via BridgeProtocol to provide analysis tools.

This server runs independently from the main combined MetaTraderBridge server,
demonstrating the domain-split architecture where individual MCP servers
handle specific concerns (analysis, trading, context, market-data).

Usage:
    python -m apps.mcp_analysis.main

Port: 8091
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path so that ``mt5_mcp`` and ``apps`` are importable
# when running this module directly (``python -m apps.mcp_analysis.main``).
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from mcp.server.fastmcp import FastMCP
from apps.mcp_analysis.tools_analysis import register_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="MetaTraderBridge Analysis",
    instructions=(
        "Access market analysis tools including regime detection, "
        "market structure, and divergence analysis. "
        "Data sourced via MetaTraderBridge TCP bridge."
    ),
    host="0.0.0.0",
    port=8091,
    sse_path="/sse",
)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Register analysis tools on the FastMCP instance
    register_tools(mcp)

    logger.info("Starting MetaTraderBridge Analysis MCP server on port 8091")

    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
