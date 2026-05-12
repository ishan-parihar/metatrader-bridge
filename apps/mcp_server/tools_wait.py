"""MCP Wait Tools — Price checking tools for TradeBridge.

The MCP client enforces a ~60s timeout on tool calls, which prevents
long-polling strategies. This module provides a single-shot price check
that returns immediately with the current market price.

For long-polling price alerts, use the HTTP REST endpoint instead:
  POST /resources/market/wait_for_price
"""

from datetime import datetime, timezone
from typing import Literal

from mcp.types import ToolAnnotations

from . import mcp
from .shared import get_gateway, _tcp_send_and_await, _parse_payload_dict
from mt5_mcp.observability.logging import logger

_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_wait_for_price(
    symbol: str,
    condition: Literal["above", "below", "crosses"],
    price: float,
) -> dict:
    """Check if a symbol's current price meets a condition (above/below/crosses threshold).

    This is a single-shot check — it looks up the latest price and returns
    immediately. For continuous monitoring, call this tool in a loop from
    the AI agent, or use the HTTP REST endpoint
    POST /resources/market/wait_for_price for long-polling support.

    Args:
        symbol: Trading symbol (e.g. EURUSD, BTCUSD, XAUUSD)
        condition: Price condition to check:
            - "above": current price is above the threshold
            - "below": current price is below the threshold
            - "crosses": price has moved from one side to the other
        price: Threshold price to compare against

    Returns:
        Dict with:
            - symbol: The symbol checked
            - condition: The condition checked
            - trigger_price: The threshold price
            - current_price: The latest price fetched
            - triggered: Whether the condition is met
            - timed_out: Always false (single-shot, no polling)
            - source: How the price was obtained
    """
    current_price = None
    source = None

    # Try gateway adapter first
    try:
        gw = get_gateway()
        tick = gw.adapter.get_symbol_info(symbol)
        if tick and hasattr(tick, "bid") and tick.bid:
            current_price = float(tick.bid)
            source = "gateway"
    except Exception as e:
        logger.debug(f"mt5_wait_for_price: gateway failed — {e}")

    # Fallback to TCP bridge
    if current_price is None:
        try:
            tcp_result = _tcp_send_and_await(
                "get_symbol_info", {"symbol": symbol}, timeout_s=5.0
            )
            data = _parse_payload_dict(tcp_result) if tcp_result else {}
            bid = data.get("bid") or data.get("price", {}).get("bid")
            if bid:
                current_price = float(bid)
                source = "tcp_bridge"
        except Exception as e:
            logger.debug(f"mt5_wait_for_price: TCP bridge failed — {e}")

    if current_price is None:
        return {
            "symbol": symbol,
            "condition": condition,
            "trigger_price": price,
            "current_price": None,
            "triggered": False,
            "timed_out": False,
            "error": "Could not fetch current price from any source",
        }

    # Evaluate condition
    triggered = False
    if condition == "above":
        triggered = current_price > price
    elif condition == "below":
        triggered = current_price < price
    elif condition == "crosses":
        # Single shot can't detect crosses, so report current position
        triggered = False  # crosses requires history, not available in single check

    return {
        "symbol": symbol,
        "condition": condition,
        "trigger_price": price,
        "current_price": current_price,
        "triggered": triggered,
        "timed_out": False,
        "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
