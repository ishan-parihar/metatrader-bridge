

import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

from . import mcp
from .shared import (
    get_gateway,
    get_http_client,
    get_settings_cached,
    _tcp_send_and_await,
    _await_result,
    _parse_payload,
    _parse_payload_dict,
    _parse_indicator_value,
    _first_bid_ask,
    is_frozen,
    _check_frozen_response,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_trailing_stops: dict[str, dict] = {}


def _resolve_order_id_from_tcp_or_gateway(
    cmd_type: str, payload: dict, timeout_s: float = 15.0
) -> dict | None:
    """Try TCP first, then fallback to HTTP gateway queue polling."""
    result = _tcp_send_and_await(cmd_type, payload, timeout_s=timeout_s)
    if result and result.get("status") == "completed":
        return result
    # TCP failed - try HTTP gateway fallback
    if result is None:
        try:
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": cmd_type, **payload},
                timeout=5.0,
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            if req_id:
                return _await_result(req_id, timeout_s=10.0)
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

_EA_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


# ---------------------------------------------------------------------------
# Tool 1: mt5_place_bracket_order
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_place_bracket_order", annotations=_EA_ANNOTATIONS)
def mt5_place_bracket_order(
    symbol: str,
    volume_lots: float,
    buy_trigger: float,
    sell_trigger: float,
    sl_atr_multiplier: float = 1.5,
    tp_atr_multiplier: float = 2.0,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> dict:
    """Place a paired BUY STOP + SELL STOP bracket order using ATR-based SL/TP.

    Places two opposing pending stop orders (buy and sell) simultaneously around
    a breakout level. Use this when you want a breakout entry with both directions
    covered — one fills on upside break, the other on downside break.

    This is an MCP-side bracket (order placement only). For EA-managed brackets
    that auto-cancel the opposite side on fill, use mt5_ea_bracket_start instead.

    Requires an active EA connection via TCP bridge (port 8025).

    Args:
        symbol (str): Trading symbol, e.g. "EURUSD".
        volume_lots (float): Volume in lots (e.g. 0.1).
        buy_trigger (float): Price level for the BUY STOP order.
        sell_trigger (float): Price level for the SELL STOP order.
        sl_atr_multiplier (float): ATR multiplier for stop-loss distance (default: 1.5).
        tp_atr_multiplier (float): ATR multiplier for take-profit distance (default: 2.0).
        session_id (str, optional): Session tracking identifier.
        strategy_id (str, optional): Strategy tracking identifier.
        intent_id (str, optional): Intent tracking identifier for idempotency.

    Returns:
        dict: Keys include 'status' ("placed" on success, "error" on failure),
              'buy_order_id', 'sell_order_id', 'buy_sl', 'buy_tp', 'sell_sl',
              'sell_tp', 'atr_value'.

    Note: Falls back to HTTP gateway if TCP bridge is temporarily unavailable.
          ATR is fetched from H1 timeframe regardless of chart timeframe.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        from mt5_mcp.policy.engine import get_policy

        decision = get_policy().validate_submit_order(
            environment=get_settings_cached().environment
        )
        if not decision.allowed:
            return {
                "status": "error",
                "error_code": "POLICY_BLOCKED",
                "message": decision.reason or "Policy blocked order",
                "details": decision.details,
            }

        atr_result = _tcp_send_and_await(
            "get_indicator",
            {
                "symbol": normalize_symbol(symbol),
                "timeframe": "H1",
                "indicator": "atr",
                "period": 14,
            },
        )
        atr_value = _parse_indicator_value(atr_result)
        if atr_value is None or atr_value <= 0:
            return {
                "status": "error",
                "error_code": "ATR_UNAVAILABLE",
                "message": "Could not retrieve ATR value for bracket calculation",
            }

        sl_distance = atr_value * sl_atr_multiplier
        tp_distance = atr_value * tp_atr_multiplier

        buy_sl = buy_trigger - sl_distance
        buy_tp = buy_trigger + tp_distance
        sell_sl = sell_trigger + sl_distance
        sell_tp = sell_trigger + tp_distance

        buy_result = _resolve_order_id_from_tcp_or_gateway(
            "submit_pending_order",
            {
                "symbol": normalize_symbol(symbol),
                "side": "buy",
                "kind": "stop",
                "price": buy_trigger,
                "volume_lots": volume_lots,
                "sl": buy_sl,
                "tp": buy_tp,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": f"bracket_buy_{intent_id or 'none'}_{uuid.uuid4().hex[:6]}",
            },
        )
        if not buy_result or buy_result.get("status") != "completed":
            return {
                "status": "error",
                "error_code": "BUY_ORDER_FAILED",
                "message": "BUY STOP order failed",
                "details": buy_result,
            }

        buy_payload = _parse_payload_dict(buy_result)
        buy_order_id = (
            buy_payload.get("order")
            or buy_payload.get("order_id")
            or buy_payload.get("ticket")
        )

        sell_result = _resolve_order_id_from_tcp_or_gateway(
            "submit_pending_order",
            {
                "symbol": normalize_symbol(symbol),
                "side": "sell",
                "kind": "stop",
                "price": sell_trigger,
                "volume_lots": volume_lots,
                "sl": sell_sl,
                "tp": sell_tp,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": f"bracket_sell_{intent_id or 'none'}_{uuid.uuid4().hex[:6]}",
            },
        )
        if not sell_result or sell_result.get("status") != "completed":
            return {
                "status": "error",
                "error_code": "SELL_ORDER_FAILED",
                "message": "SELL STOP order failed",
                "buy_order_id": buy_order_id,
                "details": sell_result,
            }

        sell_payload = _parse_payload_dict(sell_result)
        sell_order_id = (
            sell_payload.get("order")
            or sell_payload.get("order_id")
            or sell_payload.get("ticket")
        )

        return {
            "status": "placed",
            "buy_order_id": buy_order_id,
            "sell_order_id": sell_order_id,
            "buy_sl": buy_sl,
            "buy_tp": buy_tp,
            "sell_sl": sell_sl,
            "sell_tp": sell_tp,
            "atr_value": atr_value,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: mt5_ea_bracket_start
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_bracket_start", annotations=_EA_ANNOTATIONS)
def mt5_ea_bracket_start(
    buy_order_ticket: str,
    sell_order_ticket: str,
    bracket_id: str,
    comment: str = "",
    magic_filter: int = 0,
) -> dict:
    """Register two existing pending orders as an EA-managed pair bracket.

    Links two already-placed pending orders (one buy, one sell) so the EA
    monitors them on the terminal side: when one order fills, the other is
    automatically cancelled. This is an EA-side bracket (native MT5), meaning
    the MT5 Expert Advisor handles the lifecycle, not the MCP server.

    Use this when you have already placed two pending orders separately and
    want them linked as a bracket. For placing both orders in one call, use
    mt5_place_bracket_order instead.

    Args:
        buy_order_ticket (str): Ticket number of the existing BUY pending order.
        sell_order_ticket (str): Ticket number of the existing SELL pending order.
        bracket_id (str): Unique identifier for this bracket pair (used for later
                          stop/list operations).
        comment (str): Optional comment attached to the bracket (default: "").
        magic_filter (int): Optional EA magic number filter (default: 0 = no filter).

    Returns:
        dict: Payload from EA confirming the bracket was registered. Keys vary
              by EA version but typically include 'status'.

    Note: Requires EA-side bracket support in BridgeConnectorEA. Falls back to
          HTTP gateway (EABridgeAdapter) if TCP is unreachable.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        params = {
            "buy_order_ticket": buy_order_ticket,
            "sell_order_ticket": sell_order_ticket,
            "bracket_id": bracket_id,
        }
        if comment:
            params["comment"] = comment
        if magic_filter:
            params["magic_filter"] = magic_filter

        tcp_result = _tcp_send_and_await("bracket_start", params)
        if tcp_result and tcp_result.get("status") == "completed":
            payload = _parse_payload_dict(tcp_result)
            if payload:
                return payload

        from mt5_mcp.adapters.ea_bridge_adapter.adapter import EABridgeAdapter

        try:
            adapter = EABridgeAdapter()
            result = adapter.ea_bracket_start(
                buy_order_ticket=buy_order_ticket,
                sell_order_ticket=sell_order_ticket,
                bracket_id=bracket_id,
                comment=comment,
                magic_filter=magic_filter,
            )
            if result.get("status") == "completed":
                return result.get("result", {}).get("payload", result)
        except Exception:
            pass

        return {
            "status": "error",
            "message": "bracket_start failed via both TCP and HTTP",
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: mt5_ea_bracket_stop
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_bracket_stop", annotations=_EA_ANNOTATIONS)
def mt5_ea_bracket_stop(
    bracket_id: str,
    intent_id: Optional[str] = None,
) -> dict:
    """Stop and de-register an active EA-side bracket by its bracket_id.

    Sends the bracket_stop command to the EA via TCP bridge, which tells the
    EA to stop monitoring the bracket pair. The individual orders remain on
    the terminal but are no longer linked.

    Use this to manually dissolve a bracket before either side has filled.

    Args:
        bracket_id (str): The bracket identifier returned by mt5_ea_bracket_start.
        intent_id (str, optional): Intent tracking identifier.

    Returns:
        dict: Payload from EA confirming the bracket was stopped. Keys include
              'status' ("completed" on success).

    Note: This only de-registers the bracket on the EA side. The underlying
          pending orders are NOT automatically deleted.
    """
    try:
        result = _tcp_send_and_await(
            "bracket_stop",
            {
                "bracket_id": bracket_id,
                "intent_id": intent_id,
            },
        )
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return {"status": "error", "message": "bracket_stop failed", "details": result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 4: mt5_ea_bracket_list
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_bracket_list", annotations=_READ_ANNOTATIONS)
def mt5_ea_bracket_list() -> dict:
    """List all active EA-side brackets currently registered in the terminal.

    Queries the EA for all active bracket pairs. Use this to inspect which
    brackets are currently being managed by the EA.

    Returns:
        dict: Contains 'brackets' key with a list of active bracket records.
              Returns {"brackets": []} if no brackets exist or on error.

    Note: Read-only — does not modify any state.
    """
    try:
        result = _tcp_send_and_await("bracket_list", {})
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            return {"brackets": payload.get("brackets", payload.get("list", []))}
        return {"brackets": []}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: mt5_ea_bracket_tick
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_bracket_tick", annotations=_EA_ANNOTATIONS)
def mt5_ea_bracket_tick() -> dict:
    """Trigger an immediate bracket evaluation cycle on the EA.

    Sends the bracket_tick command to the EA, forcing it to re-evaluate all
    active brackets (check fills, cancel opposing sides, etc.). Use this to
    accelerate bracket processing instead of waiting for the EA's natural
    tick cycle.

    Returns:
        dict: Results of the bracket evaluation cycle. Keys vary by EA version.
              Returns {"status": "no_result"} if the EA did not respond.

    Note: This is a fire-and-forget trigger — the EA processes and responds
          asynchronously.
    """
    try:
        result = _tcp_send_and_await("bracket_tick", {})
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return result or {"status": "no_result"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: mt5_set_trailing_stop
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_set_trailing_stop", annotations=_EA_ANNOTATIONS)
def mt5_set_trailing_stop(
    position_id: str,
    distance_atr_multiplier: float = 1.5,
    lock_in_profit_after_atr: float = 1.0,
    check_interval_seconds: int = 30,
) -> dict:
    """Set an MCP-side trailing stop on an open position using ATR-based distance.

    Configures in-memory trailing stop parameters for a position. The MCP server
    tracks the trailing stop locally using the `_trailing_stops` dict — this is
    NOT managed by the EA. Use mt5_ea_trailing_start instead for EA-managed
    trailing stops that survive server restarts.

    ATR is fetched from H1 timeframe to compute stop distance in points.

    Args:
        position_id (str): Open position ID, ticket, or order ID to trail.
        distance_atr_multiplier (float): ATR multiplier for trailing distance
            behind price (default: 1.5).
        lock_in_profit_after_atr (float): ATR multiplier threshold before
            locking in profit (default: 1.0).
        check_interval_seconds (int): Seconds between trailing evaluations
            (default: 30).

    Returns:
        dict: Keys include 'status' ("active" on success, "error" on failure),
              'position_id', 'distance_points', 'lock_in_points'.

    Note: MCP-side trailing stops only work while this Python process is
          running and the trailing loop is active. They do NOT persist across
          restarts. The position must be open and found via mt5_positions_open.
    """
    try:
        from .tools_resources import mt5_positions_open, mt5_symbol_info

        positions_result = mt5_positions_open()
        positions = positions_result.get("positions", [])
        position = None
        for p in positions:
            if (
                p.get("position_id") == position_id
                or p.get("id") == position_id
                or p.get("ticket") == position_id
            ):
                position = p
                break

        if position is None:
            return {
                "status": "error",
                "position_id": position_id,
                "message": "Position not found",
            }

        sym = position.get("symbol", "")
        sym_info = mt5_symbol_info(sym)
        point = sym_info.get("point", 0.00001)
        if isinstance(point, str):
            point = float(point)

        atr_result = _tcp_send_and_await(
            "get_indicator",
            {
                "symbol": normalize_symbol(sym),
                "timeframe": "H1",
                "indicator": "atr",
                "period": 14,
            },
        )
        atr_value = _parse_indicator_value(atr_result)
        if atr_value is None or atr_value <= 0:
            return {
                "status": "error",
                "error_code": "ATR_UNAVAILABLE",
                "message": "Could not retrieve ATR value",
            }

        distance_points = int(atr_value * distance_atr_multiplier / point)
        lock_in_points = int(atr_value * lock_in_profit_after_atr / point)

        _trailing_stops[position_id] = {
            "position_id": position_id,
            "symbol": sym,
            "side": position.get("side", "buy"),
            "distance_points": distance_points,
            "lock_in_points": lock_in_points,
            "check_interval": check_interval_seconds,
            "initial_sl": position.get("sl"),
            "active": True,
            "last_check": time.time(),
        }

        return {
            "status": "active",
            "position_id": position_id,
            "distance_points": distance_points,
            "lock_in_points": lock_in_points,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 7: mt5_trailing_stop_cancel
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_trailing_stop_cancel", annotations=_EA_ANNOTATIONS)
def mt5_trailing_stop_cancel(position_id: str) -> dict:
    """Cancel an active MCP-side trailing stop for a specific position.

    Removes the position from the in-memory `_trailing_stops` dictionary and
    stops any local trailing evaluation. Use this when you want to disable
    trailing on a position that was previously configured with
    mt5_set_trailing_stop.

    Args:
        position_id (str): The position ID used when the trailing stop was set.

    Returns:
        dict: Keys include 'status' ("cancelled" if found and removed,
              "not_found" if no trailing stop exists for this position),
              and 'position_id'.

    Note: This only affects MCP-side trailing stops. For EA-side trailing
          stops managed via mt5_ea_trailing_start, use mt5_ea_trailing_stop.
    """
    try:
        if position_id in _trailing_stops:
            _trailing_stops[position_id]["active"] = False
            del _trailing_stops[position_id]
            return {"status": "cancelled", "position_id": position_id}
        return {"status": "not_found", "position_id": position_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 8: mt5_trailing_stop_list
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_trailing_stop_list", annotations=_READ_ANNOTATIONS)
def mt5_trailing_stop_list() -> dict:
    """List all active MCP-side trailing stops currently being tracked.

    Returns the in-memory registry of trailing stops configured via
    mt5_set_trailing_stop. The 'last_check' timestamp is filtered out for
    cleaner output.

    Returns:
        dict: Contains 'active_stops' (dict of position_id to stop config),
              and 'count' (number of active stops).

    Note: This only shows MCP-side trailing stops, not EA-side ones managed
          via mt5_ea_trailing_start. Read-only — does not modify any state.
    """
    try:
        active = {
            pid: {k: v for k, v in ts.items() if k != "last_check"}
            for pid, ts in _trailing_stops.items()
            if ts.get("active")
        }
        return {"active_stops": active, "count": len(active)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 9: mt5_ea_trailing_start
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_trailing_start", annotations=_EA_ANNOTATIONS)
def mt5_ea_trailing_start(
    position_id: str,
    distance_atr_multiplier: float = 1.5,
    lock_in_profit_after_atr: float = 1.0,
    check_interval_seconds: int = 30,
    intent_id: Optional[str] = None,
) -> dict:
    """Start an EA-side trailing stop on an open position (managed in MT5 EA).

    Sends the trailing_start command to the EA via TCP bridge. The EA handles
    all trailing logic on the terminal side — adjusting SL as price moves in
    favor. Use this instead of mt5_set_trailing_stop when you want reliable,
    persistent trailing that survives MCP server restarts.

    ATR multiplier parameters are sent to the EA which computes point distances.

    Args:
        position_id (str): Open position ID, ticket, or order ID to trail.
        distance_atr_multiplier (float): ATR multiplier for trailing distance
            behind price (default: 1.5).
        lock_in_profit_after_atr (float): ATR multiplier threshold before
            locking in profit (default: 1.0).
        check_interval_seconds (int): Seconds between EA-side trailing
            evaluations (default: 30).
        intent_id (str, optional): Intent tracking identifier.

    Returns:
        dict: Payload from EA confirming trailing start. Keys vary by EA
              version. Returns {"status": "no_result"} if no response.

    Note: EA-side trailing persists in the MT5 terminal. The ATR multipliers
          are sent to the EA, which computes its own point distances.
    """
    try:
        result = _tcp_send_and_await(
            "trailing_start",
            {
                "position_id": position_id,
                "distance_atr_multiplier": distance_atr_multiplier,
                "lock_in_profit_after_atr": lock_in_profit_after_atr,
                "check_interval_seconds": check_interval_seconds,
                "intent_id": intent_id,
            },
        )
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return result or {"status": "no_result"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 10: mt5_ea_trailing_stop
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_trailing_stop", annotations=_EA_ANNOTATIONS)
def mt5_ea_trailing_stop(
    position_id: str,
    intent_id: Optional[str] = None,
) -> dict:
    """Stop an active EA-side trailing stop for a specific position.

    Sends the trailing_stop command to the EA via TCP bridge, telling the EA
    to stop trailing the specified position. The current SL remains in place
    but will no longer be adjusted automatically.

    Use this to disable EA-managed trailing on a position previously configured
    with mt5_ea_trailing_start.

    Args:
        position_id (str): The position ID used when trailing was started.
        intent_id (str, optional): Intent tracking identifier.

    Returns:
        dict: Payload from EA confirming trailing was stopped. Returns
              {"status": "no_result"} if no response.

    Note: This only stops EA-side trailing. For MCP-side trailing (managed
          in-memory via mt5_set_trailing_stop), use mt5_trailing_stop_cancel.
    """
    try:
        result = _tcp_send_and_await(
            "trailing_stop",
            {
                "position_id": position_id,
                "intent_id": intent_id,
            },
        )
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return result or {"status": "no_result"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 11: mt5_ea_trailing_list
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_trailing_list", annotations=_READ_ANNOTATIONS)
def mt5_ea_trailing_list() -> dict:
    """List all active EA-side trailing stops from the MT5 terminal.

    Queries the EA via TCP bridge for all currently active trailing stop
    configurations. Use this to monitor which positions have EA-managed
    trailing stops active.

    Returns:
        dict: Contains trailing stop records from the EA. Returns
              {"trailing_stops": []} if none exist or on error.

    Note: This lists EA-side trailing stops only. For MCP-side trailing
          stops, use mt5_trailing_stop_list. Read-only.
    """
    try:
        result = _tcp_send_and_await("trailing_list", {})
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return result or {"trailing_stops": []}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 12: mt5_ea_trailing_tick
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_ea_trailing_tick", annotations=_EA_ANNOTATIONS)
def mt5_ea_trailing_tick() -> dict:
    """Trigger an immediate trailing stop evaluation cycle on the EA.

    Sends the trailing_tick command to the EA, forcing it to re-evaluate all
    active trailing stops immediately. Use this to accelerate trailing SL
    adjustments instead of waiting for the EA's natural tick cycle.

    Returns:
        dict: Results of the trailing evaluation cycle. Keys vary by EA
              version. Returns {"status": "no_result"} if no response.

    Note: Fire-and-forget trigger — the EA processes asynchronously.
    """
    try:
        result = _tcp_send_and_await("trailing_tick", {})
        if result and result.get("status") == "completed":
            payload = _parse_payload_dict(result)
            if payload:
                return payload
        return result or {"status": "no_result"}
    except Exception as e:
        return {"error": str(e)}
