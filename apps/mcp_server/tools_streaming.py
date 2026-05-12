"""MCP tools for controlling real-time price streaming subscriptions.

Allows AI agents to subscribe/unsubscribe to tick streams and check
subscription status. Used alongside the WebSocket endpoint at /ws/prices.

Design:
  - Tools delegate to a module-level StreamManager singleton.
  - Singleton is initialized lazily when main.py calls set_stream_manager().
  - Follows the same patterns as tools_context.py / tools_management.py.
"""

import logging

from mcp.types import ToolAnnotations

from . import mcp
from .shared import (
    _tcp_send_and_await,
    get_gateway,
    get_http_client,
    get_settings_cached,
    is_frozen,
    _first_bid_ask,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol

logger = logging.getLogger(__name__)

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

# ---------------------------------------------------------------------------
# Module-level StreamManager singleton
# ---------------------------------------------------------------------------

_stream_manager = None


def set_stream_manager(manager):
    global _stream_manager
    _stream_manager = manager


def get_stream_manager():
    return _stream_manager


# ===================================================================
# Tool 1: mt5_stream_subscribe
# ===================================================================


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_stream_subscribe(symbol: str) -> dict:
    """Subscribe to real-time tick streaming for a symbol via WebSocket.

    Registers the symbol with the StreamManager so that tick data is
    polled from the EA every ~1.5 seconds and broadcast to all connected
    WebSocket clients subscribed to that symbol.

    Args:
        symbol: Trading symbol to subscribe to (e.g. EURUSD, BTCUSD, XAUUSD).
                Normalized automatically.

    Returns:
        dict: Contains "status" ("subscribed"), "symbol" (denormalized).
              On error, returns {"error": str}.

    Example:
        mt5_stream_subscribe(symbol="EURUSD") →
          {"status": "subscribed", "symbol": "EURUSD"}

    Note:
        Requires the StreamManager to be initialized (happens automatically
        on server startup). If streaming is not initialized, returns an error.
    """
    try:
        manager = get_stream_manager()
        if manager is None:
            return {"error": "streaming not initialized"}

        symbol_norm = normalize_symbol(symbol)

        try:
            import asyncio

            coro = manager.subscribe_symbol(symbol_norm)
            _run_async(coro)
        except Exception:
            return {"error": "streaming not initialized"}

        return {"status": "subscribed", "symbol": denormalize_symbol(symbol_norm)}
    except Exception as e:
        return {"error": str(e)}


# ===================================================================
# Tool 2: mt5_stream_unsubscribe
# ===================================================================


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_stream_unsubscribe(symbol: str = None) -> dict:
    """Unsubscribe from real-time tick streaming for a symbol or all symbols.

    Stops polling for the given symbol (or all symbols) and removes any
    connected WebSocket clients from the subscription set.

    Args:
        symbol: Trading symbol to unsubscribe from (e.g. "EURUSD").
                If None or omitted, unsubscribes from ALL symbols.

    Returns:
        dict: Contains "status" ("unsubscribed"), "symbol" (the symbol or "all").
              On error, returns {"error": str}.

    Examples:
        mt5_stream_unsubscribe(symbol="EURUSD") →
          {"status": "unsubscribed", "symbol": "EURUSD"}

        mt5_stream_unsubscribe() →
          {"status": "unsubscribed", "symbol": "all"}

    Note:
        Unsubscribing removes all WebSocket clients for the given symbol.
        New subscriptions can be created at any time via mt5_stream_subscribe.
    """
    try:
        manager = get_stream_manager()
        if manager is None:
            return {"error": "streaming not initialized"}

        symbol_norm = normalize_symbol(symbol) if symbol else None

        try:
            import asyncio

            if symbol_norm:
                coro = manager.unsubscribe_symbol(symbol=symbol_norm)
            else:
                coro = manager.unsubscribe_symbol()
            _run_async(coro)
        except Exception:
            return {"error": "streaming not initialized"}

        return {
            "status": "unsubscribed",
            "symbol": denormalize_symbol(symbol_norm) if symbol_norm else "all",
        }
    except Exception as e:
        return {"error": str(e)}


# ===================================================================
# Tool 3: mt5_stream_status
# ===================================================================


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_stream_status() -> dict:
    """Current streaming subscription status — which symbols are active and how many clients are connected.

    Returns a snapshot of all active symbol subscriptions and the total
    number of connected WebSocket clients across all symbols.

    Args:
        None

    Returns:
        dict: Contains "subscriptions" (dict of symbol → client_count),
              "client_count" (total unique WebSocket clients).
              On error, returns {"error": str}.

    Example:
        mt5_stream_status() →
          {"subscriptions": {"EURUSD": 2, "BTCUSD": 1}, "client_count": 3}

    Note:
        A single WebSocket client can be subscribed to multiple symbols
        simultaneously, so client_count may be less than the sum of
        subscription counts.
    """
    try:
        manager = get_stream_manager()
        if manager is None:
            return {"subscriptions": {}, "client_count": 0}

        try:
            import asyncio

            subs = _run_async(manager.get_subscriptions())
        except Exception:
            return {"subscriptions": {}, "client_count": 0, "by_client": {}, "by_mcp_tool": [], "active_polls": []}

        subs = subs or {}
        return {
            "subscriptions": subs.get("by_client", {}),
            "by_mcp_tool": subs.get("by_mcp_tool", []),
            "active_polls": subs.get("active_polls", []),
            "client_count": subs.get("client_count", 0),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helper: run a coroutine in the current event loop (from sync MCP tools)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run a coroutine from a synchronous MCP tool context.

    MCP tools registered via @mcp.tool() on a FastMCP instance are sync
    functions. Since StreamManager methods are async, we need to bridge
    by running the coroutine on the running event loop.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        # We're inside a running loop — use run_coroutine_threadsafe
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=10.0)
    else:
        return loop.run_until_complete(coro)
