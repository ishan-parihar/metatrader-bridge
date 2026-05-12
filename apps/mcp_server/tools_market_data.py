

import json
from typing import Any, Optional

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
    _parse_indicator_value_from_data,
    _first_bid_ask,
    INDICATOR_DEFAULTS,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


@mcp.tool(name="mt5_get_bars", annotations=_READ_ANNOTATIONS)
def mt5_get_bars(symbol: str, timeframe: str = "H1", count: int = 100) -> dict:
    """Fetch historical OHLCV bar (candlestick) data for a symbol.

    Use this tool when you need price history, chart data, or time-series data for technical analysis. Returns open, high, low, close, volume for each bar. Preferred over mt5_get_ticks for chart-level analysis.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD" or "XAUUSD". Will be normalized automatically.
        timeframe (str): Timeframe string — one of "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")
        count (int): Number of bars to retrieve, 1-10000. Higher values may be slower. (default: 100)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "data" (list of bar dicts with open/high/low/close/volume/time), "source" (str — "bridge" or "tcp_bridge"). On error, returns {"error": str}.

    Example: mt5_get_bars(symbol="EURUSD", timeframe="H1", count=50)

    Note: Bars are returned in chronological order (oldest first). The most recent (current forming) bar may be incomplete. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await(
            "get_bars", {"symbol": symbol_norm, "timeframe": timeframe, "count": count}
        )
        source = "bridge"
        if result is None:
            source = "tcp_bridge"
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "get_bars",
                    "symbol": symbol_norm,
                    "timeframe": timeframe,
                    "count": count,
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if "error" in data or "data" not in data:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "data": [],
                "source": source,
            }

        bars_data = data.get("data", [])
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "data": bars_data,
            "source": source,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_indicator", annotations=_READ_ANNOTATIONS)
def mt5_get_indicator(
    symbol: str,
    timeframe: str = "H1",
    indicator: str = "rsi",
    period: Optional[int] = None,
    fast: Optional[int] = None,
    slow: Optional[int] = None,
    signal: Optional[int] = None,
) -> dict:
    """Fetch a computed technical indicator value for a symbol.

    Use this tool when you need a single numeric value from a technical analysis indicator (RSI, MACD, EMA, ATR, Bollinger Bands, etc.). Returns one value per call — for multi-line indicators like Bollinger Bands, use mt5_chart_intelligence which returns structured data instead.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string — "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")
        indicator (str): Indicator name — "rsi", "macd", "ema", "sma", "atr", "bbands", "stoch", "adx", "cci", "williams", "momentum", "ichimoku". (default: "rsi")
        period (Optional[int]): Main period for indicators that use one (RSI=14, EMA=20, etc.). Overrides the default from INDICATOR_DEFAULTS. (default: None)
        fast (Optional[int]): Fast period for dual-line indicators like MACD (default: 12) and stochastic. (default: None)
        slow (Optional[int]): Slow period for dual-line indicators like MACD (default: 26). (default: None)
        signal (Optional[int]): Signal period for MACD (default: 9). (default: None)

    Returns:
        dict: With keys "symbol" (str), "indicator" (str), "timeframe" (str), "value" (float or dict — the computed indicator value), "status" (str — "success" or "unknown"). On error, returns {"error": str}.

    Example: mt5_get_indicator(symbol="EURUSD", timeframe="H1", indicator="rsi", period=14)

    Note: Returns a single scalar value for most indicators. For MACD, value is a dict with "macd", "signal", and "histogram" keys. Default parameters from INDICATOR_DEFAULTS are applied when explicit values are not provided. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        indicator_lower = indicator.lower()
        defaults = INDICATOR_DEFAULTS.get(indicator_lower, {})
        params: dict[str, Any] = {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": timeframe,
            "indicator": indicator_lower,
        }
        if period is not None:
            params["period"] = period
        elif "period" in defaults:
            params["period"] = defaults["period"]
        if fast is not None:
            params["fast"] = fast
        elif "fast" in defaults:
            params["fast"] = defaults["fast"]
        if slow is not None:
            params["slow"] = slow
        elif "slow" in defaults:
            params["slow"] = defaults["slow"]
        if signal is not None:
            params["signal"] = signal
        elif "signal" in defaults:
            params["signal"] = defaults["signal"]

        # Forward remaining defaults not covered by explicit parameters
        _HANDLED_PARAMS = {"period", "fast", "slow", "signal"}
        for key, val in defaults.items():
            if key not in _HANDLED_PARAMS and key not in params:
                params[key] = val

        result = _tcp_send_and_await(
            params["type"], {k: v for k, v in params.items() if k != "type"}
        )
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params=params,
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])

        value = _parse_indicator_value_from_data(data, indicator_lower)

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "indicator": indicator_lower,
            "timeframe": timeframe,
            "value": value,
            "status": result.get("status", "unknown") if result else "unknown",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_ticks", annotations=_READ_ANNOTATIONS)
def mt5_get_ticks(symbol: str, count: int = 100) -> dict:
    """Fetch real-time tick data (bid/ask changes) for a symbol.

    Use this tool when you need granular, transaction-level price data including bid, ask, volume, and timestamps for each individual tick. Best for microstructure analysis, spread monitoring, or real-time price tracking. For OHLCV bars, use mt5_get_bars instead.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        count (int): Number of ticks to retrieve, 1-100000. Higher values may be slower. (default: 100)

    Returns:
        dict: With keys "symbol" (str), "count" (int — number of ticks returned), "ticks" (list of tick dicts with time/bid/ask/volume/flags), "source" (str — "tcp" or "http"). On error, returns {"error": str}.

    Example: mt5_get_ticks(symbol="EURUSD", count=500)

    Note: Ticks are returned in chronological order. Each tick represents a single market price change event. Tick data volume can be large — use a reasonable count limit. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await(
            "get_ticks", {"symbol": symbol_norm, "count": count}
        )
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_ticks", "symbol": symbol_norm, "count": count},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "count": count,
            "ticks": data.get("ticks", []),
            "source": "tcp" if result else "http",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_order_book", annotations=_READ_ANNOTATIONS)
def mt5_get_order_book(symbol: str) -> dict:
    """Fetch the current order book (market depth) for a symbol.

    Use this tool when you need the current bid/ask prices and market depth (price levels with volumes on each side). Essential for liquidity assessment, spread analysis, and understanding market microstructure before entering a trade.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.

    Returns:
        dict: With keys "symbol" (str), "bid" (float — current best bid), "ask" (float — current best ask), "bids" (list of price-level dicts on the bid side), "asks" (list of price-level dicts on the ask side). On error, returns {"error": str}.

    Example: mt5_get_order_book(symbol="EURUSD")

    Note: Market depth availability depends on the broker. Some brokers only provide best bid/ask (top level). The "bids" and "asks" lists contain price-level data with volume at each level. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await("get_order_book", {"symbol": symbol_norm})
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_order_book", "symbol": symbol_norm},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_symbol_info", annotations=_READ_ANNOTATIONS)
def mt5_get_symbol_info(symbol: str) -> dict:
    """Fetch detailed specification and trading information for a symbol.

    Use this tool when you need symbol metadata such as contract size, tick size, tick value, minimum/maximum trade volume, margin requirements, spreads, swap rates, trading hours, and other instrument specifications. Essential for position sizing, risk calculation, and understanding trading conditions.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.

    Returns:
        dict: With keys "symbol" (str) and "info" (dict — full symbol specification including trade_allow, contract_size, tick_size, tick_value, point, digits, spread, spread_raw, swap_long, swap_short, margin_initial, margin_maintenance, volume_min, volume_max, volume_step, description, path, etc.). On error, returns {"error": str}.

    Example: mt5_get_symbol_info(symbol="XAUUSD")

    Note: The returned "info" dict contains both static specification fields and dynamic fields like current spread. Check "trade_allow" field to verify trading is permitted for this symbol. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await("get_symbol_info", {"symbol": symbol_norm})
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_symbol_info", "symbol": symbol_norm},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])

        return {"symbol": denormalize_symbol(symbol_norm), "info": data}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_deals_history", annotations=_READ_ANNOTATIONS)
def mt5_get_deals_history(
    limit: int = 100, symbol: Optional[str] = None, days: int = 30
) -> dict:
    """Fetch historical trade deals (executed orders) for the current account.

    Use this tool when you need the account's trade execution history — closed positions, deposits, withdrawals, and other account operations. Essential for post-trade analysis, P&L review, trade journaling, and performance auditing.

    Args:
        limit (int): Maximum number of deals to return, 1-10000. (default: 100)
        symbol (Optional[str]): Filter by MT5 symbol name, e.g. "EURUSD". If None, returns deals for all symbols. (default: None)
        days (int): Look-back period in days from the current date. (default: 30)

    Returns:
        dict: With keys "deals" (list of deal dicts with fields like ticket, order, symbol, type, entry, volume, price, profit, commission, swap, time, comment) and "count" (int — number of deals returned). On error, returns {"error": str}.

    Example: mt5_get_deals_history(limit=50, symbol="EURUSD", days=7)

    Note: Deals are returned in reverse chronological order (newest first). Supported deal types include buy, sell, deposit, withdrawal, and commission. The "entry" field indicates how the deal was executed (entry_in, entry_out, entry_reverse). Uses a longer timeout (15s) since deal history queries may take longer. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        params_dict: dict[str, Any] = {
            "type": "get_deals_history",
            "limit": limit,
            "days": days,
        }
        if symbol:
            params_dict["symbol"] = normalize_symbol(symbol)

        result = _tcp_send_and_await(
            params_dict["type"], {k: v for k, v in params_dict.items() if k != "type"}
        )
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params=params_dict,
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id, timeout_s=15.0)

        data = _parse_payload_dict(result) if result else {}
        deals = data.get("deals", [])
        for deal in deals:
            if "symbol" in deal:
                deal["symbol"] = denormalize_symbol(deal["symbol"])

        return {"deals": deals, "count": len(deals)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_account_summary", annotations=_READ_ANNOTATIONS)
def mt5_get_account_summary() -> dict:
    """Fetch the current trading account summary and status.

    Use this tool when you need account-level information such as balance, equity, margin, free margin, margin level, leverage, trading permissions, account currency, broker name, and server connection status. Call this before trading to verify account health and risk parameters.

    Args:
        No parameters required.

    Returns:
        dict: With keys "account" (dict — account details including balance, equity, margin, free_margin, margin_level, leverage, currency, broker, server, name, login, trade_allow, trade_expert) and "status" (str — "success" or "unknown"). On error, returns {"error": str}.

    Example: mt5_get_account_summary()

    Note: This is a lightweight, no-argument call that returns the full account state. Check "trade_allow" in the account dict to confirm trading is permitted. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        result = _tcp_send_and_await("get_account", {})
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_account"},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        return {
            "account": data,
            "status": result.get("status", "unknown") if result else "unknown",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_positions", annotations=_READ_ANNOTATIONS)
def mt5_get_positions() -> dict:
    """Fetch all currently open positions for the trading account.

    Use this tool when you need the current portfolio of open trades including ticket number, symbol, type (buy/sell), volume, open price, current price, stop loss, take profit, profit/loss, swap, commission, and open time. Essential for risk assessment, portfolio monitoring, and deciding which positions to modify or close.

    Args:
        No parameters required.

    Returns:
        dict: With keys "positions" (list of position dicts with fields like ticket, symbol, type, volume, price_open, sl, tp, price_current, profit, swap, commission, time, comment, magic) and "count" (int — number of open positions). On error, returns {"error": str}.

    Example: mt5_get_positions()

    Note: An empty positions list (count=0) means no open trades. Symbol names in position data are denormalized automatically. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        result = _tcp_send_and_await("get_positions", {})
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_positions"},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        positions = data.get("positions", [])
        for pos in positions:
            if "symbol" in pos:
                pos["symbol"] = denormalize_symbol(pos["symbol"])

        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_orders", annotations=_READ_ANNOTATIONS)
def mt5_get_orders() -> dict:
    """Fetch all currently pending orders for the trading account.

    Use this tool when you need pending orders (limit/stop orders that haven't been executed yet) including ticket, symbol, type (buy_limit, sell_limit, buy_stop, sell_stop), volume, price, stop loss, take profit, expiration, and open time. Essential for monitoring unfilled orders and deciding whether to modify or cancel them.

    Args:
        No parameters required.

    Returns:
        dict: With keys "orders" (list of pending order dicts with fields like ticket, symbol, type, volume, price, sl, tp, time_setup, time_expiration, comment, magic) and "count" (int — number of pending orders). On error, returns {"error": str}.

    Example: mt5_get_orders()

    Note: An empty orders list (count=0) means no pending orders. This only returns pending (unfilled) orders — for executed orders, use mt5_get_deals_history. Symbol names are denormalized automatically. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        result = _tcp_send_and_await("get_orders", {})
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_orders"},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id)

        data = _parse_payload_dict(result) if result else {}
        orders = data.get("orders", [])
        for order in orders:
            if "symbol" in order:
                order["symbol"] = denormalize_symbol(order["symbol"])

        return {"orders": orders, "count": len(orders)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_get_chart_screenshot", annotations=_READ_ANNOTATIONS)
def mt5_get_chart_screenshot(
    symbol: str, timeframe: str = "H1", width: int = 1920, height: int = 1080
) -> dict:
    """Capture a screenshot of the MT5 chart for a symbol as a base64-encoded image.

    Use this tool when you need a visual representation of a chart — useful for visual analysis, sharing chart images, or including chart context in AI-driven analysis. The image is returned as a base64-encoded string that can be rendered or saved.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Chart timeframe — "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")
        width (int): Screenshot width in pixels. Range typically 800-3840. (default: 1920)
        height (int): Screenshot height in pixels. Range typically 600-2160. (default: 1080)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "width" (int), "height" (int), "image_base64" (str — base64-encoded PNG image data). On error, returns {"error": str}.

    Example: mt5_get_chart_screenshot(symbol="EURUSD", timeframe="H1", width=1920, height=1080)

    Note: Requires the MT5 terminal chart to be visible (Xvfb display). Large dimensions produce larger base64 strings. Uses a longer timeout (15s) since screenshot capture may take longer than data queries. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        result = _tcp_send_and_await(
            "get_chart_screenshot",
            {
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "width": width,
                "height": height,
            },
        )
        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={
                    "type": "get_chart_screenshot",
                    "symbol": symbol_norm,
                    "timeframe": timeframe,
                    "width": width,
                    "height": height,
                },
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id, timeout_s=15.0)

        data = _parse_payload_dict(result) if result else {}
        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "width": width,
            "height": height,
            "image_base64": data.get("image_base64") or data.get("screenshot"),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_market_snapshot", annotations=_READ_ANNOTATIONS)
def mt5_market_snapshot(
    symbol: str,
    timeframe: str = "H1",
    bar_count: int = 100,
    include_coaching: bool = True,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
) -> dict:
    """Fetch a comprehensive multi-indicator market snapshot for a symbol in a single batched call.

    Use this tool when you need a complete market picture — OHLCV bars + ATR + RSI + EMA(20) + EMA(50) + MACD + order book + symbol info + open positions — plus optional AI coaching analysis. This is the primary "analyze this symbol" tool for trading agents. Batching all queries reduces latency vs calling individual tools sequentially.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Chart timeframe for bar data and indicators. (default: "H1")
        bar_count (int): Number of historical bars to include. (default: 100)
        include_coaching (bool): Whether to include AI-generated trading coaching/analysis text in the snapshot. (default: True)
        session_id (Optional[str]): Session identifier for coaching context continuity. (default: None)
        strategy_id (Optional[str]): Strategy identifier for coaching context continuity. (default: None)

    Returns:
        dict: Comprehensive snapshot with keys including "symbol", "timeframe", "bars" (list), "indicators" (dict with atr/rsi/ema20/ema50/macd), "order_book" (dict), "symbol_info" (dict), "positions" (list), "coaching" (str — if include_coaching=True), "timestamp" (str). On error, returns {"symbol": str, "error": str}.

    Example: mt5_market_snapshot(symbol="EURUSD", timeframe="H1", bar_count=100, include_coaching=True)

    Note: Uses batch enqueue with a 30s timeout. Fetches 9 data sources in parallel. If coaching is enabled, the SymbolSnapshotService and TradingCoach services generate natural-language analysis. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        commands = [
            {
                "type": "get_bars",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "count": bar_count,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "atr",
                "period": 14,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "rsi",
                "period": 14,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "ema",
                "period": 20,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "ema",
                "period": 50,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "macd",
                "fast": 12,
                "slow": 26,
                "signal": 9,
            },
            {"type": "get_order_book", "symbol": symbol_norm},
            {"type": "get_symbol_info", "symbol": symbol_norm},
            {"type": "get_positions", "symbol": symbol_norm},
        ]

        results = _batch_enqueue_and_await(commands, timeout_s=30.0)

        bars_result = results[0] if len(results) > 0 else None
        atr_result = results[1] if len(results) > 1 else None
        rsi_result = results[2] if len(results) > 2 else None
        ema_fast_result = results[3] if len(results) > 3 else None
        ema_slow_result = results[4] if len(results) > 4 else None
        macd_result = results[5] if len(results) > 5 else None
        book_result = results[6] if len(results) > 6 else None
        symbol_info_result = results[7] if len(results) > 7 else None
        positions_result = results[8] if len(results) > 8 else None

        bars_data = (
            _parse_payload_dict(bars_result).get("data", []) if bars_result else []
        )
        atr_value = _parse_indicator_value(atr_result)
        rsi = _parse_indicator_value(rsi_result)
        ema_fast = _parse_indicator_value(ema_fast_result)
        ema_slow = _parse_indicator_value(ema_slow_result)

        macd_payload = _parse_payload_dict(macd_result) if macd_result else {}
        macd_data = macd_payload.get("value") or macd_payload

        book_data = _parse_payload_dict(book_result) if book_result else {}
        symbol_info_data = (
            _parse_payload_dict(symbol_info_result) if symbol_info_result else {}
        )
        positions_data = (
            _parse_payload_dict(positions_result).get("positions", [])
            if positions_result
            else []
        )

        bid, ask = _first_bid_ask(book_data)

        from mt5_mcp.services.snapshot_service import SymbolSnapshotService
        from mt5_mcp.services.trading_coach import TradingCoach

        snapshot_svc = SymbolSnapshotService(
            coach=TradingCoach(), reconciliation_service=None
        )
        return snapshot_svc.build(
            symbol=symbol,
            timeframe=timeframe,
            bars_data=bars_data,
            atr_value=atr_value,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            macd=macd_data,
            order_book_data=book_data,
            bid=bid,
            ask=ask,
            symbol_info_data=symbol_info_data,
            positions=positions_data,
            include_coaching=include_coaching,
            session_id=session_id,
            strategy_id=strategy_id,
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"Batch fetch failed: {e}"}


@mcp.tool(name="mt5_chart_intelligence", annotations=_READ_ANNOTATIONS)
def mt5_chart_intelligence(
    symbol: str,
    timeframe: str = "H1",
    bar_count: int = 100,
    include_screenshot: bool = False,
    width: int = 1920,
    height: int = 1080,
) -> dict:
    """Generate a comprehensive AI-driven chart analysis with technical indicators and optional screenshot.

    Use this tool for in-depth technical analysis of a symbol — fetches bars + ATR + RSI + EMA(20) + EMA(50) + MACD + Bollinger Bands (+ optional chart screenshot) and passes them through the ChartIntelligenceService for AI-powered market commentary. Richer than mt5_market_snapshot — includes Bollinger Bands and optional screenshot, but no order book, positions, or coaching.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Chart timeframe for bar data and indicators. (default: "H1")
        bar_count (int): Number of historical bars to include. (default: 100)
        include_screenshot (bool): Whether to also capture a chart screenshot (base64 PNG). Increases response time. (default: False)
        width (int): Screenshot width in pixels (only used if include_screenshot=True). (default: 1920)
        height (int): Screenshot height in pixels (only used if include_screenshot=True). (default: 1080)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "bars_received" (int), "indicators" (dict with atr/rsi/ema20/ema50/macd/bbands), "intelligence" (dict — AI-generated analysis text and insights), and optionally "screenshot" (dict with base64/width/height). On empty bar data, returns {"error": str, "symbol": str, "timeframe": str, "bars_received": 0}. On exception, returns {"symbol": str, "error": str}.

    Example: mt5_chart_intelligence(symbol="EURUSD", timeframe="H1", bar_count=100, include_screenshot=True)

    Note: Uses batch enqueue with a 30s timeout. Fetches 7 data sources (8 if screenshot requested). Returns early with error if no bar data is available for the symbol. The ChartIntelligenceService produces structured analysis including trend direction, volatility assessment, support/resistance levels, and trading recommendations. Falls back to HTTP gateway if TCP bridge is unavailable.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        commands = [
            {
                "type": "get_bars",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "count": bar_count,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "atr",
                "period": 14,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "rsi",
                "period": 14,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "ema",
                "period": 20,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "ema",
                "period": 50,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "macd",
                "fast": 12,
                "slow": 26,
                "signal": 9,
            },
            {
                "type": "get_indicator",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "indicator": "bbands",
                "period": 20,
            },
        ]
        if include_screenshot:
            commands.append(
                {
                    "type": "get_chart_screenshot",
                    "symbol": symbol_norm,
                    "timeframe": timeframe,
                    "width": width,
                    "height": height,
                }
            )

        results = _batch_enqueue_and_await(commands, timeout_s=30.0)

        # Pad results to expected length if gateway returned fewer results
        expected = len(commands)
        if len(results) < expected:
            results.extend([None] * (expected - len(results)))

        bars_result = results[0]
        atr_result = results[1]
        rsi_result = results[2]
        ema_fast_result = results[3]
        ema_slow_result = results[4]
        macd_result = results[5]
        bbands_result = results[6]
        screenshot_result = (
            results[7] if len(results) > 7 and include_screenshot else None
        )

        bars_data = (
            _parse_payload_dict(bars_result).get("data", []) if bars_result else []
        )
        if not bars_data:
            return {
                "error": f"No bar data available for {symbol} on {timeframe}. The EA may not have data for this symbol.",
                "symbol": symbol,
                "timeframe": timeframe,
                "bars_received": 0,
            }
        atr_value = _parse_indicator_value(atr_result) if atr_result else None
        rsi = _parse_indicator_value(rsi_result) if rsi_result else None
        ema_fast = _parse_indicator_value(ema_fast_result) if ema_fast_result else None
        ema_slow = _parse_indicator_value(ema_slow_result) if ema_slow_result else None

        macd_payload = _parse_payload_dict(macd_result) if macd_result else {}
        macd_data = macd_payload.get("value") or macd_payload

        bbands_payload = _parse_payload_dict(bbands_result) if bbands_result else {}
        bbands_data = bbands_payload.get("value") or bbands_payload

        screenshot_base64 = None
        screenshot_data = None
        if include_screenshot and screenshot_result:
            screenshot_payload = _parse_payload_dict(screenshot_result)
            screenshot_base64 = screenshot_payload.get(
                "image_base64"
            ) or screenshot_payload.get("screenshot")
            if screenshot_base64:
                screenshot_data = {
                    "base64": screenshot_base64,
                    "width": width,
                    "height": height,
                }

        from mt5_mcp.services.chart_intelligence import ChartIntelligenceService

        svc = ChartIntelligenceService()
        return svc.get_intelligence(
            symbol=symbol,
            timeframe=timeframe,
            bars_data=bars_data,
            atr_value=atr_value,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            macd=macd_data,
            bbands=bbands_data,
            screenshot_data=screenshot_data,
            include_screenshot_base64=include_screenshot
            and screenshot_base64 is not None,
            bar_count=bar_count,
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"Batch fetch failed: {e}"}
