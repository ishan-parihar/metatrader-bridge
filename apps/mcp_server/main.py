from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request, Body
from pydantic import BaseModel
from typing import Any, Literal
import httpx
import time
import uuid
from datetime import datetime, timezone
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from mt5_mcp.observability.logging import setup_logging
from mt5_mcp.schemas.models import (
    AccountSummary,
    Bars,
    Deal,
    Order,
    PerformanceSummary,
    Position,
    ExecutionResult,
    MarginEstimateRequest,
    SymbolInfo,
    TerminalStatus,
    TradeIntent,
)
from mt5_mcp.services.execution_gateway.service import ExecutionGateway
from mt5_mcp.settings.config import get_settings
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol
from mt5_mcp.schemas.tools import (
    BarsRequest,
    IndicatorRequest,
    ChartScreenshotRequest,
    ChartScreenshotResult,
    CorrelationMatrixRequest,
    DealsHistoryRequest,
    ModifyPositionSLTPRequest,
    MultiTimeframeIndicatorRequest,
    ClosePositionRequest as ClosePosReq,
    PositionSizeRequest,
    SubmitPendingOrderRequest,
    CancelOrderRequest,
    SymbolInfoRequest,
    TicksRequest,
    TrailPositionRequest,
    OrderBookRequest,
    ValidateTradeSetupRequest,
    VolatilityProfileRequest,
    DivergenceRequest,
    VolumeProfileRequest,
    MultiBarPatternsRequest,
    MomentumCheckRequest,
    ModifyOrderRequest as ModOrderReq,
    CloseAllPositionsRequest,
    CancelAllOrdersRequest,
    BracketOrderRequest,
    BracketOrderResult,
    SetTrailingStopRequest,
    TrailingStopResult,
    PriceAlertRequest,
    PriceAlertResult,
    PositionMonitorRequest,
    PositionMonitorResult,
    WaitDelayRequest,
    WaitDelayResult,
    WaitForIndicatorRequest,
    WaitForIndicatorResult,
    MarketRegimeRequest,
    MarketScanRequest,
    TradeDecisionLogRequest,
    TradeJournalReflectionRequest,
    TradingContextRequest,
    TradingCoachRequest,
    SnapshotRequest,
    OpportunityRankRequest,
    ChartIntelligenceRequest,
    CustomIndicatorRequest,
    PortfolioExposureRequest,
    PreTradeGateRequest,
)
from mt5_mcp.policy.engine import get_policy
from mt5_mcp.services.agent_capabilities import (
    build_volatility_profile,
    calculate_position_size,
    compute_correlation_matrix,
    summarize_deals,
    validate_trade_setup,
)
from mt5_mcp.services.market_regime import detect_regime
from mt5_mcp.services.divergence import detect_divergence
from mt5_mcp.services.multi_bar_patterns import detect_multi_bar_patterns
from mt5_mcp.services.trade_journal_db import get_journal_db
from mt5_mcp.services.market_context import build_context
from mt5_mcp.services.trading_coach import TradingCoach
from mt5_mcp.services.reconciliation import ReconciliationService
from mt5_mcp.services.snapshot_service import SymbolSnapshotService
from mt5_mcp.services.opportunity_rank import OpportunityRanker
from mt5_mcp.services.portfolio_risk import PortfolioRiskService
from mt5_mcp.services.session_service import (
    get_session_context as _get_session_context,
    get_session_for_pair as _get_session_for_pair,
)
from mt5_mcp.services.economic_calendar import (
    get_upcoming_events as _get_upcoming_events,
    is_blackout_now as _is_blackout_now,
    get_blackout_windows as _get_blackout_windows,
    get_daily_briefing as _get_daily_briefing,
)
from mt5_mcp.services.news_service import (
    fetch_news as _fetch_news,
    enrich_news as _enrich_news,
    get_available_pools as _get_available_pools,
    get_available_sources as _get_available_sources,
)
from mt5_mcp.schemas.tools import (
    TradingPolicyConfigRequest,
    TradingDecisionSupportRequest,
    NewsFetchRequest,
    EconomicCalendarRequest,
    EATrailingStartRequest,
    EATrailingStopRequest,
    EABracketStartRequest,
    EABracketStopRequest,
    SafeShutdownRequest,
    MLPredictRequest,
    DataImportRequest,
    HistoricalBarsRequest,
    HistoricalTicksRequest,
    HistoricalDealsRequest,
)


setup_logging()
app = FastAPI(title="MT5 MCP Server", version="0.1.0")

# Mount FastMCP SSE app for standard MCP protocol (Hermes/Claude connect here)
from apps.mcp_server import create_mcp_server

try:
    fastmcp = create_mcp_server()
    app.mount("/mcp", fastmcp.sse_app())
except Exception as e:
    import logging

    logging.getLogger("mt5_mcp.observability.logging").warning(
        f"Failed to mount MCP SSE app: {e}",
    )

# Include shared gateway routes (bridge/commands, bridge/heartbeat, bridge/results,
# vibe endpoints, metrics) so they are all served on the same port 8010.
from apps.bridge_gateway.router import router as gateway_router

app.include_router(gateway_router)


@app.middleware("http")
async def correlation_middleware(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    from mt5_mcp.observability.logging import set_correlation_id, set_request_id

    corr_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:12])
    req_id = str(uuid.uuid4())[:8]
    set_correlation_id(corr_id)
    set_request_id(req_id)

    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    response.headers["X-Correlation-ID"] = corr_id
    return response


_gw = None
_settings = None
_http_client = None  # Persistent HTTP client for gateway communication (Keep-Alive)

# Module-level freeze state for safe_shutdown
_shutdown_state = {"frozen": False, "frozen_at": None, "frozen_by": None}


def is_frozen() -> bool:
    return _shutdown_state["frozen"]


def set_frozen(frozen: bool, by: str = None):
    from datetime import datetime, timezone

    _shutdown_state["frozen"] = frozen
    _shutdown_state["frozen_at"] = (
        datetime.now(timezone.utc).isoformat() if frozen else None
    )
    _shutdown_state["frozen_by"] = by


def thaw():
    set_frozen(False)


def _check_frozen_response() -> dict | None:
    if is_frozen():
        return {
            "error": "Trading is frozen",
            "frozen_at": _shutdown_state["frozen_at"],
            "frozen_by": _shutdown_state["frozen_by"],
        }
    return None


def get_http_client() -> httpx.Client:
    """Get or create persistent HTTP client with Keep-Alive connection pooling.

    Eliminates ~50ms TCP handshake per request by reusing connections.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=10.0,
            http2=False,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            ),
        )
    return _http_client


def get_gateway():
    global _gw
    if _gw is None:
        try:
            _gw = ExecutionGateway()
        except Exception as e:
            logger = __import__(
                "mt5_mcp.observability.logging", fromlist=["logger"]
            ).logger
            logger.warning(f"Gateway initialization failed: {e}")
            raise
    return _gw


def get_settings_cached():
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


_tcp_client: Any | None = None
_tcp_client_connected: bool = False


def _get_tcp_client() -> Any | None:
    """Get or create persistent TCP bridge client.

    Returns None if TCP bridge is unavailable. Client connects lazily
    on first use and maintains connection via internal reconnect loop.
    """
    global _tcp_client, _tcp_client_connected
    if _tcp_client is None and not _tcp_client_connected:
        try:
            from mt5_mcp.services.tcp_bridge_client import TCPBridgeClient

            _tcp_client = TCPBridgeClient()
            _tcp_client_connected = True
            _logger = __import__(
                "mt5_mcp.observability.logging", fromlist=["logger"]
            ).logger
            _logger.info("TCP bridge client initialized (connects on first use)")
        except Exception as e:
            _logger = __import__(
                "mt5_mcp.observability.logging", fromlist=["logger"]
            ).logger
            _logger.warning(f"TCP bridge client init failed: {e}")
            _tcp_client = None
            _tcp_client_connected = True  # Don't retry on this import error
    return _tcp_client


# Resources (read-only)
@app.get("/resources/mt5/terminal/status", response_model=TerminalStatus)
def resource_terminal_status() -> TerminalStatus:
    return get_gateway().terminal_status()


def _resource_account_summary_raw() -> AccountSummary:
    summary = get_gateway().account_summary()
    if summary.account_id is not None:
        return summary
    try:
        return AccountSummary(**tool_get_account_summary())
    except Exception:
        return summary


@app.get("/resources/account/summary")
def resource_account_summary() -> dict:
    raw_summary = _resource_account_summary_raw()
    return {
        "account": raw_summary.model_dump()
        if hasattr(raw_summary, "model_dump")
        else raw_summary,
        "snapshot_metadata": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "next_recommended_check_seconds": 300,
            "data_freshness": "live",
        },
    }


@app.get("/resources/symbols/{symbol}/info", response_model=SymbolInfo)
def resource_symbol_info(symbol: str) -> SymbolInfo:
    adapter = getattr(get_gateway(), "adapter", None)
    if adapter is not None and hasattr(adapter, "get_symbol_info"):
        try:
            info = adapter.get_symbol_info(symbol)
        except Exception:
            info = None
        if info is not None:
            if info.symbol:
                info.symbol = denormalize_symbol(info.symbol)
            return info
    try:
        return SymbolInfo(**tool_get_symbol_info(symbol))
    except Exception:
        return SymbolInfo(symbol=symbol)


@app.get("/resources/deals/history", response_model=list[Deal])
def resource_deals_history(
    limit: int = 100, symbol: str | None = None, days: int = 30
) -> list[Deal]:
    adapter = getattr(get_gateway(), "adapter", None)
    if adapter is not None and hasattr(adapter, "get_deals_history"):
        try:
            deals = adapter.get_deals_history(limit=limit, symbol=symbol, days=days)
        except Exception:
            deals = []
        if deals:
            for deal in deals:
                if deal.symbol:
                    deal.symbol = denormalize_symbol(deal.symbol)
            return deals
    try:
        return [
            Deal(**item)
            for item in tool_get_deals_history(
                limit=limit, symbol=symbol, days=days
            ).get("deals", [])
        ]
    except Exception:
        return []


@app.get("/resources/performance/summary", response_model=PerformanceSummary)
def resource_performance_summary(
    limit: int = 100, symbol: str | None = None, days: int = 30
) -> PerformanceSummary:
    deals = resource_deals_history(limit=limit, symbol=symbol, days=days)
    summary = summarize_deals([deal.model_dump() for deal in deals])
    return PerformanceSummary(**summary)


@app.get("/resources/bars/{symbol}/{timeframe}", response_model=Bars)
def resource_bars(symbol: str, timeframe: str, count: int = 100) -> Bars:
    return get_gateway().get_bars(symbol, timeframe, count)


def _fetch_symbol_infos(symbols: list[str]) -> dict[str, dict]:
    """Fetch symbol info for unique symbols."""
    result: dict[str, dict] = {}
    for sym in set(symbols):
        try:
            info = tool_get_symbol_info(sym)
            result[sym] = info
        except Exception as e:
            from mt5_mcp.observability.logging import logger as _si_logger

            _si_logger.warning(f"Failed to fetch symbol info for {sym}: {e}")
            result[sym] = {}
    return result


def _compute_position_health(position: dict, symbol_info: dict) -> dict:
    """Compute health metrics for a single position dict.

    Returns a health dict with all fields computed from position + symbol data.
    Handles missing data gracefully — sets field to null if computation fails.
    """
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

        # Profit: use raw 'profit' if available, fall back to unrealized_pnl
        profit_raw = position.get("profit")
        if profit_raw is None:
            profit_raw = position.get("unrealized_pnl", 0.0)
        profit = float(profit_raw) if profit_raw is not None else 0.0

        mark_price = float(mark_price) if mark_price else None
        entry_price = float(entry_price) if entry_price else None
        volume = float(volume) if volume else 0.0

        # Point and tick_value from symbol_info
        point = symbol_info.get("point")
        point = float(point) if point else None
        tick_value = symbol_info.get("tick_value")
        tick_value = float(tick_value) if tick_value else None

        # Spread: from position dict (raw EA data) or symbol_info
        spread_raw = position.get("spread")
        if spread_raw is None:
            spread_raw = symbol_info.get("spread_points", 0)
        spread = int(spread_raw) if spread_raw is not None else 0

        if mark_price and point and point > 0:
            # distance_to_sl_pips
            if sl and float(sl) > 0:
                sl_f = float(sl)
                if side == "buy":
                    health["distance_to_sl_pips"] = (mark_price - sl_f) / point
                else:
                    health["distance_to_sl_pips"] = (sl_f - mark_price) / point

            # distance_to_tp_pips
            if tp and float(tp) > 0:
                tp_f = float(tp)
                if side == "buy":
                    health["distance_to_tp_pips"] = (tp_f - mark_price) / point
                else:
                    health["distance_to_tp_pips"] = (mark_price - tp_f) / point

        # pnl_percent_of_risk
        if entry_price and sl and float(sl) > 0 and tick_value and volume > 0:
            sl_f = float(sl)
            risk_per_lot = abs(entry_price - sl_f) * volume * tick_value
            if risk_per_lot > 0:
                health["pnl_percent_of_risk"] = (profit / risk_per_lot) * 100

        # time_in_trade_minutes
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

        # is_winning
        health["is_winning"] = profit > 0

        # is_at_breakeven and trail_eligible (need spread + tick_value)
        if spread > 0 and tick_value and volume > 0:
            spread_cost_dollar = spread * volume * tick_value
            health["is_at_breakeven"] = abs(profit) < (spread_cost_dollar * 0.5)
            health["trail_eligible"] = health["is_winning"] and profit > (
                spread_cost_dollar * 2
            )
        else:
            health["is_at_breakeven"] = None
            health["trail_eligible"] = None

        # spread_cost_pips
        health["spread_cost_pips"] = spread

        # profit_multiple_of_spread
        if spread > 0 and point and point > 0 and tick_value and volume > 0:
            denom = volume * tick_value
            profit_in_points = profit / denom if denom > 0 else 0
            health["profit_multiple_of_spread"] = profit_in_points / spread

    except Exception as e:
        _ch_logger.error(
            f"Health computation failed for position "
            f"{position.get('position_id', 'unknown')}: {e}"
        )

    # --- action_required synthesis ---
    # Priority order: stale_position > time_exit_approaching > invalidation_check > trail_to_breakeven > trail_stop_closer > none
    trail_eligible = health.get("trail_eligible")
    is_at_breakeven = health.get("is_at_breakeven")
    is_winning = health.get("is_winning")
    distance_to_sl_pips = health.get("distance_to_sl_pips")
    pnl_percent_of_risk = health.get("pnl_percent_of_risk")
    time_in_trade_bars_h1 = health.get("time_in_trade_bars_h1")

    action_required = "none"
    action_reason = "Position healthy — no action needed"

    # 1. stale_position: 16+ bars with < 50% risk profit
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
    # 2. time_exit_approaching: 12+ bars, winning, but < 50% risk profit
    elif (
        time_in_trade_bars_h1 is not None
        and time_in_trade_bars_h1 >= 12
        and is_winning is True
        and pnl_percent_of_risk is not None
        and pnl_percent_of_risk < 50
    ):
        action_required = "time_exit_approaching"
        action_reason = "Dead money — consider closing if no progress in 4 more bars"
    # 3. invalidation_check: very close to SL (< 10 pips)
    elif distance_to_sl_pips is not None and distance_to_sl_pips < 10:
        action_required = "invalidation_check"
        action_reason = "Price approaching stop loss — verify thesis still valid"
    # 4. trail_to_breakeven: eligible and not yet at breakeven
    elif trail_eligible is True and is_at_breakeven is False:
        action_required = "trail_to_breakeven"
        action_reason = "Profit > 2x spread — move SL to entry price"
    # 5. trail_stop_closer: eligible, at breakeven, still room to trail
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
    """Enrich a list of position dicts with health metrics."""
    if not positions:
        return positions

    symbols = list({p.get("symbol", "") for p in positions if p.get("symbol")})
    symbol_infos = _fetch_symbol_infos(symbols)

    for position in positions:
        sym = position.get("symbol", "")
        sym_info = symbol_infos.get(sym, {})
        position["health"] = _compute_position_health(position, sym_info)

    return positions


@app.get("/resources/positions/open")
def resource_positions_open() -> dict:
    from mt5_mcp.observability.logging import logger

    sync_status = {
        "positions_count": 0,
        "last_sync_age_ms": 0,
        "retry_count": 0,
        "stale_warning": False,
    }

    start = time.monotonic()

    positions = get_gateway().adapter.get_positions()
    if positions:
        position_dicts = [p.model_dump() for p in positions]
        _enrich_positions_with_health(position_dicts)
        sync_status["positions_count"] = len(position_dicts)
        sync_status["last_sync_age_ms"] = int((time.monotonic() - start) * 1000)
        return {
            "positions": position_dicts,
            "sync_status": sync_status,
            "snapshot_metadata": {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "next_recommended_check_seconds": 600,
                "data_freshness": "live",
            },
        }

    try:
        data = tool_get_positions()
        pos_list = data.get("positions", [])
        result = []
        for item in pos_list:
            try:
                result.append(Position(**item))
            except Exception as e:
                logger.warning(
                    f"Position parse failed: {e} — keys: {list(item.keys())}"
                )
        if result:
            position_dicts = [p.model_dump() for p in result]
            _enrich_positions_with_health(position_dicts)
            sync_status["positions_count"] = len(position_dicts)
            sync_status["last_sync_age_ms"] = int((time.monotonic() - start) * 1000)
            return {
                "positions": position_dicts,
                "sync_status": sync_status,
                "snapshot_metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "next_recommended_check_seconds": 600,
                    "data_freshness": "live",
                },
            }
    except Exception as e:
        logger.warning(f"positions_open bridge fallback failed: {e}")

    _MAX_POSITIONS_RETRY_ATTEMPTS = 2
    _POSITIONS_RETRY_DELAY_S = 0.5

    first_attempt_count = 0
    retry_count = 0
    best_result = []

    for attempt in range(_MAX_POSITIONS_RETRY_ATTEMPTS):
        try:
            tcp_result = _tcp_send_and_await("get_positions", {}, timeout_s=5.0)
            if tcp_result and tcp_result.get("status") == "completed":
                payload = tcp_result.get("result", {}).get("payload", "{}")
                data = _parse_payload(payload)
                pos_list = data.get("positions", [])
                parsed = [Position(**item) for item in pos_list if item]
                if not best_result:
                    first_attempt_count = len(parsed)
                if parsed:
                    best_result = parsed
                    break
        except Exception as e:
            logger.warning(f"positions_open TCP attempt {attempt + 1} failed: {e}")

        if attempt == 0 and not best_result:
            retry_count = 1
            logger.warning(
                "positions_open: first attempt returned 0 positions, retrying"
            )
            time.sleep(_POSITIONS_RETRY_DELAY_S)

    stale_warning = (
        retry_count > 0 and first_attempt_count == 0 and len(best_result) > 0
    )

    position_dicts = [p.model_dump() for p in best_result]
    _enrich_positions_with_health(position_dicts)

    sync_status["positions_count"] = len(position_dicts)
    sync_status["last_sync_age_ms"] = int((time.monotonic() - start) * 1000)
    sync_status["retry_count"] = retry_count
    sync_status["stale_warning"] = stale_warning

    if stale_warning:
        logger.warning(
            f"positions_open: stale cache detected — first attempt=0, second attempt={len(position_dicts)} positions"
        )

    return {
        "positions": position_dicts,
        "sync_status": sync_status,
        "snapshot_metadata": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "next_recommended_check_seconds": 600,
            "data_freshness": "live",
        },
    }


def _resource_orders_pending_raw() -> list[Order]:
    orders = get_gateway().adapter.get_orders()
    if orders:
        return orders
    try:
        return [Order(**item) for item in tool_get_orders().get("orders", [])]
    except Exception:
        return orders


@app.get("/resources/orders/pending")
def resource_orders_pending() -> dict:
    raw_orders = _resource_orders_pending_raw()
    return {
        "orders": raw_orders,
        "snapshot_metadata": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "next_recommended_check_seconds": 120,
            "data_freshness": "live",
        },
    }


@app.get("/tcp-bridge/status")
def tcp_bridge_status() -> dict:
    """TCP bridge server status (EA connection, pending commands)."""
    from apps.mcp_server.shared import _bridge_server

    if _bridge_server is None:
        return {"ea_connected": False, "ea_address": "", "pending_commands": 0}
    return {
        "ea_connected": _bridge_server.ea_connected,
        "ea_address": getattr(_bridge_server, "_ea_address", ""),
        "pending_commands": _bridge_server.pending_count,
    }


@app.get("/health")
def health() -> dict:
    """Enhanced health check with subsystem aggregation."""
    import time as _health_time

    status = "healthy"
    issues = []

    # Re-evaluate adapter selection (detect EA bridge reconnection)
    try:
        get_gateway().reload_adapter()
    except Exception as e:
        issues.append(f"Adapter reload failed: {e}")

    # Check bridge connection
    bridge_connected = False
    heartbeat_age = None
    try:
        ts = get_gateway().terminal_status()
        bridge_connected = ts.connected if hasattr(ts, "connected") else False
        if hasattr(ts, "last_heartbeat") and ts.last_heartbeat:
            heartbeat_age = _health_time.time() - ts.last_heartbeat
            if heartbeat_age > 30:
                issues.append(f"Stale heartbeat: {heartbeat_age:.0f}s")
    except Exception as e:
        issues.append(f"Bridge status check failed: {e}")

    # Check TCP bridge (in-process or separate)
    tcp_connected = False
    if _TCP_BRIDGE_ENABLED:
        try:
            from apps.mcp_server.shared import is_bridge_connected as _bridge_ok

            if _bridge_ok():
                tcp_connected = True
            else:
                tcp_client = _get_tcp_client()
                tcp_connected = tcp_client is not None and getattr(
                    tcp_client, "is_connected", False
                )
        except Exception as e:
            tcp_connected = False
            issues.append(f"TCP bridge unavailable: {e}")

    # Check journal DB
    journal_writable = False
    try:
        journal = get_journal_db()
        journal._conn.execute(
            "INSERT INTO trade_decisions (decision_id, timestamp, symbol, side, action) VALUES ('health_check', 'now', 'TEST', 'TEST', 'TEST')"
        )
        journal._conn.execute(
            "DELETE FROM trade_decisions WHERE decision_id = 'health_check'"
        )
        journal._conn.commit()
        journal_writable = True
    except Exception as e:
        issues.append(f"Journal DB not writable: {e}")

    # Determine adapter
    adapter_name = "unknown"
    try:
        gw = get_gateway()
        adapter = getattr(gw, "adapter", None)
        if adapter is not None:
            adapter_name = type(adapter).__name__
    except Exception:
        pass

    # Determine uptime (use process start time as proxy)
    uptime = _health_time.monotonic()

    # Set overall status
    if len(issues) >= 2:
        status = "degraded"
    elif issues:
        status = "healthy"  # Single issue, still healthy

    return {
        "status": status,
        "bridge_connected": bridge_connected,
        "tcp_bridge_connected": tcp_connected,
        "journal_db_writable": journal_writable,
        "adapter": adapter_name,
        "uptime_seconds": round(uptime, 1),
        "last_heartbeat_age_seconds": round(heartbeat_age, 1)
        if heartbeat_age is not None
        else None,
        "issues": issues,
    }


@app.get("/tools/health/tool_status")
def tool_health_status() -> dict:
    """Report which tool categories are operational for autonomous agents."""
    reads_ok = True
    writes_ok = True
    waits_ok = True
    analysis_ok = True
    write_failing = []
    analysis_failing = []

    try:
        get_gateway().terminal_status()
    except Exception:
        reads_ok = False

    try:
        get_gateway().account_summary()
    except Exception:
        reads_ok = False

    try:
        get_http_client().get(
            f"{get_settings_cached().gateway_url}/health", timeout=2.0
        )
    except Exception:
        writes_ok = False
        write_failing.append("submit_*_order")

    try:
        get_journal_db()
    except Exception:
        writes_ok = False

    try:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()
    except Exception:
        waits_ok = False

    try:
        detect_regime(bars=[], atr_value=None)
    except Exception:
        analysis_ok = False
        analysis_failing.append("market_regime")

    return {
        "reads": {"status": "ok" if reads_ok else "degraded"},
        "writes": {
            "status": "ok" if writes_ok else "degraded",
            "failing": write_failing,
        },
        "waits": {"status": "ok" if waits_ok else "degraded"},
        "analysis": {
            "status": "ok" if analysis_ok else "degraded",
            "failing": analysis_failing,
        },
        "overall": "ok"
        if (reads_ok and writes_ok and waits_ok and analysis_ok)
        else "degraded",
    }


@app.get("/resources/mt5/bridge/status", response_model=TerminalStatus)
def resource_bridge_terminal_status() -> TerminalStatus:
    # Proxy bridge heartbeat status into MCP for unified visibility
    try:
        client = get_http_client()
        r = client.get(f"{get_settings_cached().gateway_url}/bridge/terminal/status")
        r.raise_for_status()
        data = r.json()
        return TerminalStatus(**data)
    except Exception:
        return TerminalStatus(connected=False, message="Bridge status unavailable")


# ============================================================
# Shared payload parsers — deduplicated from nested functions
# ============================================================


def _parse_indicator_value(result: dict) -> float | None:
    """Extract a single indicator value from a batch result payload."""
    if not result or result.get("status") != "completed":
        return None
    payload = result.get("result", {}).get("payload", {})
    if isinstance(payload, str):
        try:
            return float(json.loads(payload).get("value", 0) or 0)
        except Exception:
            return None
    elif isinstance(payload, dict):
        v = payload.get("value")
        return float(v) if v is not None else None
    return None


def _parse_payload_dict(result: dict) -> dict:
    """Parse a dict result into its inner payload dict."""
    if not result or result.get("status") != "completed":
        return {}
    payload = result.get("result", {}).get("payload", {})
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {}
    elif isinstance(payload, dict):
        return payload
    return {}


# Bridge-backed tools (EA polling model)
# TCP Bridge: low-latency push communication (port 8025)
# HTTP Bridge: legacy polling fallback (port 8020)

import os as _os

_TCP_BRIDGE_ENABLED = _os.getenv("MT5_TCP_BRIDGE_ENABLED", "true").lower() == "true"


def _parse_payload(payload) -> dict:
    """Parse EA payload, handling both string and dict formats."""

    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {}
    elif isinstance(payload, dict):
        return payload
    return {}


def _await_result(req_id: str, timeout_s: float = 20.0, poll_s: float = 0.1) -> dict:
    """Wait for bridge command result."""
    import time as _t

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    end = _t.time() + timeout_s
    while _t.time() < end:
        r = client.get(f"{gw_url}/bridge/results/{req_id}")
        if r.status_code == 200:
            data = r.json()
            if data.get("status") in {"completed", "error"}:
                return data
        _t.sleep(poll_s)
    return {"status": "timeout", "error": "timeout"}


from apps.mcp_server.shared import _tcp_send_and_await, _batch_enqueue_and_await


def _map_trade_retcode(rc: int | str | None) -> str | None:
    try:
        ival = int(rc) if rc is not None else None
    except Exception:
        return None

    mapping = {
        10004: "REQUOTE",
        10006: "REJECTED",
        10008: "PLACED",
        10009: "DONE",
        10010: "DONE_PARTIAL",
        10012: "TIMEOUT",
        10013: "INVALID",
        10014: "INVALID_VOLUME",
        10015: "INVALID_PRICE",
        10016: "INVALID_STOPS",
        10017: "TRADE_DISABLED",
        10018: "MARKET_CLOSED",
        10019: "NO_MONEY",
        10020: "PRICE_CHANGED",
        10021: "PRICE_OFF",
        10022: "INVALID_EXPIRATION",
        10023: "ORDER_CHANGED",
        10024: "TOO_MANY_REQUESTS",
        10025: "NO_CHANGES",
        10026: "SERVER_DISABLES_AT",
        10027: "CLIENT_DISABLES_AT",
        10028: "LOCKED",
        10029: "FROZEN",
        10030: "INVALID_FILL",
        10031: "CONNECTION",
        10032: "ONLY_REAL",
        10033: "LIMIT_ORDERS",
        10034: "LIMIT_VOLUME",
        10035: "INVALID_ORDER",
        10036: "POSITION_CLOSED",
        10038: "INVALID_CLOSE_VOLUME",
        10039: "CLOSE_ORDER_EXIST",
        10040: "LIMIT_POSITIONS",
    }
    return mapping.get(ival, str(ival) if ival is not None else None)


def _build_trade_error_result(
    intent_id: str, payload: str | dict | None
) -> ExecutionResult:
    data = _parse_payload(payload)
    if not data:
        return ExecutionResult(
            intent_id=intent_id,
            status="error",
            message=str(payload or "timeout"),
        )

    retcode = data.get("retcode")
    retcode_label = _map_trade_retcode(retcode)
    try:
        retcode_int = int(retcode) if retcode is not None else None
    except Exception:
        retcode_int = None

    return ExecutionResult(
        intent_id=intent_id,
        status="error",
        adapter="EASocketAdapter",
        broker_order_id=str(data.get("order", "")) if data.get("order") else None,
        position_id=str(data.get("deal", "")) if data.get("deal") else None,
        retcode=retcode_label,
        message=f"Order failed: retcode={retcode_int} ({retcode_label})",
        raw=data,
    )


def _build_tool_error(
    message: str,
    *,
    error_code: str = "UNKNOWN",
    details: dict | None = None,
) -> dict:
    """Build a standardized error response for trade tools.

    Args:
        message: Human-readable error description.
        error_code: Machine-readable code (e.g. INVALID_STOPS, NO_MONEY).
        details: Optional contextual data for debugging.
    """
    result: dict = {"status": "error", "error_code": error_code, "message": message}
    if details:
        result["details"] = details
    return result


def _first_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = bids[0].get("price") if bids else None
    ask = asks[0].get("price") if asks else None
    return (
        float(bid) if bid is not None else None,
        float(ask) if ask is not None else None,
    )


def _error_payload(error: object) -> object:
    if isinstance(error, str):
        parsed = _parse_payload(error)
        if parsed:
            return parsed
    return error


@app.post("/tools/get_bars", response_model=Bars)
def tool_get_bars(req: BarsRequest) -> Bars:
    symbol_normalized = normalize_symbol(req.symbol)

    tcp_result = _tcp_send_and_await(
        "get_bars",
        {"symbol": symbol_normalized, "timeframe": req.timeframe, "count": req.count},
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            data = json.loads(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {"data": []}
        if "error" in data or "data" not in data:
            return Bars(
                symbol=req.symbol, timeframe=req.timeframe, data=[], source="tcp_bridge"
            )
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return Bars(**data)

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "get_bars",
            "symbol": symbol_normalized,
            "timeframe": req.timeframe,
            "count": req.count,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") != "completed":
        return Bars(
            symbol=req.symbol, timeframe=req.timeframe, data=[], source="bridge"
        )
    payload = res.get("result", {}).get("payload", {})
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            data = {"data": []}
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {"data": []}
    if "error" in data or "data" not in data:
        return Bars(
            symbol=req.symbol, timeframe=req.timeframe, data=[], source="bridge"
        )
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return Bars(**data)


# Sensible defaults for advanced indicators — AI agent should never get bad_args
# for standard indicator calls. These match MT5 standard defaults.
# MASTER COPY: apps/mcp_server/shared.py:INDICATOR_DEFAULTS — keep in sync.
_INDICATOR_DEFAULTS: dict[str, dict[str, int]] = {
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "bbands": {"period": 20, "deviation": 2},
    "stoch": {"k_period": 5, "d_period": 3, "slowing": 3},
    "atr": {"period": 14},
    "adx": {"period": 14},
    "dmi": {"period": 14},
    "ichimoku": {"tenkan": 9, "kijun": 26, "senkou": 52},
    "cci": {"period": 14},
    "rsi": {"period": 14},
    "sma": {"period": 20},
    "ema": {"period": 20},
    "wma": {"period": 20},
    "momentum": {"period": 14},
    "williams": {"period": 14},
}


@app.post("/tools/get_indicator")
def tool_get_indicator(req: IndicatorRequest) -> dict:
    symbol_normalized = normalize_symbol(req.symbol)
    gw_url = get_settings_cached().gateway_url
    params = {
        "type": "get_indicator",
        "symbol": symbol_normalized,
        "timeframe": req.timeframe,
        "indicator": req.indicator,
    }

    # Inject sensible defaults for advanced indicators so AI agent never gets bad_args
    indicator_lower = req.indicator.lower() if req.indicator else ""
    defaults = _INDICATOR_DEFAULTS.get(indicator_lower, {})
    for key, default_val in defaults.items():
        current = getattr(req, key, None)
        if current is None:
            setattr(req, key, default_val)

    # Only include optional params when provided
    for key, val in (
        ("period", req.period),
        ("fast", req.fast),
        ("slow", req.slow),
        ("signal", req.signal),
        ("deviation", req.deviation),
        ("shift", req.shift),
        ("k_period", req.k_period),
        ("d_period", req.d_period),
        ("slowing", req.slowing),
        ("tenkan", req.tenkan),
        ("kijun", req.kijun),
        ("senkou", req.senkou),
        ("window", req.window),
    ):
        if val is not None:
            params[key] = val

    # TCP-first: try direct TCP command, fall back to HTTP polling
    tcp_result = _tcp_send_and_await("get_indicator", params)
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            data = json.loads(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return data

    # HTTP fallback: reuse params dict (already built above)
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=20.0)
    if res.get("status") != "completed":
        return {"status": "error", "message": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", {})
    # Handle both string and dict payloads
    if isinstance(payload, str):
        try:
            data = _parse_payload(payload)
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {}
    # Denormalize symbol in response
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return data


@app.post("/tools/get_chart_screenshot", response_model=ChartScreenshotResult)
def tool_get_chart_screenshot(req: ChartScreenshotRequest) -> ChartScreenshotResult:
    symbol_normalized = normalize_symbol(req.symbol)

    tcp_result = _tcp_send_and_await(
        "get_chart_screenshot",
        {
            "symbol": symbol_normalized,
            "timeframe": req.timeframe,
            "width": req.width,
            "height": req.height,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        if isinstance(payload, str):
            data = _parse_payload(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {"image_base64": ""}
        return ChartScreenshotResult(**data)

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "get_chart_screenshot",
            "symbol": symbol_normalized,
            "timeframe": req.timeframe,
            "width": req.width,
            "height": req.height,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=15.0)
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {"image_base64": ""}
    return ChartScreenshotResult(**data)


@app.post("/tools/get_ticks", response_model=dict)
def tool_get_ticks(req: TicksRequest) -> dict:
    symbol_normalized = normalize_symbol(req.symbol)

    tcp_result = _tcp_send_and_await(
        "get_ticks",
        {"symbol": symbol_normalized, "count": req.count},
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        if isinstance(payload, str):
            try:
                data = _parse_payload(payload)
            except Exception:
                data = {}
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "get_ticks",
            "symbol": symbol_normalized,
            "count": req.count,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") != "completed":
        return {"status": "error", "message": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {}
    # Denormalize symbol in response
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return data


@app.post("/tools/get_order_book", response_model=dict)
def tool_get_order_book(req: OrderBookRequest) -> dict:
    symbol_normalized = normalize_symbol(req.symbol)

    tcp_result = _tcp_send_and_await(
        "get_order_book",
        {"symbol": symbol_normalized},
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        if isinstance(payload, str):
            try:
                data = _parse_payload(payload)
            except Exception:
                data = {}
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={"type": "get_order_book", "symbol": symbol_normalized},
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") != "completed":
        return {"status": "error", "message": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {}
    # Denormalize symbol in response
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return data


@app.post("/tools/get_symbol_info", response_model=dict)
def post_tool_get_symbol_info(req: SymbolInfoRequest) -> dict:
    return tool_get_symbol_info(req.symbol)


def tool_get_symbol_info(symbol: str) -> dict:
    symbol_normalized = normalize_symbol(symbol)

    tcp_result = _tcp_send_and_await(
        "get_symbol_info",
        {"symbol": symbol_normalized},
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        if isinstance(payload, str):
            data = _parse_payload(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={"type": "get_symbol_info", "symbol": symbol_normalized},
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") != "completed":
        return {"symbol": symbol, "error": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", "{}")
    data = _parse_payload(payload)
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return data


@app.post("/tools/get_deals_history", response_model=dict)
def post_tool_get_deals_history(req: DealsHistoryRequest) -> dict:
    return tool_get_deals_history(limit=req.limit, symbol=req.symbol, days=req.days)


def tool_get_deals_history(
    limit: int = 100, symbol: str | None = None, days: int = 30
) -> dict:
    params: dict[str, object] = {
        "type": "get_deals_history",
        "limit": limit,
        "days": days,
    }
    if symbol:
        params["symbol"] = normalize_symbol(symbol)

    tcp_result = _tcp_send_and_await("get_deals_history", params)
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        data = _parse_payload(payload)
        for deal in data.get("deals", []):
            if "symbol" in deal:
                deal["symbol"] = denormalize_symbol(deal["symbol"])
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=15.0)
    if res.get("status") != "completed":
        return {"deals": [], "error": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", "{}")
    data = _parse_payload(payload)
    for deal in data.get("deals", []):
        if "symbol" in deal:
            deal["symbol"] = denormalize_symbol(deal["symbol"])
    return data


@app.post("/tools/modify_order", response_model=dict)
def tool_modify_order(req: ModOrderReq) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    params: dict[str, object] = {"type": "modify_order", "order_id": req.order_id}
    # Only include fields when provided to avoid unintended zeroing in EA
    if req.new_price is not None:
        params["new_price"] = req.new_price
    if req.new_sl is not None:
        params["new_sl"] = req.new_sl
    if req.new_tp is not None:
        params["new_tp"] = req.new_tp

    # Add ownership fields when provided
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("modify_order", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/close_all_positions", response_model=dict)
def tool_close_all_positions(req: CloseAllPositionsRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    params: dict[str, object] = {"type": "close_all_positions", "side": req.side}
    if req.symbol is not None and req.symbol != "":
        params["symbol"] = normalize_symbol(req.symbol)

    # Add ownership fields when provided
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("close_all_positions", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=60.0)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/cancel_all_orders", response_model=dict)
def tool_cancel_all_orders(req: CancelAllOrdersRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    params: dict[str, object] = {"type": "cancel_all_orders", "side": req.side}
    if req.symbol is not None and req.symbol != "":
        params["symbol"] = normalize_symbol(req.symbol)

    # Add ownership fields when provided
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("cancel_all_orders", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=60.0)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


def _auto_log_trade(
    symbol: str,
    side: str,
    action: str,
    *,
    intent_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    entry_price: float | None = None,
    volume_lots: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    message: str | None = None,
) -> None:
    try:
        journal = get_journal_db()
        journal.log_execution_result(
            symbol=symbol,
            side=side,
            action=action,
            intent_id=intent_id,
            session_id=session_id,
            strategy_id=strategy_id,
            entry_price=entry_price,
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
            message=message,
        )
    except Exception as e:
        from mt5_mcp.observability.logging import logger

        logger.warning(f"Auto-journal failed: {e}")


@app.post("/tools/submit_market_order_via_bridge", response_model=ExecutionResult)
def tool_submit_market_order_via_bridge(req: TradeIntent) -> ExecutionResult:
    frozen = _check_frozen_response()
    if frozen:
        return _build_trade_error_result(req.intent_id or "safe_shutdown", frozen)
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    # Policy gate — enhanced with TradingPolicy engine

    policy = get_policy()
    decision = policy.validate_submit_order(
        environment=get_settings_cached().environment,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason or "denied")

    if req.idempotency_key is None:
        req.idempotency_key = str(uuid.uuid4())

    symbol_normalized = normalize_symbol(req.symbol)

    # Flatten trail_config for EA bridge (nested → flat trail_* keys)
    trail_params: dict[str, object] = {}
    if req.trail_config and isinstance(req.trail_config, dict):
        tc = req.trail_config
        trail_params = {
            "trail_atr_multiplier": tc.get("atr_multiplier"),
            "trail_lock_profit_atr": tc.get("lock_profit_atr"),
            "trail_check_interval": tc.get("check_interval_seconds"),
            "trail_timeframe": tc.get("atr_timeframe"),
            "trail_atr_period": tc.get("atr_period"),
        }

    tcp_result = _tcp_send_and_await(
        "submit_order",
        {
            "symbol": symbol_normalized,
            "side": req.side,
            "volume_lots": req.volume_lots,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "deviation": req.deviation_points or 20,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
            **trail_params,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        try:
            data = _parse_payload(payload)
        except Exception:
            data = {}
        retcode = data.get("retcode")
        retcode_mapped = _map_trade_retcode(retcode)
        success_retcodes = {10009, 10008}
        try:
            retcode_int = int(retcode) if retcode else None
        except (ValueError, TypeError):
            retcode_int = None
        if retcode_int not in success_retcodes:
            return _build_trade_error_result(req.intent_id, data)
        _auto_log_trade(
            symbol=req.symbol,
            side=req.side,
            action="entry",
            intent_id=req.intent_id,
            session_id=req.session_id,
            strategy_id=req.strategy_id,
            volume_lots=req.volume_lots,
            sl=req.sl,
            tp=req.tp,
            message=f"Market order submitted via TCP (retcode={retcode_int})",
        )
        return ExecutionResult(
            intent_id=req.intent_id,
            status="submitted",
            adapter="EASocketAdapter",
            broker_order_id=str(data.get("order", "")) if data else None,
            retcode=retcode_mapped,
            message=f"Order submitted successfully (retcode={retcode_int})",
            raw=data,
        )

    # Enqueue order submit and await result
    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "submit_order",
            "symbol": symbol_normalized,
            "side": req.side,
            "volume_lots": req.volume_lots,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "deviation": req.deviation_points or 20,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
            **trail_params,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=30.0)
    if res.get("status") != "completed":
        return _build_trade_error_result(req.intent_id, res.get("error"))
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {}

    # Check retcode to determine success/failure
    retcode = data.get("retcode")
    retcode_mapped = _map_trade_retcode(retcode)

    # Success retcodes: 10009 (DONE), 10008 (PLACED)
    success_retcodes = {10009, 10008}
    try:
        retcode_int = int(retcode) if retcode else None
    except (ValueError, TypeError):
        retcode_int = None

    if retcode_int not in success_retcodes:
        return _build_trade_error_result(req.intent_id, data)

    _auto_log_trade(
        symbol=req.symbol,
        side=req.side,
        action="entry",
        intent_id=req.intent_id,
        session_id=req.session_id,
        strategy_id=req.strategy_id,
        volume_lots=req.volume_lots,
        sl=req.sl,
        tp=req.tp,
        message=f"Market order submitted via HTTP (retcode={retcode_int})",
    )

    return ExecutionResult(
        intent_id=req.intent_id,
        status="submitted",
        adapter="EASocketAdapter",
        broker_order_id=str(data.get("order", "")) if data else None,
        retcode=retcode_mapped,
        message=f"Order submitted successfully (retcode={retcode_int})",
        raw=data,
    )


@app.get("/tools/get_account_summary", response_model=dict)
def tool_get_account_summary() -> dict:
    tcp_result = _tcp_send_and_await("get_account", {})
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                data = _parse_payload(payload)
            except Exception:
                data = {}
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        return data

    # via EA bridge command
    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={"type": "get_account"},
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    payload = res.get("result", {}).get("payload", {})
    # Handle both string and dict payloads
    if isinstance(payload, str):
        try:
            data = _parse_payload(payload)
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {}
    return data


@app.get("/tools/get_positions", response_model=dict)
def tool_get_positions() -> dict:
    tcp_result = _tcp_send_and_await("get_positions", {})
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        try:
            data = _parse_payload(payload)
        except Exception:
            data = {}
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={"type": "get_positions"},
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {}
    return data


@app.get("/tools/get_orders", response_model=dict)
def tool_get_orders() -> dict:
    tcp_result = _tcp_send_and_await("get_orders", {})
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        try:
            data = _parse_payload(payload)
        except Exception:
            data = {}
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={"type": "get_orders"},
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    payload = res.get("result", {}).get("payload", "{}")
    try:
        data = _parse_payload(payload)
    except Exception:
        data = {}
    return data


@app.post("/tools/modify_position_sl_tp", response_model=dict)
def tool_modify_position_sl_tp(req: ModifyPositionSLTPRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    tcp_result = _tcp_send_and_await(
        "modify_position_sl_tp",
        {
            "position_id": req.position_id,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "modify_position_sl_tp",
            "position_id": req.position_id,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/close_position", response_model=dict)
def tool_close_position(req: ClosePosReq) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    tcp_result = _tcp_send_and_await(
        "close_position",
        {
            "position_id": req.position_id,
            "volume": req.volume or 0,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        result = tcp_result.get("result", {})
        _auto_log_trade(
            symbol="",
            side="",
            action="close",
            intent_id=req.intent_id,
            session_id=req.session_id,
            strategy_id=req.strategy_id,
            message=f"Position {req.position_id} closed",
        )
        return result

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "close_position",
            "position_id": req.position_id,
            "volume": req.volume or 0,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=20.0)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    _auto_log_trade(
        symbol="",
        side="",
        action="close",
        intent_id=req.intent_id,
        session_id=req.session_id,
        strategy_id=req.strategy_id,
        message=f"Position {req.position_id} closed via HTTP",
    )
    return res


@app.post("/tools/submit_pending_order", response_model=dict)
def tool_submit_pending_order(req: SubmitPendingOrderRequest) -> dict:
    frozen = _check_frozen_response()
    if frozen:
        return frozen
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)

    decision = get_policy().validate_submit_order(
        environment=get_settings_cached().environment,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason or "denied")
    symbol_normalized = normalize_symbol(req.symbol)

    # Flatten trail_config for EA bridge (nested → flat trail_* keys)
    trail_params: dict[str, object] = {}
    if req.trail_config and isinstance(req.trail_config, dict):
        tc = req.trail_config
        trail_params = {
            "trail_atr_multiplier": tc.get("atr_multiplier"),
            "trail_lock_profit_atr": tc.get("lock_profit_atr"),
            "trail_check_interval": tc.get("check_interval_seconds"),
            "trail_timeframe": tc.get("atr_timeframe"),
            "trail_atr_period": tc.get("atr_period"),
        }

    tcp_result = _tcp_send_and_await(
        "submit_pending_order",
        {
            "symbol": symbol_normalized,
            "side": req.side,
            "kind": req.kind,
            "price": req.price,
            "volume_lots": req.volume_lots,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "deviation": req.deviation,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
            **trail_params,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        result = tcp_result.get("result", {})
        if isinstance(result, dict) and result.get("status") == "error":
            payload = (
                result.get("result", {}).get("payload", {})
                if isinstance(result.get("result"), dict)
                else {}
            )
            if isinstance(payload, str):
                payload = _parse_payload(payload)
            retcode = payload.get("retcode") if isinstance(payload, dict) else None
            error_code = _map_trade_retcode(retcode) or "UNKNOWN"
            result["error_code"] = error_code
        _auto_log_trade(
            symbol=req.symbol,
            side=req.side,
            action="pending_entry",
            intent_id=req.intent_id,
            session_id=req.session_id,
            strategy_id=req.strategy_id,
            entry_price=req.price,
            volume_lots=req.volume_lots,
            sl=req.sl,
            tp=req.tp,
            message=f"Pending {req.kind} order placed @ {req.price}",
        )
        return result

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "submit_pending_order",
            "symbol": symbol_normalized,
            "side": req.side,
            "kind": req.kind,
            "price": req.price,
            "volume_lots": req.volume_lots,
            "sl": req.sl or 0,
            "tp": req.tp or 0,
            "deviation": req.deviation,
            "session_id": req.session_id,
            "strategy_id": req.strategy_id,
            "intent_id": req.intent_id,
            "idempotency_key": req.idempotency_key,
            **trail_params,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=20.0)
    if isinstance(res, dict) and res.get("status") == "error":
        payload = (
            res.get("result", {}).get("payload", {})
            if isinstance(res.get("result"), dict)
            else {}
        )
        if isinstance(payload, str):
            payload = _parse_payload(payload)
        retcode = payload.get("retcode") if isinstance(payload, dict) else None
        error_code = _map_trade_retcode(retcode) or "UNKNOWN"
        res["error_code"] = error_code
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    _auto_log_trade(
        symbol=req.symbol,
        side=req.side,
        action="pending_entry",
        intent_id=req.intent_id,
        session_id=req.session_id,
        strategy_id=req.strategy_id,
        entry_price=req.price,
        volume_lots=req.volume_lots,
        sl=req.sl,
        tp=req.tp,
        message=f"Pending {req.kind} order placed via HTTP @ {req.price}",
    )
    return res


@app.post("/tools/cancel_order", response_model=dict)
def tool_cancel_order(req: CancelOrderRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)
    params: dict[str, object] = {"type": "cancel_order", "order_id": req.order_id}

    # Add ownership fields when provided
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("cancel_order", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/calculate_position_size", response_model=dict)
def tool_calculate_position_size(req: PositionSizeRequest) -> dict:
    symbol_info = resource_symbol_info(req.symbol)
    account = resource_account_summary()
    result = calculate_position_size(
        equity=req.equity
        if req.equity is not None
        else float(account.equity or account.balance or 0.0),
        risk_percent=req.risk_percent,
        entry_price=req.entry_price,
        stop_loss_price=req.stop_loss_price,
        tick_size=float(symbol_info.tick_size or 0.0),
        tick_value=float(symbol_info.tick_value or 0.0),
        volume_min=float(symbol_info.volume_min or 0.0),
        volume_max=float(symbol_info.volume_max or 0.0),
        volume_step=float(symbol_info.volume_step or 0.0),
    )
    return {"symbol": req.symbol, **result}


@app.post("/tools/validate_trade_setup", response_model=dict)
def tool_validate_trade_setup(req: ValidateTradeSetupRequest) -> dict:
    symbol_info = resource_symbol_info(req.symbol)
    account = resource_account_summary()
    book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
    bid, ask = _first_bid_ask(book)
    if bid is None or ask is None:
        return {"valid": False, "errors": ["market price unavailable"], "warnings": []}

    margin_estimate = get_gateway().estimate_margin(
        MarginEstimateRequest(
            symbol=req.symbol,
            side=req.side,
            volume_lots=req.volume_lots,
            price_hint=req.entry_price,
        )
    )
    result = validate_trade_setup(
        symbol_info=symbol_info.model_dump(),
        account_summary=account.model_dump(),
        side=req.side,
        order_kind=req.order_kind,
        volume_lots=req.volume_lots,
        current_bid=bid,
        current_ask=ask,
        entry_price=req.entry_price,
        sl=req.sl,
        tp=req.tp,
        required_margin=margin_estimate.required_margin,
    )

    # Correlation warning: check existing positions for same-symbol or correlated exposure
    positions_data = resource_positions_open()
    positions = positions_data.get("positions", [])
    same_symbol_positions = [
        p for p in positions if p.get("symbol", "").upper() == req.symbol.upper()
    ]
    same_symbol_count = len(same_symbol_positions)

    correlated_positions = []
    existing_symbols = set(
        p.get("symbol", "").upper().replace("M", "").replace("m", "")
        for p in positions
        if p.get("symbol", "").upper() != req.symbol.upper()
    )
    req_sym_clean = req.symbol.upper().replace("M", "").replace("m", "")
    for sym in existing_symbols:
        corr = PortfolioRiskService.CORRELATION_MATRIX.get(req_sym_clean, {}).get(
            sym, 0
        )
        if abs(corr) > 0.7:
            matching = [
                p
                for p in positions
                if p.get("symbol", "").upper().replace("M", "").replace("m", "") == sym
            ]
            total_vol = sum(float(p.get("volume", 0)) for p in matching)
            correlated_positions.append(
                {
                    "symbol": sym,
                    "correlation": round(corr, 4),
                    "existing_volume": round(total_vol, 2),
                }
            )
    req_sym_clean = req.symbol.upper().replace("M", "").replace("m", "")
    for sym in existing_symbols:
        corr = (
            compute_correlation_matrix({req_sym_clean: [], sym: []})
            .get(req_sym_clean, {})
            .get(sym, 0)
            if req_sym_clean in compute_correlation_matrix({req_sym_clean: [], sym: []})
            else 0
        )
        if abs(corr) > 0.7:
            matching = [
                p
                for p in positions
                if p.get("symbol", "").upper().replace("M", "").replace("m", "") == sym
            ]
            total_vol = sum(float(p.get("volume", 0)) for p in matching)
            correlated_positions.append(
                {
                    "symbol": sym,
                    "correlation": round(corr, 4),
                    "existing_volume": round(total_vol, 2),
                }
            )

    has_exposure = same_symbol_count > 0 or len(correlated_positions) > 0
    warning_msg = None
    if same_symbol_count > 0:
        warning_msg = f"Already have {same_symbol_count} open position(s) on {req.symbol} — concentrated exposure"
    elif correlated_positions:
        cp = correlated_positions[0]
        warning_msg = f"High correlation ({cp['correlation']:.2f}) with open {cp['symbol']} position"

    correlation_warning = {
        "has_exposure": has_exposure,
        "same_symbol_positions": same_symbol_count,
        "correlated_positions": correlated_positions,
        "warning": warning_msg,
    }

    if warning_msg:
        if "warnings" not in result:
            result["warnings"] = []
        result["warnings"].append(warning_msg)

    return {
        "symbol": req.symbol,
        "bid": bid,
        "ask": ask,
        "required_margin": margin_estimate.required_margin,
        "correlation_warning": correlation_warning,
        **result,
    }


@app.post("/tools/trail_position", response_model=dict)
def tool_trail_position(req: TrailPositionRequest) -> dict:
    positions_data = resource_positions_open()
    positions = [Position(**p) for p in positions_data.get("positions", [])]
    position = next(
        (item for item in positions if item.position_id == req.position_id), None
    )
    if position is None:
        return {"status": "error", "error": "position_not_found"}

    info = resource_symbol_info(denormalize_symbol(position.symbol))
    point = float(info.point or 0.0)
    if point <= 0 or position.mark_price is None:
        return {"status": "error", "error": "price_context_unavailable"}

    distance = req.distance_points * point
    lock_in = req.lock_in_points * point
    book = tool_get_order_book(
        OrderBookRequest(symbol=denormalize_symbol(position.symbol))
    )
    bid, ask = _first_bid_ask(book)
    if bid is None or ask is None:
        return {"status": "error", "error": "price_context_unavailable"}

    if position.side == "buy":
        new_sl = ask - distance
        if lock_in > 0:
            new_sl = max(new_sl, position.entry_price + lock_in)
        new_sl = min(new_sl, bid - point)
    else:
        new_sl = bid + distance
        if lock_in > 0:
            new_sl = min(new_sl, position.entry_price - lock_in)
        new_sl = max(new_sl, ask + point)

    result = tool_modify_position_sl_tp(
        ModifyPositionSLTPRequest(
            position_id=req.position_id,
            sl=new_sl,
            tp=position.tp,
        )
    )
    return {"computed_sl": new_sl, "result": result}


@app.post("/tools/volatility_profile", response_model=dict)
def tool_volatility_profile(req: VolatilityProfileRequest) -> dict:
    bars = tool_get_bars(
        BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
    )
    atr_data = tool_get_indicator(
        IndicatorRequest(
            symbol=req.symbol,
            timeframe=req.timeframe,
            indicator="atr",
            period=req.atr_period,
        )
    )
    atr_warning = None
    if atr_data.get("status") == "error":
        atr_warning = "ATR unavailable"
        logger.warning(
            f"ATR request failed for {req.symbol}: {atr_data.get('message', 'unknown error')}"
        )
        atr_value = 0.0
    elif "value" in atr_data and atr_data["value"]:
        atr_value = float(atr_data["value"])
    elif "data" in atr_data and atr_data["data"]:
        atr_value = float(atr_data["data"][-1])
    else:
        atr_value = 0.0
    result = build_volatility_profile(
        symbol=req.symbol,
        timeframe=req.timeframe,
        bars=[bar.model_dump() for bar in bars.data],
        atr_value=atr_value,
    )
    if atr_warning:
        result["warning"] = atr_warning
    return result


@app.post("/tools/analysis/divergence", response_model=dict)
def tool_divergence_detection(req: DivergenceRequest) -> dict:
    try:
        # Fetch only as many bars as needed (lookback), not an arbitrary count=200
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
        )
        result = detect_divergence(
            bars=[bar.model_dump() for bar in bars.data],
            lookback=req.lookback,
            macd_fast=req.macd_fast,
            macd_slow=req.macd_slow,
            macd_signal_period=req.macd_signal_period,
            rsi_period=req.rsi_period,
            swing_window=req.swing_window,
        )
        # Add symbol and timeframe context for consistency with other analysis tools
        result["symbol"] = req.symbol
        result["timeframe"] = req.timeframe
        return result
    except Exception as e:
        logger.error(f"Divergence detection failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/analysis/multi_bar_patterns", response_model=dict)
def tool_multi_bar_patterns(req: MultiBarPatternsRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
        )
        result = detect_multi_bar_patterns(
            bars=[bar.model_dump() for bar in bars.data],
            period=req.period,
            fib_lookback=req.fib_lookback,
        )
        result["symbol"] = req.symbol
        result["timeframe"] = req.timeframe
        return result
    except Exception as e:
        logger.error(f"Multi-bar pattern detection failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/analysis/volume_profile", response_model=dict)
def tool_volume_profile(req: VolumeProfileRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
        )
        has_volume = any(getattr(b, "tick_volume", 0) > 0 for b in bars.data)
        if not has_volume and bars.data:
            return {
                "symbol": req.symbol,
                "timeframe": req.timeframe,
                "status": "unavailable",
                "hint": (
                    f"Tick volume is unavailable for {req.symbol} on this broker/demo account. "
                    f"Volume analysis requires live market data. "
                    f"Support/resistance and price action analysis remain available via other tools."
                ),
                "bars_count": len(bars.data),
                "volume_points": 0,
            }
        from mt5_mcp.services.volume_analysis import detect_volume_anomalies

        result = detect_volume_anomalies(
            bars=[bar.model_dump() for bar in bars.data],
            lookback=req.lookback,
            symbol=req.symbol,
        )
        result["timeframe"] = req.timeframe
        return result
    except Exception as e:
        logger.error(f"Volume analysis failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/analysis/momentum", response_model=dict)
def tool_momentum_check(req: MomentumCheckRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
        )
        from mt5_mcp.services.momentum import compute_momentum_penalty

        result = compute_momentum_penalty(
            bars=[bar.model_dump() for bar in bars.data],
            rsi=req.rsi,
            atr=req.atr,
            lookback=req.lookback,
        )
        result["symbol"] = req.symbol
        result["timeframe"] = req.timeframe
        return result
    except Exception as e:
        logger.error(f"Momentum check failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/multi_timeframe_indicators", response_model=dict)
def tool_multi_timeframe_indicators(req: MultiTimeframeIndicatorRequest) -> dict:
    readings: dict[str, dict] = {}
    for timeframe in req.timeframes:
        readings[timeframe] = tool_get_indicator(
            IndicatorRequest(
                symbol=req.symbol,
                timeframe=timeframe,
                indicator=req.indicator,
                period=req.period,
                fast=req.fast,
                slow=req.slow,
                signal=req.signal,
                deviation=req.deviation,
                shift=req.shift,
                k_period=req.k_period,
                d_period=req.d_period,
                slowing=req.slowing,
                tenkan=req.tenkan,
                kijun=req.kijun,
                senkou=req.senkou,
                window=req.window,
            )
        )
    return {"symbol": req.symbol, "indicator": req.indicator, "readings": readings}


@app.post("/tools/correlation_matrix", response_model=dict)
def tool_correlation_matrix(req: CorrelationMatrixRequest) -> dict:
    close_series: dict[str, list[float]] = {}
    for symbol in req.symbols:
        bars = tool_get_bars(
            BarsRequest(symbol=symbol, timeframe=req.timeframe, count=req.lookback)
        )
        close_series[symbol] = [bar.close for bar in bars.data]
    return {
        "timeframe": req.timeframe,
        "lookback": req.lookback,
        "matrix": compute_correlation_matrix(close_series),
    }


class MarketStructureRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    swing_lookback: int = 5
    confirm_bos_pips: float = 0.0


@app.post("/tools/market/structure", response_model=dict)
def tool_market_structure(req: MarketStructureRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=100)
        )
        from mt5_mcp.services.market_structure import detect_market_structure

        result = detect_market_structure(
            bars=[
                {"high": b.high, "low": b.low, "close": b.close, "open": b.open}
                for b in bars.data
            ],
            swing_lookback=req.swing_lookback,
            confirm_bos_pips=req.confirm_bos_pips,
        )
        return {
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "structure": result.structure,
            "trend_health": result.trend_health,
            "swing_points": result.swing_points,
            "last_bos": result.last_bos,
            "last_choch": result.last_choch,
            "recent_structure": result.recent_structure,
        }
    except Exception as e:
        logger.error(f"Market structure failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class StrategySelectorRequest(BaseModel):
    regime: str | None = None


@app.post("/tools/strategy/selector", response_model=dict)
def tool_strategy_selector(req: StrategySelectorRequest | None = None) -> dict:
    from mt5_mcp.services.strategy_selector import list_strategies, select_strategy

    if req and req.regime:
        strategy = select_strategy(req.regime)
        return {
            "recommended": {
                "name": strategy.name,
                "regime": strategy.regime,
                "entry_style": strategy.entry_style,
                "stop_type": strategy.stop_type,
                "take_profit_type": strategy.take_profit_type,
                "max_positions": strategy.max_positions,
                "risk_multiplier": strategy.risk_multiplier,
                "trailing": strategy.trailing,
                "description": strategy.description,
            },
            "all_strategies": list_strategies(),
        }
    return {"all_strategies": list_strategies()}


class VWAPRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    std_dev_multiplier: float = 2.0


class VolumeAtPriceRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    num_bins: int = 20


@app.post("/tools/vwap", response_model=dict)
def tool_vwap(req: VWAPRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.bar_count)
        )
        from mt5_mcp.services.vwap import compute_vwap

        result = compute_vwap(
            bars=[
                {
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.tick_volume,
                }
                for b in bars.data
            ],
            std_dev_multiplier=req.std_dev_multiplier,
        )
        return {
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "current_vwap": result.current_vwap,
            "vwap_deviation_upper": result.vwap_deviation_upper,
            "vwap_deviation_lower": result.vwap_deviation_lower,
            "distance_from_vwap_pct": result.distance_from_vwap_pct,
            "price_position": result.price_position,
        }
    except Exception as e:
        logger.error(f"VWAP failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tools/volume_at_price", response_model=dict)
def tool_volume_at_price(req: VolumeAtPriceRequest) -> dict:
    try:
        bars = tool_get_bars(
            BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.bar_count)
        )
        from mt5_mcp.services.vwap import compute_volume_at_price

        result = compute_volume_at_price(
            bars=[
                {"high": b.high, "low": b.low, "volume": b.tick_volume}
                for b in bars.data
            ],
            num_bins=req.num_bins,
        )
        return {
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "poc": result.poc,
            "value_area_high": result.value_area_high,
            "value_area_low": result.value_area_low,
            "value_area_width": result.value_area_width,
            "current_price_position": result.current_price_position,
            "distribution": result.price_distribution,
        }
    except Exception as e:
        logger.error(f"Volume-at-Price failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class SetupProbabilityRequest(BaseModel):
    symbol: str | None = None
    regime: str | None = None
    session: str | None = None
    min_samples: int = 5


@app.post("/tools/setup_probability", response_model=dict)
def tool_setup_probability(req: SetupProbabilityRequest) -> dict:
    try:
        journal = get_journal_db()
        trades = journal.query(
            symbol=req.symbol,
            limit=500,
        )
        from mt5_mcp.services.setup_probability import estimate_setup_probability

        result = estimate_setup_probability(
            trades=trades,
            current_regime=req.regime,
            current_session=req.session,
            current_symbol=req.symbol,
            min_samples=req.min_samples,
        )
        return {
            "estimated_win_rate": result.estimated_win_rate,
            "sample_size": result.sample_size,
            "confidence": result.confidence,
            "recommendation": result.recommendation,
            "win_rate_by_regime": result.win_rate_by_regime,
            "win_rate_by_session": result.win_rate_by_session,
            "common_mistakes": result.common_mistakes,
            "recent_similar_trades": result.similar_trades,
        }
    except Exception as e:
        logger.error(f"Setup probability failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class SupportResistanceRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    lookback: int = 100


@app.post("/tools/support_resistance", response_model=dict)
def tool_support_resistance(req: SupportResistanceRequest) -> dict:
    bars = tool_get_bars(
        BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
    )
    if not bars.data:
        return {
            "symbol": req.symbol,
            "support_levels": [],
            "resistance_levels": [],
        }

    highs = [b.high for b in bars.data]
    lows = [b.low for b in bars.data]
    closes = [b.close for b in bars.data]

    support = sorted(lows)[:3]
    resistance = sorted(highs, reverse=True)[:3]

    return {
        "symbol": denormalize_symbol(req.symbol),
        "timeframe": req.timeframe,
        "lookback": req.lookback,
        "support_levels": support,
        "resistance_levels": resistance,
        "current_price": closes[-1] if closes else None,
    }


# Tools


@app.post("/tools/submit_market_order", response_model=ExecutionResult)
def tool_submit_market_order(req: TradeIntent) -> ExecutionResult:
    return tool_submit_market_order_via_bridge(req)


# ============================================================
# Market Regime Detection
# ============================================================


@app.post("/tools/market/regime", response_model=dict)
def tool_market_regime(req: MarketRegimeRequest) -> dict:
    """Detect current market regime (ranging, trending, compressing).

    Returns regime classification with strategy hints.
    """
    # Fetch bars and ATR via existing tools
    bars_result = tool_get_bars(
        BarsRequest(symbol=req.symbol, timeframe=req.timeframe, count=req.lookback)
    )
    atr_result = tool_get_indicator(
        IndicatorRequest(
            symbol=req.symbol,
            timeframe=req.timeframe,
            indicator="atr",
            period=req.atr_period,
        )
    )

    # Parse ATR with error detection and data fallback
    atr_value = 0.0
    if atr_result.get("status") == "error":
        from mt5_mcp.observability.logging import logger

        logger.warning(
            f"ATR request failed for {req.symbol}: {atr_result.get('message', 'unknown error')}"
        )
    elif "value" in atr_result and atr_result["value"]:
        atr_value = float(atr_result["value"])
    elif "data" in atr_result and atr_result["data"]:
        atr_value = float(atr_result["data"][-1])
    bars_data = [bar.model_dump() for bar in bars_result.data]

    # Try to get EMA for trend direction
    ema_result = tool_get_indicator(
        IndicatorRequest(
            symbol=req.symbol, timeframe=req.timeframe, indicator="ema", period=20
        )
    )
    ema_slow_result = tool_get_indicator(
        IndicatorRequest(
            symbol=req.symbol, timeframe=req.timeframe, indicator="ema", period=50
        )
    )

    ema_fast = ema_result.get("value")
    ema_slow = ema_slow_result.get("value")

    regime = detect_regime(
        bars=bars_data,
        atr_value=atr_value,
        atr_period=req.atr_period,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
    )

    return {"symbol": req.symbol, **regime}


@app.post("/tools/market/scan", response_model=dict)
def tool_market_scan(req: MarketScanRequest) -> dict:
    """Multi-symbol market scan — returns price, ATR, and regime for all symbols.

    OPTIMIZED: Uses batched bridge commands instead of sequential per-symbol calls.
    All symbols' data is fetched in parallel, then analyzed locally.
    """
    symbol_norms = [normalize_symbol(s) for s in req.symbols]

    # Build batch commands: for each symbol, get bars + ATR + order book
    commands = []
    for sym in symbol_norms:
        commands.append(
            {"type": "get_bars", "symbol": sym, "timeframe": req.timeframe, "count": 20}
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "atr",
                "period": req.atr_period,
            }
        )
        commands.append({"type": "get_order_book", "symbol": sym})

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=30.0)
    except Exception as e:
        return {
            "symbols": {s: {"error": str(e)} for s in req.symbols},
            "timeframe": req.timeframe,
        }

    results = {}
    for i, sym in enumerate(req.symbols):
        try:
            bars_result = batch_results[i * 3]
            atr_result = batch_results[i * 3 + 1]
            book_result = batch_results[i * 3 + 2]

            # Parse ATR
            atr_value = 0.0
            if atr_result.get("status") == "completed":
                payload = atr_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        parsed = json.loads(payload)
                        atr_value = float(parsed.get("value", 0) or 0)
                        if atr_value == 0 and "data" in parsed and parsed["data"]:
                            atr_value = float(parsed["data"][-1])
                    except Exception:
                        pass
                elif isinstance(payload, dict):
                    atr_value = float(payload.get("value", 0) or 0)
                    if atr_value == 0 and "data" in payload and payload["data"]:
                        atr_value = float(payload["data"][-1])

            # Parse order book
            bid, ask = None, None
            if book_result.get("status") == "completed":
                payload = book_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        book_data = json.loads(payload)
                    except Exception:
                        book_data = {}
                else:
                    book_data = payload
                bid, ask = _first_bid_ask(book_data)

            # Parse bars for regime
            bars_data = []
            if bars_result.get("status") == "completed":
                payload = bars_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        bars_data = json.loads(payload).get("data", [])
                    except Exception:
                        pass
                elif isinstance(payload, dict):
                    bars_data = payload.get("data", [])

            # Regime detection (local, no bridge call)
            regime = detect_regime(bars=bars_data, atr_value=atr_value)

            results[sym] = {
                "bid": bid,
                "ask": ask,
                "atr": atr_value,
                "regime": regime.get("regime", "unknown"),
                "recommendation": regime.get("recommendation", "unknown"),
            }
        except Exception as e:
            results[sym] = {"error": str(e)}

    # Synthesize next_action_guidance from all scanned symbols
    if results:
        bullish_count = sum(
            1 for s in results.values() if s.get("regime") == "trending_up"
        )
        bearish_count = sum(
            1 for s in results.values() if s.get("regime") == "trending_down"
        )
        ranging_count = sum(1 for s in results.values() if s.get("regime") == "ranging")
        compressing_count = sum(
            1 for s in results.values() if s.get("regime") == "compressing"
        )
        total = len(results)

        # Determine overall bias
        if bullish_count > total * 0.5:
            bias = "broadly_bullish"
        elif bearish_count > total * 0.5:
            bias = "broadly_bearish"
        elif compressing_count > total * 0.4:
            bias = "consolidation_phase"
        elif ranging_count > total * 0.5:
            bias = "range_bound"
        else:
            bias = "mixed"

        # Find symbols worth alert-level monitoring (compressing = potential breakout)
        alert_symbols = [
            sym for sym, data in results.items() if data.get("regime") == "compressing"
        ]

        # Recommend next scan time based on market state
        if compressing_count > 0:
            next_scan_seconds = 600  # 10 min — breakouts can happen fast
        elif bias in ("broadly_bullish", "broadly_bearish"):
            next_scan_seconds = 900  # 15 min — trending, stable
        else:
            next_scan_seconds = 600  # 10 min default

        guidance = {
            "market_bias": bias,
            "regime_distribution": {
                "trending_up": bullish_count,
                "trending_down": bearish_count,
                "ranging": ranging_count,
                "compressing": compressing_count,
            },
            "next_scan_recommended_seconds": next_scan_seconds,
            "alert_symbols": alert_symbols,
            "action_hint": "set_bracket_orders"
            if compressing_count > 0
            else "wait_for_pullback"
            if bias in ("broadly_bullish", "broadly_bearish")
            else "scan_again_later",
        }
    else:
        guidance = None

    return {
        "symbols": results,
        "timeframe": req.timeframe,
        "next_action_guidance": guidance,
    }


# ============================================================
# Symbol Snapshot — One-Call Market Context
# ============================================================


@app.post("/tools/market/snapshot", response_model=dict)
def tool_market_snapshot(req: SnapshotRequest) -> dict:
    """Complete market snapshot for a symbol in a single call.

    Replaces 5+ separate API calls (bars, indicators, order book,
    symbol info, coaching) with one authoritative snapshot payload.

    OPTIMIZED: Uses batched bridge commands in a single round-trip,
    then assembles the snapshot locally via SymbolSnapshotService.
    """
    symbol = req.symbol
    symbol_norm = normalize_symbol(symbol)

    commands = [
        {
            "type": "get_bars",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "count": req.bar_count,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "atr",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "rsi",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "ema",
            "period": 20,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "ema",
            "period": 50,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "macd",
            "fast": 12,
            "slow": 26,
            "signal": 9,
        },
        {"type": "get_order_book", "symbol": symbol_norm},
        {"type": "get_symbol_info", "symbol": symbol_norm},
        {"type": "get_positions", "symbol": symbol_norm},
    ]

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=30.0)
    except Exception as e:
        return {"symbol": symbol, "error": f"Batch fetch failed: {e}"}

    bars_result = batch_results[0]
    atr_result = batch_results[1]
    rsi_result = batch_results[2]
    ema20_result = batch_results[3]
    ema50_result = batch_results[4]
    macd_result = batch_results[5]
    book_result = batch_results[6]
    symbol_info_result = batch_results[7]
    positions_result = batch_results[8]

    atr_value = _parse_indicator_value(atr_result)
    rsi = _parse_indicator_value(rsi_result)
    ema_fast = _parse_indicator_value(ema20_result)
    ema_slow = _parse_indicator_value(ema50_result)

    macd_data = None
    if macd_result.get("status") == "completed":
        payload = macd_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                macd_data = json.loads(payload)
            except Exception:
                pass
        elif isinstance(payload, dict):
            macd_data = payload

    bid, ask = None, None
    book_data = _parse_payload_dict(book_result)
    if book_data:
        bid, ask = _first_bid_ask(book_data)

    symbol_info_data = _parse_payload_dict(symbol_info_result)
    if "symbol" in symbol_info_data:
        symbol_info_data["symbol"] = denormalize_symbol(symbol_info_data["symbol"])

    bars_data = []
    if bars_result.get("status") == "completed":
        payload = bars_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                bars_data = json.loads(payload).get("data", [])
            except Exception:
                pass
        elif isinstance(payload, dict):
            bars_data = payload.get("data", [])

    positions_data = []
    if positions_result.get("status") == "completed":
        payload = positions_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        positions_list = (
            payload.get("positions", []) if isinstance(payload, dict) else []
        )
        for p in positions_list:
            if p.get("symbol"):
                p["symbol"] = denormalize_symbol(p["symbol"])
            positions_data.append(p)

    snapshot_svc = SymbolSnapshotService(
        coach=TradingCoach(),
        reconciliation_service=None,
    )

    return snapshot_svc.build(
        symbol=symbol,
        timeframe=req.timeframe,
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
        include_coaching=req.include_coaching,
        session_id=req.session_id,
        strategy_id=req.strategy_id,
    )


# ============================================================
# Opportunity Ranking — Multi-Symbol Trade-Readiness
# ============================================================


@app.post("/tools/market/opportunity_rank", response_model=dict)
def tool_market_opportunity_rank(req: OpportunityRankRequest) -> dict:
    """Rank symbols by trade-readiness across 7 weighted factors.

    Returns a ranked list of symbols with composite scores, individual
    factor scores, and skip reasons for symbols below threshold.

    Factors: regime clarity, spread/ATR, volatility usability, session
    quality, indicator confluence, portfolio overlap, calendar events.
    """
    if not req.symbols:
        return {"rankings": [], "total_symbols": 0, "tradeable": 0}

    symbol_norms = [normalize_symbol(s) for s in req.symbols]

    # Build batch commands: for each symbol, get bars + ATR + indicators + order book
    commands = []
    for sym in symbol_norms:
        commands.append(
            {"type": "get_bars", "symbol": sym, "timeframe": req.timeframe, "count": 20}
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "atr",
                "period": 14,
            }
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "rsi",
                "period": 14,
            }
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "ema",
                "period": 20,
            }
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "ema",
                "period": 50,
            }
        )
        commands.append(
            {
                "type": "get_indicator",
                "symbol": sym,
                "timeframe": req.timeframe,
                "indicator": "macd",
                "fast": 12,
                "slow": 26,
                "signal": 9,
            }
        )
        commands.append({"type": "get_order_book", "symbol": sym})
        commands.append({"type": "get_positions"})

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=45.0)
    except Exception as e:
        return {
            "rankings": [],
            "total_symbols": len(req.symbols),
            "error": f"Batch fetch failed: {e}",
        }

    # Parse results into snapshots
    snapshots: dict[str, dict] = {}
    n_commands_per_symbol = (
        8  # bars, atr, rsi, ema20, ema50, macd, orderbook, positions
    )

    for i, sym in enumerate(req.symbols):
        sym_upper = sym.upper()
        base = i * n_commands_per_symbol

        try:
            bars_result = (
                batch_results[base + 0] if base + 0 < len(batch_results) else {}
            )
            atr_result = (
                batch_results[base + 1] if base + 1 < len(batch_results) else {}
            )
            rsi_result = (
                batch_results[base + 2] if base + 2 < len(batch_results) else {}
            )
            ema20_result = (
                batch_results[base + 3] if base + 3 < len(batch_results) else {}
            )
            ema50_result = (
                batch_results[base + 4] if base + 4 < len(batch_results) else {}
            )
            macd_result = (
                batch_results[base + 5] if base + 5 < len(batch_results) else {}
            )
            book_result = (
                batch_results[base + 6] if base + 6 < len(batch_results) else {}
            )
            positions_result = (
                batch_results[base + 7] if base + 7 < len(batch_results) else {}
            )

            atr_value = _parse_indicator_value(atr_result)
            rsi = _parse_indicator_value(rsi_result)
            ema_fast = _parse_indicator_value(ema20_result)
            ema_slow = _parse_indicator_value(ema50_result)

            macd_data = None
            if macd_result.get("status") == "completed":
                payload = macd_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        macd_data = json.loads(payload)
                    except Exception:
                        pass
                elif isinstance(payload, dict):
                    macd_data = payload

            bid, ask = None, None
            book_data = _parse_payload_dict(book_result)
            if book_data:
                bid, ask = _first_bid_ask(book_data)

            bars_data = []
            if bars_result.get("status") == "completed":
                payload = bars_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        bars_data = json.loads(payload).get("data", [])
                    except Exception:
                        pass
                elif isinstance(payload, dict):
                    bars_data = payload.get("data", [])

            positions_data = []
            if positions_result.get("status") == "completed":
                payload = positions_result.get("result", {}).get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                positions_list = (
                    payload.get("positions", []) if isinstance(payload, dict) else []
                )
                for p in positions_list:
                    if p.get("symbol"):
                        p["symbol"] = denormalize_symbol(p["symbol"])
                    positions_data.append(p)

            # Use snapshot service to build a consistent snapshot
            snapshot_svc = SymbolSnapshotService(
                coach=TradingCoach(),
                reconciliation_service=None,
            )
            snapshot = snapshot_svc.build(
                symbol=sym_upper,
                timeframe=req.timeframe,
                bars_data=bars_data,
                atr_value=atr_value,
                atr_percentile=None,
                rsi=rsi,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                macd=macd_data,
                order_book_data=book_data,
                bid=bid,
                ask=ask,
                symbol_info_data={},
                positions=positions_data,
                include_coaching=False,
                session_id=req.session_id,
                strategy_id=req.strategy_id,
            )
            snapshots[sym_upper] = snapshot

        except Exception:
            snapshots[sym_upper] = {}

    # Collect all positions for portfolio overlap detection
    all_positions: list[dict] = []
    try:
        all_pos_result = tool_get_positions()
        if isinstance(all_pos_result, dict):
            all_positions = all_pos_result.get("positions", [])
    except Exception:
        pass

    # Run ranking
    ranker = OpportunityRanker()
    rankings = ranker.rank(
        symbols=req.symbols,
        snapshots=snapshots,
        portfolio_positions=all_positions,
        min_score=req.min_score,
        weights=req.weights,
    )

    tradeable_count = sum(
        1 for r in rankings if r.get("recommendation") in ("trade", "watch")
    )

    return {
        "rankings": rankings,
        "total_symbols": len(req.symbols),
        "tradeable": tradeable_count,
        "timeframe": req.timeframe,
    }


# ============================================================
# Chart Intelligence — Unified Chart Analysis Bundle
# ============================================================


@app.post("/tools/market/chart_intelligence", response_model=dict)
def tool_chart_intelligence(req: ChartIntelligenceRequest) -> dict:
    """Unified chart intelligence: screenshot + S/R + indicators + patterns.

    Replaces 3+ separate calls (screenshot, support/resistance, indicators)
    with a single agent-friendly response bundle.

    OPTIMIZED: Uses batched bridge commands in a single round-trip,
    then assembles intelligence locally via ChartIntelligenceService.
    """
    symbol = req.symbol
    symbol_norm = normalize_symbol(symbol)

    commands: list[dict] = [
        {
            "type": "get_bars",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "count": req.bar_count,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "atr",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "rsi",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "ema",
            "period": 20,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "ema",
            "period": 50,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "macd",
            "fast": 12,
            "slow": 26,
            "signal": 9,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": req.timeframe,
            "indicator": "bbands",
            "period": 20,
        },
    ]

    if req.include_screenshot:
        commands.append(
            {
                "type": "get_chart_screenshot",
                "symbol": symbol_norm,
                "timeframe": req.timeframe,
                "width": req.width,
                "height": req.height,
            }
        )

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=30.0)
    except Exception as e:
        return {"symbol": symbol, "error": f"Batch fetch failed: {e}"}

    idx = 0
    bars_result = batch_results[idx]
    idx += 1
    atr_result = batch_results[idx]
    idx += 1
    rsi_result = batch_results[idx]
    idx += 1
    ema20_result = batch_results[idx]
    idx += 1
    ema50_result = batch_results[idx]
    idx += 1
    macd_result = batch_results[idx]
    idx += 1
    bbands_result = batch_results[idx]
    idx += 1
    screenshot_result = batch_results[idx] if req.include_screenshot else None

    atr_value = _parse_indicator_value(atr_result)
    rsi = _parse_indicator_value(rsi_result)
    ema_fast = _parse_indicator_value(ema20_result)
    ema_slow = _parse_indicator_value(ema50_result)

    macd_data = _parse_payload_dict(macd_result)
    bbands_data = _parse_payload_dict(bbands_result)

    bars_data = []
    if bars_result and bars_result.get("status") == "completed":
        payload = bars_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                bars_data = json.loads(payload).get("data", [])
            except Exception:
                pass
        elif isinstance(payload, dict):
            bars_data = payload.get("data", [])

    screenshot_data = None
    if req.include_screenshot and screenshot_result:
        ss_payload = _parse_payload_dict(screenshot_result)
        if ss_payload and ss_payload.get("image_base64"):
            screenshot_data = {
                "base64": ss_payload["image_base64"],
                "width": req.width,
                "height": req.height,
            }

    from mt5_mcp.services.chart_intelligence import ChartIntelligenceService

    svc = ChartIntelligenceService()
    return svc.get_intelligence(
        symbol=symbol,
        timeframe=req.timeframe,
        bars_data=bars_data,
        atr_value=atr_value,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        macd=macd_data if macd_data else None,
        bbands=bbands_data if bbands_data else None,
        screenshot_data=screenshot_data,
        include_screenshot_base64=req.include_screenshot_base64,
        bar_count=req.bar_count,
        session_id=req.session_id,
        strategy_id=req.strategy_id,
    )


# ============================================================
# Bracket Orders
# ============================================================


@app.post("/tools/place_bracket_order", response_model=BracketOrderResult)
def tool_place_bracket_order(req: BracketOrderRequest) -> BracketOrderResult:
    """Place paired BUY STOP + SELL STOP for breakout capture.

    When one order fills, the other is auto-cancelled.
    SL/TP are computed from ATR.
    """
    if is_frozen():
        return BracketOrderResult(
            status="error",
            message="Trading is frozen",
        )
    import uuid

    # Policy gate
    decision = get_policy().validate_submit_order(
        environment=get_settings_cached().environment,
    )
    if not decision.allowed:
        return BracketOrderResult(
            status="error",
            message=f"Policy blocked: {decision.reason}",
        )

    # Get ATR for SL/TP calculation
    try:
        atr_result = tool_get_indicator(
            IndicatorRequest(
                symbol=req.symbol, timeframe="H1", indicator="atr", period=14
            )
        )
        # Parse ATR with error detection and data fallback
        atr_value = 0.0
        if atr_result.get("status") == "error":
            from mt5_mcp.observability.logging import logger

            logger.warning(
                f"ATR request failed for {req.symbol}: {atr_result.get('message', 'unknown error')}"
            )
        elif "value" in atr_result and atr_result["value"]:
            atr_value = float(atr_result["value"])
        elif "data" in atr_result and atr_result["data"]:
            atr_value = float(atr_result["data"][-1])

        if atr_value <= 0:
            return BracketOrderResult(
                status="error",
                message="Could not determine ATR for SL/TP calculation",
            )

        # Compute SL/TP distances
        sl_distance = atr_value * req.sl_atr_multiplier
        tp_distance = atr_value * req.tp_atr_multiplier

        # BUY STOP order
        buy_sl = req.buy_trigger - sl_distance
        buy_tp = req.buy_trigger + tp_distance

        # SELL STOP order
        sell_sl = req.sell_trigger + sl_distance
        sell_tp = req.sell_trigger - tp_distance

        # Pre-flight validation for BUY STOP leg
        buy_validation = tool_validate_trade_setup(
            ValidateTradeSetupRequest(
                symbol=req.symbol,
                side="buy",
                order_kind="stop",
                volume_lots=req.volume_lots,
                entry_price=req.buy_trigger,
                sl=buy_sl,
                tp=buy_tp,
            )
        )
        if isinstance(buy_validation, dict) and not buy_validation.get("valid", True):
            errors = buy_validation.get("errors", [])
            return BracketOrderResult(
                status="error",
                message=f"BUY STOP leg validation failed: {', '.join(errors)}",
            )

        # Submit BUY STOP
        buy_req = SubmitPendingOrderRequest(
            symbol=req.symbol,
            side="buy",
            kind="stop",
            price=req.buy_trigger,
            volume_lots=req.volume_lots,
            sl=buy_sl,
            tp=buy_tp,
            session_id=getattr(req, "session_id", None),
            strategy_id=getattr(req, "strategy_id", None),
            intent_id=getattr(req, "intent_id", None),
            idempotency_key=f"bracket_buy_{getattr(req, 'intent_id', 'none')}_{uuid.uuid4().hex[:6]}",
            magic_number=getattr(req, "magic_number", None),
        )
        buy_result = tool_submit_pending_order(buy_req)

        buy_order_id = (
            buy_result.get("payload", {}).get("order")
            if isinstance(buy_result, dict)
            else None
        )

        # If BUY failed, return immediately (no rollback needed)
        if isinstance(buy_result, dict) and buy_result.get("status") == "error":
            buy_error = buy_result.get("error", "unknown")
            return BracketOrderResult(
                status="error",
                message=f"Bracket order failed — BUY STOP not placed: {buy_error}",
            )

        # BUY succeeded, now try SELL STOP
        sell_req = SubmitPendingOrderRequest(
            symbol=req.symbol,
            side="sell",
            kind="stop",
            price=req.sell_trigger,
            volume_lots=req.volume_lots,
            sl=sell_sl,
            tp=sell_tp,
            session_id=getattr(req, "session_id", None),
            strategy_id=getattr(req, "strategy_id", None),
            intent_id=getattr(req, "intent_id", None),
            idempotency_key=f"bracket_sell_{getattr(req, 'intent_id', 'none')}_{uuid.uuid4().hex[:6]}",
            magic_number=getattr(req, "magic_number", None),
        )
        sell_result = tool_submit_pending_order(sell_req)

        sell_order_id = (
            sell_result.get("payload", {}).get("order")
            if isinstance(sell_result, dict)
            else None
        )

        # If SELL failed, rollback the BUY order
        if isinstance(sell_result, dict) and sell_result.get("status") == "error":
            sell_error = sell_result.get("error", "unknown")
            rollback_status = ""
            if buy_order_id:
                try:
                    cancel_resp = tool_cancel_order(
                        CancelOrderRequest(order_id=str(buy_order_id))
                    )
                    cancel_ok = (
                        isinstance(cancel_resp, dict)
                        and cancel_resp.get("status") != "error"
                    )
                    rollback_status = (
                        f" Rollback: BUY order {buy_order_id} cancelled successfully."
                        if cancel_ok
                        else f" Rollback: BUY order {buy_order_id} cancellation also failed."
                    )
                except Exception as cancel_exc:
                    rollback_status = f" Rollback: BUY order {buy_order_id} cancellation failed ({cancel_exc})."
            return BracketOrderResult(
                status="error",
                message=(
                    f"Bracket order partially failed — BUY STOP placed (order {buy_order_id}) "
                    f"but SELL STOP failed: {sell_error}.{rollback_status}"
                ),
            )

        # Both succeeded — log to journal (non-fatal)
        try:
            journal = get_journal_db()
            import uuid as _uuid

            journal.log_decision(
                symbol=req.symbol,
                side="bracket",
                action="entry",
                entry_price=0.0,  # Not filled yet
                volume_lots=req.volume_lots,
                model_justification=req.rationale
                or f"Bracket order: buy@{req.buy_trigger}, sell@{req.sell_trigger}",
                session_id=f"bracket_{_uuid.uuid4().hex[:8]}",
                indicators_considered=["atr", "bracket_breakout"],
            )
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Bracket order journal failed (non-fatal): {e}")

        # Register bracket with EA-native OCO manager (non-fatal if EA unavailable)
        if buy_order_id and sell_order_id:
            try:
                import uuid as _bracket_uuid

                bracket_id = f"bracket_{req.symbol}_{_bracket_uuid.uuid4().hex[:8]}"
                ea_bracket_result = tool_ea_bracket_start(
                    EABracketStartRequest(
                        buy_order_ticket=str(buy_order_id),
                        sell_order_ticket=str(sell_order_id),
                        bracket_id=bracket_id,
                        comment=f"bracket:{bracket_id} breakout",
                        magic_filter=req.magic_number
                        if hasattr(req, "magic_number") and req.magic_number
                        else 0,
                        session_id=getattr(req, "session_id", None),
                        strategy_id=getattr(req, "strategy_id", None),
                        intent_id=getattr(req, "intent_id", None),
                    )
                )
                from mt5_mcp.observability.logging import logger

                if (
                    isinstance(ea_bracket_result, dict)
                    and ea_bracket_result.get("status") == "error"
                ):
                    logger.warning(
                        f"EA bracket registration failed for {bracket_id}: {ea_bracket_result.get('message', 'unknown')}. "
                        f"Agent must manually cancel orphan leg on fill."
                    )
                else:
                    logger.info(
                        f"Bracket {bracket_id} registered with EA-native OCO manager"
                    )
            except Exception as bracket_exc:
                from mt5_mcp.observability.logging import logger

                logger.warning(
                    f"EA bracket registration failed (non-fatal): {bracket_exc}"
                )

        return BracketOrderResult(
            buy_order_id=str(buy_order_id) if buy_order_id else None,
            sell_order_id=str(sell_order_id) if sell_order_id else None,
            status="placed",
            message=f"Bracket orders placed. BUY STOP @ {req.buy_trigger}, SELL STOP @ {req.sell_trigger}",
        )

    except Exception as exc:
        return BracketOrderResult(
            status="error",
            message=f"Unexpected error placing bracket order: {exc}",
        )


# ============================================================
# Phase 3: Trailing Stop (Server-Side)
# ============================================================


# In-memory trailing stop trackers
_trailing_stops: dict[str, dict] = {}


@app.post("/tools/set_trailing_stop", response_model=TrailingStopResult)
def tool_set_trailing_stop(req: SetTrailingStopRequest) -> TrailingStopResult:
    """Start server-side trailing stop for a position.

    The server will check price every check_interval_seconds and
    automatically trail the stop loss.
    """
    positions_data = resource_positions_open()
    positions = [Position(**p) for p in positions_data.get("positions", [])]
    position = next((p for p in positions if p.position_id == req.position_id), None)
    if position is None:
        return TrailingStopResult(
            position_id=req.position_id,
            status="error",
            message="Position not found",
        )

    info = resource_symbol_info(position.symbol)
    point = float(info.point or 0.0)
    if point <= 0:
        return TrailingStopResult(
            position_id=req.position_id,
            status="error",
            message="Could not determine point value",
        )

    # Get ATR for distance calculation
    atr_result = tool_get_indicator(
        IndicatorRequest(
            symbol=position.symbol, timeframe="H1", indicator="atr", period=14
        )
    )
    # Parse ATR with error detection and data fallback
    atr_value = 0.0
    if atr_result.get("status") == "error":
        from mt5_mcp.observability.logging import logger

        logger.warning(
            f"ATR request failed for {position.symbol}: {atr_result.get('message', 'unknown error')}"
        )
    elif "value" in atr_result and atr_result["value"]:
        atr_value = float(atr_result["value"])
    elif "data" in atr_result and atr_result["data"]:
        atr_value = float(atr_result["data"][-1])

    if atr_value <= 0:
        return TrailingStopResult(
            position_id=req.position_id,
            status="error",
            message="Could not determine ATR",
        )

    distance_points = int(atr_value * req.distance_atr_multiplier / point)
    lock_in_points = int(atr_value * req.lock_in_profit_after_atr / point)

    # Register trailing stop
    _trailing_stops[req.position_id] = {
        "position_id": req.position_id,
        "symbol": position.symbol,
        "side": position.side,
        "distance_points": distance_points,
        "lock_in_points": lock_in_points,
        "check_interval": req.check_interval_seconds,
        "initial_sl": position.sl,
        "active": True,
        "last_check": time.time(),
    }

    return TrailingStopResult(
        position_id=req.position_id,
        status="active",
        message=f"Trailing stop active: distance={distance_points}pts, lock_in={lock_in_points}pts",
        initial_sl=position.sl,
    )


@app.post("/tools/trailing_stop/cancel", response_model=dict)
def tool_trailing_stop_cancel(position_id: str = Body(..., embed=True)) -> dict:
    """Cancel an active trailing stop."""
    if position_id in _trailing_stops:
        _trailing_stops[position_id]["active"] = False
        del _trailing_stops[position_id]
        return {"status": "cancelled", "position_id": position_id}
    return {"status": "not_found", "position_id": position_id}


@app.post("/tools/trailing_stop/list", response_model=dict)
def tool_trailing_stop_list() -> dict:
    """List all active trailing stops."""
    active = {
        pid: {k: v for k, v in ts.items() if k != "last_check"}
        for pid, ts in _trailing_stops.items()
        if ts.get("active")
    }
    return {"active_stops": active, "count": len(active)}


# ============================================================
# EA-Native ATR Trailing Stop (Phase 3)
# ============================================================


@app.post("/tools/ea_trailing/start", response_model=dict)
def tool_ea_trailing_start(req: EATrailingStartRequest) -> dict:
    """Start EA-native ATR trailing stop.

    Unlike /tools/set_trailing_stop, this runs inside the EA process,
    surviving MCP/gateway instability.
    """
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)

    if req.atr_multiplier < 0.5 or req.atr_multiplier > 5.0:
        return {
            "status": "error",
            "error": "atr_multiplier must be between 0.5 and 5.0",
        }

    params: dict[str, object] = {
        "type": "trailing_start",
        "ticket": req.ticket,
        "atr_multiplier": req.atr_multiplier,
        "check_interval": req.check_interval_seconds,
        "lock_in_profit_atr": req.lock_in_profit_atr,
    }
    if req.magic_filter:
        params["magic_filter"] = req.magic_filter
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("trailing_start", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_trailing/stop", response_model=dict)
def tool_ea_trailing_stop(req: EATrailingStopRequest) -> dict:
    """Stop EA-native trailing for a position."""
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)

    params: dict[str, object] = {
        "type": "trailing_stop",
        "ticket": req.ticket,
    }
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id

    tcp_result = _tcp_send_and_await("trailing_stop", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_trailing/list", response_model=dict)
def tool_ea_trailing_list() -> dict:
    """List all active EA-native trailing stops."""
    params: dict[str, object] = {"type": "trailing_list"}

    tcp_result = _tcp_send_and_await("trailing_list", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_trailing/tick", response_model=dict)
def tool_ea_trailing_tick() -> dict:
    """Manually trigger a trailing stop check cycle."""
    params: dict[str, object] = {"type": "trailing_tick"}

    tcp_result = _tcp_send_and_await("trailing_tick", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


# --- EA-Native Bracket/OCO Management ---


@app.post("/tools/ea_bracket/start", response_model=dict)
def tool_ea_bracket_start(req: EABracketStartRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)

    params: dict[str, object] = {
        "type": "bracket_start",
        "buy_order_ticket": req.buy_order_ticket,
        "sell_order_ticket": req.sell_order_ticket,
        "bracket_id": req.bracket_id,
    }
    if req.comment:
        params["comment"] = req.comment
    if req.magic_filter:
        params["magic_filter"] = req.magic_filter
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id
    if req.intent_id:
        params["intent_id"] = req.intent_id
    if req.idempotency_key:
        params["idempotency_key"] = req.idempotency_key

    tcp_result = _tcp_send_and_await("bracket_start", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_bracket/stop", response_model=dict)
def tool_ea_bracket_stop(req: EABracketStopRequest) -> dict:
    if req.intent_id:
        from mt5_mcp.observability.logging import set_intent_id

        set_intent_id(req.intent_id)

    params: dict[str, object] = {
        "type": "bracket_stop",
        "bracket_id": req.bracket_id,
    }
    if req.session_id:
        params["session_id"] = req.session_id
    if req.strategy_id:
        params["strategy_id"] = req.strategy_id

    tcp_result = _tcp_send_and_await("bracket_stop", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_bracket/list", response_model=dict)
def tool_ea_bracket_list() -> dict:
    params: dict[str, object] = {"type": "bracket_list"}

    tcp_result = _tcp_send_and_await("bracket_list", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


@app.post("/tools/ea_bracket/tick", response_model=dict)
def tool_ea_bracket_tick() -> dict:
    params: dict[str, object] = {"type": "bracket_tick"}

    tcp_result = _tcp_send_and_await("bracket_tick", params)
    if tcp_result and tcp_result.get("status") == "completed":
        return tcp_result.get("result", {})

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params=params,
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id)
    if res.get("status") == "error":
        return {**res, "error": _error_payload(res.get("error"))}
    return res


# ============================================================
# Phase 4: Price Alert (Long-Polling)
# ============================================================


@app.post("/resources/market/wait_for_price", response_model=PriceAlertResult)
async def tool_wait_for_price(req: PriceAlertRequest) -> PriceAlertResult:
    import asyncio
    import logging

    logger = logging.getLogger("mt5_mcp.wait_for_price")

    timeout = max(5, min(3600, req.timeout_seconds))
    end_time = asyncio.get_event_loop().time() + timeout

    previous_price: float | None = None

    while asyncio.get_event_loop().time() < end_time:
        try:
            book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
            bid, ask = _first_bid_ask(book)

            if bid is None or ask is None:
                await asyncio.sleep(1)
                continue

            if req.condition == "above":
                current = ask
                triggered = current >= req.price
            elif req.condition == "below":
                current = bid
                triggered = current <= req.price
            else:
                mid = (bid + ask) / 2
                current = mid
                if previous_price is not None:
                    triggered = (
                        previous_price < req.price and current >= req.price
                    ) or (previous_price >= req.price and current < req.price)
                else:
                    previous_price = current
                    triggered = False

            if triggered:
                return PriceAlertResult(
                    symbol=req.symbol,
                    condition=req.condition,
                    trigger_price=req.price,
                    actual_price=current,
                    triggered=True,
                )

            if req.condition == "crosses":
                previous_price = current

        except Exception as e:
            logger.warning("wait/price: error polling price for %s: %s", req.symbol, e)

        await asyncio.sleep(1)

    try:
        book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
        bid, ask = _first_bid_ask(book)
        current = (bid + ask) / 2 if bid and ask else 0
    except Exception:
        current = 0

    return PriceAlertResult(
        symbol=req.symbol,
        condition=req.condition,
        trigger_price=req.price,
        actual_price=current,
        triggered=False,
        timed_out=True,
    )


# ============================================================
# Metacognition & AI Reasoning — Decision Journal
# ============================================================


@app.post("/tools/trading/log_decision")
def tool_log_trade_decision(req: TradeDecisionLogRequest) -> dict:
    try:
        try:
            journal = get_journal_db()
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal DB unavailable: {e}")
            return {"status": "error", "message": f"Journal unavailable: {e}"}

        decision_id = req.decision_id

        try:
            if decision_id:
                updated = journal.update_decision(
                    decision_id,
                    exit_price=req.exit_price,
                    pnl=req.pnl,
                    outcome=req.outcome,
                    lesson_learned=req.lesson_learned,
                    would_do_differently=req.would_do_differently,
                    mistake_category=req.mistake_category,
                    quality_rating=req.quality_rating,
                    emotional_self_report=req.emotional_self_report,
                    model_justification=req.model_justification,
                )
                return {
                    "status": "updated" if updated else "not_found",
                    "decision_id": decision_id,
                }

            decision_id = journal.log_decision(
                symbol=req.symbol,
                side=req.side,
                action=req.action,
                entry_price=req.entry_price,
                exit_price=req.exit_price,
                sl=req.sl,
                tp=req.tp,
                volume_lots=req.volume_lots,
                pnl=req.pnl,
                session_id=req.session_id,
                regime=req.regime,
                atr_value=req.atr_value,
                atr_percent_of_price=req.atr_percent_of_price,
                rsi_value=req.rsi_value,
                indicator_snapshot=req.indicator_snapshot,
                model_justification=req.model_justification,
                indicators_considered=req.indicators_considered,
                confidence_level=req.confidence_level,
                risk_assessment=req.risk_assessment,
                emotional_self_report=req.emotional_self_report,
                alternatives_considered=req.alternatives_considered,
                expected_duration=req.expected_duration,
                expected_move_points=req.expected_move_points,
                outcome=req.outcome,
                lesson_learned=req.lesson_learned,
                would_do_differently=req.would_do_differently,
                mistake_category=req.mistake_category,
                quality_rating=req.quality_rating,
                note=req.note,
            )

            return {
                "status": "logged",
                "decision_id": decision_id,
                "message": "Decision logged. Use this ID to update with outcome later.",
            }
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal operation failed: {e}")
            return {"status": "error", "message": f"Failed to log decision: {e}"}
    except Exception as e:
        from mt5_mcp.observability.logging import logger

        logger.error(f"tool_log_trade_decision failed: {e}")
        return {"status": "error", "message": f"tool_log_trade_decision failed: {e}"}


@app.post("/tools/trading/reflect")
def tool_reflect_on_trades(req: TradeJournalReflectionRequest) -> dict:
    try:
        try:
            journal = get_journal_db()
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal DB unavailable for reflection: {e}")
            return {"count": 0, "decisions": [], "warning": f"Journal unavailable: {e}"}

        try:
            decisions = journal.query(
                symbol=req.symbol,
                outcome=req.outcome,
                regime=req.regime,
                emotional_self_report=req.emotional_self_report,
                mistake_category=req.mistake_category,
                action=req.action,
                limit=req.limit,
            )
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal query failed: {e}")
            return {"count": 0, "decisions": [], "warning": f"Query failed: {e}"}

        for d in decisions:
            if d.get("indicator_snapshot"):
                try:
                    d["indicator_snapshot"] = json.loads(d["indicator_snapshot"])
                except Exception:
                    pass
            if d.get("indicators_considered"):
                try:
                    d["indicators_considered"] = json.loads(d["indicators_considered"])
                except Exception:
                    pass

        return {
            "count": len(decisions),
            "decisions": decisions,
        }
    except Exception as e:
        from mt5_mcp.observability.logging import logger

        logger.error(f"tool_reflect_on_trades failed: {e}")
        return {"status": "error", "message": f"tool_reflect_on_trades failed: {e}"}


@app.post("/tools/trading/insights")
def tool_trading_insights(lookback_days: int = Body(7, embed=True)) -> dict:
    try:
        try:
            journal = get_journal_db()
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal DB unavailable for insights: {e}")
            return {"warning": f"Journal unavailable: {e}"}

        try:
            insights = journal.get_reflection_insights(lookback_days=lookback_days)
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Journal insights failed: {e}")
            return {"warning": f"Insights generation failed: {e}"}

        # Add AI-actionable guidance
        guidance = []
        if insights.get("win_rate_by_emotional_state"):
            for state, data in insights["win_rate_by_emotional_state"].items():
                if data["win_rate"] < 30:
                    guidance.append(
                        f"⚠️ When you feel '{state}', your win rate is only {data['win_rate']}%. "
                        f"Consider stepping back or reducing position size."
                    )

        if insights.get("mistake_frequency"):
            top_mistake = insights["mistake_frequency"][0]
            guidance.append(
                f"📌 Your most common mistake: {top_mistake['category']} "
                f"({top_mistake['count']} times). Be conscious of this today."
            )

        if insights.get("overall"):
            overall = insights["overall"]
            if overall["win_rate"] < 40:
                guidance.append(
                    f"📊 Your recent win rate is {overall['win_rate']}%. "
                    "Consider reducing trade frequency and focusing on higher-conviction setups."
                )
            elif overall["win_rate"] > 60:
                guidance.append(
                    f"📊 Your recent win rate is {overall['win_rate']}%. "
                    "You're in good form. Stick to your process."
                )

        insights["ai_guidance"] = guidance
        return insights
    except Exception as e:
        from mt5_mcp.observability.logging import logger

        logger.error(f"tool_trading_insights failed: {e}")
        return {"status": "error", "message": f"tool_trading_insights failed: {e}"}


# ============================================================
# Trading Context — Symbol Education
# ============================================================
# Trading Context — Live Market-Derived Composure Report
# ============================================================


@app.post("/tools/trading/context")
def tool_trading_context(req: TradingContextRequest) -> dict:
    """Get LIVE market context for a symbol.

    OPTIMIZED: Uses batched bridge commands instead of 6 sequential calls.
    All data fetched in single round-trip, then assembled locally.

    Combines baseline knowledge (point values, typical ranges) with
    REAL-TIME market data (current ATR, order book, indicators, bar patterns).

    Answers: "Is 200 points a lot RIGHT NOW?" "How does current volatility compare?"
    """
    symbol = req.symbol
    symbol_norm = normalize_symbol(symbol)

    # Batch all bridge commands into a single round-trip
    commands = [
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "atr",
            "period": 14,
        },
        {"type": "get_order_book", "symbol": symbol_norm},
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "rsi",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 20,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 50,
        },
        {"type": "get_bars", "symbol": symbol_norm, "timeframe": "H1", "count": 3},
    ]

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=15.0)
    except Exception:
        # Fallback: build context with no live data
        return build_context(symbol=symbol)

    # Parse results
    atr_result, book_result, rsi_result, ema20_result, ema50_result, bars_result = (
        batch_results
    )

    current_atr = None
    current_price = None
    spread_points = None
    rsi = None
    ema_fast = None
    ema_slow = None
    last_bar_range = None
    last_bar_direction = None

    # Parse ATR
    if atr_result.get("status") == "completed":
        payload = atr_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                current_atr = float(parsed.get("value", 0) or 0)
                if current_atr == 0 and "data" in parsed and parsed["data"]:
                    current_atr = float(parsed["data"][-1])
            except Exception:
                pass
        elif isinstance(payload, dict):
            current_atr = float(payload.get("value", 0) or 0)
            if current_atr == 0 and "data" in payload and payload["data"]:
                current_atr = float(payload["data"][-1])

    # Parse order book
    if book_result.get("status") == "completed":
        payload = book_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                book_data = json.loads(payload)
            except Exception:
                book_data = {}
        else:
            book_data = payload
        bid, ask = _first_bid_ask(book_data)
        if bid and ask:
            current_price = (bid + ask) / 2
            spread_points = ask - bid

    # Parse RSI
    if rsi_result.get("status") == "completed":
        payload = rsi_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                rsi = float(json.loads(payload).get("value", 0) or 0)
            except Exception:
                pass
        elif isinstance(payload, dict):
            rsi = float(payload.get("value", 0) or 0)

    # Parse EMAs
    if ema20_result.get("status") == "completed":
        payload = ema20_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_fast = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_fast = payload.get("value")
    if ema50_result.get("status") == "completed":
        payload = ema50_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_slow = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_slow = payload.get("value")

    # Parse bars
    if bars_result.get("status") == "completed":
        payload = bars_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                bars_data = json.loads(payload).get("data", [])
            except Exception:
                bars_data = []
        elif isinstance(payload, dict):
            bars_data = payload.get("data", [])
        else:
            bars_data = []
        if bars_data:
            last = bars_data[-1]
            last_bar_range = last.get("high", 0) - last.get("low", 0)
            body = last.get("close", 0) - last.get("open", 0)
            if body > 0:
                last_bar_direction = "bullish"
            elif body < 0:
                last_bar_direction = "bearish"
            else:
                last_bar_direction = "doji"

    context = build_context(
        symbol=symbol,
        current_atr=current_atr,
        current_price=current_price,
        spread_points=spread_points,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        last_bar_range=last_bar_range,
        last_bar_direction=last_bar_direction,
    )

    return context


# ============================================================
# Trading Coach — Data-Driven Advisory Feedback
# ============================================================


@app.post("/tools/trading/coach")
def tool_trading_coach(req: TradingCoachRequest) -> dict:
    """Get advisory coaching derived from LIVE market data.

    OPTIMIZED: Uses batched bridge commands instead of 8+ sequential calls.
    All data fetched in single round-trip, then analyzed locally.

    This does NOT block. It fetches real-time indicators, bar patterns,
    order book depth, and journal history to generate context-specific advice.

    Every warning and insight is computed from actual market conditions.
    """
    symbol = req.symbol
    side = req.side
    symbol_norm = normalize_symbol(symbol)
    symbol_info = resource_symbol_info(symbol)

    # Batch all bridge commands into a single round-trip
    commands = [
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "atr",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "rsi",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 20,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 50,
        },
        {"type": "get_order_book", "symbol": symbol_norm},
        {"type": "get_bars", "symbol": symbol_norm, "timeframe": "H1", "count": 5},
    ]

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=15.0)
    except Exception:
        # Fallback: coach with provided params only
        coach = TradingCoach()
        advice = coach.evaluate(symbol=symbol, side=side, point=symbol_info.point)
        return {
            "recommendation": advice.recommendation,
            "warnings": advice.warnings,
            "insights": advice.insights,
            "confidence_factors": advice.confidence_factors,
            "raw_metrics": advice.raw_metrics,
        }

    atr_result, rsi_result, ema20_result, ema50_result, book_result, bars_result = (
        batch_results
    )

    # Parse all results
    current_atr = None
    rsi = None
    ema_fast = None
    ema_slow = None
    current_price = None
    spread_points = None
    last_bar_range = None
    last_bar_body = None
    last_bar_direction = None
    recent_compression = None
    regime = req.regime
    position_in_range = None

    # Parse ATR
    if atr_result.get("status") == "completed":
        payload = atr_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                current_atr = float(parsed.get("value", 0) or 0)
                if current_atr == 0 and "data" in parsed and parsed["data"]:
                    current_atr = float(parsed["data"][-1])
            except Exception:
                pass
        elif isinstance(payload, dict):
            current_atr = float(payload.get("value", 0) or 0)
            if current_atr == 0 and "data" in payload and payload["data"]:
                current_atr = float(payload["data"][-1])
    else:
        logger.warning(
            f"ATR batch request failed for {req.symbol}: status={atr_result.get('status', 'unknown')}"
        )

    # Parse RSI
    if rsi_result.get("status") == "completed":
        payload = rsi_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                rsi = float(json.loads(payload).get("value", 0) or 0)
            except Exception:
                pass
        elif isinstance(payload, dict):
            rsi = float(payload.get("value", 0) or 0)

    # Parse EMAs
    if ema20_result.get("status") == "completed":
        payload = ema20_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_fast = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_fast = payload.get("value")
    if ema50_result.get("status") == "completed":
        payload = ema50_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_slow = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_slow = payload.get("value")

    # Parse order book
    if book_result.get("status") == "completed":
        payload = book_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                book_data = json.loads(payload)
            except Exception:
                book_data = {}
        else:
            book_data = payload
        bid, ask = _first_bid_ask(book_data)
        if bid and ask:
            current_price = (bid + ask) / 2
            spread_points = ask - bid

    # Parse bars
    bars_data = []
    if bars_result.get("status") == "completed":
        payload = bars_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                bars_data = json.loads(payload).get("data", [])
            except Exception:
                bars_data = []
        elif isinstance(payload, dict):
            bars_data = payload.get("data", [])
        else:
            bars_data = []

        if len(bars_data) >= 2:
            last = bars_data[-1]
            prev = bars_data[-2]
            last_bar_range = last.get("high", 0) - last.get("low", 0)
            last_bar_body = last.get("close", 0) - last.get("open", 0)
            if last_bar_body > 0:
                last_bar_direction = "bullish"
            elif last_bar_body < 0:
                last_bar_direction = "bearish"
            else:
                last_bar_direction = "doji"

            # Compression: average of last 3 bars vs ATR
            if len(bars_data) >= 3 and current_atr and current_atr > 0:
                recent_ranges = [
                    b.get("high", 0) - b.get("low", 0) for b in bars_data[-3:]
                ]
                avg_recent = sum(recent_ranges) / len(recent_ranges)
                recent_compression = avg_recent / current_atr

    # Detect regime if not provided (local, no bridge call)
    if not regime and bars_data:
        regime_result = detect_regime(
            bars=bars_data,
            atr_value=current_atr or 0,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )
        regime = regime_result.get("regime")
        if "price_position_pct" in regime_result:
            position_in_range = regime_result["price_position_pct"]

    # Get journal context
    journal = get_journal_db()
    recent_decisions = journal.query(symbol=symbol, limit=10)
    recent_with_outcome = [
        d for d in recent_decisions if d.get("outcome") in ("win", "loss")
    ]

    consecutive_losses = 0
    for d in reversed(recent_with_outcome):
        if d["outcome"] == "loss":
            consecutive_losses += 1
        else:
            break

    win_rate_10 = None
    if recent_with_outcome:
        wins = sum(1 for d in recent_with_outcome if d["outcome"] == "win")
        win_rate_10 = wins / len(recent_with_outcome) * 100

    # Compute SL/TP distances if provided
    sl_points = req.sl_distance_points
    tp_points = req.tp_distance_points

    # Run the data-driven coach
    coach = TradingCoach()
    advice = coach.evaluate(
        symbol=symbol,
        side=side,
        atr_value=current_atr,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        current_price=current_price,
        spread_points=spread_points,
        last_bar_range=last_bar_range,
        last_bar_body=last_bar_body,
        last_bar_direction=last_bar_direction,
        recent_bars_compression=recent_compression,
        proposed_sl_points=sl_points,
        proposed_tp_points=tp_points,
        point=symbol_info.point,
        indicator_agreements=req.indicator_agreements,
        total_indicators_checked=None,
        trades_today=req.trades_today,
        daily_pnl=req.daily_pnl,
        recent_consecutive_losses=consecutive_losses,
        win_rate_last_10=win_rate_10,
        position_in_range=position_in_range,
        regime=regime,
    )

    return {
        "recommendation": advice.recommendation,
        "warnings": advice.warnings,
        "insights": advice.insights,
        "confidence_factors": advice.confidence_factors,
        "raw_metrics": advice.raw_metrics,
    }


# ============================================================
# Quick Context — One-Call Decision Support
# ============================================================


@app.post("/tools/trading/decision_support", response_model=dict)
def tool_decision_support(req: TradingDecisionSupportRequest) -> dict:
    """One-call decision support: fetches ALL market data in a SINGLE
    batched bridge round-trip, then runs analysis locally.

    BEFORE: 6+ sequential bridge calls = 3-5 seconds
    AFTER:  1 batched enqueue + 1 poll cycle = ~200-400ms

    Combines: regime detection + ATR + RSI + EMAs + coaching advice.
    """
    symbol = req.symbol
    side = req.side
    symbol_norm = normalize_symbol(symbol)
    symbol_info = resource_symbol_info(symbol)
    result: dict = {"symbol": symbol, "side": side}

    # Batch all bridge commands into a single round-trip
    commands = [
        {"type": "get_bars", "symbol": symbol_norm, "timeframe": "H1", "count": 20},
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "atr",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "rsi",
            "period": 14,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 20,
        },
        {
            "type": "get_indicator",
            "symbol": symbol_norm,
            "timeframe": "H1",
            "indicator": "ema",
            "period": 50,
        },
        {"type": "get_order_book", "symbol": symbol_norm},
    ]

    try:
        batch_results = _batch_enqueue_and_await(commands, timeout_s=15.0)
    except Exception as e:
        return {"symbol": symbol, "side": side, "error": f"Batch fetch failed: {e}"}

    # Parse results
    bars_result = batch_results[0]
    atr_result = batch_results[1]
    rsi_result = batch_results[2]
    ema20_result = batch_results[3]
    ema50_result = batch_results[4]
    book_result = batch_results[5]

    # Parse bars
    bars_data = []
    if bars_result.get("status") == "completed":
        payload = bars_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                bars_data = json.loads(payload).get("data", [])
            except Exception:
                pass
        elif isinstance(payload, dict):
            bars_data = payload.get("data", [])

    # Parse ATR
    atr_value = 0.0
    atr_warning = None
    if atr_result.get("status") == "completed":
        payload = atr_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                atr_value = float(parsed.get("value", 0) or 0)
                if atr_value == 0 and "data" in parsed and parsed["data"]:
                    atr_value = float(parsed["data"][-1])
            except Exception:
                pass
        elif isinstance(payload, dict):
            atr_value = float(payload.get("value", 0) or 0)
            if atr_value == 0 and "data" in payload and payload["data"]:
                atr_value = float(payload["data"][-1])
    else:
        atr_warning = "ATR unavailable"
        from mt5_mcp.observability.logging import logger

        logger.warning(
            f"ATR batch request failed for decision_support: status={atr_result.get('status', 'unknown')}"
        )
    result["atr"] = {"value": atr_value}
    if atr_warning:
        result["atr"]["warning"] = atr_warning

    # Parse RSI
    rsi = None
    if rsi_result.get("status") == "completed":
        payload = rsi_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                rsi = float(json.loads(payload).get("value", 0) or 0)
            except Exception:
                pass
        elif isinstance(payload, dict):
            rsi = float(payload.get("value", 0) or 0)
    result["rsi"] = rsi

    # Parse EMAs
    ema_fast = None
    ema_slow = None
    if ema20_result.get("status") == "completed":
        payload = ema20_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_fast = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_fast = payload.get("value")
    if ema50_result.get("status") == "completed":
        payload = ema50_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                ema_slow = json.loads(payload).get("value")
            except Exception:
                pass
        elif isinstance(payload, dict):
            ema_slow = payload.get("value")
    result["ema_20"] = ema_fast
    result["ema_50"] = ema_slow

    # Parse order book
    bid, ask = None, None
    if book_result.get("status") == "completed":
        payload = book_result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            try:
                book_data = json.loads(payload)
            except Exception:
                book_data = {}
        else:
            book_data = payload
        bid, ask = _first_bid_ask(book_data)
    result["bid"] = bid
    result["ask"] = ask

    # Run regime detection locally (no bridge call needed - we have bars + ATR + EMAs)
    regime = detect_regime(
        bars=bars_data,
        atr_value=atr_value,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
    )
    result["regime"] = regime

    # Run coaching locally
    coach = TradingCoach()
    advice = coach.evaluate(
        symbol=symbol,
        side=side,
        regime=regime.get("regime"),
        atr_value=atr_value,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        current_price=(bid + ask) / 2 if bid and ask else None,
        proposed_sl_points=req.sl_distance_points,
        proposed_tp_points=req.tp_distance_points,
        point=symbol_info.point,
    )
    result["coaching"] = {
        "recommendation": advice.recommendation,
        "warnings": advice.warnings,
        "insights": advice.insights,
        "raw_metrics": advice.raw_metrics,
    }

    # ====== SESSION CONTEXT ======
    try:
        session_ctx = _get_session_context()
        pair_ctx = _get_session_for_pair(symbol)
        result["session_context"] = {
            "current_sessions": session_ctx.current_sessions,
            "active_overlaps": session_ctx.active_overlaps,
            "is_market_open": session_ctx.is_market_open,
            "volatility_regime": session_ctx.volatility_regime,
            "spread_quality": session_ctx.spread_quality,
            "volume_concentration_pct": round(
                session_ctx.volume_concentration * 100, 1
            ),
            "day_of_week": session_ctx.day_of_week,
            "day_of_week_factor": session_ctx.day_of_week_factor,
            "pair_quality_score": pair_ctx.get("quality_score", 0),
            "pair_is_optimal": pair_ctx.get("is_optimal", False),
            "pair_warnings": pair_ctx.get("warnings", []),
        }
    except Exception:
        result["session_context"] = {"error": "unavailable"}

    # ====== ECONOMIC CALENDAR / NEWS BLACKOUT ======
    # Try MT5 Terminal Calendar API first, fall back to schedule-based
    try:
        sym_clean = symbol.upper().replace("/", "")
        currencies_to_check = set()
        if len(sym_clean) >= 6:
            currencies_to_check.add(sym_clean[:3])
            currencies_to_check.add(sym_clean[3:6])

        mt5_calendar_ok = False

        # PRIMARY: Try MT5 Terminal Calendar via bridge
        try:
            cal_cmd = {
                "type": "get_calendar",
                "currency": ",".join(currencies_to_check)
                if currencies_to_check
                else "",
                "hours_ahead": "2",
                "min_impact": "HIGH",
            }
            cal_results = _batch_enqueue_and_await([cal_cmd], timeout_s=10.0)
            cal_result = cal_results[0]

            if cal_result.get("status") == "completed":
                cal_payload = cal_result.get("result", {}).get("payload", {})
                if isinstance(cal_payload, str):
                    cal_payload = json.loads(cal_payload)

                if "events" in cal_payload and "error" not in cal_payload:
                    mt5_calendar_ok = True
                    result["calendar_context"] = {
                        "is_blackout": cal_payload.get("event_count", 0) > 0,
                        "source": "mt5_terminal",
                        "upcoming_high_impact_events": cal_payload.get("events", [])[
                            :5
                        ],
                        "event_count_next_2h": cal_payload.get("event_count", 0),
                    }
        except Exception:
            pass  # Fall through to schedule-based

        # FALLBACK: Schedule-based calendar
        if not mt5_calendar_ok:
            blackouts = []
            for cur in currencies_to_check:
                blackout = _is_blackout_now(currency=cur, minutes_ahead=30)
                if blackout.get("is_blackout"):
                    blackouts.append(blackout)

            upcoming = _get_upcoming_events(hours_ahead=2, min_impact="HIGH")
            relevant_events = [
                e.to_dict()
                for e in upcoming
                if not currencies_to_check or e.currency in currencies_to_check
            ]

            result["calendar_context"] = {
                "is_blackout": len(blackouts) > 0,
                "source": "schedule_based_fallback",
                "blackout_details": blackouts,
                "upcoming_high_impact_events": relevant_events[:5],
                "event_count_next_2h": len(relevant_events),
            }
    except Exception:
        result["calendar_context"] = {"error": "unavailable"}

    return result


# ============================================================
# Phase 4: Position Monitor (Long-Polling)
# ============================================================


@app.post("/resources/positions/monitor", response_model=PositionMonitorResult)
async def tool_monitor_position(req: PositionMonitorRequest) -> PositionMonitorResult:
    """Long-polling position monitor. Holds connection until alert triggers."""
    import asyncio

    end_time = asyncio.get_event_loop().time() + req.timeout_seconds

    while asyncio.get_event_loop().time() < end_time:
        try:
            positions_data = resource_positions_open()
            positions = [Position(**p) for p in positions_data.get("positions", [])]
            position = next(
                (p for p in positions if p.position_id == req.position_id), None
            )

            if position is None:
                return PositionMonitorResult(
                    position_id=req.position_id,
                    alert_type="closed",
                    triggered_value=0,
                )

            current_pnl = position.pnl or 0

            for pnl_level in req.alert_at_pnl:
                # Direction-aware comparison: profit targets (>=0) trigger on rising P&L,
                # loss thresholds (<0) trigger on falling P&L
                if (pnl_level >= 0 and current_pnl >= pnl_level) or (
                    pnl_level < 0 and current_pnl <= pnl_level
                ):
                    return PositionMonitorResult(
                        position_id=req.position_id,
                        alert_type="pnl",
                        current_pnl=current_pnl,
                        triggered_value=pnl_level,
                    )

            book = tool_get_order_book(OrderBookRequest(symbol=position.symbol))
            bid, ask = _first_bid_ask(book)
            if bid and ask:
                current_price = (bid + ask) / 2
                for price_level in req.alert_at_price:
                    if position.side == "buy" and current_price >= price_level:
                        return PositionMonitorResult(
                            position_id=req.position_id,
                            alert_type="price",
                            current_pnl=current_pnl,
                            current_price=current_price,
                            triggered_value=price_level,
                        )
                    elif position.side == "sell" and current_price <= price_level:
                        return PositionMonitorResult(
                            position_id=req.position_id,
                            alert_type="price",
                            current_pnl=current_pnl,
                            current_price=current_price,
                            triggered_value=price_level,
                        )

        except Exception:
            pass

        await asyncio.sleep(2)

    try:
        positions_data = resource_positions_open()
        positions = [Position(**p) for p in positions_data.get("positions", [])]
        position = next(
            (p for p in positions if p.position_id == req.position_id), None
        )
        current_pnl = position.pnl if position else 0
        current_price = position.mark_price if position else 0
    except Exception:
        current_pnl = 0
        current_price = 0

    return PositionMonitorResult(
        position_id=req.position_id,
        alert_type="timeout",
        current_pnl=current_pnl,
        current_price=current_price,
        timed_out=True,
    )


# ============================================================
# Agent Wait/Timer Tools
# ============================================================


@app.post("/tools/wait/delay", response_model=WaitDelayResult)
async def tool_wait_delay(req: WaitDelayRequest) -> WaitDelayResult:
    import asyncio
    import logging
    from datetime import datetime, timezone

    logger = logging.getLogger("mt5_mcp.wait_delay")

    # Validate duration (BUG-001/BUG-007 fix)
    if req.duration_seconds < 0:
        raise ValueError("duration_seconds must be at least 0")
    if req.duration_seconds > 3600:
        raise ValueError("duration_seconds must not exceed 3600 (1 hour)")

    market_summary = None
    capture_error: str | None = None

    if req.symbol:
        try:
            sym = normalize_symbol(req.symbol)
        except Exception as e:
            capture_error = str(e)
            req.symbol = None  # Disable market capture, just sleep

    def _capture_before() -> dict | None:
        """Capture pre-wait market state."""
        if not req.symbol:
            return None
        try:
            book_before = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
            bid_before = float(book_before.get("bid", 0))
            ask_before = float(book_before.get("ask", 0))
            if not bid_before or not ask_before:
                return None
            mid_before = (bid_before + ask_before) / 2

            rsi_before_req = IndicatorRequest(
                symbol=req.symbol, timeframe="H1", indicator="rsi", period=14
            )
            rsi_before_resp = tool_get_indicator(rsi_before_req)
            rsi_before = (
                rsi_before_resp.get("value") if "error" not in rsi_before_resp else None
            )

            bars_before = tool_get_bars(
                BarsRequest(symbol=req.symbol, timeframe="H1", count=20)
            )
            bars_data = (
                bars_before.get("data", []) if "error" not in bars_before else []
            )
            regime_before = detect_regime(bars=bars_data, atr_value=0)
            regime_before_label = regime_before.get("regime", "unknown")

            sym_info = tool_get_symbol_info(req.symbol)
            point = float(sym_info.get("point", 0))
            digits = int(sym_info.get("digits", 5))
            pip_size = point * 10 if digits in (3, 5) else point

            return {
                "bid": bid_before,
                "ask": ask_before,
                "mid": mid_before,
                "rsi": rsi_before,
                "regime": regime_before_label,
                "point": point,
                "digits": digits,
                "pip_size": pip_size,
            }
        except Exception as e:
            logger.warning("wait/delay: pre-capture failed for %s: %s", req.symbol, e)
            return None

    def _build_summary(before: dict) -> dict | None:
        """Build market summary comparing before/after state."""
        if not req.symbol:
            return None
        try:
            book_after = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
            bid_after = float(book_after.get("bid", 0))
            ask_after = float(book_after.get("ask", 0))
            if not bid_after or not ask_after:
                return None
            mid_after = (bid_after + ask_after) / 2

            rsi_after_req = IndicatorRequest(
                symbol=req.symbol, timeframe="H1", indicator="rsi", period=14
            )
            rsi_after_resp = tool_get_indicator(rsi_after_req)
            rsi_after = (
                rsi_after_resp.get("value") if "error" not in rsi_after_resp else None
            )

            bars_after = tool_get_bars(
                BarsRequest(symbol=req.symbol, timeframe="H1", count=20)
            )
            bars_data = bars_after.get("data", []) if "error" not in bars_after else []
            regime_after = detect_regime(bars=bars_data, atr_value=0)
            regime_after_label = regime_after.get("regime", "unknown")

            pip_size = before.get("pip_size", 0)
            digits = before.get("digits", 5)
            mid_before = before.get("mid", 0)

            if mid_before > 0 and mid_after > 0 and pip_size > 0:
                price_change_pips = round((mid_after - mid_before) / pip_size, 1)
            else:
                price_change_pips = None

            return {
                "symbol": req.symbol,
                "price_before": round(mid_before, digits) if mid_before else None,
                "price_after": round(mid_after, digits) if mid_after else None,
                "price_change_pips": price_change_pips,
                "regime_before": before.get("regime", "unknown"),
                "regime_after": regime_after_label,
                "rsi_before": before.get("rsi"),
                "rsi_after": rsi_after,
            }
        except Exception as e:
            logger.warning("wait/delay: post-capture failed for %s: %s", req.symbol, e)
            return None

    if req.symbol:
        before_data = _capture_before()
    else:
        before_data = None

    elapsed = 0
    while elapsed < req.duration_seconds:
        remaining = req.duration_seconds - elapsed
        sleep_time = min(30, remaining)
        await asyncio.sleep(sleep_time)
        elapsed += sleep_time

    if req.symbol and before_data:
        market_summary = _build_summary(before_data)

    result_data: dict = {
        "waited_seconds": req.duration_seconds,
        "resumed_at": datetime.now(timezone.utc).isoformat(),
        "market_summary": market_summary,
    }
    if capture_error:
        result_data["capture_error"] = capture_error
    return WaitDelayResult(**result_data)


@app.post("/tools/wait/indicator", response_model=WaitForIndicatorResult)
async def tool_wait_for_indicator(
    req: WaitForIndicatorRequest,
) -> WaitForIndicatorResult:
    import asyncio
    import logging

    logger = logging.getLogger("mt5_mcp.wait_indicator")

    check_interval = max(1, min(60, req.check_interval_seconds))
    timeout = max(5, min(3600, req.timeout_seconds))

    end_time = asyncio.get_event_loop().time() + timeout

    previous_value: float | None = None
    last_valid_value: float | None = None
    poll_count = 0

    while asyncio.get_event_loop().time() < end_time:
        try:
            indicator_req = IndicatorRequest(
                symbol=req.symbol,
                timeframe=req.timeframe,
                indicator=req.indicator,
                period=req.period,
                fast=req.fast,
                slow=req.slow,
                signal=req.signal,
            )
            result = tool_get_indicator(indicator_req)

            if "error" in result or result.get("value") is None:
                poll_count += 1
                logger.warning(
                    "wait/indicator: poll returned no value for %s %s: %s",
                    req.indicator,
                    req.symbol,
                    result.get("error", "value is None"),
                )
                await asyncio.sleep(check_interval)
                continue

            current_value = float(result["value"])
            poll_count += 1
            last_valid_value = current_value

            if req.condition == "above" and current_value >= req.value:
                return WaitForIndicatorResult(
                    symbol=req.symbol,
                    indicator=req.indicator,
                    condition=req.condition,
                    target_value=req.value,
                    actual_value=current_value,
                    triggered=True,
                )
            elif req.condition == "below" and current_value <= req.value:
                return WaitForIndicatorResult(
                    symbol=req.symbol,
                    indicator=req.indicator,
                    condition=req.condition,
                    target_value=req.value,
                    actual_value=current_value,
                    triggered=True,
                )
            elif req.condition == "equals":
                tolerance = abs(req.value) * 0.001 if req.value != 0 else 0.001
                if abs(current_value - req.value) <= tolerance:
                    return WaitForIndicatorResult(
                        symbol=req.symbol,
                        indicator=req.indicator,
                        condition=req.condition,
                        target_value=req.value,
                        actual_value=current_value,
                        triggered=True,
                    )
            elif req.condition == "crosses":
                if previous_value is not None:
                    crossed = (
                        previous_value < req.value and current_value >= req.value
                    ) or (previous_value >= req.value and current_value < req.value)
                    if crossed:
                        return WaitForIndicatorResult(
                            symbol=req.symbol,
                            indicator=req.indicator,
                            condition=req.condition,
                            target_value=req.value,
                            actual_value=current_value,
                            triggered=True,
                        )
                previous_value = current_value

        except Exception as e:
            poll_count += 1
            logger.warning(
                "wait/indicator: error polling %s for %s: %s",
                req.indicator,
                req.symbol,
                e,
            )

        await asyncio.sleep(check_interval)

    return WaitForIndicatorResult(
        symbol=req.symbol,
        indicator=req.indicator,
        condition=req.condition,
        target_value=req.value,
        actual_value=last_valid_value,
        triggered=False,
        timed_out=True,
    )


# ============================================================
# Trade Monitor — Long-Polling Price Condition Watch
# ============================================================


class TradeMonitorRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    duration: str
    expected: dict
    invalidation: dict
    check_interval_seconds: int = 5


@app.post("/tools/wait/trade_monitor", response_model=dict)
async def tool_wait_trade_monitor(req: TradeMonitorRequest) -> dict:
    """Long-polling trade monitor: holds connection until target or invalidation is reached.

    Computes target/invalidation prices from spec (price/pips/atr), then polls
    the market at configurable intervals until a condition is met or duration expires.
    """
    import asyncio
    import time as _time

    from mt5_mcp.services.trade_monitor import (
        parse_duration,
        compute_price_bracket,
        check_price_condition,
    )

    # Validate and clamp check_interval_seconds
    check_interval = max(1, min(60, req.check_interval_seconds))

    # Parse duration
    try:
        duration_seconds = parse_duration(req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid duration: {e}")

    # Enforce max timeout of 3600 seconds (1 hour)
    if duration_seconds > 3600:
        raise HTTPException(
            status_code=400,
            detail=f"Duration {duration_seconds}s exceeds maximum of 3600s (1 hour)",
        )

    if duration_seconds <= 0:
        raise HTTPException(status_code=400, detail="Duration must be positive")

    # Fetch symbol info for pip/point calculations
    symbol_info_data = tool_get_symbol_info(req.symbol)
    if "error" in symbol_info_data:
        raise HTTPException(
            status_code=400,
            detail=f"Symbol info unavailable for {req.symbol}: {symbol_info_data['error']}",
        )

    # Ensure point is available
    point = symbol_info_data.get("point")
    if not point or point <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Could not determine point value for {req.symbol}",
        )

    # Fetch ATR if needed for atr-type boundaries
    expected_type = req.expected.get("type")
    invalidation_type = req.invalidation.get("type")
    atr_value = 0.0

    if expected_type == "atr" or invalidation_type == "atr":
        try:
            atr_result = tool_get_indicator(
                IndicatorRequest(
                    symbol=req.symbol, timeframe="H1", indicator="atr", period=14
                )
            )
            if atr_result.get("status") == "error":
                from mt5_mcp.observability.logging import logger

                logger.warning(
                    f"ATR request failed for trade_monitor {req.symbol}: "
                    f"{atr_result.get('message', 'unknown error')}"
                )
            elif "value" in atr_result and atr_result["value"]:
                atr_value = float(atr_result["value"])
            elif "data" in atr_result and atr_result["data"]:
                atr_value = float(atr_result["data"][-1])
        except Exception:
            pass

    # Inject atr_value into atr-type specs
    if expected_type == "atr":
        req.expected["atr_value"] = atr_value
    if invalidation_type == "atr":
        req.invalidation["atr_value"] = atr_value

    # Get initial price for bracket computation
    try:
        book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
        bid, ask = _first_bid_ask(book)
        if bid is None or ask is None:
            raise ValueError("Order book returned no bid/ask")
        current_price = (bid + ask) / 2
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not fetch current price for {req.symbol}: {e}",
        )

    # Compute target and invalidation prices
    try:
        bracket = compute_price_bracket(
            current_price=current_price,
            side=req.side,
            spec={"expected": req.expected, "invalidation": req.invalidation},
            symbol_info=symbol_info_data,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid price bracket: {e}")

    target_price = bracket["target_price"]
    invalidation_price = bracket["invalidation_price"]
    target_pips = bracket["target_pips"]
    invalidation_pips = bracket["invalidation_pips"]

    # Gather initial market context
    market_context = {
        "regime": None,
        "atr": atr_value,
        "rsi": None,
        "spread_points": None,
    }
    try:
        regime_result = detect_regime(
            bars=[], atr_value=atr_value if atr_value > 0 else None
        )
        market_context["regime"] = regime_result.get("regime", "unknown")
    except Exception:
        market_context["regime"] = "unknown"

    try:
        rsi_result = tool_get_indicator(
            IndicatorRequest(
                symbol=req.symbol, timeframe="H1", indicator="rsi", period=14
            )
        )
        if "value" in rsi_result and rsi_result["value"]:
            market_context["rsi"] = float(rsi_result["value"])
    except Exception:
        pass

    if bid and ask:
        point_val = float(point)
        market_context["spread_points"] = (
            int((ask - bid) / point_val) if point_val > 0 else 0
        )

    def _refresh_market_context(
        symbol: str, point: str, bid: float | None, ask: float | None
    ) -> dict:
        """Re-fetch market context (regime, ATR, RSI, spread) at resolution time."""
        ctx = {
            "regime": "unknown",
            "atr": atr_value,
            "rsi": None,
            "spread_points": None,
        }
        try:
            regime_result = detect_regime(
                bars=[], atr_value=atr_value if atr_value > 0 else None
            )
            ctx["regime"] = regime_result.get("regime", "unknown")
        except Exception:
            pass

        try:
            rsi_result = tool_get_indicator(
                IndicatorRequest(
                    symbol=symbol, timeframe="H1", indicator="rsi", period=14
                )
            )
            if "value" in rsi_result and rsi_result["value"]:
                ctx["rsi"] = float(rsi_result["value"])
        except Exception:
            pass

        try:
            if bid and ask:
                point_val = float(point)
                ctx["spread_points"] = (
                    int((ask - bid) / point_val) if point_val > 0 else 0
                )
        except Exception:
            pass

        return ctx

    start_time = _time.monotonic()
    end_time = _time.monotonic() + duration_seconds

    while _time.monotonic() < end_time:
        try:
            # Fetch current price
            book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
            bid, ask = _first_bid_ask(book)

            if bid is not None and ask is not None:
                current_price = (bid + ask) / 2
            else:
                # Price fetch failed — log and continue polling
                from mt5_mcp.observability.logging import logger

                logger.warning(
                    f"Trade monitor: could not fetch price for {req.symbol}, continuing poll"
                )
                await asyncio.sleep(check_interval)
                continue

            # Check price conditions
            condition = check_price_condition(
                current_price=current_price,
                bid=bid,
                ask=ask,
                target_price=target_price,
                invalidation_price=invalidation_price,
                side=req.side,
            )

            elapsed = int(_time.monotonic() - start_time)
            distance_to_target = abs(target_price - current_price)
            distance_to_invalidation = abs(invalidation_price - current_price)
            pip = 10 * float(point)
            dist_target_pips = distance_to_target / pip if pip > 0 else 0.0
            dist_inval_pips = distance_to_invalidation / pip if pip > 0 else 0.0

            if condition == "target_reached":
                try:
                    market_context.update(
                        _refresh_market_context(req.symbol, point, bid, ask)
                    )
                except Exception:
                    pass
                return {
                    "symbol": req.symbol,
                    "reason": "target_reached",
                    "current_price": current_price,
                    "bid": bid,
                    "ask": ask,
                    "target_price": target_price,
                    "invalidation_price": invalidation_price,
                    "distance_to_target_pips": round(dist_target_pips, 1),
                    "distance_to_invalidation_pips": round(dist_inval_pips, 1),
                    "elapsed_seconds": elapsed,
                    "duration_seconds": duration_seconds,
                    "market_context": dict(market_context),
                }
            elif condition == "invalidation_hit":
                try:
                    market_context.update(
                        _refresh_market_context(req.symbol, point, bid, ask)
                    )
                except Exception:
                    pass
                return {
                    "symbol": req.symbol,
                    "reason": "invalidation_hit",
                    "current_price": current_price,
                    "bid": bid,
                    "ask": ask,
                    "target_price": target_price,
                    "invalidation_price": invalidation_price,
                    "distance_to_target_pips": round(dist_target_pips, 1),
                    "distance_to_invalidation_pips": round(dist_inval_pips, 1),
                    "elapsed_seconds": elapsed,
                    "duration_seconds": duration_seconds,
                    "market_context": dict(market_context),
                }

        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Trade monitor poll error for {req.symbol}: {e}")

        await asyncio.sleep(check_interval)

    # Timeout — return final state
    try:
        book = tool_get_order_book(OrderBookRequest(symbol=req.symbol))
        bid, ask = _first_bid_ask(book)
        if bid is not None and ask is not None:
            current_price = (bid + ask) / 2
        else:
            current_price = 0.0
    except Exception:
        current_price = 0.0
        bid = 0.0
        ask = 0.0

    elapsed = int(_time.monotonic() - start_time)
    distance_to_target = abs(target_price - current_price)
    distance_to_invalidation = abs(invalidation_price - current_price)
    pip = 10 * float(point)
    dist_target_pips = distance_to_target / pip if pip > 0 else 0.0
    dist_inval_pips = distance_to_invalidation / pip if pip > 0 else 0.0

    try:
        market_context.update(_refresh_market_context(req.symbol, point, bid, ask))
    except Exception:
        pass

    return {
        "symbol": req.symbol,
        "reason": "timeout",
        "current_price": current_price,
        "bid": bid,
        "ask": ask,
        "target_price": target_price,
        "invalidation_price": invalidation_price,
        "distance_to_target_pips": round(dist_target_pips, 1),
        "distance_to_invalidation_pips": round(dist_inval_pips, 1),
        "elapsed_seconds": elapsed,
        "duration_seconds": duration_seconds,
        "market_context": dict(market_context),
    }


# ============================================================
# News & Session Awareness Tools
# ============================================================


@app.post("/tools/news/fetch", response_model=dict)
def tool_news_fetch(req: NewsFetchRequest) -> dict:
    """Fetch forex-relevant news from RSS feeds.

    Default: FOREX_MAJOR pool, last 6 hours. Custom: specify pools/currencies/keywords.
    Set enrich=true to add sentiment, topics, and entity extraction.
    """
    result = _fetch_news(
        pools=req.pools,
        currencies=req.currencies,
        keywords=req.keywords,
        exclude_keywords=req.exclude_keywords,
        limit=req.limit,
        hours_back=req.hours_back,
        match_all=req.match_all,
        source_ids=req.source_ids,
    )

    # Optionally enrich
    if req.enrich and result.get("items"):
        enriched = _enrich_news(result["items"])
        result["items"] = enriched["items"]

    return result


class NewsEnrichRequest(BaseModel):
    items: list[dict] = []
    extract: list[str] = ["sentiment", "topics", "entities", "currency_relevance"]


@app.post("/tools/news/enrich", response_model=dict)
def tool_news_enrich(req: NewsEnrichRequest) -> dict:
    """Add sentiment analysis, topics, entities, and currency relevance to news items.

    Pass items from news.fetch in the request body.
    """
    items = req.items
    extract = req.extract
    extract = req.get(
        "extract", ["sentiment", "topics", "entities", "currency_relevance"]
    )
    return _enrich_news(items, extract=extract)


@app.post("/tools/news/pools", response_model=dict)
def tool_news_pools() -> dict:
    """Get all available news pools and sources."""
    return {
        "pools": _get_available_pools(),
        "sources": _get_available_sources(),
    }


@app.post("/tools/trading/economic_calendar", response_model=dict)
def tool_economic_calendar(req: EconomicCalendarRequest) -> dict:
    """Get upcoming high-impact economic events.

    PRIMARY: Fetches from MT5 Terminal's native Economic Calendar API
    via the EA bridge (CalendarEventByCurrency + CalendarValueHistory).

    FALLBACK: If MT5 calendar is unavailable (disconnected, broker doesn't
    provide calendar data), falls back to schedule-based recurring events.

    The MT5 terminal provides REAL data — actual/forecast/previous values
    for 600+ indicators across 18 major economies. This is the ground truth.
    """
    mt5_data = None
    mt5_error = None

    # PRIMARY: Try MT5 Terminal Calendar API via EA bridge
    try:
        calendar_cmd = {
            "type": "get_calendar",
            "currency": req.currency or "",
            "hours_ahead": str(req.hours_ahead),
            "min_impact": req.min_impact,
        }
        batch_results = _batch_enqueue_and_await([calendar_cmd], timeout_s=15.0)
        result = batch_results[0]

        if result.get("status") == "completed":
            payload = result.get("result", {}).get("payload", {})
            if isinstance(payload, str):
                payload = json.loads(payload)

            if "error" not in payload:
                mt5_data = payload
            else:
                mt5_error = payload.get("error", "unknown")
        else:
            mt5_error = result.get("error", "bridge_timeout")
    except Exception as e:
        mt5_error = str(e)

    # FALLBACK: Use hardcoded schedule-based events
    fallback_events = _get_upcoming_events(
        hours_ahead=req.hours_ahead,
        currency=req.currency,
        min_impact=req.min_impact,
    )
    fallback_blackouts = _get_blackout_windows(hours_ahead=req.hours_ahead)
    fallback_current = _is_blackout_now(currency=req.currency, minutes_ahead=30)

    if mt5_data and "events" in mt5_data:
        # MT5 calendar succeeded — use real terminal data
        events = mt5_data["events"]

        # Compute blackout windows from MT5 events
        from datetime import datetime as _dt, timedelta as _td

        blackouts = []
        current_blackout = False
        now = _dt.utcnow()

        for evt in events:
            impact = evt.get("importance", "LOW")
            if impact in ("HIGH",):
                # Parse timestamp and compute blackout window
                ts = evt.get("timestamp", 0)
                if ts > 0:
                    evt_time = _dt.utcfromtimestamp(ts)
                    # 60 min blackout for HIGH, 120 min for CRITICAL-level events
                    blackout_minutes = (
                        120
                        if "rate" in evt.get("name", "").lower()
                        or "decision" in evt.get("name", "").lower()
                        else 60
                    )
                    blackouts.append(
                        {
                            "event_name": evt.get("name"),
                            "start_utc": (
                                evt_time - _td(minutes=blackout_minutes)
                            ).isoformat(),
                            "end_utc": (
                                evt_time + _td(minutes=blackout_minutes)
                            ).isoformat(),
                            "event_time_utc": evt_time.isoformat(),
                            "currency": evt.get("currency"),
                            "impact": impact,
                            "blackout_minutes": blackout_minutes,
                            "source": "mt5_terminal",
                        }
                    )
                    if (
                        evt_time - _td(minutes=blackout_minutes)
                        <= now
                        <= evt_time + _td(minutes=blackout_minutes)
                    ):
                        current_blackout = True

        return {
            "events": events,
            "event_count": len(events),
            "source": "mt5_terminal_calendar",
            "blackout_windows": blackouts,
            "current_blackout": {
                "is_blackout": current_blackout,
                "events_causing_blackout": [
                    b
                    for b in blackouts
                    if _dt.fromisoformat(b["start_utc"])
                    <= now
                    <= _dt.fromisoformat(b["end_utc"])
                ],
                "source": "mt5_terminal",
            },
            "mt5_calendar_status": "connected",
        }
    else:
        # MT5 unavailable — fall back to schedule-based events
        return {
            "events": [e.to_dict() for e in fallback_events],
            "event_count": len(fallback_events),
            "source": "schedule_based_fallback",
            "blackout_windows": fallback_blackouts,
            "current_blackout": fallback_current,
            "daily_briefing": _get_daily_briefing(),
            "mt5_calendar_status": f"unavailable ({mt5_error}) — using schedule-based fallback",
            "warning": "MT5 Terminal calendar data unavailable. Events are estimated from recurring schedules, not real-time terminal data.",
        }


@app.post("/tools/trading/safe_shutdown", response_model=dict)
def tool_safe_shutdown(req: SafeShutdownRequest) -> dict:
    mode = req.mode
    positions_closed = []
    orders_cancelled = []
    failed = []

    try:
        positions_data = tool_get_positions()
        orders_data = tool_get_orders()
    except Exception as e:
        return {
            "error": f"Failed to fetch state: {e}",
            "mode": mode,
            "positions_closed": [],
            "orders_cancelled": [],
            "failed": [],
        }

    all_positions = positions_data.get("positions", [])
    all_orders = orders_data.get("orders", [])

    def _is_owned(item: dict) -> bool:
        if req.session_id and item.get("session_id") != req.session_id:
            return False
        if req.strategy_id and item.get("strategy_id") != req.strategy_id:
            return False
        if not req.session_id and not req.strategy_id:
            if item.get("session_id") is None and item.get("strategy_id") is None:
                return False
        return True

    owned_positions = [p for p in all_positions if _is_owned(p)]
    owned_orders = [o for o in all_orders if _is_owned(o)]

    if mode in ("flatten", "full"):
        for pos in owned_positions:
            pos_id = pos.get("ticket") or pos.get("position_id") or pos.get("id")
            if not pos_id:
                failed.append({"type": "position", "item": pos, "reason": "no_id"})
                continue
            try:
                close_req = ClosePosReq(
                    position_id=str(pos_id),
                    session_id=req.session_id,
                    strategy_id=req.strategy_id,
                )
                result = tool_close_position(close_req)
                if result.get("status") == "error":
                    failed.append({"type": "position", "id": pos_id, "result": result})
                else:
                    positions_closed.append({"id": pos_id, "result": result})
            except Exception as e:
                failed.append({"type": "position", "id": pos_id, "error": str(e)})

    if mode in ("freeze", "full"):
        for order in owned_orders:
            order_id = order.get("ticket") or order.get("order_id") or order.get("id")
            if not order_id:
                failed.append({"type": "order", "item": order, "reason": "no_id"})
                continue
            try:
                cancel_req = CancelOrderRequest(
                    order_id=str(order_id),
                    session_id=req.session_id,
                    strategy_id=req.strategy_id,
                )
                result = tool_cancel_order(cancel_req)
                if result.get("status") == "error":
                    failed.append({"type": "order", "id": order_id, "result": result})
                else:
                    orders_cancelled.append({"id": order_id, "result": result})
            except Exception as e:
                failed.append({"type": "order", "id": order_id, "error": str(e)})

    if mode == "full":
        set_frozen(True, by=req.intent_id or "safe_shutdown")

    return {
        "mode": mode,
        "positions_closed": positions_closed,
        "orders_cancelled": orders_cancelled,
        "failed": failed,
        "summary": {
            "total_positions_found": len(owned_positions),
            "total_orders_found": len(owned_orders),
            "positions_closed": len(positions_closed),
            "orders_cancelled": len(orders_cancelled),
            "failed": len(failed),
        },
        "freeze_state": dict(_shutdown_state),
    }


@app.post("/tools/trading/thaw", response_model=dict)
def tool_thaw() -> dict:
    thaw()
    return {
        "status": "thawed",
        "freeze_state": dict(_shutdown_state),
    }


@app.get("/tools/trading/freeze_status", response_model=dict)
def tool_freeze_status() -> dict:
    return {
        "frozen": is_frozen(),
        "freeze_state": dict(_shutdown_state),
    }


@app.post("/tools/trading/policy_config", response_model=dict)
def tool_policy_config(req: TradingPolicyConfigRequest) -> dict:
    """Update trading policy limits at runtime."""

    config_dict = req.model_dump(exclude_none=True)
    result = get_policy().update_limits(**config_dict)
    return result


@app.get("/tools/trading/policy_status", response_model=dict)
def tool_policy_status() -> dict:
    """Return current policy limits and status."""

    policy = get_policy()
    return {
        "limits": policy.get_limits(),
        "status": policy.get_status(),
    }


@app.post("/tools/market/custom_indicator", response_model=dict)
def tool_custom_indicator(req: CustomIndicatorRequest) -> dict:
    symbol_normalized = normalize_symbol(req.symbol)

    tcp_result = _tcp_send_and_await(
        "get_custom_indicator",
        {
            "symbol": symbol_normalized,
            "timeframe": req.timeframe,
            "indicator_name": req.indicator_name,
            "params": req.params,
            "buffer_index": req.buffer_index,
            "count": req.count,
        },
    )
    if tcp_result and tcp_result.get("status") == "completed":
        payload = tcp_result.get("result", {}).get("payload", "{}")
        if isinstance(payload, str):
            data = _parse_payload(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = {}
        if "symbol" in data:
            data["symbol"] = denormalize_symbol(data["symbol"])
        return data

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/enqueue",
        params={
            "type": "get_custom_indicator",
            "symbol": symbol_normalized,
            "timeframe": req.timeframe,
            "indicator_name": req.indicator_name,
            "params": req.params,
            "buffer_index": req.buffer_index,
            "count": req.count,
        },
    )
    r.raise_for_status()
    req_id = r.json()["id"]
    res = _await_result(req_id, timeout_s=20.0)
    if res.get("status") != "completed":
        return {"error": res.get("error", "timeout")}
    payload = res.get("result", {}).get("payload", "{}")
    data = _parse_payload(payload)
    if "symbol" in data:
        data["symbol"] = denormalize_symbol(data["symbol"])
    return data


class ReconcileRequest(BaseModel):
    intent_ids: list[str] = []


def _get_reconcile_context() -> dict:
    adapter = getattr(get_gateway(), "adapter", None)
    positions: list[Position] = []
    deals: list[Deal] = []

    if adapter is not None:
        try:
            positions = adapter.get_positions() or []
        except Exception:
            pass
        if hasattr(adapter, "get_deals_history"):
            try:
                raw = adapter.get_deals_history(limit=500, symbol=None, days=7)
                deals = raw if isinstance(raw, list) else raw.get("deals", [])
            except Exception:
                pass

    if not positions:
        try:
            raw_positions = tool_get_positions().get("positions", [])
            positions = [Position(**p) for p in raw_positions]
        except Exception:
            pass

    if not deals:
        try:
            raw_deals = tool_get_deals_history(limit=500, days=7).get("deals", [])
            deals = [Deal(**d) for d in raw_deals]
        except Exception:
            pass

    return {"positions": positions, "deals": deals}


@app.post("/tools/reconcile", response_model=dict)
def tool_reconcile(req: ReconcileRequest) -> dict:
    settings = get_settings_cached()
    svc = ReconciliationService(settings)
    ctx = _get_reconcile_context()

    positions = ctx["positions"]
    deals = ctx["deals"]

    positions = [p if isinstance(p, Position) else Position(**p) for p in positions]
    deals = [d if isinstance(d, Deal) else Deal(**d) for d in deals]

    reconcile_result = svc.reconcile(req.intent_ids, positions)
    owned = svc.get_owned_positions(positions)
    foreign_pnl = svc.calculate_foreign_pnl(owned, deals)

    reconcile_result["foreign_pnl"] = foreign_pnl
    return reconcile_result


# ============================================================
# Portfolio Risk — Exposure & Pre-Trade Gate
# ============================================================


@app.post("/tools/portfolio/exposure", response_model=dict)
def tool_portfolio_exposure(req: PortfolioExposureRequest) -> dict:
    positions_data = resource_positions_open()
    positions = positions_data.get("positions", [])
    orders = resource_orders_pending()
    account = resource_account_summary()

    svc = PortfolioRiskService(
        get_positions_fn=lambda: positions,
        get_orders_fn=lambda: orders,
        get_account_fn=lambda: account,
    )
    return svc.get_exposure()


class PortfolioRiskRequest(BaseModel):
    symbol: str | None = None
    days: int = 7
    limit: int = 100


@app.post("/tools/portfolio/risk", response_model=dict)
def tool_portfolio_risk(req: PortfolioRiskRequest) -> dict:
    """Portfolio-wide risk analysis using PortfolioRiskService.

    Returns exposure, concentration, and correlation metrics across all open positions.
    """
    positions_data = resource_positions_open()
    positions = positions_data.get("positions", [])
    if not positions:
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

    orders = resource_orders_pending()
    account = resource_account_summary()

    svc = PortfolioRiskService(
        get_positions_fn=lambda: positions,
        get_orders_fn=lambda: orders,
        get_account_fn=lambda: account,
    )
    exposure = svc.get_exposure()

    # Build correlated pairs from correlation groups
    correlated_pairs = []
    seen = set()
    sym_symbols = [p.get("symbol", "") for p in positions]
    unique_symbols = list(
        set(s.upper().replace("M", "").replace("m", "") for s in sym_symbols if s)
    )
    for i, sa in enumerate(unique_symbols):
        for sb in unique_symbols[i + 1 :]:
            key = tuple(sorted([sa, sb]))
            if key in seen:
                continue
            seen.add(key)
            corr = svc._correlation(sa, sb)
            if abs(corr) > 0.5:
                correlated_pairs.append(
                    {"symbol_a": sa, "symbol_b": sb, "correlation": round(corr, 4)}
                )

    # Build exposure_by_symbol list
    exposure_by_symbol = []
    total_exposure = exposure.get("total_exposure_usd", 0)
    equity = float(getattr(account, "equity", 0) or getattr(account, "balance", 0) or 0)
    margin = float(getattr(account, "margin", 0) or 0)
    exposure_map = exposure.get("exposure_by_symbol", {})
    if hasattr(exposure_map, "items"):
        for sym, data in exposure_map.items():
            sym_exposure = data.get("notional_usd", 0) if isinstance(data, dict) else 0
            # Determine net exposure direction
            usd_dir = (
                data.get("usd_direction", "usd_short")
                if isinstance(data, dict)
                else "usd_short"
            )
            net_exposure = sym_exposure if usd_dir == "usd_short" else -sym_exposure
            exposure_by_symbol.append(
                {
                    "symbol": sym,
                    "exposure_usd": round(abs(sym_exposure), 2),
                    "net_exposure_usd": round(net_exposure, 2),
                    "margin_usd": round(margin / len(exposure_map), 2)
                    if exposure_map
                    else 0,
                }
            )

    # Concentration metrics
    max_single = max(
        (abs(e.get("exposure_usd", 0)) for e in exposure_by_symbol), default=0
    )
    concentration_ratio = round(max_single / equity, 4) if equity > 0 else 0
    max_single_position_pct = round(max_single / equity * 100, 2) if equity > 0 else 0

    net_exposure_usd = sum(e.get("net_exposure_usd", 0) for e in exposure_by_symbol)

    return {
        "total_exposure_usd": round(total_exposure, 2),
        "net_exposure_usd": round(net_exposure_usd, 2),
        "exposure_by_symbol": exposure_by_symbol,
        "risk_metrics": {
            "concentration_ratio": concentration_ratio,
            "max_single_position_pct": max_single_position_pct,
            "correlated_pairs": sorted(
                correlated_pairs, key=lambda x: abs(x["correlation"]), reverse=True
            ),
        },
    }


@app.post("/tools/portfolio/pre_trade_gate", response_model=dict)
def tool_portfolio_pre_trade_gate(req: PreTradeGateRequest) -> dict:
    positions_data = resource_positions_open()
    positions = positions_data.get("positions", [])
    orders = resource_orders_pending()
    account = resource_account_summary()

    svc = PortfolioRiskService(
        get_positions_fn=lambda: positions,
        get_orders_fn=lambda: orders,
        get_account_fn=lambda: account,
    )
    return svc.pre_trade_gate(
        symbol=req.symbol,
        side=req.side,
        volume=req.volume_lots,
        sl_distance=req.sl_distance,
    )


# ============================================================
# Server entry point — MUST be at the end so all routes register first
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)


# ============================================================
# ONNX ML Inference
# ============================================================

_onnx_service = None


def _get_onnx_service():
    global _onnx_service
    if _onnx_service is None:
        try:
            from mt5_mcp.services.onnx_inference import ONNXInferenceService

            _onnx_service = ONNXInferenceService()
        except ImportError:
            return None
    return _onnx_service


@app.post("/tools/ml/predict", response_model=dict)
def tool_ml_predict(req: MLPredictRequest) -> dict:
    svc = _get_onnx_service()
    if svc is None:
        return {"error": "ONNX runtime not available. Install onnxruntime package."}
    try:
        return svc.predict(req.model_name, req.features, req.feature_names)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools/ml/models", response_model=dict)
def tool_ml_models() -> dict:
    svc = _get_onnx_service()
    if svc is None:
        return {"models": {}, "error": "ONNX runtime not available"}
    return svc.list_models()


@app.post("/tools/ml/models/reload", response_model=dict)
def tool_ml_models_reload() -> dict:
    svc = _get_onnx_service()
    if svc is None:
        return {"error": "ONNX runtime not available"}
    return svc.reload()


# ============================================================
# Historical Data Cache (SQLite-backed)
# ============================================================

_data_store = None


def _get_data_store():
    global _data_store
    if _data_store is None:
        try:
            from mt5_mcp.services.data_store import DataStore

            _data_store = DataStore()
        except Exception as e:
            from mt5_mcp.observability.logging import logger

            logger.warning(f"Data store initialization failed: {e}")
            return None
    return _data_store


@app.post("/tools/data/import", response_model=dict)
def tool_data_import(req: DataImportRequest) -> dict:
    """Import historical data from CSV or JSON."""
    store = _get_data_store()
    if store is None:
        return {"error": "Data store unavailable"}

    try:
        if req.data_type == "bars":
            if req.format == "csv":
                return store.import_bars_csv(req.content, req.symbol, req.timeframe)
            else:
                return store.import_bars_json(req.content)
        elif req.data_type == "ticks":
            if req.format == "csv":
                return store.import_ticks_csv(req.content, req.symbol)
            else:
                return {"error": "JSON ticks import not yet supported"}
        elif req.data_type == "deals":
            if req.format == "json":
                return store.import_deals_json(req.content)
            else:
                return {"error": "CSV deals import not yet supported"}
    except Exception as e:
        return {
            "error": str(e),
            "imported": 0,
            "duplicates_skipped": 0,
            "errors": [str(e)],
        }


@app.post("/tools/data/bars", response_model=dict)
def tool_data_bars(req: HistoricalBarsRequest) -> dict:
    """Query cached historical bars."""
    store = _get_data_store()
    if store is None:
        return {"error": "Data store unavailable", "data": []}
    return store.query_bars(
        req.symbol, req.timeframe, req.start_time, req.end_time, req.limit
    )


@app.post("/tools/data/ticks", response_model=dict)
def tool_data_ticks(req: HistoricalTicksRequest) -> dict:
    """Query cached historical ticks."""
    store = _get_data_store()
    if store is None:
        return {"error": "Data store unavailable", "data": []}
    return store.query_ticks(req.symbol, req.start_time_ms, req.end_time_ms, req.limit)


@app.post("/tools/data/deals", response_model=dict)
def tool_data_deals(req: HistoricalDealsRequest) -> dict:
    """Query cached deals history."""
    store = _get_data_store()
    if store is None:
        return {"error": "Data store unavailable", "data": []}
    return store.query_deals(req.symbol, req.limit)


@app.get("/tools/data/stats", response_model=dict)
def tool_data_stats() -> dict:
    """Get stats about cached data."""
    store = _get_data_store()
    if store is None:
        return {"error": "Data store unavailable"}
    return store.get_stats()


# ============================================================
# Emergency Kill Switch
# ============================================================


@app.post("/tools/management/emergency_liquidate", response_model=dict)
def tool_emergency_liquidate(keep_frozen: bool = Body(True, embed=True)) -> dict:
    """Emergency kill switch — closes ALL positions, cancels ALL orders, freezes trading.

    Most destructive operation. Use only for catastrophic scenarios.
    """
    from apps.mcp_server.tools_management import mt5_emergency_liquidate

    return mt5_emergency_liquidate(keep_frozen=keep_frozen)
