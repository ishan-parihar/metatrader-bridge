

from itertools import combinations
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

from . import mcp

_PORTFOLIO_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


# ---------------------------------------------------------------------------
# Tool 1: mt5_portfolio_exposure
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_portfolio_exposure", annotations=_PORTFOLIO_ANNOTATIONS)
def mt5_portfolio_exposure() -> dict:
    """
    Calculate the current portfolio exposure across all open positions and pending orders.

    Use this before any trade entry to understand your current market exposure. This is a
    read-only analytical tool that breaks down notional exposure by symbol, including
    directional bias (long/short) and correlated exposure. Helps identify over-concentration
    in any single asset or correlated group.

    Args:
        None

    Returns:
        dict: Symbol-level exposure breakdown with notional values, directional information,
              and correlation data. Structure depends on PortfolioRiskService. Key fields:
              total_exposure_usd, net_exposure_usd, exposure_by_symbol, margin_usage_pct,
              risk_score, correlation_groups.

    Example: mt5_portfolio_exposure() → {"total_exposure_usd": 15000, "exposure_by_symbol": {...}, "risk_score": 0.35}

    Note: This tool is purely analytical and does not modify any state. Requires MT5
          connectivity to fetch current positions and orders. Returns error dict on failure.
    """
    try:
        from .tools_resources import (
            mt5_positions_open,
            mt5_orders_pending,
            mt5_account_summary,
        )
        from mt5_mcp.services.portfolio_risk import PortfolioRiskService

        positions = mt5_positions_open()
        orders = mt5_orders_pending()
        account = mt5_account_summary()

        svc = PortfolioRiskService(
            get_positions_fn=lambda: positions.get("positions", []),
            get_orders_fn=lambda: orders.get("orders", []),
            get_account_fn=lambda: account,
        )
        return svc.get_exposure()
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: mt5_portfolio_risk
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_portfolio_risk", annotations=_PORTFOLIO_ANNOTATIONS)
def mt5_portfolio_risk(
    symbol: Optional[str] = None, days: int = 7, limit: int = 100
) -> dict:
    """
    Compute comprehensive portfolio risk metrics including concentration, correlation, and margin usage.

    Use this to assess portfolio-level risk before major trading decisions. Evaluates how
    concentrated your exposure is in single positions, identifies correlated asset pairs
    (|correlation| > 0.5), and calculates a composite risk score. Correlated pairs represent
    assets that move together — when one suffers, the correlated counterpart likely will too,
    amplifying portfolio drawdown.

    Args:
        symbol (Optional[str]): If provided, filter risk analysis to this symbol only. (default: None)
        days (int): Look-back period in days for historical correlation calculation. (default: 7)
        limit (int): Maximum number of data points per symbol for correlation calculations. (default: 100)

    Returns:
        dict: Contains "total_exposure_usd", "net_exposure_usd", "exposure_by_symbol" (list with
              per-symbol breakdown), "risk_metrics" (concentration_ratio, max_single_position_pct,
              correlated_pairs list of {symbol_a, symbol_b, correlation}), "margin_usage_pct",
              "risk_score", and "correlation_groups".

    Example: mt5_portfolio_risk() → {"total_exposure_usd": 15000, "risk_metrics": {"concentration_ratio": 0.45, "correlated_pairs": [{"symbol_a": "EURUSD", "symbol_b": "GBPUSD", "correlation": 0.82}]}}

    Note: If no positions are open, returns zero-valued defaults. Higher concentration_ratio
          (exposure/equity) indicates greater risk concentration. Requires at least two different
          symbols with positions to identify correlated pairs.
    """
    try:
        from .tools_resources import (
            mt5_positions_open,
            mt5_orders_pending,
            mt5_account_summary,
        )
        from mt5_mcp.services.portfolio_risk import PortfolioRiskService

        positions = mt5_positions_open()
        orders = mt5_orders_pending()
        account = mt5_account_summary()

        pos_list = positions.get("positions", [])
        if not pos_list:
            return {
                "total_exposure_usd": 0,
                "net_exposure_usd": 0,
                "exposure_by_symbol": [],
                "risk_metrics": {
                    "concentration_ratio": 0,
                    "max_single_position_pct": 0,
                    "correlated_pairs": [],
                },
            }

        svc = PortfolioRiskService(
            get_positions_fn=lambda: pos_list,
            get_orders_fn=lambda: orders.get("orders", []),
            get_account_fn=lambda: account,
        )
        exposure = svc.get_exposure()

        unique_symbols = list(
            {p.get("symbol", "") for p in pos_list if p.get("symbol")}
        )
        correlated_pairs = []
        for sa, sb in combinations(unique_symbols, 2):
            corr = svc._correlation(sa, sb)
            if abs(corr) > 0.5:
                correlated_pairs.append(
                    {"symbol_a": sa, "symbol_b": sb, "correlation": corr}
                )

        exposure_by_symbol = []
        raw_by_symbol = exposure.get("exposure_by_symbol", {})
        if isinstance(raw_by_symbol, dict):
            for sym, data in raw_by_symbol.items():
                exposure_by_symbol.append(
                    {
                        "symbol": sym,
                        "notional_usd": data.get("notional_usd", 0),
                        "usd_direction": data.get("usd_direction", "unknown"),
                        "correlated_exposure_usd": data.get(
                            "correlated_exposure_usd", 0
                        ),
                    }
                )
        elif isinstance(raw_by_symbol, list):
            exposure_by_symbol = raw_by_symbol

        equity = 0.0
        if account:
            acc = account if isinstance(account, dict) else {}
            if hasattr(account, "model_dump"):
                acc = account.model_dump()
            elif hasattr(account, "__dict__"):
                acc = {
                    k: v for k, v in account.__dict__.items() if not k.startswith("_")
                }
            equity = float(acc.get("equity", 0) or 0)

        total_exposure = exposure.get("total_exposure_usd", 0)
        concentration_ratio = round(total_exposure / equity, 4) if equity > 0 else 0

        max_single = 0.0
        for item in exposure_by_symbol:
            notional = abs(item.get("notional_usd", 0))
            if equity > 0:
                pct = notional / equity
                if pct > max_single:
                    max_single = pct

        return {
            "total_exposure_usd": total_exposure,
            "net_exposure_usd": exposure.get("total_exposure_usd", 0),
            "exposure_by_symbol": exposure_by_symbol,
            "risk_metrics": {
                "concentration_ratio": concentration_ratio,
                "max_single_position_pct": round(max_single, 4),
                "correlated_pairs": correlated_pairs,
            },
            "margin_usage_pct": exposure.get("margin_usage_pct", 0),
            "risk_score": exposure.get("risk_score", 0),
            "correlation_groups": exposure.get("correlation_groups", []),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: mt5_pre_trade_gate
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_pre_trade_gate", annotations=_PORTFOLIO_ANNOTATIONS)
def mt5_pre_trade_gate(symbol: str, volume_lots: float) -> dict:
    """
    Evaluate whether a proposed trade passes portfolio-level risk checks before execution.

    Use this as a pre-flight check before submitting any trade. It validates the proposed
    trade against current portfolio exposure, concentration limits, and risk policies
    defined in PortfolioRiskService. This is a read-only analytical gate — no trade is executed.

    Args:
        symbol (str): The trading symbol to evaluate (e.g., "EURUSD", "GBPUSD", "BTCUSD").
        volume_lots (float): The proposed trade volume in lots (e.g., 0.1 for a micro lot, 1.0 for a standard lot).

    Returns:
        dict: Gate check results indicating whether the trade is approved, flagged, or
              rejected, with reasons and current portfolio context (exposure, concentration).

    Example: mt5_pre_trade_gate(symbol="EURUSD", volume_lots=0.1) → {"approved": true, "reason": "within_limits", "exposure_after": 16500}

    Note: This tool is purely analytical and does NOT execute any trade. Returns error dict
          if MT5 is unreachable or account data cannot be fetched. Volume must be in lots
          as used by your broker (not units).
    """
    try:
        from .tools_resources import mt5_positions_open, mt5_account_summary
        from mt5_mcp.services.portfolio_risk import PortfolioRiskService

        account = mt5_account_summary()
        positions = mt5_positions_open()

        svc = PortfolioRiskService(
            get_positions_fn=lambda: positions.get("positions", []),
            get_orders_fn=lambda: [],
            get_account_fn=lambda: account,
        )
        return svc.pre_trade_gate(
            symbol=symbol,
            side="buy",
            volume=volume_lots,
            sl_distance=0.0,
        )
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 4: mt5_reconcile
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_reconcile", annotations=_PORTFOLIO_ANNOTATIONS)
def mt5_reconcile(intent_ids: Optional[list[str]] = None) -> dict:
    """
    Reconcile trading intents against actual MT5 positions and calculate foreign P&L.

    Use this after trading sessions to verify that all intended trades were executed as
    expected. Compares the list of intent IDs against real open positions, identifying
    discrepancies, orphaned positions, and calculating P&L from deals not tracked by intents.
    Essential for audit trails and monitoring automated trading system fidelity.

    Args:
        intent_ids (Optional[list[str]]): List of intent IDs to reconcile against open
                                          positions. Accepts a Python list or a JSON-encoded
                                          string (auto-parsed). (default: None)

    Returns:
        dict: Reconciliation results including matched/unmatched positions, foreign positions
              (those not matching any intent), and "foreign_pnl" — P&L from deals outside
              tracked intents.

    Example: mt5_reconcile(intent_ids=["int_001", "int_002"]) → {"matched": [...], "unmatched_intents": [], "foreign_pnl": {"total": 45.50}}

    Note: If intent_ids is passed as a string, it will be parsed as JSON automatically.
          An empty list reconciles against all open positions. Foreign P&L represents
          profit/loss from positions not attributed to any tracked intent.
    """
    try:
        from .tools_resources import mt5_positions_open, mt5_deals_history
        from .shared import get_settings_cached
        from mt5_mcp.services.reconciliation import ReconciliationService

        if isinstance(intent_ids, str):
            import json

            try:
                intent_ids = json.loads(intent_ids)
            except (json.JSONDecodeError, TypeError):
                intent_ids = [intent_ids] if intent_ids else []

        intent_ids = intent_ids or []

        positions = mt5_positions_open()
        deals = mt5_deals_history()

        pos_list = positions.get("positions", [])
        deals_list = deals.get("deals", [])

        svc = ReconciliationService(get_settings_cached())

        result = svc.reconcile(intent_ids, pos_list)

        owned = svc.get_owned_positions(pos_list)
        foreign_pnl = svc.calculate_foreign_pnl(owned, deals_list)

        result["foreign_pnl"] = foreign_pnl
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: mt5_custom_indicator
# ---------------------------------------------------------------------------
@mcp.tool(name="mt5_custom_indicator", annotations=_PORTFOLIO_ANNOTATIONS)
def mt5_custom_indicator(
    symbol: str,
    timeframe: str = "H1",
    indicator_name: str = "custom",
    params: Optional[dict] = None,
    buffer_index: int = 0,
    count: int = 100,
) -> dict:
    """
    Compute a custom indicator from a compiled .ex5 file installed on the MT5 terminal.

    Use this to run proprietary or third-party indicators installed as .ex5 files in MT5's
    Indicators folder. For built-in indicators (RSI, MACD, Bollinger Bands, etc.), use
    mt5_get_indicator instead — it handles built-ins natively without requiring compiled files.

    Args:
        symbol (str): Trading symbol to compute the indicator on (e.g., "EURUSD", "BTCUSD").
        timeframe (str): Chart timeframe — one of "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")
        indicator_name (str): Name of the custom .ex5 indicator file (without the .ex5 extension). (default: "custom")
        params (Optional[dict]): Optional parameters passed to the indicator as key-value pairs. (default: None)
        buffer_index (int): Which indicator buffer to read (0-based, typically 0 = main line). (default: 0)
        count (int): Number of most recent data points to return. Lower = faster. (default: 100)

    Returns:
        dict: Contains "symbol", "timeframe", "indicator_name", "data" (indicator values),
              and "status" ("completed" or error). On error, includes structured error info
              with error code, message, and suggested remediation action.

    Example: mt5_custom_indicator(symbol="EURUSD", indicator_name="MyCustomRSI", timeframe="H1") → {"data": [0.5, 0.6, ...], "status": "completed"}

    Note: Falls back to HTTP bridge if TCP bridge is unavailable. Common errors include:
          "INDICATOR_NOT_FOUND" (.ex5 file missing from MQL5/Indicators/),
          "INDICATOR_LOAD_FAILED" (use mt5_get_indicator for built-ins instead), and
          "INVALID_PARAMETER" (check parameter types/ranges). Timeout is 20 seconds.
    """
    try:
        from mt5_mcp.adapters.common.symbol_utils import (
            normalize_symbol,
            denormalize_symbol,
        )
        from .shared import (
            _tcp_send_and_await,
            _parse_payload,
            get_http_client,
            get_settings_cached,
            _await_result,
        )

        symbol_norm = normalize_symbol(symbol)
        payload = {
            "symbol": symbol_norm,
            "timeframe": timeframe,
            "indicator_name": indicator_name,
            "params": params,
            "buffer_index": buffer_index,
            "count": count,
        }

        result = _tcp_send_and_await("get_custom_indicator", payload, timeout_s=20.0)

        if result is None:
            client = get_http_client()
            settings = get_settings_cached()
            req = client.post(
                f"{settings.gateway_url}/bridge/commands/enqueue",
                params={"type": "get_custom_indicator", **payload},
            )
            req.raise_for_status()
            req_id = req.json().get("id") or req.json().get("request_id")
            result = _await_result(req_id, timeout_s=20.0)

        data = _parse_payload(result) if result else {}
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        if isinstance(data, dict) and "payload" in data:
            data = data["payload"]

        if isinstance(data, dict):
            error_raw = data.get("error", data.get("error_code", ""))
            if error_raw:
                error_int = str(error_raw).upper()
                error_map = {
                    "4802": {
                        "code": "INDICATOR_NOT_FOUND",
                        "message": "Custom indicator file not found. Verify the .ex5 file exists in MQL5/Indicators/.",
                        "action": "Place the compiled .ex5 file in MT5's Indicators folder and restart the EA.",
                    },
                    "ERR_INDICATOR_CANNOT_LOAD": {
                        "code": "INDICATOR_LOAD_FAILED",
                        "message": "MT5 cannot load this indicator. Built-in indicators (rsi, macd, etc.) should use mt5_get_indicator instead.",
                        "action": "Use mt5_get_indicator for built-in indicators. Reserve mt5_custom_indicator for custom .ex5 files.",
                    },
                    "4801": {
                        "code": "INVALID_PARAMETER",
                        "message": "One or more indicator parameters are invalid.",
                        "action": "Check parameter types and ranges against the indicator's documentation.",
                    },
                    "4806": {
                        "code": "INDICATOR_DATA_NOT_FOUND",
                        "message": "Indicator loaded but no data available for the requested timeframe/count.",
                        "action": "Try a different timeframe or reduce the count parameter.",
                    },
                }
                mapped = error_map.get(
                    error_int,
                    {
                        "code": "UNKNOWN_ERROR",
                        "message": str(error_raw),
                        "action": "Check MT5 Experts log for details.",
                    },
                )
                return {
                    "symbol": denormalize_symbol(symbol_norm),
                    "timeframe": timeframe,
                    "indicator_name": indicator_name,
                    "status": "error",
                    "error": mapped,
                    "hint": f"For built-in indicators (RSI, MACD, etc.), use mt5_get_indicator instead.",
                }
            if "symbol" in data:
                data["symbol"] = denormalize_symbol(data["symbol"])

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "indicator_name": indicator_name,
            "data": data,
            "status": result.get("status", "unknown") if result else "unknown",
        }
    except Exception as e:
        return {"error": str(e)}
