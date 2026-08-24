"""MCP Context Tools — market context, session context, agent communication, and system context.

Provides AI agents with aggregated market context, forex session awareness,
agent-to-agent messaging, system health overview, and educational trading context.
All tools are read-only (no destructive operations) and return dicts.

Dependencies:
    - mt5_mcp.services.session_service.get_session_context
    - mt5_mcp.services.market_context.build_context
    - mt5_mcp.services.trading_context.get_trading_context
"""

import json
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations
from . import mcp
from .shared import (
    get_gateway,
    get_http_client,
    get_settings_cached,
    _tcp_send_and_await,
    _batch_enqueue_and_await,
    _await_result,
    _parse_payload,
    _parse_payload_dict,
    _parse_indicator_value,
    _first_bid_ask,
    is_frozen,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol
from mt5_mcp.observability.logging import logger
from mt5_mcp.services.session_service import get_session_context
from mt5_mcp.services.market_context import build_context
from mt5_mcp.services.trading_context import get_trading_context

_CONTEXT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# ---------------------------------------------------------------------------
# In-memory message board for agent-to-agent communication.
# Stored as a module-level list of dicts with keys:
#   {sender, content, timestamp, agent_id}
# Cleared on read or explicit clear action.
# ---------------------------------------------------------------------------
_agent_messages: list[dict] = []




# ===================================================================
# Tool 3: mt5_agent_context
# ===================================================================


@mcp.tool(annotations=_CONTEXT_ANNOTATIONS)
def mt5_agent_context(
    action: str = "status",
    message: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> dict:
    """Agent-to-agent communication channel using an in-process in-memory message board.

    Use this tool when multiple AI agents need to coordinate or share state
    through the MetaTraderBridge server. The message board is purely in-memory
    (no persistence, no bridge calls) and supports four actions:

      - "status":  Returns pending message count and basic info (default).
      - "send":    Appends a message to the board with a UTC timestamp.
      - "read":    Returns all pending messages and clears the board.
      - "clear":   Discards all pending messages.

    Args:
        action: The action to perform — "status", "send", "read", "clear". (default: "status")
        message: Message content to send (required when action="send").
        agent_id: Identifier for the sending or reading agent (optional).

    Returns:
        dict: Contains "action" (the action performed), "status" or "messages"
              depending on action, "pending_count", and "timestamp".
              On error, returns {"error": str}.

    Examples:
        mt5_agent_context(action="send", message="Analysis complete for EURUSD", agent_id="agent_1")
        mt5_agent_context(action="read", agent_id="agent_2")
        mt5_agent_context(action="status")

    Note:
        This is an in-memory-only tool — messages are lost on server restart.
        Designed for ephemeral coordination within a single session.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()

        if action == "send":
            if not message:
                return {
                    "action": action,
                    "error": "message is required for send action",
                    "timestamp": now,
                }
            _agent_messages.append(
                {
                    "sender": agent_id or "unknown",
                    "content": message,
                    "timestamp": now,
                    "agent_id": agent_id,
                }
            )
            return {
                "action": action,
                "status": "sent",
                "pending_count": len(_agent_messages),
                "timestamp": now,
            }

        elif action == "read":
            messages = list(_agent_messages)
            _agent_messages.clear()
            return {
                "action": action,
                "messages": messages,
                "count": len(messages),
                "timestamp": now,
            }

        elif action == "clear":
            count = len(_agent_messages)
            _agent_messages.clear()
            return {
                "action": action,
                "cleared": count,
                "timestamp": now,
            }

        else:  # "status" (default)
            return {
                "action": "status",
                "agent_id": agent_id,
                "pending_count": len(_agent_messages),
                "timestamp": now,
            }
    except Exception as e:
        return {"error": str(e)}


# ===================================================================
# Tool 4: mt5_system_context
# ===================================================================


@mcp.tool(annotations=_CONTEXT_ANNOTATIONS)
def mt5_system_context() -> dict:
    """Comprehensive system health overview — MT5 connection, account state, frozen status, and bridge health.

    Use this tool as an enhanced health check when you need to understand
    the full system state: whether MT5 is connected, whether the EA is
    online, account currency/balance/equity, server info, and whether
    trading is frozen. Returns an overall health assessment ("healthy",
    "degraded", "offline", or "frozen").

    Args:
        None

    Returns:
        dict: Contains "mt5_connected", "ea_connected", "frozen",
              "account_currency", "balance", "equity", "server",
              "terminal_build", "health", "timestamp".
              On error, returns {"error": str}.

    Example:
        mt5_system_context() →
          {"mt5_connected": true, "ea_connected": true, "frozen": false,
           "account_currency": "USD", "balance": 10000.0, "health": "healthy", ...}

    Note:
        "frozen" means trading has been administratively paused (via
        set_frozen / thaw). "mt5_connected" means the TCP bridge can reach
        the EA. "ea_connected" specifically means the EA reports being
        attached to a chart and communicating.
    """
    try:
        frozen = is_frozen()
        client = get_http_client()
        settings = get_settings_cached()
        gw_url = settings.gateway_url

        # ---- 1. Fetch terminal status ----
        tcp_result = _tcp_send_and_await("terminal_status", {})
        if tcp_result is None:
            try:
                req = client.post(
                    f"{gw_url}/bridge/commands/enqueue",
                    params={"type": "terminal_status"},
                )
                req.raise_for_status()
                req_id = req.json().get("id") or req.json().get("request_id")
                tcp_result = _await_result(req_id)
            except Exception:
                pass
        terminal_data = (
            _parse_payload_dict(tcp_result) if tcp_result else {}
        )

        # ---- 2. Fetch account summary ----
        acct_result = _tcp_send_and_await("account_summary", {})
        if acct_result is None:
            try:
                req = client.post(
                    f"{gw_url}/bridge/commands/enqueue",
                    params={"type": "account_summary"},
                )
                req.raise_for_status()
                req_id = req.json().get("id") or req.json().get("request_id")
                acct_result = _await_result(req_id)
            except Exception:
                pass
        acct_data = _parse_payload_dict(acct_result) if acct_result else {}

        # ---- 3. Assess health ----
        mt5_connected = terminal_data.get("connected", False) or bool(
            acct_data.get("login")
        )
        ea_connected = terminal_data.get("ea_connected", False)

        if frozen:
            health = "frozen"
        elif mt5_connected and ea_connected:
            health = "healthy"
        elif mt5_connected:
            health = "degraded"
        else:
            health = "offline"

        return {
            "mt5_connected": mt5_connected,
            "ea_connected": ea_connected,
            "frozen": frozen,
            "account_currency": acct_data.get("currency"),
            "balance": acct_data.get("balance"),
            "equity": acct_data.get("equity"),
            "server": terminal_data.get("server") or acct_data.get("server"),
            "terminal_build": terminal_data.get("build"),
            "health": health,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


# ===================================================================
# Tool 5: mt5_trading_context
# ===================================================================


@mcp.tool(annotations=_CONTEXT_ANNOTATIONS)
def mt5_trading_context(symbol: str, timeframe: str = "H1") -> dict:
    """Educational trading context for a symbol — what makes it tick, typical behaviour, risk notes, and composure guidance.

    Use this tool when you feel uncertain about a symbol and need to
    understand what is normal vs abnormal. It returns symbol-specific
    education including point values, typical ATR ranges, reasonable
    SL/TP distances, expected trade durations, common pitfalls, and
    trading wisdom. Combined with real-time ATR for a live volatility
    assessment.

    Args:
        symbol: Trading symbol (e.g. EURUSD, BTCUSD, XAUUSD). Normalized automatically.
        timeframe: Timeframe string for ATR and bar data — "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")

    Returns:
        dict: Contains "symbol", "timeframe", "current_atr", "average_range",
              "name", "asset_class", "description", "point_value",
              "typical_atr_h1", "typical_atr_d1", "reasonable_sl",
              "reasonable_tp", "expected_duration", "volatility_context",
              "atr_assessment", "common_pitfalls", "trading_wisdom",
              "comparison". On error, returns {"error": str}.

    Example:
        mt5_trading_context(symbol="XAUUSD", timeframe="H1")

    Note:
        Symbol-specific baseline data exists for major symbols (EURUSD, GBPUSD,
        USDJPY, BTCUSD, XAUUSD, etc.). For unknown symbols, generic fallback
        text is provided — always verify point values via symbol_info.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        client = get_http_client()
        settings = get_settings_cached()
        gw_url = settings.gateway_url

        # ---- 1. Fetch ATR for real-time volatility context ----
        atr_result = _tcp_send_and_await(
            "get_indicator",
            {
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "atr",
                "period": 14,
            },
        )
        if atr_result is None:
            req = client.post(
                f"{gw_url}/bridge/commands/enqueue",
                params={
                    "type": "get_indicator",
                    "symbol": symbol_norm,
                    "timeframe": timeframe,
                    "indicator": "atr",
                    "period": 14,
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            atr_result = _await_result(req_id)
        atr_value = _parse_indicator_value(atr_result)

        # ---- 2. Fetch bars for average range calculation ----
        bars_result = _tcp_send_and_await(
            "get_bars",
            {"symbol": symbol_norm, "timeframe": timeframe, "count": 20},
        )
        if bars_result is None:
            req = client.post(
                f"{gw_url}/bridge/commands/enqueue",
                params={
                    "type": "get_bars",
                    "symbol": symbol_norm,
                    "timeframe": timeframe,
                    "count": 20,
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            bars_result = _await_result(req_id)
        bars_data = (
            _parse_payload_dict(bars_result).get("data", [])
            if bars_result
            else []
        )

        # ---- 3. Get trading context from service ----
        context = get_trading_context(
            symbol=symbol, current_atr=atr_value, include_comparison=True
        )

        # ---- 4. Compute average bar range from fetched bars ----
        avg_range = None
        if bars_data:
            ranges = [
                float(b.get("high", 0)) - float(b.get("low", 0))
                for b in bars_data
                if b.get("high") and b.get("low")
            ]
            avg_range = sum(ranges) / len(ranges) if ranges else None

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "current_atr": atr_value,
            "average_range": avg_range,
            "name": context.get("name"),
            "asset_class": context.get("asset_class"),
            "description": context.get("description"),
            "point_value": context.get("point_value", {}),
            "typical_atr_h1": context.get("typical_atr_h1", {}),
            "typical_atr_d1": context.get("typical_atr_d1", {}),
            "reasonable_sl": context.get("reasonable_sl", {}),
            "reasonable_tp": context.get("reasonable_tp", {}),
            "expected_duration": context.get("expected_duration", {}),
            "volatility_context": context.get("volatility_context", {}),
            "atr_assessment": context.get("atr_assessment"),
            "common_pitfalls": context.get("common_pitfalls", []),
            "trading_wisdom": context.get("trading_wisdom"),
            "comparison": context.get("comparison", []),
        }
    except Exception as e:
        return {"error": str(e)}
