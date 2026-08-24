# MetaTraderBridge Upgrade Plan

> **Generated:** 2026-04-08 | **Source:** UPGRADE_MANDATE_20260408.md + Agent Request
> **Scope:** EA (MQL5) + MCP Server (Python) + Trading Skill (Agent Playbook) — treated as single project
> **Total Effort:** 37-54 hours | **7 Phases** | **Critical Path:** Phase 1 → 2 → 5 → 6

---

## System Architecture Recap

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent (mt5-trading SKILL.md)                     │
│  10-phase playbook · 18 rules · 7 reference files · polling tiers  │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ MCP Protocol (TOOL_SPECS in mcp_mt5_wrapper.py)
                              │ 28+ tools · descriptions · schemas
┌─────────────────────────────▼───────────────────────────────────────┐
│                    MCP Server (apps/mcp_server/)                    │
│  FastAPI endpoints · Pydantic schemas · service layer · journal DB  │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ TCP Bridge (port 8025) / HTTP Fallback (port 8020)
┌─────────────────────────────▼───────────────────────────────────────┐
│                  MQL5 EA (ea/BridgeConnectorEA.mq5)                  │
│  OnTimer loop · TrailingStopManager · BracketManager · 30+ commands │
└─────────────────────────────────────────────────────────────────────┘
```

**Key Discovery:** The EA already has fully functional trailing stop (`CTrailingStopManager`) and bracket (`CBracketManager`) engines running in `OnTimer()`. The gaps are in **integration** (not wiring them into order submission) and **new features** (time-based exit, position health, dedicated wait tool).

---

## Phase 1: Reliability Foundation (P0-1, P0-3, P1-1)

**Goal:** Fix the three reliability issues that caused direct trading losses.

### 1.1 Fix `positions_open()` Reliability (P0-1)

**Root cause hypothesis:** MT5 terminal sync lag, or MCP not forcing `PositionsTotal()` refresh before querying.

**Changes — Python side (`apps/mcp_server/main.py`):**
```python
def tool_get_positions() -> dict:
    # Primary: TCP
    tcp_result = _tcp_send_and_await("get_positions", {})
    
    # If empty but margin > 0, force retry
    positions = tcp_result.get("positions", [])
    if not positions:
        account = tool_get_account_summary()
        if account.get("margin", 0) > 0:
            time.sleep(0.5)
            tcp_result = _tcp_send_and_await("get_positions", {})
            positions = tcp_result.get("positions", [])
    
    # Add sync_status
    sync_status = "fresh" if positions else (
        "stale_investigate" if account.get("margin", 0) > 0 else "no_positions"
    )
    tcp_result["sync_status"] = sync_status
    tcp_result["last_sync_timestamp"] = time.time()
    if sync_status == "stale_investigate":
        tcp_result.setdefault("warnings", []).append(
            "positions_open returned empty but margin > 0 — possible sync lag"
        )
    return tcp_result
```

**Changes — EA side (`ea/BridgeConnectorEA.mq5`):**
In `JsonPositions()`, force refresh before serialization:
```cpp
string JsonPositions()
{
   // Force MT5 to refresh position cache
   PositionsTotal();  // Forces internal refresh
   
   int total = PositionsTotal();
   // ... existing serialization
}
```

**Acceptance Criteria:**
- [ ] `positions_open()` never returns `[]` when `account_summary().margin > 0`
- [ ] Response includes `sync_status`: "fresh" | "stale_investigate" | "no_positions"
- [ ] Response includes `last_sync_timestamp` (Unix float)
- [ ] If stale, response includes warning message in `warnings` array

**Estimated effort:** 2-3 hours

---

### 1.2 Add Position Health Data (P0-3)

**Changes — Python side (`apps/mcp_server/main.py`):**

After fetching positions, enrich each with computed health metrics:

```python
def _enrich_position_health(pos: dict) -> dict:
    """Compute health metrics server-side — zero agent computation needed."""
    symbol = pos["symbol"]
    entry = pos["entry_price"]
    sl = pos.get("sl", 0)
    tp = pos.get("tp", 0)
    mark = pos["mark_price"]
    profit = pos.get("profit", 0)
    side = pos["side"]
    
    # Get symbol info for pip/point conversion
    sym_info = tool_get_symbol_info(symbol)
    point = sym_info.get("point", 0.00001)
    tick_value = sym_info.get("tick_value", 1.0)
    volume = pos.get("volume", 0.01)
    
    # Distance calculations
    if side == "buy":
        dist_to_sl = (entry - sl) / point if sl > 0 else 0
        dist_to_tp = (tp - entry) / point if tp > 0 else 0
    else:
        dist_to_sl = (sl - entry) / point if sl > 0 else 0
        dist_to_tp = (entry - tp) / point if tp > 0 else 0
    
    # P&L as multiple of risk
    risk_pips = dist_to_sl if dist_to_sl > 0 else 1
    risk_usd = risk_pips * tick_value * volume / point
    pnl_pct_of_risk = profit / risk_usd if risk_usd > 0 else 0
    
    # Spread cost
    spread_points = sym_info.get("spread", 0)
    spread_cost_pips = spread_points  # points = pips for most forex
    profit_multiple_of_spread = profit / (spread_cost_pips * tick_value * volume / point) if spread_cost_pips > 0 else 0
    
    # Time in trade
    opened_at = pos.get("opened_at", 0)
    time_in_trade_minutes = int((time.time() - opened_at) / 60) if opened_at else 0
    time_in_trade_bars_h1 = time_in_trade_minutes // 60
    
    # Trail eligibility
    trail_eligible = profit_multiple_of_spread > 2.0
    
    pos["health"] = {
        "distance_to_sl_pips": round(dist_to_sl, 1),
        "distance_to_tp_pips": round(dist_to_tp, 1),
        "pnl_percent_of_risk": round(pnl_pct_of_risk, 2),
        "time_in_trade_minutes": time_in_trade_minutes,
        "time_in_trade_bars_h1": time_in_trade_bars_h1,
        "is_winning": profit > 0,
        "is_at_breakeven": abs(profit) < (spread_cost_pips * tick_value * volume / point),
        "trail_eligible": trail_eligible,
        "spread_cost_pips": spread_cost_pips,
        "profit_multiple_of_spread": round(profit_multiple_of_spread, 1),
    }
    return pos
```

**Acceptance Criteria:**
- [ ] Every position in `positions_open()` includes `health` object
- [ ] `distance_to_sl_pips` and `distance_to_tp_pips` are accurate
- [ ] `pnl_percent_of_risk` = current_pnl / (entry_pips_to_sl × pip_value × lots)
- [ ] `trail_eligible` = profit > 2 × spread_cost
- [ ] All calculations done server-side

**Estimated effort:** 3-4 hours

---

### 1.3 Fix `ea_bracket_start()` 422 Error (P1-1)

**Root cause analysis:** The endpoint is likely a FastAPI validation issue, not an EA issue. The EA's `bracket_start` command handler works (lines 2057+). The 422 suggests Pydantic schema rejection.

**Investigation steps:**
1. Check the `EABracketStartRequest` schema in `src/mt5_mcp/schemas/tools.py` — `buy_order_ticket` and `sell_order_ticket` are typed as `int | str` in the schema but may fail coercion
2. Check the endpoint handler in `apps/mcp_server/main.py` for routing issues
3. Verify the HTTP request format sent to EA matches what `ParseKV()` expects

**Likely fix:** The ticket values are being sent as strings but the EA's `ParseKV()` expects numeric format, or vice versa. The `_coerce_numeric_args()` function in `mcp_mt5_wrapper.py` handles this for the wrapper but the direct endpoint may not.

**Acceptance Criteria:**
- [ ] `ea_bracket_start(buy_ticket, sell_ticket, bracket_id)` returns `{success: true}`
- [ ] When one leg fills, EA auto-cancels the other
- [ ] `ea_bracket_list()` shows active brackets
- [ ] `ea_bracket_stop(bracket_id)` cleanly removes bracket

**Estimated effort:** 1-2 hours

---

**Phase 1 Total: 6-9 hours**
**Parallelization:** 1.1 and 1.2 are Python-only (parallel). 1.3 is debugging (parallel). All three can run simultaneously.

---

## Phase 2: Auto-Trailing Integration (P0-2)

**Goal:** Agent submits order with `trail_config` → EA auto-trails from first tick. Zero separate calls needed.

### 2.1 EA: Extend `TrailingStopManager.mqh` for Configurable Timeframe

**Current state:** ATR is hardcoded to `PERIOD_H1, 14` (line 157).

**Change:** Add timeframe and period parameters to `StartTrailing()`:
```cpp
bool CTrailingStopManager::StartTrailing(
    ulong ticket, 
    double atr_multiplier, 
    int check_interval_seconds, 
    double lock_in_profit_atr = 0.0,
    ENUM_TIMEFRAMES atr_timeframe = PERIOD_H1,  // NEW
    int atr_period = 14,                         // NEW
    long magic_filter = 0
)
```

Store per-position: `m_atr_timeframes[]`, `m_atr_periods[]`. Update `GetATR()` to use per-position handles or create on-demand.

### 2.2 EA: Parse `trail_config` in `submit_order` Handler

**Location:** `BridgeConnectorEA.mq5`, lines 1856-1924 (`submit_order` handler).

**Add parsing:**
```cpp
string trail_atr_mult_s; double trail_atr_multiplier = 0;
string trail_lock_in_s; double trail_lock_in_atr = 0;
string trail_interval_s; int trail_check_interval = 10;
string trail_tf_s; ENUM_TIMEFRAMES trail_timeframe = PERIOD_H1;
string trail_period_s; int trail_atr_period = 14;

ParseKV(cmd, "trail_atr_multiplier", trail_atr_mult_s);
if(trail_atr_mult_s != "") trail_atr_multiplier = StringToDouble(trail_atr_mult_s);
ParseKV(cmd, "trail_lock_in_atr", trail_lock_in_s);
if(trail_lock_in_s != "") trail_lock_in_atr = StringToDouble(trail_lock_in_s);
ParseKV(cmd, "trail_check_interval", trail_interval_s);
if(trail_interval_s != "") trail_check_interval = (int)StringToInteger(trail_interval_s);
ParseKV(cmd, "trail_timeframe", trail_tf_s);
if(trail_tf_s != "") trail_timeframe = StringToTimeframe(trail_tf_s);
ParseKV(cmd, "trail_atr_period", trail_period_s);
if(trail_period_s != "") trail_atr_period = (int)StringToInteger(trail_period_s);
```

### 2.3 EA: Auto-Register Trailing on Order Fill

After successful `OrderSend()`, register position for trailing:
```cpp
if(trail_atr_multiplier > 0)
{
   // Build trail config comment for recovery
   string trail_comment = StringFormat(
       "trail:atr=%.1f|lock=%.1f|int=%d|tf=%s|per=%d",
       trail_atr_multiplier, trail_lock_in_atr, 
       trail_check_interval, 
       EnumToString(trail_timeframe), trail_atr_period
   );
   
   // After order fills, get position ticket and register
   // Note: For market orders, the position is created immediately
   // We need to look up the new position by magic number + symbol
   if(g_trailing_manager.AutoRegisterFromOrder(
       order_result.order, trail_atr_multiplier, trail_check_interval,
       trail_lock_in_atr, trail_timeframe, trail_atr_period
   ))
   {
       // Also embed trail config in position comment via modify
       AppendToPositionComment(order_result.order, trail_comment);
   }
}
```

**Helper: `AutoRegisterFromOrder()` in `CTrailingStopManager`:**
```cpp
bool CTrailingStopManager::AutoRegisterFromOrder(ulong order_ticket, ...)
{
   // Wait briefly for position to be created
   Sleep(100);
   
   // Find the new position by iterating positions
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      
      // Match by open time being very recent
      if(TimeCurrent() - PositionGetInteger(POSITION_TIME) < 5)
      {
         return StartTrailing(ticket, atr_multiplier, check_interval,
                              lock_in_profit_atr, atr_timeframe, atr_period);
      }
   }
   return false;
}
```

### 2.4 Python: Add `trail_config` to Submission Schemas

**`src/mt5_mcp/schemas/tools.py`:**
```python
class TrailConfig(BaseModel):
    atr_multiplier: float  # 0.5-5.0
    lock_in_profit_atr: float = 0.0  # ATR multiples before trailing begins
    check_interval_seconds: int = 10
    atr_timeframe: str = "H1"
    atr_period: int = 14

class TradeIntent(OwnershipMixin):
    # ... existing fields ...
    trail_config: TrailConfig | None = None
```

**`apps/mcp_server/main.py`:** Pass trail_config fields to EA command.

### 2.5 EA: Extend `JsonPositions()` with Trail Status

Add to each position's JSON:
```cpp
// Check if this position has active trailing
bool is_trailing = g_trailing_manager.IsTrailing(ticket);
double current_trail_sl = is_trailing ? g_trailing_manager.GetLastSL(ticket) : 0;

// Add to position JSON
",\"trail_active\":%s,\"trail_current_sl\":%G",
is_trailing ? "true" : "false", current_trail_sl
```

**Acceptance Criteria:**
- [ ] Order submission tools accept `trail_config` parameter
- [ ] EA stores trail config per position (via comment for recovery)
- [ ] EA `OnTimer()` auto-trails without any agent tool calls
- [ ] Trail actions are logged to journal via `trading/log_decision(action="trail")`
- [ ] Agent can query current trail state via `positions_open().health.trail_status`
- [ ] ATR timeframe/period configurable per position (not hardcoded H1/14)

**Estimated effort:** 6-8 hours

---

## Phase 3: Dedicated Wait/Trade Monitor Tool

**Goal:** A single tool that waits AND monitors price against expected/invalidation brackets, returning structured market context.

### 3.1 Tool Design

```python
# tools/wait/trade_monitor
# 
# What: Long-polling trade monitor that waits for a duration while monitoring
#       price against expected (target) and invalidation (stop) boundaries.
#       Returns immediately if either boundary is hit, or after timeout.
#
# Input:
#   - symbol: String. MT5 symbol name.
#   - duration: String. Format: "M5" (5 min), "M15" (15 min), "14:30" (wait until 2:30 PM),
#               "H1:4" (4 completed H1 bars), or integer (seconds, backwards compat).
#   - expected: Object. Target zone — alert when price reaches this.
#       - type: "price" | "pips" | "atr"
#       - value: Float. Absolute price, pip distance, or ATR multiple.
#       - direction: "favorable" (default) | "any"
#   - invalidation: Object. Stop monitoring zone — alert when price hits this.
#       - type: "price" | "pips" | "atr"
#       - value: Float.
#       - direction: "adverse" (default) | "any"
#   - side: String. "buy" | "sell" | "neutral" — determines favorable/adverse direction.
#   - timeframe: String. For bar-based duration and ATR calculations. Default: "H1".
#   - atr_period: Integer. For ATR-based brackets. Default: 14.
#   - check_interval_seconds: Integer. Sampling frequency. Default: 5.
#
# Output:
#   - symbol: String
#   - reason: "target_reached" | "invalidation_hit" | "timeout" | "error"
#   - current_price: Float
#   - bid: Float, ask: Float
#   - distance_to_target_pips: Float (signed: positive = not yet reached)
#   - distance_to_invalidation_pips: Float (signed: negative = already breached)
#   - elapsed_seconds: Int
#   - duration_seconds: Int
#   - timed_out: Bool
#   - market_context: {
#       regime: "ranging"|"trending_up"|"trending_down"|"compressing",
#       atr: Float,
#       rsi: Float,
#       spread_points: Int
#     }
```

### 3.2 Duration Parser

```python
def parse_duration(duration_str: str, default_timeframe: str = "H1") -> int:
    """Parse duration string into seconds.
    
    "M5" → 300
    "M10" → 600  
    "M15" → 900
    "14:30" → seconds until 14:30 UTC (or broker time)
    "H1:4" → 4 * 3600 = 14400 (4 H1 bars)
    "H4:2" → 2 * 14400 = 28800 (2 H4 bars)
    "300" → 300 (integer, backwards compatible)
    """
    duration_str = duration_str.strip()
    
    # Bar-based: "H1:4", "M15:8"
    bar_match = re.match(r'^(M1|M5|M15|M30|H1|H4|D1):(\d+)$', duration_str, re.IGNORECASE)
    if bar_match:
        tf = bar_match.group(1).upper()
        bars = int(bar_match.group(2))
        bar_seconds = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, 
                       "H1": 3600, "H4": 14400, "D1": 86400}
        return bars * bar_seconds.get(tf, 3600)
    
    # Minute-based: "M5", "M10", "M15"
    min_match = re.match(r'^M(\d+)$', duration_str, re.IGNORECASE)
    if min_match:
        return int(min_match.group(1)) * 60
    
    # Time-based: "14:30"
    time_match = re.match(r'^(\d{1,2}):(\d{2})$', duration_str)
    if time_match:
        target_hour = int(time_match.group(1))
        target_min = int(time_match.group(2))
        now = datetime.utcnow()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return int((target - now).total_seconds())
    
    # Integer: seconds
    return int(duration_str)
```

### 3.3 Bracket Computation

```python
def compute_bracket_levels(expected, invalidation, current_price, atr, side):
    """Compute absolute price levels from bracket specs."""
    
    def resolve(spec, direction="favorable"):
        if spec["type"] == "price":
            return spec["value"]
        elif spec["type"] == "pips":
            point = 0.0001  # from symbol_info
            if direction == "favorable" and side == "buy":
                return current_price + spec["value"] * point
            elif direction == "favorable" and side == "sell":
                return current_price - spec["value"] * point
            else:  # adverse
                return current_price - spec["value"] * point if side == "buy" else current_price + spec["value"] * point
        elif spec["type"] == "atr":
            if direction == "favorable" and side == "buy":
                return current_price + spec["value"] * atr
            elif direction == "favorable" and side == "sell":
                return current_price - spec["value"] * atr
            else:
                return current_price - spec["value"] * atr if side == "buy" else current_price + spec["value"] * atr
    
    target_price = resolve(expected, "favorable")
    invalidation_price = resolve(invalidation, "adverse")
    return target_price, invalidation_price
```

### 3.4 Implementation

**`apps/mcp_server/main.py`:**
```python
@app.post("/tools/tools/wait/trade_monitor")
def tool_wait_trade_monitor(req: WaitTradeMonitorRequest) -> dict:
    """Long-polling trade monitor with dual-boundary awareness."""
    start_time = time.time()
    duration_seconds = parse_duration(req.duration, req.timeframe)
    end_time = start_time + duration_seconds
    
    # Get initial market context
    atr = get_indicator(req.symbol, req.timeframe, "atr", period=req.atr_period)
    current_price = get_ticks(req.symbol, count=1)[0]["bid"]
    
    target_price, invalidation_price = compute_bracket_levels(
        req.expected, req.invalidation, current_price, atr, req.side
    )
    
    while time.time() < end_time:
        tick = get_ticks(req.symbol, count=1)[0]
        price = tick["bid"] if req.side != "sell" else tick["ask"]
        
        # Check boundaries
        if req.side == "buy":
            if price >= target_price:
                return build_monitor_result("target_reached", price, target_price, invalidation_price, tick)
            if price <= invalidation_price:
                return build_monitor_result("invalidation_hit", price, target_price, invalidation_price, tick)
        elif req.side == "sell":
            if price <= target_price:
                return build_monitor_result("target_reached", price, target_price, invalidation_price, tick)
            if price >= invalidation_price:
                return build_monitor_result("invalidation_hit", price, target_price, invalidation_price, tick)
        
        time.sleep(req.check_interval_seconds)
    
    # Timeout — return fresh market context
    return build_monitor_result("timeout", price, target_price, invalidation_price, 
                                tick, include_full_context=True)
```

**Acceptance Criteria:**
- [ ] Duration parsing: "M5", "M10", "M15", "14:30", "H1:4", integer all work
- [ ] Bracket types: price, pips, ATR all compute correctly
- [ ] Returns immediately on target_hit or invalidation_hit
- [ ] Returns fresh market_context (regime, atr, rsi) on completion
- [ ] Timeout after duration with graceful return (no errors)
- [ ] Tool spec added to `mcp_mt5_wrapper.py` with comprehensive description

**Estimated effort:** 4-6 hours

---

## Phase 4: Portfolio Risk & Correlation (P1-2, P1-4)

### 4.1 Portfolio Risk Tool (P1-2)

**Current state:** `src/mt5_mcp/services/portfolio_risk.py` exists with `PortfolioRiskService` class. Just needs tool endpoint.

**Add endpoint:**
```python
@app.post("/tools/portfolio/risk")
def tool_portfolio_risk(req: PortfolioExposureRequest) -> dict:
    """Complete portfolio picture in one call."""
    service = PortfolioRiskService()
    return service.compute_risk(
        positions=positions_open(),
        account=account_summary(),
        symbols=[p["symbol"] for p in positions],
        timeframe="H1",
        lookback=50
    )
```

### 4.2 Correlation in `validate_trade_setup()` (P1-4)

**Add to validation response:**
```python
def validate_trade_setup(req: ValidateTradeSetupRequest) -> dict:
    # ... existing validation ...
    
    # Correlation check against existing positions
    positions = positions_open()
    if positions and req.symbol:
        candidate_symbols = list(set([req.symbol] + [p["symbol"] for p in positions]))
        if len(candidate_symbols) >= 2:
            corr_matrix = compute_correlation_matrix(candidate_symbols, "H1", 50)
            correlation_warnings = []
            for pos in positions:
                pair_key = f"{req.symbol}_{pos['symbol']}"
                reverse_key = f"{pos['symbol']}_{req.symbol}"
                corr = corr_matrix.get(pair_key, corr_matrix.get(reverse_key))
                if corr and abs(corr) > 0.6:
                    combined_risk = compute_combined_risk(req, pos, corr)
                    correlation_warnings.append({
                        "existing_symbol": pos["symbol"],
                        "correlation_coefficient": round(corr, 2),
                        "existing_position": {"side": pos["side"], "volume": pos["volume"]},
                        "combined_risk_pct": round(combined_risk * 100, 1),
                        "recommendation": get_correlation_recommendation(corr, combined_risk)
                    })
            
            result["correlation_warning"] = {
                "correlated_with_existing": correlation_warnings,
                "portfolio_correlation_score": round(max(abs(w["correlation_coefficient"]) for w in correlation_warnings), 2) if correlation_warnings else 0,
                "risk_level": classify_correlation_risk(correlation_warnings)
            }
    
    return result
```

**Acceptance Criteria:**
- [ ] `portfolio/risk` returns complete portfolio picture in one call
- [ ] `validate_trade_setup()` includes `correlation_warning` object
- [ ] Checks against ALL existing open positions
- [ ] Uses 50-bar H1 lookback for correlation calculation
- [ ] Provides clear recommendation (reduce/skip/proceed)
- [ ] Health status: "low_risk" | "moderate_risk" | "high_risk" | "critical"

**Estimated effort:** 4-6 hours

---

## Phase 5: Time-Based Exit (P1-3)

**Goal:** EA auto-closes positions exceeding `max_hold_time` without minimum profit.

### 5.1 Create `ea/PositionTimeManager.mqh`

Following the exact pattern of `CTrailingStopManager`:

```cpp
class CPositionTimeManager
{
private:
   ulong             m_tickets[];
   datetime          m_entry_times[];
   int               m_max_hold_bars[];
   double            m_min_profit_points[];
   ENUM_TIMEFRAMES   m_timeframes[];
   int               m_entry_bar_counts[];
   string            m_symbols[];
   int               m_count;
   
public:
   bool Register(ulong ticket, int max_hold_bars, double min_profit_points, 
                 ENUM_TIMEFRAMES timeframe = PERIOD_H1);
   void Unregister(ulong ticket);
   int CheckAll();  // Returns number of positions time-exited
   string GetTimeHealth(ulong ticket);  // JSON: {"bars_elapsed":N,"bars_remaining":N}
   int GetActiveCount() { return m_count; }
   string GetActiveList();  // JSON array for monitoring
};
```

**`CheckAll()` logic:**
```cpp
int CPositionTimeManager::CheckAll()
{
   int closed = 0;
   for(int i = 0; i < m_count; i++)
   {
      ulong ticket = m_tickets[i];
      if(!PositionSelectByTicket(ticket)) { Unregister(ticket); i--; continue; }
      
      // Count bars since entry
      int current_bar_count = Bars(m_symbols[i], m_timeframes[i], m_entry_times[i], TimeCurrent());
      int bars_elapsed = current_bar_count - m_entry_bar_counts[i];
      
      if(bars_elapsed >= m_max_hold_bars[i])
      {
         double profit = PositionGetDouble(POSITION_PROFIT);
         double min_profit = m_min_profit_points[i] * SymbolInfoDouble(m_symbols[i], SYMBOL_POINT);
         
         if(profit < min_profit)
         {
            CTrade trade;
            if(trade.PositionClose(ticket))
            {
               Print("TIME_EXIT: Position #", ticket, " closed after ", bars_elapsed, " bars. Profit: ", profit);
               closed++;
               Unregister(ticket);
               i--;
            }
         }
      }
   }
   return closed;
}
```

### 5.2 Wire into EA

**`OnInit()`:**
```cpp
CPositionTimeManager g_time_manager;  // Global
// ... in OnInit: nothing special needed
```

**`OnTimer()`:**
```cpp
// After trailing and bracket processing
if(g_time_manager.GetActiveCount() > 0)
   g_time_manager.CheckAll();
```

**`submit_order` handler:**
```cpp
string max_hold_bars_s; int max_hold_bars = 0;
string min_profit_points_s; double min_profit_points = 0;
string hold_timeframe_s; ENUM_TIMEFRAMES hold_timeframe = PERIOD_H1;

ParseKV(cmd, "max_hold_bars", max_hold_bars_s);
if(max_hold_bars_s != "") max_hold_bars = (int)StringToInteger(max_hold_bars_s);
ParseKV(cmd, "min_profit_points", min_profit_points_s);
if(min_profit_points_s != "") min_profit_points = StringToDouble(min_profit_points_s);
ParseKV(cmd, "hold_timeframe", hold_timeframe_s);
if(hold_timeframe_s != "") hold_timeframe = StringToTimeframe(hold_timeframe_s);

// After successful order fill:
if(max_hold_bars > 0)
{
   g_time_manager.AutoRegister(order_result.order, max_hold_bars, 
                               min_profit_points, hold_timeframe);
}
```

### 5.3 Extend `JsonPositions()` with Time Health

```cpp
int bars_elapsed = 0, bars_remaining = 0;
if(g_time_manager.GetActiveCount() > 0)
{
   string health = g_time_manager.GetTimeHealth(ticket);
   // Append to position JSON
   out += ",\"time_health\":" + health;
}
```

### 5.4 Python Schema Updates

```python
class MaxHoldTime(BaseModel):
    max_hold_bars: int
    min_profit_points: float = 0
    timeframe: str = "H1"

class TradeIntent(OwnershipMixin):
    max_hold_time: MaxHoldTime | None = None
```

**Acceptance Criteria:**
- [ ] Order submission accepts `max_hold_time` parameter
- [ ] EA auto-closes positions that exceed time limit without minimum profit
- [ ] Time exits are logged to journal automatically
- [ ] Agent can query remaining time via `positions_open()[].health.time_health`
- [ ] Survives EA restart (recovery from position comment)

**Estimated effort:** 6-8 hours

---

## Phase 6: Skill & Tool Co-Engineering

**Goal:** Tool descriptions and skill playbook are mutually consistent, self-contained, and guide agents through the full trading cycle.

### 6.1 Tool Description Rewrite Pattern

Every tool in `mcp_mt5_wrapper.py` TOOL_SPECS gets enhanced with:

```
What: [current description — keep as-is]

When to use:
  - [Trigger 1 from skill playbook]
  - [Trigger 2]

Before calling:
  - [Pre-check 1]
  - [Pre-check 2]

After calling:
  - [Action 1 — e.g., "Reconcile via positions_open() within 30s"]
  - [Action 2]

Never:
  - [Anti-pattern 1 — e.g., "Submit without unique intent_id"]
  - [Anti-pattern 2]

Composed with:
  - [Related tool 1] → [Related tool 2]
```

**Example — `submit_market_order_via_bridge`:**
```
When to use:
  - After validate_trade_setup() returns valid=true
  - After economic_calendar() confirms no blackout windows
  - After correlation_matrix() if multi-symbol setup

Before calling:
  - calculate_position_size() for correct lot size
  - validate_trade_setup() for broker constraints
  - Check positions_open() for existing exposure

After calling:
  - Reconcile within 30s: positions_open() + deals_history(limit=5)
  - trading/log_decision(action="entry") with full parameters
  - If trail_config provided: verify trailing active via positions_open()[].health.trail_active

Never:
  - Submit without unique intent_id (use uuid4())
  - Submit duplicate orders (check orders_pending() first)
  - Skip journaling after submission
  - Submit during economic blackout (check economic_calendar())

Composed with:
  calculate_position_size() → validate_trade_setup() → submit_market_order_via_bridge() → 
  positions_open() → trading/log_decision() → tools/wait/trade_monitor()
```

### 6.2 SKILL.md Updates

1. **Replace conceptual tool references with exact names:**
   - "Submit market order" → `submit_market_order_via_bridge()`
   - "Submit pending order" → `submit_pending_order()`
   - "Check positions" → `positions_open()`
   - "Calculate position size" → `calculate_position_size()`

2. **Add Phase 11: Wait Protocol**
   - When to use `tools/wait/trade_monitor` vs `tools/wait/delay` vs `tools/wait/indicator`
   - Bracket setup patterns (price/pips/ATR)
   - Integration with trailing: "Set wait invalidation = trail breakeven level"

3. **Update Position Management (Phase 8):**
   - Reference `trail_config` in order submission (no separate trailing call needed)
   - Reference `positions_open()[].health` for all position metrics
   - Reference `max_hold_time` for stale position prevention

4. **Update Correlation Gate (Phase 3.5):**
   - Reference `validate_trade_setup()[].correlation_warning` as pre-entry check
   - Reference `portfolio/risk` for multi-position assessment

5. **Update Polling Protocol:**
   - Replace manual trailing checklist with `tools/wait/trade_monitor` pattern
   - Define when to use long-polling vs active polling tiers

### 6.3 New Reference File

Create `~/.agents/skills/mt5-trading/references/wait-protocol.md`:
- Duration format reference (M5, M10, M15, HH:MM, H1:N)
- Bracket configuration patterns
- Expected vs invalidation zone strategies
- Integration with trailing and time-based exits

### 6.4 Consistency Verification

Run a cross-reference check:
```python
# Verify every tool name in SKILL.md exists in mcp_mt5_wrapper.py
skill_tool_refs = extract_tool_names_from_skill()
wrapper_tool_names = set(TOOL_SPECS.keys())
missing = skill_tool_refs - wrapper_tool_names
assert missing == set(), f"SKILL.md references non-existent tools: {missing}"
```

**Acceptance Criteria:**
- [ ] Every tool description includes what/when/what-next/anti-patterns/pre-checks
- [ ] SKILL.md references tools by exact names (no conceptual descriptions)
- [ ] New Phase 11 (Wait Protocol) added to SKILL.md
- [ ] New references/wait-protocol.md created
- [ ] Cross-reference check passes (zero missing tool references)
- [ ] All tool descriptions reference correct schema fields

**Estimated effort:** 8-10 hours

---

## Phase 7: Roadmap Items (P2 — Deferred)

### 7.1 ISO Timestamps in Deals (P2-3)
**Effort:** 1 hour. Trivial. Add `time_iso` and `age_minutes` to `deals_history()` output.

### 7.2 Session Performance (P2-2)
**Effort:** 3-4 hours. New tool `session_performance(session_id)` that filters trades by order comment tag.

### 7.3 Event Subscription (P2-1)
**Effort:** 12-16 hours. WebSocket/SSE endpoint. Defer to separate project — highest effort, lowest immediate ROI.

**Phase 7 Total: 4-5 hours (excluding P2-1)**

---

## File Change Inventory

| File | Phase | Change Type | Lines Est. |
|---|---|---|---|
| `ea/TrailingStopManager.mqh` | 2 | Modify | +40 |
| `ea/BridgeConnectorEA.mq5` | 1,2,5 | Modify | +200 |
| `ea/PositionTimeManager.mqh` | 5 | **New** | ~300 |
| `src/mt5_mcp/schemas/tools.py` | 2,3,5 | Modify | +80 |
| `apps/mcp_server/main.py` | 1,3,4 | Modify | +300 |
| `tools/mcp_mt5_wrapper.py` | 3,6 | Modify | +400 |
| `~/.agents/skills/mt5-trading/SKILL.md` | 6 | Modify | +200 |
| `~/.agents/skills/mt5-trading/references/wait-protocol.md` | 6 | **New** | ~150 |

**Total: ~1670 lines added/modified across 8 files (6 modified, 2 created)**

---

## Dependency Graph

```
Phase 1 (Reliability) ──► Phase 2 (Auto-Trailing) ──┐
                     ──► Phase 5 (Time Exit) ────────┤
                                                    ▼
Phase 3 (Wait Monitor) ───────────────────────► Phase 6 (Skill/Tool Sync)
Phase 4 (Portfolio/Correlation) ────────────────► Phase 6 (Skill/Tool Sync)
```

**Parallel execution:**
- Phase 1, 3, 4 can all start simultaneously
- Phase 2 depends on Phase 1 (position health needed for trail_status)
- Phase 5 depends on Phase 1 (position health needed for time data)
- Phase 6 depends on ALL of 1-5 (can't sync tools that don't exist yet)
- Phase 7 is independent (can run anytime)

---

## Verification Strategy

| Phase | Verification Method |
|---|---|
| 1 | Unit tests with mocked EA responses; `positions_open()` health validation |
| 2 | EA compile check (zero warnings); demo account test with trail_config |
| 3 | Mock tests for duration parsing, bracket computation, timeout behavior |
| 4 | Existing `portfolio_risk.py` service tests; correlation computation validation |
| 5 | EA compile check; demo account test with max_hold_time |
| 6 | Cross-reference script (Python); manual SKILL.md readability review |
| 7 | Simple endpoint tests |

**Before each phase:** Run `poetry run pytest` — must pass.
**After each phase:** EA compile check — zero warnings.

---

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| positions_open() bug is in EA, not Python | Medium | High | Phase 1.1 includes both EA and Python fixes |
| Comment field collision (bracket + trail) | Low | Medium | Unified format: `bracket:abc123;trail:atr=2.0\|lock=1.5` |
| Long-polling timeout on wait tool | Low | Low | Graceful degradation — return partial context |
| EA OnTimer performance degradation | Low | Medium | Time manager CheckAll() runs only when active count > 0 |
| Skill/tool description bloat | Medium | Low | Enforce 500-char max per section; use bullet points |
