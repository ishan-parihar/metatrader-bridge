import time
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.types import ToolAnnotations

from mt5_mcp.observability.logging import logger
from . import mcp
from .shared import (
    get_gateway,
    get_http_client,
    get_settings_cached,
    _tcp_send_and_await,
    _parse_payload,
    _parse_payload_dict,
)

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


# ---------------------------------------------------------------------------
# Tool 1: mt5_terminal_status
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_terminal_status", annotations=_READ_ANNOTATIONS)
def mt5_terminal_status() -> dict:
    """Check whether the MT5 terminal is connected and responsive.

    Use this before any trading operation to confirm the bridge is alive. If
    the terminal is disconnected, all other tools will return empty or error
    responses — this is the first tool to call in any trading cycle.

    Args:
        None

    Returns:
        dict: Terminal connection state with keys:
            - connected (bool): Whether MT5 terminal is reachable
            - server (str, optional): Connected server name
            - company (str, optional): Broker company name
            - error (str, present on failure): Error description

    Example: mt5_terminal_status()

    Note: This is a LIVE check — always reflects current state. Does not
    require an active account session to succeed.
    """
    try:
        result = get_gateway().terminal_status()
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)
    except Exception as e:
        return {"error": "terminal_status_unavailable", "connected": False}


# ---------------------------------------------------------------------------
# Tool 2: mt5_account_summary
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_account_summary", annotations=_READ_ANNOTATIONS)
def mt5_account_summary() -> dict:
    """Retrieve the current MT5 account summary including balance, equity, margin, and leverage.

    Use this to assess account health before placing trades — check free margin,
    margin level, and whether the account is allowed to trade. Call periodically
    during a trading session to monitor equity drawdown.

    Args:
        None

    Returns:
        dict: Contains two top-level keys:
            - account (dict): Account info with keys:
                - account_id (int): MT5 account number
                - balance (float): Current account balance
                - equity (float): Current equity (balance +/- floating P&L)
                - margin (float): Used margin
                - free_margin (float): Available margin
                - margin_level (float): Margin level percentage
                - leverage (int): Account leverage
                - currency (str): Account base currency
                - trade_allowed (bool): Whether trading is enabled
                - name (str, optional): Account holder name
                - server (str, optional): Connected server name
            - snapshot_metadata (dict): Timestamp and freshness info
        On complete failure: {"error": "account_summary_unavailable"}

    Example: mt5_account_summary()

    Note: LIVE data fetched directly from MT5 via gateway adapter with TCP
    bridge fallback. Check snapshot_metadata.data_freshness for source.
    """
    # Path 1: Gateway adapter
    try:
        summary = get_gateway().account_summary()
        if summary.account_id is not None:
            return {
                "account": summary.model_dump()
                if hasattr(summary, "model_dump")
                else dict(summary),
                "snapshot_metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "next_recommended_check_seconds": 300,
                    "data_freshness": "live",
                },
            }
    except Exception as e:
        logger.warning(f"mt5_account_summary: gateway adapter failed — {e}")

    # Path 2: TCP bridge
    try:
        result = _tcp_send_and_await("get_account", {}, timeout_s=10.0)
        data = _parse_payload_dict(result)
        if data:
            return {
                "account": data,
                "snapshot_metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "next_recommended_check_seconds": 300,
                    "data_freshness": "live",
                },
            }
        else:
            logger.warning("mt5_account_summary: TCP bridge returned empty payload")
    except Exception as e:
        logger.warning(f"mt5_account_summary: TCP bridge failed — {e}")

    return {"error": "account_summary_unavailable"}


# ---------------------------------------------------------------------------
# Tool 3: mt5_symbol_info
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_symbol_info", annotations=_READ_ANNOTATIONS)
def mt5_symbol_info(symbol: str) -> dict:
    """Get detailed specification and market data for a single trading symbol.

    Use this to check symbol properties before trading: point size, tick value,
    minimum volume, lot step, spread, swap rates, session times, and price
    filters. Essential for position sizing calculations and trade validation.

    Args:
        symbol (str): Trading symbol (e.g. "EURUSD", "GBPJPY", "XAUUSD").
            Case-insensitive; will be normalized automatically.

    Returns:
        dict: Symbol specification with keys including:
            - symbol (str): Normalized symbol name
            - point (float): Point value (smallest price change)
            - digits (int): Number of decimal places
            - tick_value (float): Value of one tick in deposit currency
            - tick_size (float): Minimum price step
            - trade_mode (int): 0=disabled, 1=long only, 2=short only, 3=both
            - spread (int): Current spread in points
            - spread_float (bool): Whether spread is floating
            - swap_long (float): Long position swap (points)
            - swap_short (float): Short position swap (points)
            - swap_type (int): Swap calculation method
            - trade_calc_mode (int): Profit/margin calculation mode
            - margin_initial (float): Initial margin required
            - margin_maintenance (float): Maintenance margin
            - volume_min (float): Minimum trade volume (lots)
            - volume_max (float): Maximum trade volume (lots)
            - volume_step (float): Lot size step
            - session_* (dict): Trading session rules
            - starting (str): Symbol start date
            - expiration (str, optional): Symbol expiration/expiry
            - description (str, optional): Human-readable description
            - error (str, present on failure): Error description

    Example: mt5_symbol_info(symbol="EURUSD")

    Note: LIVE data from MT5 gateway adapter with TCP bridge fallback. Returns
    {"error": "symbol_info_unavailable", "symbol": symbol} if symbol is invalid
    or data cannot be retrieved.
    """
    try:
        from mt5_mcp.adapters.common.symbol_utils import (
            denormalize_symbol,
            normalize_symbol,
        )

        result = get_gateway().adapter.get_symbol_info(symbol)
        if result is not None and getattr(result, "symbol", None):
            result.symbol = denormalize_symbol(result.symbol)
            return (
                result.model_dump() if hasattr(result, "model_dump") else dict(result)
            )
    except Exception:
        pass

    try:
        tcp_result = _tcp_send_and_await(
            "get_symbol_info", {"symbol": normalize_symbol(symbol)}, timeout_s=10.0
        )
        data = _parse_payload_dict(tcp_result)
        if data:
            return data
    except Exception:
        pass

    return {"error": "symbol_info_unavailable", "symbol": symbol}


# ---------------------------------------------------------------------------
# Tool 4: mt5_deals_history
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_deals_history", annotations=_READ_ANNOTATIONS)
def mt5_deals_history(
    limit: int = 100, symbol: Optional[str] = None, days: int = 30
) -> dict:
    """Retrieve historical closed deals from MT5 for performance analysis.

    Use this to review filled orders, calculate win/loss ratios, and analyze
    trading history over a specified lookback period. Combines with
    mt5_performance_summary for deeper analytics.

    Args:
        limit (int): Maximum number of deals to return (default: 100,
            range: 1-10000). Higher values increase response time.
        symbol (Optional[str]): Filter by trading symbol (e.g. "EURUSD").
            None returns deals for all symbols. (default: None)
        days (int): Lookback period in days (default: 30, range: 1-365).

    Returns:
        dict: Contains a single key:
            - deals (list): Array of deal dicts, each with:
                - deal_id (int): MT5 deal identifier
                - order_id (int): Originating order ID
                - symbol (str): Trading symbol
                - side (str): "buy" or "sell"
                - volume (float): Deal volume in lots
                - price (float): Execution price
                - profit (float): Deal profit/loss in account currency
                - commission (float): Commission charged
                - swap (float): Swap/rollover charged
                - time (str): Deal execution time (ISO format)
                - comment (str, optional): Deal comment
                - entry (str): "entry", "exit", or "reverse"

    Example: mt5_deals_history(limit=50, symbol="EURUSD", days=7)

    Note: LIVE data fetched from MT5 gateway adapter with TCP bridge fallback.
    Returns {"deals": []} if no deals found or connection fails. Deals include
    both entry and exit records for closed positions.
    """
    try:
        deals = get_gateway().adapter.get_deals_history(
            limit=limit, symbol=symbol, days=days
        )
        if deals:
            return {
                "deals": [
                    d.model_dump() if hasattr(d, "model_dump") else dict(d)
                    for d in deals
                ]
            }
    except Exception as e:
        logger.warning(f"mt5_deals_history: gateway adapter failed — {e}")

    try:
        tcp_result = _tcp_send_and_await(
            "get_deals_history",
            {"limit": limit, "symbol": symbol or "", "days": days},
            timeout_s=15.0,
        )
        data = _parse_payload_dict(tcp_result)
        if data:
            return {"deals": data.get("deals", [])}
        else:
            logger.warning("mt5_deals_history: TCP bridge returned empty payload")
    except Exception as e:
        logger.warning(f"mt5_deals_history: TCP bridge failed — {e}")

    return {"deals": []}


# ---------------------------------------------------------------------------
# Tool 5: mt5_performance_summary
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_performance_summary", annotations=_READ_ANNOTATIONS)
def mt5_performance_summary(
    limit: int = 100, symbol: Optional[str] = None, days: int = 30
) -> dict:
    """Compute aggregated performance metrics from historical deal data.

    Use this after mt5_deals_history to get summarized stats: win rate, profit
    factor, average win/loss, Sharpe-like ratio, and symbol-level breakdowns.
    Helps the agent evaluate strategy effectiveness without manual calculations.

    Args:
        limit (int): Maximum number of deals to analyze (default: 100,
            range: 1-10000). Affects statistical significance.
        symbol (Optional[str]): Filter to a single symbol (e.g. "EURUSD").
            None includes all symbols. (default: None)
        days (int): Lookback period in days (default: 30, range: 1-365).

    Returns:
        dict: Performance summary with keys:
            - total_deals (int): Number of deals analyzed
            - total_profit (float): Net profit/loss
            - total_commission (float): Total commission paid
            - total_swap (float): Total swap charged
            - win_count (int): Number of winning deals
            - loss_count (int): Number of losing deals
            - win_rate (float): Win rate as decimal (0.0-1.0)
            - profit_factor (float): Gross profit / gross loss
            - avg_win (float): Average winning deal profit
            - avg_loss (float): Average losing deal profit (negative)
            - largest_win (float): Largest single deal profit
            - largest_loss (float): Largest single deal loss
            - avg_bars_held (float): Average hold time in bars
            - sharpe_ratio (float, optional): Risk-adjusted return metric
            - symbol_breakdown (dict, optional): Per-symbol performance
            - error (str, present on failure): Error description

    Example: mt5_performance_summary(days=90, symbol="EURUSD")

    Note: LIVE computation from MT5 deal history via gateway adapter with TCP
    bridge fallback. Performance is based on closed deals only — open positions
    are not included. Empty history returns default zero-value metrics.
    """
    try:
        from mt5_mcp.services.agent_capabilities import summarize_deals

        deals_list = []
        try:
            deals = get_gateway().adapter.get_deals_history(
                limit=limit, symbol=symbol, days=days
            )
            if deals:
                deals_list = [
                    d.model_dump() if hasattr(d, "model_dump") else dict(d)
                    for d in deals
                ]
        except Exception:
            pass

        if not deals_list:
            try:
                tcp_result = _tcp_send_and_await(
                    "get_deals_history",
                    {"limit": limit, "symbol": symbol or "", "days": days},
                    timeout_s=15.0,
                )
                data = _parse_payload_dict(tcp_result)
                if data:
                    deals_list = data.get("deals", [])
            except Exception:
                pass

        return summarize_deals(deals_list)
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: mt5_positions_open
# ---------------------------------------------------------------------------
def _compute_position_health(position: dict, symbol_info: dict) -> dict:
    from mt5_mcp.observability.logging import logger as _ch_logger

    health: dict[str, object] = {
        "distance_to_sl_pips": None,
        "distance_to_tp_pips": None,
        "pnl_percent_of_risk": None,
        "time_in_trade_minutes": None,
        "time_in_trade_bars_h1": None,
        "is_winning": None,
        "is_at_breakeven": None,
        "trail_eligible": None,
        "spread_cost_pips": None,
        "profit_multiple_of_spread": None,
    }

    try:
        mark_price = position.get("mark_price")
        entry_price = position.get("entry_price")
        sl = position.get("sl")
        tp = position.get("tp")
        volume = position.get("volume", 0.0)
        side = position.get("side", "buy")

        profit_raw = position.get("profit")
        if profit_raw is None:
            profit_raw = position.get("unrealized_pnl", 0.0)
        profit = float(profit_raw) if profit_raw is not None else 0.0

        mark_price = float(mark_price) if mark_price else None
        entry_price = float(entry_price) if entry_price else None
        volume = float(volume) if volume else 0.0

        point = symbol_info.get("point")
        point = float(point) if point else None
        tick_value = symbol_info.get("tick_value")
        tick_value = float(tick_value) if tick_value else None

        spread_raw = position.get("spread")
        if spread_raw is None:
            spread_raw = symbol_info.get("spread_points", 0)
        spread = int(spread_raw) if spread_raw is not None else 0

        if mark_price and point and point > 0:
            if sl and float(sl) > 0:
                sl_f = float(sl)
                if side == "buy":
                    health["distance_to_sl_pips"] = (mark_price - sl_f) / point
                else:
                    health["distance_to_sl_pips"] = (sl_f - mark_price) / point

            if tp and float(tp) > 0:
                tp_f = float(tp)
                if side == "buy":
                    health["distance_to_tp_pips"] = (tp_f - mark_price) / point
                else:
                    health["distance_to_tp_pips"] = (mark_price - tp_f) / point

        if entry_price and sl and float(sl) > 0 and tick_value and volume > 0:
            sl_f = float(sl)
            risk_per_lot = abs(entry_price - sl_f) * volume * tick_value
            if risk_per_lot > 0:
                health["pnl_percent_of_risk"] = (profit / risk_per_lot) * 100

        time_raw = position.get("time")
        if time_raw is not None:
            try:
                time_in_trade_minutes = int((time.time() - float(time_raw)) / 60)
                health["time_in_trade_minutes"] = time_in_trade_minutes
                health["time_in_trade_bars_h1"] = round(time_in_trade_minutes / 60.0, 1)
            except (ValueError, TypeError, OverflowError):
                pass
        else:
            opened_at = position.get("opened_at")
            if opened_at:
                try:
                    from datetime import datetime, timezone

                    if isinstance(opened_at, (int, float)):
                        opened_dt = datetime.fromtimestamp(
                            float(opened_at), tz=timezone.utc
                        )
                    else:
                        opened_dt = datetime.fromisoformat(
                            str(opened_at).replace("Z", "+00:00")
                        )
                    now = datetime.now(timezone.utc)
                    delta = now - opened_dt
                    time_in_trade_minutes = int(delta.total_seconds() / 60)
                    health["time_in_trade_minutes"] = time_in_trade_minutes
                    health["time_in_trade_bars_h1"] = round(
                        time_in_trade_minutes / 60.0, 1
                    )
                except (ValueError, TypeError, OverflowError):
                    pass

        health["is_winning"] = profit > 0

        if spread > 0 and tick_value and volume > 0:
            spread_cost_dollar = spread * volume * tick_value
            health["is_at_breakeven"] = abs(profit) < (spread_cost_dollar * 0.5)
            health["trail_eligible"] = health["is_winning"] and profit > (
                spread_cost_dollar * 2
            )
        else:
            health["is_at_breakeven"] = None
            health["trail_eligible"] = None

        health["spread_cost_pips"] = spread

        if spread > 0 and point and point > 0 and tick_value and volume > 0:
            denom = volume * tick_value
            profit_in_points = profit / denom if denom > 0 else 0
            health["profit_multiple_of_spread"] = profit_in_points / spread

    except Exception as e:
        _ch_logger.error(
            f"Health computation failed for position "
            f"{position.get('position_id', 'unknown')}: {e}"
        )

    trail_eligible = health.get("trail_eligible")
    is_at_breakeven = health.get("is_at_breakeven")
    is_winning = health.get("is_winning")
    distance_to_sl_pips = health.get("distance_to_sl_pips")
    pnl_percent_of_risk = health.get("pnl_percent_of_risk")
    time_in_trade_bars_h1 = health.get("time_in_trade_bars_h1")

    action_required = "none"
    action_reason = "Position healthy — no action needed"

    if (
        time_in_trade_bars_h1 is not None
        and time_in_trade_bars_h1 >= 16
        and pnl_percent_of_risk is not None
        and pnl_percent_of_risk < 50
    ):
        action_required = "stale_position"
        action_reason = (
            "Position open 16+ bars with < 0.5x ATR profit — close and redeploy"
        )
    elif (
        time_in_trade_bars_h1 is not None
        and time_in_trade_bars_h1 >= 12
        and is_winning is True
        and pnl_percent_of_risk is not None
        and pnl_percent_of_risk < 50
    ):
        action_required = "time_exit_approaching"
        action_reason = "Dead money — consider closing if no progress in 4 more bars"
    elif distance_to_sl_pips is not None and distance_to_sl_pips < 10:
        action_required = "invalidation_check"
        action_reason = "Price approaching stop loss — verify thesis still valid"
    elif trail_eligible is True and is_at_breakeven is False:
        action_required = "trail_to_breakeven"
        action_reason = "Profit > 2x spread — move SL to entry price"
    elif (
        trail_eligible is True
        and is_at_breakeven is True
        and distance_to_sl_pips is not None
        and distance_to_sl_pips > 0
    ):
        action_required = "trail_stop_closer"
        action_reason = "Position in profit — trail SL 0.5x ATR in profit direction"

    health["action_required"] = action_required
    health["action_reason"] = action_reason

    return health


def _enrich_positions_with_health(positions: list[dict]) -> list[dict]:
    if not positions:
        return positions

    symbols = list({p.get("symbol", "") for p in positions if p.get("symbol")})
    symbol_infos: dict[str, dict] = {}

    if len(symbols) == 1:
        sym = symbols[0]
        try:
            tcp_result = _tcp_send_and_await(
                "get_symbol_info", {"symbol": sym}, timeout_s=5.0
            )
            symbol_infos[sym] = _parse_payload_dict(tcp_result)
        except Exception as e:
            logger.warning(f"_enrich_positions: symbol_info({sym}) failed — {e}")
            symbol_infos[sym] = {}
    else:
        commands = [{"type": "get_symbol_info", "symbol": sym} for sym in set(symbols)]
        try:
            results = _batch_enqueue_and_await(commands, timeout_s=15.0)
            for sym, result in zip(set(symbols), results):
                symbol_infos[sym] = _parse_payload_dict(result) if result else {}
        except Exception as e:
            logger.warning(f"_enrich_positions: batch symbol_info failed — {e}")
            symbol_infos = {sym: {} for sym in set(symbols)}

    for position in positions:
        sym = position.get("symbol", "")
        sym_info = symbol_infos.get(sym, {})
        position["health"] = _compute_position_health(position, sym_info)

    return positions

@mcp.tool(name="mt5_positions_open", annotations=_READ_ANNOTATIONS)
def mt5_positions_open() -> dict:
    """List all currently open positions with computed health metrics.

    Use this to monitor active trades, check unrealized P&L, assess position
    health (distance to SL/TP, time in trade, trailing eligibility), and
    decide whether to modify, close, or hold each position. Each position
    includes a health assessment with actionable recommendations.

    Args:
        None

    Returns:
        dict: Contains three top-level keys:
            - positions (list): Array of open position dicts, each with:
                - position_id (int): MT5 position ticket
                - symbol (str): Trading symbol
                - side (str): "buy" or "sell"
                - volume (float): Volume in lots
                - entry_price (float): Position entry price
                - mark_price (float): Current market price
                - profit (float): Unrealized P&L in account currency
                - sl (float, optional): Stop loss price
                - tp (float, optional): Take profit price
                - time (int): Open time as Unix timestamp
                - comment (str, optional): Position comment
                - health (dict): Computed metrics:
                    - distance_to_sl_pips (float, optional): Pips to stop loss
                    - distance_to_tp_pips (float, optional): Pips to take profit
                    - pnl_percent_of_risk (float, optional): P&L as % of initial risk
                    - time_in_trade_minutes (int, optional): Age in minutes
                    - is_winning (bool, optional): Whether position is in profit
                    - is_at_breakeven (bool, optional): P&L within 0.5x spread
                    - trail_eligible (bool, optional): Profit > 2x spread
                    - action_required (str): Suggested action: "none",
                      "stale_position", "time_exit_approaching",
                      "invalidation_check", "trail_to_breakeven",
                      "trail_stop_closer"
                    - action_reason (str): Human-readable explanation
            - sync_status (dict): Caching and retry metadata
            - snapshot_metadata (dict): Timestamp and data freshness info

    Example: mt5_positions_open()

    Note: LIVE data from MT5 gateway adapter with up to 3 retry attempts and
    TCP bridge fallback. Health computation requires symbol info (may add
    latency for multi-symbol portfolios). Empty array means no open positions.
    """
    # Retry loop: try gateway adapter up to 3 times with 0.5s delay
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            positions = get_gateway().adapter.get_positions()
            if positions:
                position_dicts = [
                    p.model_dump() if hasattr(p, "model_dump") else dict(p)
                    for p in positions
                ]
                _enrich_positions_with_health(position_dicts)
                return {
                    "positions": position_dicts,
                    "sync_status": {
                        "positions_count": len(position_dicts),
                        "last_sync_age_ms": 0,
                        "retry_count": attempt,
                        "stale_warning": False,
                    },
                    "snapshot_metadata": {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "next_recommended_check_seconds": 600,
                        "data_freshness": "live",
                    },
                }
        except Exception as e:
            last_error = e
            logger.warning(
                f"mt5_positions_open: gateway adapter attempt {attempt + 1}/3 failed — {e}"
            )
            time.sleep(0.5)

    # All attempts exhausted — log the last error
    logger.warning(
        f"mt5_positions_open: gateway adapter failed after 3 attempts — {last_error}"
    )

    # Fallback: TCP bridge
    try:
        tcp_result = _tcp_send_and_await("get_positions", {}, timeout_s=10.0)
        data = _parse_payload_dict(tcp_result)
        pos_list = data.get("positions", []) if data else []
        if pos_list:
            _enrich_positions_with_health(pos_list)
            return {
                "positions": pos_list,
                "sync_status": {
                    "positions_count": len(pos_list),
                    "last_sync_age_ms": 0,
                    "retry_count": 0,
                    "stale_warning": False,
                },
                "snapshot_metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "next_recommended_check_seconds": 600,
                    "data_freshness": "live",
                },
            }
    except Exception as e:
        logger.warning(f"mt5_positions_open: TCP bridge failed — {e}")

    return {
        "positions": [],
        "sync_status": {
            "positions_count": 0,
            "last_sync_age_ms": 0,
            "retry_count": 0,
            "stale_warning": False,
        },
        "snapshot_metadata": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "next_recommended_check_seconds": 600,
            "data_freshness": "live",
        },
    }


# ---------------------------------------------------------------------------
# Tool 7: mt5_orders_pending
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_orders_pending", annotations=_READ_ANNOTATIONS)
def mt5_orders_pending() -> dict:
    """List all pending orders (limit/stop orders not yet executed).

    Use this to review orders waiting to be filled — check their type, price
    level, expiry, and distance from current market price. Essential before
    placing new orders to avoid duplicates and after cancelling orders to
    confirm removal.

    Args:
        None

    Returns:
        dict: Contains one key:
            - orders (list): Array of pending order dicts, each with:
                - order_id (int): MT5 order ticket
                - symbol (str): Trading symbol
                - type (str): Order type ("buy_limit", "sell_limit",
                  "buy_stop", "sell_stop")
                - volume (float): Order volume in lots
                - price (float): Order trigger/limit price
                - sl (float, optional): Stop loss price
                - tp (float, optional): Take profit price
                - time_setup (int): Order creation time (Unix timestamp)
                - time_expiration (int, optional): Order expiry time
                - comment (str, optional): Order comment
            - snapshot_metadata (dict): Timestamp and freshness info

    Example: mt5_orders_pending()

    Note: LIVE data from MT5 gateway adapter with TCP bridge fallback. Empty
    array means no pending orders. Recommend rechecking every 120 seconds
    (see snapshot_metadata.next_recommended_check_seconds).
    """
    try:
        orders = get_gateway().adapter.get_orders()
        if orders:
            order_list = [
                o.model_dump() if hasattr(o, "model_dump") else dict(o) for o in orders
            ]
            return {
                "orders": order_list,
                "snapshot_metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "next_recommended_check_seconds": 120,
                    "data_freshness": "live",
                },
            }
    except Exception as e:
        logger.warning(f"mt5_orders_pending: gateway adapter failed — {e}")

    try:
        tcp_result = _tcp_send_and_await("get_orders", {}, timeout_s=10.0)
        data = _parse_payload_dict(tcp_result)
        order_list = data.get("orders", []) if data else []
        return {
            "orders": order_list,
            "snapshot_metadata": {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "next_recommended_check_seconds": 120,
                "data_freshness": "live",
            },
        }
    except Exception as e:
        logger.warning(f"mt5_orders_pending: TCP bridge failed — {e}")

    return {
        "orders": [],
        "snapshot_metadata": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "next_recommended_check_seconds": 120,
            "data_freshness": "live",
        },
    }


# ---------------------------------------------------------------------------
# Tool 8: mt5_bridge_status
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_bridge_status", annotations=_READ_ANNOTATIONS)
def mt5_bridge_status() -> dict:
    """Check the health and connectivity of the TCP bridge to the MT5 terminal.

    Use this to diagnose connection issues when other tools return errors. If
    the bridge is disconnected, check that the MT5 terminal is running, the
    EA is attached to a chart, and TCP bridge settings are correct.

    Args:
        None

    Returns:
        dict: Bridge status with keys:
            - connected (bool): Whether TCP bridge is connected to MT5 terminal
            - terminal_build (int, optional): MT5 build number
            - terminal_version (str, optional): MT5 version string
            - bridge_uptime (float, optional): Bridge uptime in seconds
            - ea_connected (bool, optional): Whether EA is connected to bridge
            - message (str): Status description
            - error (str, present on failure): Error description

    Example: mt5_bridge_status()

    Note: LIVE check against the HTTP gateway endpoint. Returns
    {"connected": False} with error details if the HTTP check fails. This is a
    critical diagnostic tool — call before escalation.
    """
    try:
        client = get_http_client()
        r = client.get(f"{get_settings_cached().gateway_url}/bridge/terminal/status")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"mt5_bridge_status: HTTP check failed — {e}")
        return {
            "connected": False,
            "message": "Bridge status unavailable",
            "error": str(e),
        }
