"""MetaTraderBridge FastMCP stdio entry point.

Thin wrapper: imports all tools from apps/mcp_server/ and serves via stdio.
Formerly 2,312 lines of FastAPI proxy logic — now a single import + run.
"""

from __future__ import annotations

import sys, os

# Ensure project src is on the path for mt5_mcp package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from apps.mcp_server import create_mcp_server

mcp = create_mcp_server()
mcp.run(transport="stdio")
