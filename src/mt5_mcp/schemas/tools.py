from __future__ import annotations

from pydantic import BaseModel, field_validator
from typing import Any, Literal, Optional, Union


class OwnershipMixin(BaseModel):
    session_id: Optional[str] = None
    strategy_id: Optional[str] = None
    intent_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    magic_number: Optional[int] = None


class BarsRequest(BaseModel):
    symbol: str
    timeframe: str
    count: int = 100


class IndicatorRequest(BaseModel):
    symbol: str
    timeframe: str
    indicator: Literal[
        "sma",
        "ema",
        "wma",
        "smma",
        "rsi",
        "macd",
        "bbands",
        "stoch",
        "atr",
        "adx",
        "dmi",
        "ichimoku",
        "obv",
        "cci",
    ]
    # Common period
    period: int | None = None
    # MACD
    fast: int | None = None
    slow: int | None = None
    signal: int | None = None
    # BBands
    deviation: float | None = None
    shift: int | None = None
    # Stochastic
    k_period: int | None = None
    d_period: int | None = None
    slowing: int | None = None
    # Ichimoku
    tenkan: int | None = None
    kijun: int | None = None
    senkou: int | None = None
    # Series window
    window: int | None = None


class ChartScreenshotRequest(BaseModel):
    symbol: str
    timeframe: str
    width: int = 1280
    height: int = 720


class ChartScreenshotResult(BaseModel):
    image_base64: str
    content_type: str = "image/png"


class AccountSummaryResult(BaseModel):
    account_id: str | None = None
    name: str | None = None
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    free_margin: float | None = None
    currency: str | None = None


class ModifyPositionSLTPRequest(OwnershipMixin):
    position_id: str
    sl: float | None = None
    tp: float | None = None


class ClosePositionRequest(OwnershipMixin):
    position_id: str
    volume: float | None = None


class SubmitPendingOrderRequest(OwnershipMixin):
    symbol: str
    side: Literal["buy", "sell"]
    kind: Literal["limit", "stop"]
    price: float
    volume_lots: float
    sl: float | None = None
    tp: float | None = None
    deviation: int = 20
    trail_config: dict | None = None


class CancelOrderRequest(OwnershipMixin):
    order_id: str


class ModifyOrderRequest(OwnershipMixin):
    order_id: str
    new_price: float | None = None
    new_sl: float | None = None
    new_tp: float | None = None


class CloseAllPositionsRequest(OwnershipMixin):
    symbol: str | None = None
    side: Literal["buy", "sell", "both"] = "both"


class CancelAllOrdersRequest(OwnershipMixin):
    symbol: str | None = None
    side: Literal["buy", "sell", "both"] = "both"


class TicksRequest(BaseModel):
    symbol: str
    count: int = 200


class OrderBookRequest(BaseModel):
    symbol: str


class SymbolInfoRequest(BaseModel):
    symbol: str


class DealsHistoryRequest(BaseModel):
    symbol: str | None = None
    limit: int = 100
    days: int = 30


class PositionSizeRequest(BaseModel):
    symbol: str
    entry_price: float
    stop_loss_price: float
    risk_percent: float
    equity: float | None = None


class ValidateTradeSetupRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    order_kind: Literal["market", "limit", "stop"] = "market"
    volume_lots: float
    entry_price: float | None = None
    sl: float | None = None
    tp: float | None = None


class TrailPositionRequest(BaseModel):
    position_id: str
    distance_points: int
    lock_in_points: int = 0


class VolatilityProfileRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 20
    atr_period: int = 14


class DivergenceRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 50
    count: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal_period: int = 9
    rsi_period: int = 14
    swing_window: int = 5


class VolumeProfileRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 20


class MultiBarPatternsRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 50
    period: int = 20
    fib_lookback: int = 50


class MomentumCheckRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 50
    rsi: float | None = None
    atr: float | None = None


class MultiTimeframeIndicatorRequest(BaseModel):
    symbol: str
    indicator: Literal[
        "sma",
        "ema",
        "wma",
        "smma",
        "rsi",
        "macd",
        "bbands",
        "stoch",
        "atr",
        "adx",
        "dmi",
        "ichimoku",
        "obv",
        "cci",
    ]
    timeframes: list[str]
    period: int | None = None
    fast: int | None = None
    slow: int | None = None
    signal: int | None = None
    deviation: float | None = None
    shift: int | None = None
    k_period: int | None = None
    d_period: int | None = None
    slowing: int | None = None
    tenkan: int | None = None
    kijun: int | None = None
    senkou: int | None = None
    window: int | None = None


class CorrelationMatrixRequest(BaseModel):
    symbols: list[str]
    timeframe: str
    lookback: int = 50


class TrailConfig(OwnershipMixin):
    """Auto-trailing stop configuration for order submission.

    When provided with a market or pending order, trailing activates
    immediately after the order fills. The EA expects flat JSON fields
    in the order command (trail_atr_multiplier, etc.), NOT nested.
    """

    atr_multiplier: float = 2.0
    lock_profit_atr: float = 1.0
    check_interval_seconds: int = 10
    atr_timeframe: str = "H1"
    atr_period: int = 14


# --- Phase 3: Bracket Orders ---


class BracketOrderRequest(OwnershipMixin):
    """Place paired BUY STOP + SELL STOP for breakout capture.

    When one fills, the other is auto-cancelled.
    Audit finding: bracket orders were the best strategy (+$3.70).
    """

    symbol: str
    buy_trigger: float  # BUY STOP price (above current)
    sell_trigger: float  # SELL STOP price (below current)
    volume_lots: float
    sl_atr_multiplier: float = 1.0  # SL distance as ATR multiplier
    tp_atr_multiplier: float = 2.0  # TP distance as ATR multiplier
    rationale: str | None = None


class BracketOrderResult(BaseModel):
    status: str
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    message: str | None = None


class SetTrailingStopRequest(OwnershipMixin):
    """Start server-side trailing stop for a position."""

    position_id: str
    distance_atr_multiplier: float = 1.0
    check_interval_seconds: int = 10
    lock_in_profit_after_atr: float = 1.0  # Lock in profit after this many ATR


class TrailingStopResult(BaseModel):
    position_id: str
    status: str  # "active", "stopped", "error"
    message: str = ""
    initial_sl: float | None = None


# --- EA-Native ATR Trailing Stop (Phase 3) ---


class EATrailingStartRequest(OwnershipMixin):
    """Start EA-native ATR trailing stop for a position.

    Unlike SetTrailingStopRequest, this runs inside the EA process,
    surviving MCP/gateway instability. State persists across OnTimer() ticks
    but not EA restarts.
    """

    ticket: str  # Position ticket number
    atr_multiplier: float = 1.5  # ATR multiplier for trailing distance (0.5-5.0)
    check_interval_seconds: int = 10  # How often to check (seconds)
    lock_in_profit_atr: float = 0.0  # Start trailing only after this many ATR in profit
    magic_filter: int = 0  # 0 = no filter, >0 = only trail positions with this magic


class EATrailingStopRequest(OwnershipMixin):
    """Stop EA-native trailing for a position."""

    ticket: str


class EATrailingListResult(BaseModel):
    """Result from listing active EA trailing stops."""

    active_trailing: list[dict] = []
    count: int = 0


class EATrailingTickResult(BaseModel):
    """Result from processing EA trailing stops."""

    processed: int = 0
    active: int = 0


# --- EA-Native Bracket/OCO Management (Phase 3) ---


class EABracketStartRequest(OwnershipMixin):
    """Start EA-native OCO bracket order management.

    Registers a pair of pending orders as a bracket. When one leg fills,
    the EA automatically cancels the sibling leg. The bracket_id is embedded
    in order comments for recovery after EA restart.
    """

    buy_order_ticket: Union[
        int, str
    ]  # BUY pending order ticket (0 if single-leg bracket)
    sell_order_ticket: Union[
        int, str
    ]  # SELL pending order ticket (0 if single-leg bracket)
    bracket_id: str  # Unique identifier for this bracket pair
    comment: Optional[str] = ""  # Additional comment to embed in orders
    magic_filter: Optional[int] = (
        0  # 0 = no filter, >0 = only manage orders with this magic
    )

    @field_validator("buy_order_ticket", "sell_order_ticket", mode="before")
    @classmethod
    def coerce_tickets_to_str(cls, v):
        """Coerce int ticket values to str for compatibility with EA bridge."""
        if v is not None:
            return str(v)
        return v


class EABracketStopRequest(OwnershipMixin):
    """Stop and remove an EA-native bracket order.

    Cancels both legs if still pending and removes from tracking.
    """

    bracket_id: str  # Bracket identifier to stop


class EABracketListResult(BaseModel):
    """Result from listing active EA brackets."""

    brackets: list[dict] = []
    count: int = 0


class EABracketTickResult(BaseModel):
    """Result from processing EA bracket orders."""

    processed: int = 0
    events: list[dict] = []
    errors: int = 0
    active: int = 0


# --- Phase 4: Price Alert (Long-Polling) ---


class PriceAlertRequest(BaseModel):
    """Long-polling price alert. Holds connection until triggered."""

    symbol: str
    condition: Literal["above", "below", "crosses"]
    price: float
    timeout_seconds: int = 300


class PriceAlertResult(BaseModel):
    symbol: str
    condition: str
    trigger_price: float
    actual_price: float
    triggered: bool
    timed_out: bool = False


# --- Phase 4: Position Monitor (Long-Polling) ---


class PositionMonitorRequest(BaseModel):
    """Long-polling position monitor."""

    position_id: str
    alert_at_pnl: list[float] = []  # Alert at these P&L levels
    alert_at_price: list[float] = []  # Alert at these price levels
    timeout_seconds: int = 600


class PositionMonitorResult(BaseModel):
    position_id: str
    alert_type: str | None = None  # "pnl", "price", "timeout", "closed"
    current_pnl: float | None = None
    current_price: float | None = None
    triggered_value: float | None = None
    timed_out: bool = False


# --- Agent Wait/Timer Tools ---


class WaitDelayRequest(BaseModel):
    """Simple time-based delay for AI agent trading loops."""

    duration_seconds: int = 60
    symbol: str | None = None


class WaitDelayResult(BaseModel):
    waited_seconds: int
    resumed_at: str
    market_summary: dict | None = None


class WaitForIndicatorRequest(BaseModel):
    """Wait until an indicator meets a condition or timeout."""

    symbol: str
    timeframe: str = "H1"
    indicator: str  # rsi, macd, cci, atr, adx, sma, ema, etc.
    condition: str = "below"  # above, below, crosses, equals
    value: float
    period: int | None = None
    fast: int | None = None
    slow: int | None = None
    signal: int | None = None
    timeout_seconds: int = 300
    check_interval_seconds: int = 5


class WaitForIndicatorResult(BaseModel):
    symbol: str
    indicator: str
    condition: str
    target_value: float
    actual_value: float | None = None
    triggered: bool
    timed_out: bool = False


# --- Phase 2: Market Regime ---


class MarketRegimeRequest(BaseModel):
    symbol: str
    timeframe: str
    lookback: int = 20
    atr_period: int = 14


# --- Phase 2: Trading Policy ---


class TradingPolicyStatusRequest(BaseModel):
    equity: float | None = None


class TradingPolicyConfigRequest(BaseModel):
    max_trades_per_day: int | None = None
    max_loss_per_day_pct: float | None = None
    min_rest_between_trades_sec: int | None = None
    max_loss_per_trade_pct: float | None = None
    require_indicator_confluence: bool | None = None
    min_confluence_count: int | None = None
    cooldown_after_consecutive_losses: int | None = None
    cooldown_duration_after_losses_sec: int | None = None
    min_profit_before_breakeven_atr: float | None = None
    allow_live_trading: bool | None = None
    session_start_time: str | None = None
    session_end_time: str | None = None


class PolicyConfigResult(BaseModel):
    applied: dict
    limits: dict
    skipped_unknown_fields: list[str] | None = None


class PolicyStatusResult(BaseModel):
    limits: dict
    status: dict


# --- Phase 2: Trade Journal ---


class TradeJournalQueryRequest(BaseModel):
    symbol: str | None = None
    side: str | None = None
    strategy: str | None = None
    exit_reason: str | None = None
    limit: int = 100


class MarketScanRequest(BaseModel):
    """Multi-symbol market scan in one call."""

    symbols: list[str]
    timeframe: str = "H1"
    atr_period: int = 14


# --- Metacognition & AI Reasoning ---


class TradeDecisionLogRequest(BaseModel):
    """Log a trading decision with full AI reasoning for metacognition."""

    symbol: str
    side: str
    action: str  # entry, exit, modify_sl, modify_tp, trail, close, monitor, decision_to_wait
    entry_price: float | None = None
    exit_price: float | None = None
    sl: float | None = None
    tp: float | None = None
    volume_lots: float | None = None
    pnl: float | None = None
    session_id: str | None = None

    # Market context (auto-fillable, but AI can override)
    regime: str | None = None
    atr_value: float | None = None
    atr_percent_of_price: float | None = None
    rsi_value: float | None = None
    indicator_snapshot: dict | None = None

    # AI reasoning (REQUIRED for metacognition)
    model_justification: str | None = None
    indicators_considered: list[str] | None = None
    confidence_level: float | None = None  # 0-1
    risk_assessment: str | None = None
    emotional_self_report: str | None = (
        None  # calm, cautious, aggressive, anxious, uncertain, confident
    )
    alternatives_considered: str | None = None
    expected_duration: str | None = None
    expected_move_points: float | None = None

    # Post-trade (filled on exit)
    outcome: str | None = None  # win, loss, breakeven, still_open
    lesson_learned: str | None = None
    would_do_differently: str | None = None
    mistake_category: str | None = (
        None  # premature_exit, late_entry, wrong_regime, ignored_signal, revenge_trade, overtrading, perfect_trade
    )
    quality_rating: int | None = None  # 1-5

    decision_id: str | None = None  # For updating existing decisions
    note: str | None = None


class TradeJournalReflectionRequest(BaseModel):
    """Query journal for metacognitive reflection."""

    symbol: str | None = None
    outcome: str | None = None
    regime: str | None = None
    emotional_self_report: str | None = None
    mistake_category: str | None = None
    action: str | None = None
    limit: int = 50


class TradingContextRequest(BaseModel):
    """Get trading context/education for a symbol."""

    symbol: str
    include_comparison: bool = True


class TradingCoachRequest(BaseModel):
    """Get advisory coaching feedback for a potential trade."""

    symbol: str
    side: str
    regime: str | None = None
    atr_value: float | None = None
    rsi: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    sl_distance_points: float | None = None
    tp_distance_points: float | None = None
    indicator_agreements: int | None = None
    trades_today: int = 0
    daily_pnl: float = 0.0
    recent_consecutive_losses: int = 0
    position_in_range: float | None = None  # 0-100


class TradingDecisionSupportRequest(BaseModel):
    """One-call decision support: regime + ATR + RSI + EMAs + coaching."""

    symbol: str
    side: str
    sl_distance_points: float | None = None
    tp_distance_points: float | None = None


# ============================================================
# News & Session Awareness Requests
# ============================================================


class NewsFetchRequest(BaseModel):
    """Fetch forex-relevant news from RSS feeds."""

    pools: list[str] | None = (
        None  # ["FOREX_MAJOR", "CENTRAL_BANKS", "MACRO_ECONOMIC", "CRYPTO_FX", "GEOPOLITICAL_FX"]
    )
    currencies: list[str] | None = (
        None  # ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
    )
    keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    limit: int = 20
    hours_back: int = 6
    match_all: bool = False
    source_ids: list[str] | None = None
    enrich: bool = False  # Add sentiment/topics/entities


class EconomicCalendarRequest(BaseModel):
    """Get upcoming high-impact economic events."""

    hours_ahead: int = 24
    currency: str | None = None
    min_impact: str = "MEDIUM"  # "LOW", "MEDIUM", "HIGH", "CRITICAL"


class SessionContextRequest(BaseModel):
    """Get current forex trading session context."""

    symbol: str | None = None  # Optional: filter for specific pair
    include_all_pairs: bool = False  # Include session quality for all pairs


# ============================================================
# Symbol Snapshot — One-Call Market Context
# ============================================================


class SnapshotRequest(BaseModel):
    """Get complete market snapshot for a symbol in one call.

    Replaces 5+ separate API calls (bars, indicators, order book,
    symbol info, coaching) with a single authoritative snapshot.
    """

    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    include_coaching: bool = True
    session_id: str | None = None
    strategy_id: str | None = None


# ============================================================
# Opportunity Ranking — Multi-Symbol Trade-Readiness Scoring
# ============================================================


class OpportunityRankRequest(BaseModel):
    """Rank symbols by trade-readiness across 7 weighted factors.

    Custom weights can be provided to adjust the importance of each factor.
    Valid keys: regime_clarity, spread_atr_ratio, volatility_usability,
    session_quality, confluence, portfolio_overlap, calendar.
    Values should sum to approximately 1.0 (0.95–1.05 tolerance).
    If not provided or invalid, default weights are used.
    """

    symbols: list[str]
    timeframe: str = "H1"
    min_score: float = 50.0
    session_id: str | None = None
    strategy_id: str | None = None
    weights: dict[str, float] | None = None


# ============================================================
# Chart Intelligence — Unified Chart Analysis Bundle
# ============================================================


class SafeShutdownRequest(BaseModel):
    """Safe shutdown request with configurable modes.

    Provides a single escape hatch to safely flatten positions and freeze
    trading when conditions degrade (bridge disconnect, data staleness,
    foreign activity confusion).

    Modes:
    - "flatten": Close all owned positions, cancel all owned pending orders
    - "freeze": Cancel all owned pending orders, keep positions open
    - "full": Flatten + freeze (set frozen flag to reject new orders)
    """

    mode: Literal["flatten", "freeze", "full"] = "full"
    preserve_stops: bool = True  # When closing, do NOT modify SL — close at market
    session_id: Optional[str] = None
    strategy_id: Optional[str] = None
    intent_id: Optional[str] = None


class SafeShutdownResult(BaseModel):
    mode: str
    positions_closed: list[dict] = []
    orders_cancelled: list[dict] = []
    failed: list[dict] = []
    summary: dict = {}
    freeze_state: dict = {}


class ChartIntelligenceRequest(BaseModel):
    """Get unified chart intelligence: screenshot + S/R + indicators + patterns.

    Replaces separate calls for screenshot, support/resistance levels,
    and indicator summary with a single agent-friendly response.
    """

    symbol: str
    timeframe: str = "H1"
    width: int = 1920
    height: int = 1080
    include_screenshot: bool = True
    include_screenshot_base64: bool = False
    bar_count: int = 100
    session_id: Optional[str] = None
    strategy_id: Optional[str] = None


# ============================================================
# Custom Indicator — Generic iCustom() Wrapper
# ============================================================


class CustomIndicatorRequest(BaseModel):
    """Query any custom indicator via iCustom() wrapper.

    Supports both built-in indicators (Examples\\MACD) and custom indicators
    placed in MQL5/Indicators/ directory.
    """

    symbol: str
    timeframe: str
    indicator_name: str  # e.g., "Examples\\MACD" or "Custom\\MyIndicator"
    params: str = ""  # KV format: "period=14,deviation=2.0,method=0"
    buffer_index: int = 0  # Which buffer to read (0-based)
    count: int = 100  # How many values to return


class PortfolioExposureRequest(BaseModel):
    """Get current portfolio exposure across all open positions."""

    pass


class PreTradeGateRequest(BaseModel):
    """Check if a proposed trade is safe given current portfolio exposure."""

    symbol: str
    side: Literal["buy", "sell"]
    volume_lots: float
    sl_distance: float


# ============================================================
# ONNX ML Inference
# ============================================================


class MLPredictRequest(BaseModel):
    """Run inference on a pre-trained ONNX model."""

    model_name: str
    features: list[float]
    feature_names: list[str] | None = None


class MLModelsListRequest(BaseModel):
    """List available ONNX models."""

    pass


class MLReloadModelsRequest(BaseModel):
    """Reload ONNX models from disk."""

    pass


# ============================================================
# Historical Data Cache (SQLite-backed)
# ============================================================


class DataImportRequest(BaseModel):
    """Import historical data from CSV or JSON."""

    data_type: Literal["bars", "ticks", "deals"]
    format: Literal["csv", "json"]
    content: str
    symbol: str | None = None
    timeframe: str | None = None


class HistoricalBarsRequest(BaseModel):
    """Query cached historical bars."""

    symbol: str
    timeframe: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int = 1000


class HistoricalTicksRequest(BaseModel):
    """Query cached historical ticks."""

    symbol: str
    start_time_ms: int | None = None
    end_time_ms: int | None = None
    limit: int = 1000


class HistoricalDealsRequest(BaseModel):
    """Query cached deals history."""

    symbol: str | None = None
    limit: int = 100


class DataStatsRequest(BaseModel):
    """Get stats about cached data."""

    pass


# --- Inline models extracted from apps/mcp_server/main.py for MCP migration ---


class MarketStructureRequest(BaseModel):
    """Request for market structure detection (BOS, CHoCH, trend health)."""

    symbol: str
    timeframe: str = "H1"
    swing_lookback: int = 5
    confirm_bos_pips: float = 0.0


class StrategySelectorRequest(BaseModel):
    """Request for strategy selection by market regime."""

    regime: str | None = None


class VWAPRequest(BaseModel):
    """Request for VWAP calculation with standard deviation bands."""

    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    std_dev_multiplier: float = 2.0


class VolumeAtPriceRequest(BaseModel):
    """Request for volume-at-price distribution analysis."""

    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    num_bins: int = 20


class SetupProbabilityRequest(BaseModel):
    """Request for historical setup win-rate estimation."""

    symbol: str | None = None
    regime: str | None = None
    session: str | None = None
    min_samples: int = 5


class SupportResistanceRequest(BaseModel):
    """Request for support/resistance level detection."""

    symbol: str
    timeframe: str = "H1"
    lookback: int = 100


class TradeMonitorRequest(BaseModel):
    """Request for long-polling trade setup monitoring."""

    symbol: str
    side: Literal["buy", "sell"]
    duration: str
    expected: dict
    invalidation: dict
    check_interval_seconds: int = 5


class ReconcileRequest(BaseModel):
    """Request for position-to-journal reconciliation."""

    intent_ids: list[str] = []


class PortfolioRiskRequest(BaseModel):
    """Request for portfolio risk metrics."""

    symbol: str | None = None
    days: int | None = None
    limit: int | None = None


# ============================================================
# P0 Additions: Backtesting, Data Sync, Script Execution
# ============================================================


class BacktestRunRequest(BaseModel):
    """Run a strategy backtest on historical data."""

    symbol: str
    timeframe: str = "H1"
    strategy_name: str = "ma_cross"
    params: dict[str, Any] = {}
    from_date: str | None = None
    to_date: str | None = None
    initial_balance: float = 10000.0
    volume_lots: float = 0.1


class DataSyncRequest(BaseModel):
    """Sync historical market data from MT5 terminal to local cache."""

    symbol: str
    timeframe: str = "M1"
    count: int = 1000


class ScriptRunRequest(BaseModel):
    """Run a Python strategy validation script in a sandboxed subprocess."""

    code: str
    timeout: int = 30
    env_vars: dict[str, str] = {}
