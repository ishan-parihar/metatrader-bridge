from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, computed_field


class OwnershipMixin(BaseModel):
    """Canonical ownership/idempotency fields for write-path request models.

    All fields are Optional[str] — additive, non-breaking, progressively adoptable.
    """

    session_id: Optional[str] = None
    strategy_id: Optional[str] = None
    intent_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class HealthStatus(BaseModel):
    state: Literal[
        "healthy",
        "degraded_read_only",
        "degraded_write_blocked",
        "disconnected",
        "incident",
    ] = "disconnected"
    details: Optional[str] = None


class TerminalStatus(BaseModel):
    connected: bool
    login: Optional[int] = None
    server: Optional[str] = None
    build: Optional[int] = None
    path: Optional[str] = None
    message: Optional[str] = None


class AccountSummary(BaseModel):
    account_id: Optional[str] = None
    name: Optional[str] = None
    balance: Optional[float] = None
    equity: Optional[float] = None
    margin: Optional[float] = None
    free_margin: Optional[float] = None
    margin_level: Optional[float] = None
    leverage: Optional[int] = None
    profit: Optional[float] = None
    margin_call_level: Optional[float] = None
    margin_stop_out_level: Optional[float] = None
    currency: Optional[str] = None
    server: Optional[str] = None
    environment: Literal["paper", "demo", "live"] = "demo"

    @computed_field(alias="pnl")
    @property
    def pnl(self) -> Optional[float]:
        return self.profit


class SymbolInfo(BaseModel):
    symbol: str
    description: Optional[str] = None
    digits: Optional[int] = None
    point: Optional[float] = None
    tick_size: Optional[float] = None
    tick_value: Optional[float] = None
    contract_size: Optional[float] = None
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None
    volume_step: Optional[float] = None
    stops_level_points: Optional[int] = None
    freeze_level_points: Optional[int] = None
    spread_points: Optional[int] = None
    spread_float: Optional[bool] = None
    trade_mode: Optional[str] = None
    calc_mode: Optional[str] = None
    currency_base: Optional[str] = None
    currency_profit: Optional[str] = None
    currency_margin: Optional[str] = None
    swap_long: Optional[float] = None
    swap_short: Optional[float] = None


class Deal(BaseModel):
    deal_id: str
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    symbol: str
    side: Optional[str] = None
    entry: Optional[str] = None
    volume: float = 0.0
    price: float = 0.0
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    time: str
    comment: Optional[str] = None
    reason: Optional[str] = None
    magic: Optional[int] = None
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    intent_id: Optional[str] = None

    @computed_field(alias="pnl")
    @property
    def pnl(self) -> float:
        return self.profit


class PerformanceSummary(BaseModel):
    closed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: Optional[float] = None
    expectancy: float = 0.0


class Position(BaseModel):
    position_id: str
    symbol: str
    side: Literal["buy", "sell"]
    volume: float
    entry_price: float
    mark_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    opened_at: Optional[str] = None
    source: Optional[str] = None
    magic_number: Optional[int] = None
    comment: Optional[str] = None

    @computed_field(alias="pnl")
    @property
    def pnl(self) -> Optional[float]:
        return self.unrealized_pnl


class Order(BaseModel):
    order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    kind: Literal["market", "limit", "stop", "stop_limit"]
    volume: float
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    status: Optional[str] = None
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    intent_id: Optional[str] = None
    magic_number: Optional[int] = None
    comment: Optional[str] = None


class Bar(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: Optional[int] = None


class Bars(BaseModel):
    symbol: str
    timeframe: str
    as_of: Optional[str] = None
    data: list[Bar]
    source: Optional[str] = None
    staleness_ms: Optional[int] = None


class MarginEstimateRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    volume_lots: float
    price_hint: Optional[float] = None


class MarginEstimate(BaseModel):
    required_margin: float
    leverage: Optional[float] = None
    comment: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TradeIntent(OwnershipMixin):
    account_id: str = (
        "demo"  # Optional — defaults to "demo" for MCP agent compatibility
    )
    environment: Literal["paper", "demo", "live"] = "demo"
    symbol: str
    side: Literal["buy", "sell"]
    order_kind: Literal["market", "limit", "stop", "stop_limit"] = "market"
    volume_lots: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation_points: Optional[int] = 20
    time_in_force: Optional[str] = "GTC"
    rationale: Optional[str] = None
    risk_tag: Optional[str] = None
    approval_mode: Optional[
        Literal[
            "observe_only",
            "recommend_only",
            "human_approval_required",
            "bounded_auto",
            "full_auto",
        ]
    ] = "human_approval_required"
    requested_at: Optional[str] = None
    trail_config: Optional[dict] = None


class SimulationResult(BaseModel):
    intent_id: str
    status: Literal["simulated", "error"] = "simulated"
    message: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    intent_id: str
    status: Literal[
        "accepted",
        "rejected",
        "submitted",
        "filled",
        "partial",
        "cancelled",
        "error",
    ]
    adapter: Optional[str] = None
    broker_order_id: Optional[str] = None
    position_id: Optional[str] = None
    retcode: Optional[str] = None
    message: Optional[str] = None
    requested_price: Optional[float] = None
    executed_price: Optional[float] = None
    slippage_points: Optional[int] = None
    timestamp: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    magic_number: Optional[int] = None
    comment: Optional[str] = None


# Minimal request models for write-path scaffolding
class ModifyOrderRequest(OwnershipMixin):
    order_id: str
    new_price: Optional[float] = None
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None


class ClosePositionRequest(OwnershipMixin):
    position_id: str
    volume: Optional[float] = None  # null -> full close


class Heartbeat(BaseModel):
    server: Optional[str] = None
    build: Optional[int] = None
    account_id: Optional[str] = None
    login: Optional[int] = None
    timestamp: Optional[str] = None


class PipelineStatus(BaseModel):
    """Single-call pipeline diagnostic for agent state triage.

    Returns everything an agent needs to determine pipeline readiness
    without making 5+ separate API calls.
    """

    # Bridge-level connectivity
    bridge_connected: bool = False
    heartbeat_age_seconds: Optional[float] = None
    terminal_login: Optional[int] = None
    terminal_server: Optional[str] = None
    terminal_build: Optional[int] = None

    # MT5 process health (Wine/Bottles)
    mt5_terminal_running: bool = False

    # Active adapter
    active_adapter: str = "unknown"

    # Last known account state (cached; may be stale)
    last_account: Optional[AccountSummary] = None

    # Summary for agents
    agent_readiness: Literal["ready", "degraded", "offline"] = "offline"
    agent_readiness_reason: Optional[str] = None
