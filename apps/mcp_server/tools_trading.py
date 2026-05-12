

import uuid
import time
from collections import OrderedDict
from typing import Any, Optional, Literal
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
    _first_bid_ask,
    _check_frozen_response,
    is_frozen,
    set_frozen,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol
from mt5_mcp.observability.logging import logger


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
class _BoundedDict:
    def __init__(self, max_size: int = 5000, ttl: float = 86400):
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl

    def __setitem__(self, key: str, value: Any) -> None:
        while len(self._data) >= self._max_size:
            self._data.popitem(last=False)
        self._data[key] = (value, time.time())
        self._cleanup()

    def __getitem__(self, key: str) -> Any:
        if key in self._data:
            val, ts = self._data[key]
            if time.time() - ts < self._ttl:
                return val
            del self._data[key]
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def _cleanup(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._data.items() if now - ts >= self._ttl]
        for k in expired:
            del self._data[k]


_trailing_stops: _BoundedDict = _BoundedDict(max_size=5000, ttl=86400)

_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)
_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# ---------------------------------------------------------------------------
# Trade retcode mapping
# ---------------------------------------------------------------------------
_SUCCESS_RETCODES = {10009, 10008}


def _map_trade_retcode(retcode) -> str:
    if retcode is None:
        return "UNKNOWN"
    try:
        code = int(retcode)
    except (ValueError, TypeError):
        return "UNKNOWN"
    if code in _SUCCESS_RETCODES:
        return "SUCCESS"
    elif code == 0:
        return "PENDING"
    else:
        return f"ERROR_{code}"


def _build_trade_error(intent_id, data):
    retcode = data.get("retcode") if isinstance(data, dict) else None
    return {
        "intent_id": intent_id,
        "status": "error",
        "retcode": _map_trade_retcode(retcode),
        "message": data.get("comment", str(data))
        if isinstance(data, dict)
        else str(data),
        "raw": data,
    }


def _await_result_with_gateway_fallback(req_id: str, timeout_s: float = 30.0) -> dict:
    """Await result with one extra gateway poll on timeout."""
    result = _await_result(req_id, timeout_s=timeout_s)
    if result and result.get("status") == "timeout":
        try:
            settings = get_settings_cached()
            client = get_http_client()
            r = client.get(
                f"{settings.gateway_url}/bridge/results/{req_id}", timeout=5.0
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("status") in ("completed", "error"):
                    return data
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Auto-log trade helper
# ---------------------------------------------------------------------------
def _auto_log_trade(
    symbol,
    side,
    action,
    intent_id=None,
    session_id=None,
    strategy_id=None,
    volume_lots=None,
    sl=None,
    tp=None,
    entry_price=None,
    message=None,
):
    try:
        from mt5_mcp.services.trade_journal_db import get_journal_db

        journal = get_journal_db()
        journal.log_decision(
            symbol=symbol,
            side=side,
            action=action,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            volume_lots=volume_lots,
            session_id=session_id,
            strategy_id=strategy_id,
            intent_id=intent_id,
            model_justification=message,
        )
    except Exception as e:
        logger.warning("Trade journal logging failed: %s", e)


# ---------------------------------------------------------------------------
# Tool 1: mt5_submit_market_order
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_submit_market_order", annotations=_WRITE_ANNOTATIONS)
def mt5_submit_market_order(
    symbol: str,
    side: str,
    volume_lots: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    deviation_points: int = 20,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    trail_config: Optional[dict] = None,
) -> dict:
    """
    Submit a market order for immediate execution at the current market price.

    Primary entry tool for live trades. Sends a buy/sell market order to MT5 via the
    TCP bridge with HTTP fallback. The policy engine validates the trade before submission.
    Use for all standard market entries — not for pending/limit/stop orders.

    Args:
        symbol (str): Trading symbol (e.g., "EURUSD", "BTCUSD", "XAUUSD"). Auto-normalized.
        side (str): Trade direction — "buy" or "sell".
        volume_lots (float): Volume in lots. 0.01 = micro, 0.1 = mini, 1.0 = standard.
            Must respect symbol's volume_min/volume_max/volume_step.
        sl (Optional[float]): Stop Loss price. 0 or None means no SL. (default: None)
        tp (Optional[float]): Take Profit price. 0 or None means no TP. (default: None)
        deviation_points (int): Max slippage in points. Higher = less requote risk
            but potentially worse fill price. (default: 20)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID. Auto-generated as UUID if None. (default: None)
        idempotency_key (Optional[str]): Safe-retry key — same key + same call = no duplicate.
            Auto-generated if None. (default: None)
        trail_config (Optional[dict]): Trailing stop setup with optional keys:
            "distance_points" (float), "lock_in_points" (float). (default: None)

    Returns:
        dict: Contains intent_id, status ("submitted"), adapter ("EASocketAdapter"),
            broker_order_id (str, MT5 ticket), retcode (str, e.g. "SUCCESS"/"ERROR_*"),
            message (str), raw (dict, full bridge response).

    Example: mt5_submit_market_order("EURUSD", "buy", 0.1, sl=1.0450, tp=1.0550, deviation_points=15)

    Note: Slippage is set via deviation_points. Policy engine rejects margin-violating orders
        before reaching broker. Crypto/metal symbols (BTC, ETH, XAU) may need variant suffix
        (e.g. "BTCUSDm"). Retcode "ERROR_*" indicates broker rejection.
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
            return {"error": f"Policy blocked: {decision.reason}"}

        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        trail_params = {}
        if trail_config:
            trail_params["trail_distance_points"] = trail_config.get(
                "distance_points", 0
            )
            trail_params["trail_lock_in_points"] = trail_config.get("lock_in_points", 0)

        result = _tcp_send_and_await(
            "submit_order",
            {
                "symbol": normalize_symbol(symbol),
                "side": side,
                "volume_lots": volume_lots,
                "sl": sl or 0,
                "tp": tp or 0,
                "deviation": deviation_points,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
                **trail_params,
            },
        )

        if result is None:
            return {"error": "submit_order failed — TCP unavailable"}

        data = _parse_payload_dict(result)
        retcode = data.get("retcode", 0) if data else 0
        retcode_int = int(retcode) if retcode is not None else 0

        if retcode_int not in _SUCCESS_RETCODES:
            return _build_trade_error(intent_id, data or result)

        _auto_log_trade(
            symbol=symbol,
            side=side,
            action="entry",
            intent_id=intent_id,
            session_id=session_id,
            strategy_id=strategy_id,
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
            entry_price=data.get("price"),
        )

        return {
            "intent_id": intent_id,
            "status": "submitted",
            "adapter": "EASocketAdapter",
            "broker_order_id": str(data.get("order", "")),
            "retcode": _map_trade_retcode(retcode),
            "message": f"Order submitted (retcode={retcode_int})",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: mt5_submit_market_order_via_bridge
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_submit_market_order_via_bridge", annotations=_WRITE_ANNOTATIONS)
def mt5_submit_market_order_via_bridge(
    symbol: str,
    side: str,
    volume_lots: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    deviation_points: int = 20,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Submit a market order via the HTTP bridge fallback path.

    Alternative to mt5_submit_market_order that routes through the HTTP gateway instead
    of the direct TCP bridge. Use this when the TCP bridge is unavailable or unreachable.
    The bridge enqueues the command and polls for the result with a 30-second timeout.

    Args:
        symbol (str): Trading symbol (e.g., "EURUSD", "BTCUSD", "XAUUSD"). Auto-normalized.
        side (str): Trade direction — "buy" or "sell".
        volume_lots (float): Volume in lots. 0.01 = micro, 0.1 = mini, 1.0 = standard.
            Must respect symbol's volume constraints.
        sl (Optional[float]): Stop Loss price. 0 or None means no SL. (default: None)
        tp (Optional[float]): Take Profit price. 0 or None means no TP. (default: None)
        deviation_points (int): Max slippage in points. (default: 20)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID. Auto-generated as UUID if None. (default: None)
        idempotency_key (Optional[str]): Safe-retry key — prevents duplicate order on retry.
            Auto-generated if None. (default: None)

    Returns:
        dict: Contains intent_id, status ("submitted"), adapter ("HTTPBridgeFallback"),
            broker_order_id (str, MT5 ticket), retcode (str, e.g. "SUCCESS"/"ERROR_*"),
            message (str), raw (dict, full bridge response). On timeout, includes a "hint"
            field advising fallback to mt5_submit_market_order (TCP path).

    Example: mt5_submit_market_order_via_bridge("GBPUSD", "sell", 0.05, sl=1.2500)

    Note: Prefer mt5_submit_market_order (TCP) for lower latency. Use this bridge variant
        only when TCP is unavailable. Timeout after 30s returns a hint suggesting TCP fallback.
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
            return {"error": f"Policy blocked: {decision.reason}"}

        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        settings = get_settings_cached()
        client = get_http_client()
        req = client.post(
            f"{settings.gateway_url}/bridge/commands/enqueue",
            params={
                "type": "submit_order",
                "symbol": normalize_symbol(symbol),
                "side": side,
                "volume_lots": volume_lots,
                "sl": sl or 0,
                "tp": tp or 0,
                "deviation": deviation_points,
                "session_id": session_id or "",
                "strategy_id": strategy_id or "",
                "intent_id": intent_id or "",
                "idempotency_key": idempotency_key,
            },
        )
        req.raise_for_status()
        req_id = req.json().get("id") or req.json().get("request_id")
        result = _await_result_with_gateway_fallback(req_id, timeout_s=30.0)

        data = _parse_payload_dict(result) if result else {}
        retcode = data.get("retcode", 0) if data else 0
        retcode_int = int(retcode) if retcode is not None else 0

        if retcode_int not in _SUCCESS_RETCODES:
            error_resp = _build_trade_error(intent_id, data or result)
            if result and result.get("status") == "timeout":
                error_resp["hint"] = (
                    "Bridge HTTP fallback timed out. Use mt5_submit_market_order (TCP path) instead. "
                    "Verify bridge EA is running on MT5 and TCP port is accessible."
                )
            return error_resp

        _auto_log_trade(
            symbol=symbol,
            side=side,
            action="entry",
            intent_id=intent_id,
            session_id=session_id,
            strategy_id=strategy_id,
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
            entry_price=data.get("price"),
        )

        return {
            "intent_id": intent_id,
            "status": "submitted",
            "adapter": "HTTPBridgeFallback",
            "broker_order_id": str(data.get("order", "")),
            "retcode": _map_trade_retcode(retcode),
            "message": f"Order submitted via bridge (retcode={retcode_int})",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: mt5_submit_pending_order
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_submit_pending_order", annotations=_WRITE_ANNOTATIONS)
def mt5_submit_pending_order(
    symbol: str,
    side: str,
    kind: str,
    price: float,
    volume_lots: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    deviation: int = 20,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Place a pending order (limit/stop) for future execution at a specified price.

    Use this tool when you want to enter at a specific price level rather than the
    current market price. Supports all MT5 pending order types: buy limit, sell limit,
    buy stop, sell stop. The order is triggered when the market reaches the target price.

    Args:
        symbol (str): Trading symbol (e.g., "EURUSD", "BTCUSD"). Auto-normalized.
        side (str): Trade direction — "buy" or "sell".
        kind (str): Pending order type — typically "limit" or "stop".
        price (float): Trigger/entry price for the pending order.
        volume_lots (float): Volume in lots. 0.01 = micro, 0.1 = mini, 1.0 = standard.
        sl (Optional[float]): Stop Loss price. 0 or None means no SL. (default: None)
        tp (Optional[float]): Take Profit price. 0 or None means no TP. (default: None)
        deviation (int): Max deviation from price for partial fills. (default: 20)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID. Auto-generated as UUID if None. (default: None)
        idempotency_key (Optional[str]): Safe-retry key — prevents duplicate order on retry.
            Auto-generated if None. (default: None)

    Returns:
        dict: Contains intent_id, status ("pending_order_submitted"),
            adapter ("EASocketAdapter"), broker_order_id (str, MT5 order ticket),
            retcode (str, e.g. "SUCCESS"/"ERROR_*"), message (str), raw (dict).

    Example: mt5_submit_pending_order("EURUSD", "buy", "limit", 1.0500, 0.1)

    Note: Crypto and metal symbols (BTC, ETH, XAU) may not support pending orders on some
        brokers — retcode 0 with a hint field suggests trying the 'm' symbol variant or
        using a market order instead. Pending orders occupy margin differently than market orders.
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
            return {"error": f"Policy blocked: {decision.reason}"}

        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await(
            "submit_pending_order",
            {
                "symbol": symbol_norm,
                "side": side,
                "kind": kind,
                "price": price,
                "volume_lots": volume_lots,
                "sl": sl or 0,
                "tp": tp or 0,
                "deviation": deviation,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
            },
        )

        if result is None:
            return {"error": "submit_pending_order failed — TCP unavailable"}

        data = _parse_payload_dict(result)
        retcode = data.get("retcode", 0) if data else 0
        retcode_int = int(retcode) if retcode is not None else 0

        if retcode_int not in _SUCCESS_RETCODES:
            symbol_warning = ""
            if retcode_int == 0 and any(
                symbol.upper().startswith(c) for c in ("BTC", "ETH", "XAU")
            ):
                symbol_warning = (
                    f" Note: retcode 0 on crypto/metal symbols may indicate the EA adapter "
                    f"rejected the pending order. Try using the '{symbol}m' variant or place a market order instead."
                )
            error_resp = _build_trade_error(intent_id, data or result)
            if symbol_warning:
                error_resp["hint"] = symbol_warning
            return error_resp

        _auto_log_trade(
            symbol=symbol,
            side=side,
            action="pending_entry",
            intent_id=intent_id,
            session_id=session_id,
            strategy_id=strategy_id,
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
            entry_price=price,
        )

        return {
            "intent_id": intent_id,
            "status": "pending_order_submitted",
            "adapter": "EASocketAdapter",
            "broker_order_id": str(data.get("order", "")),
            "retcode": _map_trade_retcode(retcode),
            "message": f"Pending order submitted (retcode={retcode_int})",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 4: mt5_close_position
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_close_position", annotations=_WRITE_ANNOTATIONS)
def mt5_close_position(
    position_id: str,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> dict:
    """
    Close a single open position by its position ID.

    Use this to exit a specific trade. Attempts TCP bridge first, falls back to HTTP
    bridge if TCP is unavailable. The position_id must match an open position's ticket
    number as returned by mt5_positions_open or the broker_order_id from entry tools.

    Args:
        position_id (str): MT5 position ticket number (e.g., "12345678"). Must be a
            currently open position. String-format accepted.
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)

    Returns:
        dict: Contains position_id, status ("closed" on success), retcode (str),
            message (str), raw (dict, full bridge response).

    Example: mt5_close_position("12345678")

    Note: Retcode 0 (PENDING) may indicate the close request was accepted but not yet
        confirmed — check positions list after a short delay. The position must be open
        on the connected MT5 terminal. Use mt5_close_all_positions for bulk exiting.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        result = _tcp_send_and_await(
            "close_position",
            {
                "position_id": position_id,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
            },
        )

        req_id: Optional[str] = None
        if result is None:
            logger.warning("mt5_close_position: TCP unavailable, falling back to HTTP")
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "close_position",
                    "position_id": position_id,
                    "session_id": session_id or "",
                    "strategy_id": strategy_id or "",
                    "intent_id": intent_id or "",
                },
            )
            req.raise_for_status()
            req_id_val = req.json().get("id") or req.json().get("request_id")
            req_id = str(req_id_val) if req_id_val else ""
            result = _await_result_with_gateway_fallback(req_id, timeout_s=30.0)

        if result is None:
            return {
                "error": "close_position failed — no response from TCP or HTTP bridge"
            }

        data = _parse_payload_dict(result) if result else {}
        if not data:
            return {"error": "close_position failed — empty response from bridge"}

        retcode = data.get("retcode", 0) if data else 0
        retcode_int = int(retcode) if retcode is not None else 0

        # retcode 0 = unknown/pending — treat as potential error for close operations
        if retcode_int not in _SUCCESS_RETCODES and retcode_int != 0:
            return _build_trade_error(intent_id, data)

        _auto_log_trade(
            symbol="",
            side="",
            action="exit",
            intent_id=intent_id,
            session_id=session_id,
            strategy_id=strategy_id,
        )

        return {
            "position_id": position_id,
            "status": "closed",
            "retcode": _map_trade_retcode(retcode),
            "message": f"Position closed (retcode={retcode_int})",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: mt5_close_all_positions
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_close_all_positions", annotations=_WRITE_ANNOTATIONS)
def mt5_close_all_positions(
    side: Optional[str] = None,
    symbol: Optional[str] = None,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Close all open positions, optionally filtered by side and/or symbol.

    Mass-exit tool for emergency flattening or session cleanup. Supports optional filters:
    close only buys/sells, or close positions on a specific symbol. This is a destructive
    operation — use with caution, especially without filters.

    Args:
        side (Optional[str]): Filter — "buy" or "sell". If omitted, closes all sides. (default: None)
        symbol (Optional[str]): Symbol filter (e.g., "EURUSD"). If omitted, closes all symbols. (default: None)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)
        idempotency_key (Optional[str]): Safe-retry key — prevents re-execution on retry.
            Auto-generated if None. (default: None)

    Returns:
        dict: Contains status ("close_all_positions_completed"), closed_count (int),
            raw (dict, full bridge response).

    Example: mt5_close_all_positions(symbol="EURUSD")  — closes all EURUSD positions

    Note: If no filters are applied, ALL open positions on the account will be closed.
        Uses _await_result with a 60-second timeout. TCP bridge used first, HTTP fallback
        on failure. Not idempotent by default — use idempotency_key for safe retry.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        result = _tcp_send_and_await(
            "close_all_positions",
            {
                "side": side or "",
                "symbol": normalize_symbol(symbol) if symbol else "",
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
            },
        )

        if result is None:
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "close_all_positions",
                    "side": side or "",
                    "symbol": normalize_symbol(symbol) if symbol else "",
                    "session_id": session_id or "",
                    "strategy_id": strategy_id or "",
                    "intent_id": intent_id or "",
                    "idempotency_key": idempotency_key,
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id, timeout_s=60.0)

        data = _parse_payload_dict(result) if result else {}
        closed_count = data.get("closed_count", data.get("closed", 0))
        return {
            "status": "close_all_positions_completed",
            "closed_count": closed_count,
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: mt5_cancel_order
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_cancel_order", annotations=_WRITE_ANNOTATIONS)
def mt5_cancel_order(
    order_id: str,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Cancel a single pending order by its order ID.

    Removes a previously placed pending (limit/stop) order before it gets triggered.
    Use this to cancel individual orders that are no longer valid. The order_id must
    match an active pending order ticket from mt5_submit_pending_order.

    Args:
        order_id (str): MT5 pending order ticket number (e.g., "12345678").
            String-format accepted. Must be a currently active pending order.
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)
        idempotency_key (Optional[str]): Safe-retry key. Auto-generated if None. (default: None)

    Returns:
        dict: Contains order_id, status ("cancelled" on success, or "error"),
            raw (dict, full bridge response). On failure, status may be
            "cancel_failed", "order_not_found", or "error" with details.

    Example: mt5_cancel_order("12345678")

    Note: Distinguish between positions (filled trades) and orders (pending entries).
        Use mt5_close_position to exit a filled position, mt5_cancel_order to remove
        a pending order. Error "order_not_found" means the order was already filled or cancelled.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        result = _tcp_send_and_await(
            "cancel_order",
            {
                "order_id": order_id,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
            },
        )

        if result is None:
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "cancel_order",
                    "order_id": order_id,
                    "session_id": session_id or "",
                    "strategy_id": strategy_id or "",
                    "intent_id": intent_id or "",
                    "idempotency_key": idempotency_key or "",
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        raw_status = data.get("status", "")
        if result and result.get("status") == "error":
            raw_status = data.get("error", raw_status) or "cancel_failed"

        if raw_status in ("cancel_failed", "order_not_found", "error"):
            return {
                "order_id": order_id,
                "status": "error",
                "error": raw_status,
                "raw": data,
            }
        return {
            "order_id": order_id,
            "status": "cancelled",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 7: mt5_cancel_all_orders
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_cancel_all_orders", annotations=_WRITE_ANNOTATIONS)
def mt5_cancel_all_orders(
    side: Optional[str] = None,
    symbol: Optional[str] = None,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Cancel all pending orders, optionally filtered by side and/or symbol.

    Mass-cancel tool for clearing pending order books. Supports optional filters to
    cancel only buy/sell orders or orders on a specific symbol. Useful when a trading
    plan changes or during market regime shifts where existing pending orders are invalid.

    Args:
        side (Optional[str]): Filter — "buy" or "sell". If omitted, cancels both sides. (default: None)
        symbol (Optional[str]): Symbol filter (e.g., "EURUSD"). If omitted, cancels on all symbols. (default: None)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)
        idempotency_key (Optional[str]): Safe-retry key. Auto-generated if None. (default: None)

    Returns:
        dict: Contains status ("cancel_all_orders_completed"), cancelled_count (int),
            raw (dict, full bridge response).

    Example: mt5_cancel_all_orders(side="buy")  — cancels all buy pending orders

    Note: If no filters are applied, ALL pending orders on the account will be cancelled.
        Uses _await_result with 60-second timeout. TCP bridge first, HTTP fallback.
        Cannot cancel orders that have already been filled — those are now positions.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        result = _tcp_send_and_await(
            "cancel_all_orders",
            {
                "side": side or "",
                "symbol": normalize_symbol(symbol) if symbol else "",
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
            },
        )

        if result is None:
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "cancel_all_orders",
                    "side": side or "",
                    "symbol": normalize_symbol(symbol) if symbol else "",
                    "session_id": session_id or "",
                    "strategy_id": strategy_id or "",
                    "intent_id": intent_id or "",
                    "idempotency_key": idempotency_key or "",
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id, timeout_s=60.0)

        data = _parse_payload_dict(result) if result else {}
        return {
            "status": "cancel_all_orders_completed",
            "cancelled_count": data.get("cancelled_count", 0),
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 8: mt5_modify_order
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_modify_order", annotations=_WRITE_ANNOTATIONS)
def mt5_modify_order(
    order_id: str,
    new_price: Optional[float] = None,
    new_sl: Optional[float] = None,
    new_tp: Optional[float] = None,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Modify a pending order's price, stop loss, and/or take profit.

    Updates an existing pending (limit/stop) order's parameters before it gets triggered.
    Supports partial updates — only provided fields are changed. Use this to adjust
    entries as market conditions change without cancelling and recreating the order.

    Args:
        order_id (str): MT5 pending order ticket number. Must be an active pending order.
        new_price (Optional[float]): New trigger/entry price for the pending order. (default: None)
        new_sl (Optional[float]): New Stop Loss price. (default: None)
        new_tp (Optional[float]): New Take Profit price. (default: None)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)
        idempotency_key (Optional[str]): Safe-retry key. Auto-generated if None. (default: None)

    Returns:
        dict: Contains order_id, status ("modified" on success, or "error"),
            raw (dict, full bridge response).

    Example: mt5_modify_order("12345678", new_price=1.0550, new_sl=1.0500)

    Note: Only the fields explicitly provided are updated. Order cannot be modified after
        it has been triggered/filled. Use mt5_modify_position_sl_tp for modifying SL/TP
        on an already-filled position.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        payload: dict[str, Any] = {
            "order_id": order_id,
            "session_id": session_id,
            "strategy_id": strategy_id,
            "intent_id": intent_id,
            "idempotency_key": idempotency_key,
        }
        if new_price is not None:
            payload["new_price"] = new_price
        if new_sl is not None:
            payload["new_sl"] = new_sl
        if new_tp is not None:
            payload["new_tp"] = new_tp

        result = _tcp_send_and_await("modify_order", payload)

        if result is None:
            settings = get_settings_cached()
            client = get_http_client()
            params = {
                "type": "modify_order",
                "order_id": order_id,
                "session_id": session_id or "",
                "strategy_id": strategy_id or "",
                "intent_id": intent_id or "",
                "idempotency_key": idempotency_key or "",
            }
            if new_price is not None:
                params["new_price"] = new_price
            if new_sl is not None:
                params["new_sl"] = new_sl
            if new_tp is not None:
                params["new_tp"] = new_tp
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params=params,
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if result and result.get("status") == "error":
            return {
                "order_id": order_id,
                "status": "error",
                "error": data.get("error", "modify_order_failed"),
                "raw": data,
            }
        return {
            "order_id": order_id,
            "status": "modified",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 9: mt5_modify_position_sl_tp
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_modify_position_sl_tp", annotations=_WRITE_ANNOTATIONS)
def mt5_modify_position_sl_tp(
    position_id: str,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> dict:
    """
    Modify the Stop Loss and/or Take Profit on an already-open position.

    Adjusts risk parameters on a filled position without closing it. Supports setting
    SL independently from TP — omit either to leave it unchanged (note: sending 0 removes it).
    TCP bridge used first with HTTP fallback.

    Args:
        position_id (str): MT5 position ticket number. Must be an open position.
        sl (Optional[float]): New Stop Loss price. Set to 0 or None to remove SL. (default: None)
        tp (Optional[float]): New Take Profit price. Set to 0 or None to remove TP. (default: None)
        session_id (Optional[str]): Session label for audit trail. (default: None)
        strategy_id (Optional[str]): Strategy label for performance attribution. (default: None)
        intent_id (Optional[str]): Unique intent ID for correlation. (default: None)

    Returns:
        dict: Contains position_id, status ("modified" on success, or "error"),
            retcode (str), message (str), raw (dict, full bridge response).

    Example: mt5_modify_position_sl_tp("12345678", sl=1.0400, tp=1.0600)

    Note: SL and TP values are absolute price levels, not distances/points.
        Broker/server restrictions may apply (e.g., minimum SL distance from market).
        Use mt5_trail_position for automated trailing stop logic.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        result = _tcp_send_and_await(
            "modify_position_sl_tp",
            {
                "position_id": position_id,
                "sl": sl or 0,
                "tp": tp or 0,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "intent_id": intent_id,
            },
        )

        if result is None:
            settings = get_settings_cached()
            client = get_http_client()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "modify_position_sl_tp",
                    "position_id": position_id,
                    "sl": sl or 0,
                    "tp": tp or 0,
                    "session_id": session_id or "",
                    "strategy_id": strategy_id or "",
                    "intent_id": intent_id or "",
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        retcode = data.get("retcode", 0) if data else 0
        retcode_int = int(retcode) if retcode is not None else 0

        if result and result.get("status") == "error":
            return {
                "position_id": position_id,
                "status": "error",
                "error": data.get("error", "modify_failed"),
                "retcode": _map_trade_retcode(retcode),
                "raw": data,
            }

        return {
            "position_id": position_id,
            "status": "modified",
            "retcode": _map_trade_retcode(retcode),
            "message": f"SL/TP modified (retcode={retcode_int})",
            "raw": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 10: mt5_calculate_position_size
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_calculate_position_size", annotations=_READ_ANNOTATIONS)
def mt5_calculate_position_size(
    symbol: str,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
    equity: Optional[float] = None,
) -> dict:
    """
    Calculate the safe position size (in lots) based on account risk parameters.

    Pre-trade risk calculator. Uses account equity, risk percentage, entry price and
    stop loss to compute a lot size that respects max risk tolerance. Fetches symbol
    info (tick_size, tick_value, volume_min/max/step) and account equity automatically.

    Args:
        symbol (str): Trading symbol (e.g., "EURUSD", "BTCUSD"). Must have tick data available.
        risk_percent (float): Percentage of account equity to risk on this trade (e.g., 1.0 = 1%).
            Typical range: 0.5–2.0 for standard risk management.
        entry_price (float): Planned entry price for the trade.
        stop_loss_price (float): Planned stop loss price. The distance from entry_price
            determines risk per unit.
        equity (Optional[float]): Account equity to use for calculation. If None, fetched
            automatically from account summary. (default: None)

    Returns:
        dict: Contains symbol, recommended_volume (float, calculated lots),
            risk_amount (float, currency amount at risk), risk_percent (float, confirmed %),
            volume_min/max/step (float, symbol constraints),
            tick_size/tick_value (float, market data used), and potential error info.

    Example: mt5_calculate_position_size("EURUSD", risk_percent=1.0, entry_price=1.0500, stop_loss_price=1.0450)

    Note: Tick size/value must be available for the symbol — crypto symbols on some brokers
        may not return tick data (try 'm' variant). The result should be rounded to the
        nearest volume_step. Always validate with mt5_validate_trade_setup before executing.
    """
    try:
        from apps.mcp_server.tools_resources import mt5_symbol_info, mt5_account_summary

        risk_percent = float(risk_percent)
        entry_price = float(entry_price)
        stop_loss_price = float(stop_loss_price)

        symbol_info = mt5_symbol_info(symbol)

        if "error" in symbol_info:
            return {
                "error": f"Cannot retrieve symbol info for {symbol}: {symbol_info.get('error')}",
                "symbol": symbol,
                "hint": "Verify the symbol name is correct and market is open.",
            }

        account = mt5_account_summary()

        from mt5_mcp.services.agent_capabilities import calculate_position_size

        if equity is None:
            acct = account.get("account", {})
            equity = float(acct.get("equity", acct.get("balance", 0)))
        else:
            equity = float(equity)

        tick_size = float(symbol_info.get("tick_size", 0) or 0)
        tick_value = float(symbol_info.get("tick_value", 0) or 0)
        volume_min = float(symbol_info.get("volume_min", 0.01) or 0.01)
        volume_max = float(symbol_info.get("volume_max", 100) or 100)
        volume_step = float(symbol_info.get("volume_step", 0.01) or 0.01)

        if tick_size == 0 or tick_value == 0:
            return {
                "error": f"tick_size or tick_value unavailable for {symbol}",
                "symbol": symbol,
                "raw_symbol_info": {
                    "tick_size": symbol_info.get("tick_size"),
                    "tick_value": symbol_info.get("tick_value"),
                },
                "hint": "MT5 did not return tick data. This is common for crypto symbols on some brokers. Try using a different symbol variant (e.g., BTCUSDm instead of BTCUSD).",
            }

        result = calculate_position_size(
            equity=equity,
            risk_percent=risk_percent,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            tick_size=tick_size,
            tick_value=tick_value,
            volume_min=volume_min,
            volume_max=volume_max,
            volume_step=volume_step,
        )

        return {"symbol": symbol, **result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 11: mt5_validate_trade_setup
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_validate_trade_setup", annotations=_READ_ANNOTATIONS)
def mt5_validate_trade_setup(
    symbol: str,
    side: str,
    order_kind: str,
    volume_lots: float,
    entry_price: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
) -> dict:
    """
    Run comprehensive pre-trade validation checks on a proposed trade setup.

    Pre-flight check before submitting any order. Validates symbol info availability,
    account margin sufficiency, order book liquidity, and risk parameter sanity.
    Combines data from symbol_info, account_summary, positions, and order book to
    assess whether the trade setup is safe to execute.

    Args:
        symbol (str): Trading symbol to validate (e.g., "EURUSD").
        side (str): Trade direction — "buy" or "sell".
        order_kind (str): Order type — typically "market" or "limit"/"stop".
        volume_lots (float): Proposed volume in lots.
        entry_price (float): Proposed entry price.
        sl (Optional[float]): Proposed Stop Loss price. (default: None)
        tp (Optional[float]): Proposed Take Profit price. (default: None)

    Returns:
        dict: Validation result from agent_capabilities.validate_trade_setup.
            Includes fields like valid (bool), reasons (list of issues if invalid),
            margin_required, margin_available, volume_ok, spread_ok, etc.

    Example: mt5_validate_trade_setup("EURUSD", "buy", "market", 0.1, 1.0500, sl=1.0450)

    Note: This performs LOCAL validation only — the broker's MT5 server may still reject
        the order (e.g., due to server-side margin rules, trading sessions, or instrument
        restrictions). Margin estimation requires gateway access; returns 0 if unavailable.
    """
    try:
        from apps.mcp_server.tools_resources import (
            mt5_symbol_info,
            mt5_account_summary,
            mt5_positions_open,
        )
        from apps.mcp_server.tools_market_data import mt5_get_order_book

        symbol_info = mt5_symbol_info(symbol)

        if "error" in symbol_info:
            return {
                "error": f"Cannot retrieve symbol info for {symbol}: {symbol_info.get('error')}",
                "symbol": symbol,
                "valid": False,
            }

        account = mt5_account_summary()
        order_book = mt5_get_order_book(symbol)

        try:
            margin_estimate = get_gateway().estimate_margin(
                type=type(
                    "R",
                    (),
                    {
                        "symbol": normalize_symbol(symbol),
                        "side": side,
                        "volume_lots": volume_lots,
                        "entry_price": entry_price,
                        "order_kind": order_kind,
                    },
                )()
            )
            required_margin = float(
                getattr(
                    margin_estimate,
                    "required_margin",
                    getattr(margin_estimate, "margin", 0),
                )
            )
        except Exception:
            required_margin = 0

        from mt5_mcp.services.agent_capabilities import validate_trade_setup

        current_bid = order_book.get("bid")
        current_ask = order_book.get("ask")

        result = validate_trade_setup(
            symbol_info=symbol_info,
            account_summary=account.get("account", {}),
            side=side,
            order_kind=order_kind,
            volume_lots=volume_lots,
            current_bid=float(current_bid) if current_bid else 0.0,
            current_ask=float(current_ask) if current_ask else 0.0,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            required_margin=required_margin,
        )

        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 12: mt5_trail_position
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_trail_position", annotations=_WRITE_ANNOTATIONS)
def mt5_trail_position(
    position_id: str,
    distance_points: float,
    lock_in_points: float = 0,
) -> dict:
    """
    Activate or update a trailing stop on an open position.

    Dynamically adjusts the stop loss as the market moves in the trade's favor.
    The trailing stop follows price movement by a fixed distance_points behind the
    current market price. Optionally locks in a minimum profit with lock_in_points.

    Args:
        position_id (str): MT5 position ticket number to trail. Must be an open position.
        distance_points (float): Trail distance in points/pips behind the current
            market price. Smaller = tighter trail, larger = more breathing room.
        lock_in_points (float): Minimum locked-in profit in points. Stop loss will
            never move below this level once reached. 0 = no lock-in. (default: 0)

    Returns:
        dict: Contains position_id, computed_sl (float, the new SL price applied),
            result (dict, raw bridge response from the SL update).

    Example: mt5_trail_position("12345678", distance_points=50, lock_in_points=20)

    Note: Trailing state is stored in-memory (_trailing_stops dict with 24h TTL).
        The stop updates atomically via modify_position_sl_tp. For buy positions,
        SL = ask - distance. For sell positions, SL = bid + distance.
        Call periodically (e.g., every bar/minute in a trading loop) to keep
        the trail active. Does NOT automatically re-trail — requires repeated calls.
    """
    try:
        frozen = _check_frozen_response()
        if frozen:
            return frozen

        from apps.mcp_server.tools_resources import mt5_positions_open, mt5_symbol_info
        from apps.mcp_server.tools_market_data import mt5_get_order_book

        positions_result = mt5_positions_open()
        positions = positions_result.get("positions", [])

        position = None
        for p in positions:
            if str(p.get("position_id", "")) == str(position_id):
                position = p
                break

        if position is None:
            return {"error": f"Position {position_id} not found"}

        symbol = position.get("symbol", "")
        sym_info = mt5_symbol_info(symbol)
        point = float(sym_info.get("point", 0) or 0)

        if point <= 0:
            return {"error": f"Could not determine point size for {symbol}"}

        order_book = mt5_get_order_book(symbol)
        bid = order_book.get("bid")
        ask = order_book.get("ask")

        side = position.get("side", "").lower()
        entry_price = float(position.get("entry_price", 0) or 0)
        distance_price = distance_points * point

        if side == "buy":
            if ask is None:
                return {"error": "Could not get ask price for trailing"}
            new_sl = ask - distance_price
            if lock_in_points > 0:
                lock_in_price = entry_price + (lock_in_points * point)
                new_sl = max(new_sl, lock_in_price)
        else:
            if bid is None:
                return {"error": "Could not get bid price for trailing"}
            new_sl = bid + distance_price
            if lock_in_points > 0:
                lock_in_price = entry_price - (lock_in_points * point)
                new_sl = min(new_sl, lock_in_price)

        result = _tcp_send_and_await(
            "modify_position_sl_tp",
            {
                "position_id": position_id,
                "sl": new_sl,
                "tp": position.get("tp", 0) or 0,
            },
        )

        data = _parse_payload_dict(result) if result else {}

        _trailing_stops[position_id] = {
            "distance_points": distance_points,
            "lock_in_points": lock_in_points,
            "last_sl": new_sl,
            "last_updated": time.time(),
        }

        return {
            "position_id": position_id,
            "computed_sl": new_sl,
            "result": data,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 13: mt5_news_fetch
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_news_fetch", annotations=_READ_ANNOTATIONS)
def mt5_news_fetch(
    limit: int = 10,
    source: Optional[str] = None,
) -> dict:
    """
    Fetch recent market news articles from available news providers.

    Retrieves financial news for market-aware analysis. Tries the IG API first,
    then falls back to the MT5 bridge endpoint. Returns an empty list with
    a status hint if no news source is available (e.g., behind a firewall).

    Args:
        limit (int): Maximum number of news articles to return. (default: 10)
        source (Optional[str]): News provider name. Currently supported: "ig", "reuters",
            "bloomberg". If None, uses IG API first, then bridge fallback. (default: None)

    Returns:
        dict: Contains news (list of article dicts with title, body, date, etc.),
            count (int), source (str, which provider served the data), and on failure:
            status ("unavailable"), hint (str, troubleshooting suggestion),
            available_sources (list).

    Example: mt5_news_fetch(limit=5, source="ig")

    Note: Requires network access to the IG API or a configured news provider.
        If running on a restricted network or VPS without internet on that interface,
        news will be unavailable and a hint will explain. Consider skipping
        news-dependent analysis until a source is available.
    """
    try:
        client = get_http_client()
        params: dict[str, Any] = {"limit": limit}
        if source:
            params["source"] = source

        try:
            resp = client.get(
                "https://www.ig.com/ig-com-api/v1/market-news/news",
                params=params,
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                news_list = data.get(
                    "items", data.get("news", data if isinstance(data, list) else [])
                )
                return {
                    "news": news_list[:limit],
                    "count": len(news_list[:limit]),
                    "source": source or "ig",
                }
        except Exception:
            pass

        settings = get_settings_cached()
        try:
            resp = client.get(
                f"{settings.gateway_url}/bridge/news",
                params=params,
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "news": data.get("news", [])[:limit],
                    "count": len(data.get("news", [])[:limit]),
                    "source": "bridge",
                }
        except Exception:
            pass

        return {
            "news": [],
            "count": 0,
            "source": "unavailable",
            "available_sources": ["ig", "reuters", "bloomberg"],
            "status": "unavailable",
            "hint": (
                "News feed requires network access to IG API or a configured news provider. "
                "If running behind a firewall or on a restricted network, configure MT5_NEWS_API_URL "
                "or provide a local news source. Consider skipping news-dependent analysis until resolved."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 14: mt5_news_enrich
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_news_enrich", annotations=_READ_ANNOTATIONS)
def mt5_news_enrich(
    title: str,
    content_snippet: str = "",
    source_name: str = "",
) -> dict:
    """
    Enrich a news item with sentiment analysis, topic extraction, and currency relevance.

    Locally processes a news headline/snippet to extract structured metadata without
    any network calls. Uses rule-based NLP (no ML model dependency). Useful for
    categorizing news relevance to currency pairs before making trading decisions.

    Args:
        title (str): News article headline. This is the primary text analyzed.
        content_snippet (str): Optional article body or summary text. Concatenated
            with title for richer analysis. (default: "")
        source_name (str): Optional source identifier (e.g., "reuters", "bloomberg")
            for attribution. (default: "")

    Returns:
        dict: Contains sentiment (str: "positive", "negative", or "neutral"),
            topics (list of str, extracted topics/keywords),
            entities (list of extracted named entities),
            currency_relevance (list of currency pairs this news may affect).

    Example: mt5_news_enrich("ECB raises interest rates by 25bp", "The European Central Bank...")

    Note: Pure local processing — no API costs, no latency. Sentiment is rule-based
        (not ML), suitable for rapid screening but not nuanced analysis. Works
        offline and has no external dependencies.
    """
    from mt5_mcp.services.news_service import (
        _simple_sentiment,
        _extract_topics,
        _extract_entities,
        _currency_relevance,
        NewsItem,
    )

    text = f"{title} {content_snippet}"
    item = NewsItem(
        id="",
        title=title,
        link="",
        pub_date="",
        source_name=source_name,
        source_id="",
        pool_id="",
        content_snippet=content_snippet,
    )

    return {
        "sentiment": _simple_sentiment(text),
        "topics": _extract_topics(text),
        "entities": _extract_entities(text),
        "currency_relevance": _currency_relevance(item),
    }


# ---------------------------------------------------------------------------
# Tool 15: mt5_news_pools
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_news_pools", annotations=_READ_ANNOTATIONS)
def mt5_news_pools() -> dict:
    """
    List available news source pools for the news system.

    Read-only discovery tool that returns the configured news provider names.
    Use this to check what sources are available before fetching news via mt5_news_fetch.

    Returns:
        dict: Contains pools (list of str, e.g. ["ig", "reuters", "bloomberg"]),
            default (str, "ig"), status_hint (str, noting availability depends on network).

    Example: mt5_news_pools()

    Note: These pools are configured but may be unavailable depending on network access
        and firewall settings. The status_hint field explains the caveat. Use
        mt5_news_fetch(source=X) to test a specific pool's connectivity.
    """
    try:
        return {
            "pools": ["ig", "reuters", "bloomberg"],
            "default": "ig",
            "status_hint": "Pools configured but may be unavailable depending on network access.",
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 16: mt5_economic_calendar
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_economic_calendar", annotations=_READ_ANNOTATIONS)
def mt5_economic_calendar() -> dict:
    """
    Fetch upcoming economic calendar events from available providers.

    Retrieves scheduled economic data releases, central bank meetings, and other
    market-moving events. Tries the IG API first, then falls back to the MT5 bridge
    calendar endpoint. Returns an empty list with hint if no source is available.

    Returns:
        dict: Contains events (list of event dicts with date, currency, impact,
            description, forecast/previous values), count (int), source (str,
            which provider served the data). On failure: status ("unavailable"),
            hint (str, troubleshooting suggestion).

    Example: mt5_economic_calendar()

    Note: Uses MT5's built-in calendar concept via IG API or bridge endpoint.
        Events include timestamp, currency, impact level, and description.
        Requires network access. If unavailable, assume potential high-impact
        events and use wider stops or skip trading during uncertain periods.
        The hint field provides configuration guidance when sources are down.
    """
    try:
        client = get_http_client()
        try:
            resp = client.get(
                "https://www.ig.com/ig-com-api/v1/economic-calendar/events",
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                events = data.get(
                    "events", data.get("data", data if isinstance(data, list) else [])
                )
                return {
                    "events": events,
                    "count": len(events),
                    "source": "ig",
                }
        except Exception:
            pass

        settings = get_settings_cached()
        try:
            resp = client.get(
                f"{settings.gateway_url}/bridge/calendar",
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "events": data.get("events", []),
                    "count": len(data.get("events", [])),
                    "source": "bridge",
                }
        except Exception:
            pass

        return {
            "events": [],
            "count": 0,
            "source": "unavailable",
            "status": "unavailable",
            "hint": (
                "Economic calendar requires network access to IG API or bridge calendar endpoint. "
                "Configure MT5_CALENDAR_API_URL for a custom source. "
                "Skip blackout-window checks until resolved — default to cautious (assume potential high-impact events)."
            ),
        }
    except Exception as e:
        return {"error": str(e)}
