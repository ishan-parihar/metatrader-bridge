

import time
from typing import Any, Optional

from mcp.types import ToolAnnotations

from . import mcp
from .shared import (
    _check_frozen_response,
    _parse_payload,
    _parse_payload_dict,
    _tcp_send_and_await,
    get_gateway,
    get_http_client,
    get_settings_cached,
    is_frozen,
    set_frozen,
    thaw,
    _shutdown_state,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_health() -> dict:
    """
    Probe all TradeBridge subsystems (gateway, HTTP bridge, TCP bridge) and return an aggregated health status.

    Use this as the first diagnostic call when anything seems wrong. It tests every communication
    path the system uses and pinpoints which layer is failing.

    Args:
        None

    Returns:
        dict: Contains "status" (healthy/degraded/unhealthy), "subsystems" with per-layer
              diagnostics, "timestamp" (ISO 8601 timestamp), and "frozen" (bool).

    Example: mt5_health() → {"status": "healthy", "subsystems": {...}, "frozen": false}

    Note: A "degraded" status means some subsystems work but others are impaired — trading may
          still function with reduced capability. "unhealthy" indicates critical path failures.
    """
    try:
        subsystems: dict[str, Any] = {}

        try:
            gw_status = get_gateway().terminal_status()
            connected = getattr(gw_status, "connected", False)
            subsystems["gateway"] = {
                "status": "ok" if connected else "unreachable",
                "details": gw_status.model_dump()
                if hasattr(gw_status, "model_dump")
                else dict(gw_status)
                if hasattr(gw_status, "__dict__")
                else str(gw_status),
            }
        except Exception as e:
            subsystems["gateway"] = {"status": "error", "error": str(e)}

        # HTTP bridge health check
        try:
            resp = get_http_client().post(
                f"{get_settings_cached().gateway_url}/bridge/health"
            )
            subsystems["bridge"] = {
                "status": "ok" if resp.status_code == 200 else "degraded",
                "http_status": resp.status_code,
            }
        except Exception as e:
            subsystems["bridge"] = {"status": "error", "error": str(e)}

        # TCP check
        try:
            tcp_result = _tcp_send_and_await("get_positions", {}, timeout_s=5.0)
            if tcp_result and tcp_result.get("status") == "completed":
                subsystems["tcp"] = {"status": "ok"}
            else:
                subsystems["tcp"] = {"status": "degraded", "result": tcp_result}
        except Exception as e:
            subsystems["tcp"] = {"status": "error", "error": str(e)}

        # Aggregate overall status
        statuses = [s.get("status") for s in subsystems.values()]
        if "error" in statuses or "unreachable" in statuses:
            overall = "unhealthy"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "healthy"

        return {
            "status": overall,
            "subsystems": subsystems,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "frozen": is_frozen(),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_tool_status() -> dict:
    """
    Validate whether TradeBridge can perform reads, writes, waits, and analysis operations.

    Use this to determine which categories of tools are available before initiating a workflow.
    For example, call before a trade to confirm write capability, or before data analysis to
    confirm read capability.

    Args:
        None

    Returns:
        dict: Contains "reads", "writes", "waits", "analysis" (each with status "ok"/"degraded"),
              and "overall" status. The "writes" key includes a "failing" list of failed operations.

    Example: mt5_tool_status() → {"reads": {"status": "ok"}, "writes": {"status": "degraded", "failing": ["get_account"]}, "overall": "degraded"}

    Note: Waits and analysis are pure-Python and always report "ok". Only reads and writes
          depend on MT5 connectivity. Inspect the "failing" lists for actionable diagnostics.
    """
    try:
        # Reads check
        try:
            get_gateway().terminal_status()
            reads_ok = True
        except Exception:
            reads_ok = False

        # Writes check
        failing_writes: list[str] = []
        try:
            result = _tcp_send_and_await("get_account", {}, timeout_s=5.0)
            if not result or result.get("status") != "completed":
                failing_writes.append("get_account")
            writes_ok = len(failing_writes) == 0
        except Exception:
            writes_ok = False
            failing_writes.append("tcp_bridge")

        # Waits and analysis are pure Python, always ok
        waits_ok = True
        analysis_ok = True
        failing_analysis: list[str] = []

        if reads_ok and writes_ok and waits_ok and analysis_ok:
            overall = "ok"
        else:
            overall = "degraded"

        return {
            "reads": {"status": "ok" if reads_ok else "degraded"},
            "writes": {
                "status": "ok" if writes_ok else "degraded",
                "failing": failing_writes,
            },
            "waits": {"status": "ok" if waits_ok else "degraded"},
            "analysis": {
                "status": "ok" if analysis_ok else "degraded",
                "failing": failing_analysis,
            },
            "overall": overall,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_freeze_status() -> dict:
    """
    Check whether trading is currently frozen and retrieve the freeze metadata.

    Use this before any trading action to confirm the system is not in a frozen state.
    A frozen system rejects all write operations (positions, orders, trades).

    Args:
        None

    Returns:
        dict: Contains "frozen" (bool), "frozen_at" (ISO 8601 timestamp or None),
              and "frozen_by" (string identifier of what caused the freeze, or None).

    Example: mt5_freeze_status() → {"frozen": true, "frozen_at": "2026-05-12T10:00:00Z", "frozen_by": "emergency_liquidate"}

    Note: If "frozen" is false, "frozen_at" and "frozen_by" will be empty/None. The frozen
          state is held in-process and persists across tool calls within the same session.
    """
    try:
        return {
            "frozen": _shutdown_state["frozen"],
            "frozen_at": _shutdown_state["frozen_at"],
            "frozen_by": _shutdown_state["frozen_by"],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_thaw() -> dict:
    """
    Unfreeze trading to re-enable write operations (positions, orders, trades).

    Use this after resolving the issue that caused the freeze. Only call when you are certain
    it is safe to resume trading. This is the counterpart to mt5_emergency_liquidate and mt5_safe_shutdown.

    Args:
        None

    Returns:
        dict: Contains "status" ("unfrozen") and "message" with a human-readable confirmation string.

    Example: mt5_thaw() → {"status": "unfrozen", "message": "Trading has been resumed"}

    Note: Thawing does NOT recover closed positions or cancelled orders — it only re-enables
          new trading activity. Calling thaw on an already-unfrozen system is safe and acts as a no-op.
    """
    try:
        thaw()
        return {"status": "unfrozen", "message": "Trading has been resumed"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_emergency_liquidate(
    keep_frozen: bool = True,
) -> dict:
    """
    EMERGENCY: Immediately close ALL positions, cancel ALL pending orders, and freeze trading.

    ⚠️  THIS IS THE MOST DESTRUCTIVE TOOL IN TRADEBRIDGE.  ⚠️
    Use ONLY in true emergencies: flash crashes, black swan events, catastrophic system
    malfunction, or runaway trading behavior. There is NO UNDO — all positions are closed
    and all orders are cancelled immediately.

    Unlike mt5_safe_shutdown, this tool applies NO filtering: every position is closed and
    every pending order is cancelled regardless of session or strategy. The system is frozen
    afterward by default to prevent accidental re-entry.

    Args:
        keep_frozen (bool): If True (default), freeze trading after liquidation to prevent
                            accidental re-entry. Set False to allow immediate resumption. (default: True)

    Returns:
        dict: Contains "mode" (always "full"), "positions_closed" (list of closed position IDs),
              "orders_cancelled" (list of cancelled order IDs), "failed" (list of failed operations
              with reasons), "frozen" (bool), and "summary" with counts of positions/orders/failed.

    Example: mt5_emergency_liquidate() → {"mode": "full", "positions_closed": [123, 456], "orders_cancelled": [789], "summary": {...}}

    Note: Failed operations are collected but do not halt the process — review the "failed" list
          after execution. This tool is idempotent: if no positions or orders exist, it returns
          empty lists. Keep the system frozen (keep_frozen=True) until you verify the emergency is resolved.
    """
    try:
        positions_closed: list[Any] = []
        orders_cancelled: list[Any] = []
        failed: list[Any] = []

        # Fetch current positions and orders via TCP
        pos_result = _tcp_send_and_await("get_positions", {})
        orders_result = _tcp_send_and_await("get_orders", {})

        pos_data = _parse_payload_dict(pos_result) if pos_result else {}
        orders_data = _parse_payload_dict(orders_result) if orders_result else {}

        positions = pos_data.get("positions", pos_data.get("data", []))
        orders = orders_data.get("orders", orders_data.get("data", []))

        total_positions = len(positions) if isinstance(positions, list) else 0
        total_orders = len(orders) if isinstance(orders, list) else 0

        # Close ALL positions (no filtering)
        for pos in positions:
            pos_id = pos.get("position_id") or pos.get("ticket") or pos.get("id")
            if not pos_id:
                failed.append(
                    {"action": "close_position", "item": pos, "reason": "no position_id"}
                )
                continue
            try:
                close_result = _tcp_send_and_await(
                    "close_position", {"position_id": pos_id}
                )
                if close_result and close_result.get("status") == "completed":
                    positions_closed.append(pos_id)
                else:
                    failed.append(
                        {
                            "action": "close_position",
                            "position_id": pos_id,
                            "result": close_result,
                        }
                    )
            except Exception as e:
                failed.append(
                    {"action": "close_position", "position_id": pos_id, "error": str(e)}
                )

        # Cancel ALL pending orders (no filtering)
        for order in orders:
            order_id = order.get("order_id") or order.get("ticket") or order.get("id")
            if not order_id:
                failed.append(
                    {"action": "cancel_order", "item": order, "reason": "no order_id"}
                )
                continue
            try:
                cancel_result = _tcp_send_and_await(
                    "cancel_order", {"order_id": order_id}
                )
                if cancel_result and cancel_result.get("status") == "completed":
                    orders_cancelled.append(order_id)
                else:
                    failed.append(
                        {
                            "action": "cancel_order",
                            "order_id": order_id,
                            "result": cancel_result,
                        }
                    )
            except Exception as e:
                failed.append(
                    {"action": "cancel_order", "order_id": order_id, "error": str(e)}
                )

        # Freeze trading
        if keep_frozen:
            set_frozen(True, by="emergency_liquidate")

        return {
            "mode": "full",
            "positions_closed": positions_closed,
            "orders_cancelled": orders_cancelled,
            "failed": failed,
            "frozen": keep_frozen,
            "summary": {
                "total_positions_found": total_positions,
                "total_orders_found": total_orders,
                "positions_closed": len(positions_closed),
                "orders_cancelled": len(orders_cancelled),
                "failed": len(failed),
            },
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def mt5_safe_shutdown(
    mode: str = "freeze",
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> dict:
    """
    Gracefully shut down trading operations with three severity modes: freeze, flatten, or full.

    Use this for controlled shutdowns (end of session, strategy rotation, maintenance). Unlike
    mt5_emergency_liquidate, this tool respects session/strategy filtering and offers graduated
    severity. Session/strategy filters narrow the scope to only the positions you intend to shut down.

    Mode behaviors:
      - "freeze":  Cancel pending orders only, leave positions open, do NOT freeze the system.
      - "flatten": Close positions only, leave orders untouched, do NOT freeze the system.
      - "full":    Close positions + cancel orders + freeze the system to prevent new activity.

    Args:
        mode (str): Shutdown mode — "freeze", "flatten", or "full". (default: "freeze")
        session_id (Optional[str]): If set, only affect positions/orders tagged with this session ID. (default: None)
        strategy_id (Optional[str]): If set, only affect positions/orders tagged with this strategy ID. (default: None)
        intent_id (Optional[str]): Source identifier recorded as the freeze trigger when mode is "full". (default: None)

    Returns:
        dict: Contains "mode" (executed mode), "positions_closed" (list of closed position IDs),
              "orders_cancelled" (list of cancelled order IDs), "failed" (list of failed operations
              with reasons), and "summary" with counts of positions/orders/failed.

    Example: mt5_safe_shutdown(mode="full", session_id="ses_abc") → {"mode": "full", "positions_closed": [...], "orders_cancelled": [...], "summary": {...}}

    Note: When both session_id and strategy_id are None (default), ALL positions/orders are
          eligible for shutdown. Providing either filter narrows the scope. An invalid mode
          string returns an error dict immediately without performing any actions.
    """
    try:
        if mode not in ("flatten", "freeze", "full"):
            return {
                "error": f"Invalid mode: {mode}. Must be one of: flatten, freeze, full"
            }

        positions_closed: list[Any] = []
        orders_cancelled: list[Any] = []
        failed: list[Any] = []

        # Fetch current positions and orders via TCP
        pos_result = _tcp_send_and_await("get_positions", {})
        orders_result = _tcp_send_and_await("get_orders", {})

        pos_data = _parse_payload_dict(pos_result) if pos_result else {}
        orders_data = _parse_payload_dict(orders_result) if orders_result else {}

        positions = pos_data.get("positions", pos_data.get("data", []))
        orders = orders_data.get("orders", orders_data.get("data", []))

        total_positions = len(positions) if isinstance(positions, list) else 0
        total_orders = len(orders) if isinstance(orders, list) else 0

        # Filter owned positions/orders
        def _is_owned(item: dict) -> bool:
            if session_id and item.get("session_id") == session_id:
                return True
            if strategy_id and item.get("strategy_id") == strategy_id:
                return True
            if not session_id and not strategy_id:
                return True
            return False

        owned_positions = [p for p in positions if isinstance(p, dict) and _is_owned(p)]
        owned_orders = [o for o in orders if isinstance(o, dict) and _is_owned(o)]

        # Flatten: close positions
        if mode in ("flatten", "full"):
            for pos in owned_positions:
                pos_id = pos.get("position_id") or pos.get("ticket") or pos.get("id")
                if not pos_id:
                    failed.append(
                        {
                            "action": "close_position",
                            "item": pos,
                            "reason": "no position_id",
                        }
                    )
                    continue
                try:
                    close_result = _tcp_send_and_await(
                        "close_position",
                        {
                            "position_id": pos_id,
                            "session_id": session_id,
                            "strategy_id": strategy_id,
                        },
                    )
                    if close_result and close_result.get("status") == "completed":
                        positions_closed.append(pos_id)
                    else:
                        failed.append(
                            {
                                "action": "close_position",
                                "position_id": pos_id,
                                "result": close_result,
                            }
                        )
                except Exception as e:
                    failed.append(
                        {
                            "action": "close_position",
                            "position_id": pos_id,
                            "error": str(e),
                        }
                    )

        # Freeze: cancel orders
        if mode in ("freeze", "full"):
            for order in owned_orders:
                order_id = (
                    order.get("order_id") or order.get("ticket") or order.get("id")
                )
                if not order_id:
                    failed.append(
                        {
                            "action": "cancel_order",
                            "item": order,
                            "reason": "no order_id",
                        }
                    )
                    continue
                try:
                    cancel_result = _tcp_send_and_await(
                        "cancel_order",
                        {
                            "order_id": order_id,
                            "session_id": session_id,
                            "strategy_id": strategy_id,
                        },
                    )
                    if cancel_result and cancel_result.get("status") == "completed":
                        orders_cancelled.append(order_id)
                    else:
                        failed.append(
                            {
                                "action": "cancel_order",
                                "order_id": order_id,
                                "result": cancel_result,
                            }
                        )
                except Exception as e:
                    failed.append(
                        {
                            "action": "cancel_order",
                            "order_id": order_id,
                            "error": str(e),
                        }
                    )

        # Full: freeze after cleanup
        if mode == "full":
            set_frozen(True, by=intent_id or "safe_shutdown")

        return {
            "mode": mode,
            "positions_closed": positions_closed,
            "orders_cancelled": orders_cancelled,
            "failed": failed,
            "summary": {
                "total_positions_found": total_positions,
                "total_orders_found": total_orders,
                "positions_closed": len(positions_closed),
                "orders_cancelled": len(orders_cancelled),
                "failed": len(failed),
            },
        }
    except Exception as e:
        return {"error": str(e)}
