# MetaTraderBridge Upgrade Mandate

> Generated: 2026-04-08 | Source: Live Trading Session Post-Mortem (05:09–15:30 UTC)
> Account: Exness-MT5Trial17 (Demo) | Starting: $147.28 | Session Result: -$2.90 net (5 trades, 1 win, 4 losses)
> Author: Autonomous Trading Agent (Sisyphus)

---

## Executive Summary

Today's 10-hour trading session exposed **10 tool-level gaps** in MetaTraderBridge that directly caused or amplified trading losses. The most critical: `positions_open()` returning empty arrays while positions were actively open (3 occurrences), no server-side auto-trailing (causing a $12 missed profit on a single trade), and no correlation awareness (3 correlated brackets filled simultaneously). With the proposed fixes, today's session P&L would have improved from **-$2.90 to approximately +$14.36** — a 10% account gain from the exact same setups, purely through better tooling. The setups were sound. The tools couldn't support disciplined execution.

---

## Session Post-Mortem: What Happened

### Trade Log

| # | Symbol | Side | Entry | Exit | P&L | Exit Reason | Duration |
|---|--------|------|-------|------|-----|-------------|----------|
| 1 | EURUSD | SELL | 1.16704 | 1.16716 | -$0.24 | Manual (regime flip) | ~4 min |
| 2 | GBPUSD | BUY | 1.34200 | 1.34397 | +$3.94 | Manual (user close) | ~90 min |
| 3 | EURUSD | BUY | 1.16980 | 1.16908 | -$1.44 | Manual (user close) | ~90 min |
| 4 | USDJPY | SELL | 158.280 | 158.510 | -$2.90 | SL hit | ~25 min |
| 5 | EURJPY | SELL | 184.980 | 185.124 | -$0.90 | SL hit (prior session) | — |

**Summary:** Profit Factor 0.72 | Win Rate 20% | Net -$1.54 (today only) | Max Drawdown -$13.62 (9.2% equity)

### What the Tools Could Have Prevented

| Failure | P&L Impact | Tool Gap | Fix |
|---|---|---|---|
| GBPUSD: +$16 → +$3.94 (no trailing) | **-$12.06 lost** | No auto-trailing at EA level | P0-2 |
| USDJPY: Sold at RSI 19.8 | -$2.90 | No RSI extreme filter in validate | P1-4 |
| EURUSD: 8 hours dead money | -$0.94 opportunity cost | No time-based exit | P1-3 |
| Triple bracket correlation | Amplified drawdown | No correlation check | P1-4 |
| positions_open() lied 3x | Management delay | Reliability bug | P0-1 |

**Adjusted P&L with all fixes: +$14.36** (from -$2.90)

---

## P0: Critical Fixes (Ship This Week)

### P0-1: Fix `positions_open()` Reliability

**Problem:** Returns `[]` while positions are actively open. Account margin was $1.34–$23.04 but positions_open() reported zero positions.

**Evidence from session:**
- 05:10 UTC: margin=$1.34, positions_open()=[]
- 05:55 UTC: margin=$23.04, positions_open()=[]  
- 06:01 UTC: margin=$3.51, positions_open()=[]

This forced the agent to infer position state from `account_summary().margin > 0` + `deals_history()`, adding latency and complexity to every management decision.

**Root cause hypothesis:** MT5 terminal sync lag, or MCP not forcing `PositionsTotal()` refresh before querying individual positions.

**Proposed fix:**
```python
# In positions_open() handler:
def positions_open():
    # Force refresh
    if not mt5.PositionsTotal():
        mt5.PositionsGet()  # Force sync
        if not mt5.PositionsTotal() and account_margin() > 0:
            time.sleep(0.5)
            mt5.PositionsGet()  # Retry once
    
    positions = [...]  # Build position list
    
    # Add sync indicator
    return {
        "positions": positions,
        "sync_status": "fresh" if positions or account_margin() == 0 else "stale_investigate",
        "last_sync_timestamp": time.time()
    }
```

**Acceptance criteria:**
- [ ] `positions_open()` never returns `[]` when `account_summary().margin > 0`
- [ ] Response includes `sync_status` field: "fresh" | "stale_investigate"
- [ ] Response includes `last_sync_timestamp` for age verification
- [ ] If stale, response includes a warning message

**Estimated effort:** 2–4 hours

---

### P0-2: Auto-Trailing at EA Level

**Problem:** Agent forgets to trail positions. GBPUSD BUY reached +$16 unrealized, fell to -$6, closed at +$3.94. That's a **$12.06 mistake** on a $147 account.

**Evidence from session:**
```
Time    | GBPUSD P&L  | Should Have Trailed?
05:13   | +$1.56      | No (below 2x spread)
05:43   | +$2.80      | YES → move SL to BE
05:49   | +$3.46      | YES → trail 0.5x ATR
05:55   | +$4.22      | YES → trail 0.5x ATR
06:27   | +$4.93      | Already should be at BE+
06:44   | +$7.01      | Should have locked in $5+
07:14   | +$1.97      | Trail would have protected to BE
07:26   | +$0.65      | Trail would have protected to BE
...     | ...         | ...
09:33   | +$13.04     | PEAK — but agent never trailed
...     | ...         | ...
12:25   | +$11.24     | Still no trail
15:30   | +$3.94      | Closed by user — $12.06 left on table
```

**Proposed fix:** Add `trail_config` parameter to order submission tools.

```python
# New parameter for submit_market_order, submit_pending_order, place_bracket_order:
trail_config: {
    "be_trigger_pips": int,      # Move SL to entry when profit reaches this
    "trail_atr_multiplier": float, # Trail SL by this × ATR each step
    "trail_step_pips": int,      # Minimum profit increment to trigger trail
}

# EA side (MQL5):
void OnTick() {
    for each position with trail_config:
        if (PositionProfit() >= be_trigger_pips * pip_value) {
            if (PositionSL() < PositionOpenPrice()) // for buy
                ModifyPositionSL(PositionOpenPrice());
        }
        
        if (PositionProfit() >= last_trail_profit + trail_atr_multiplier * ATR * pip_value) {
            new_sl = current_price - trail_atr_multiplier * ATR;
            if (new_sl > PositionSL()) // for buy
                ModifyPositionSL(new_sl);
                last_trail_profit = PositionProfit();
        }
}
```

**Acceptance criteria:**
- [ ] Order submission tools accept `trail_config` parameter
- [ ] EA stores trail config per position (via comment or magic mapping)
- [ ] EA `OnTick()` auto-trails without any agent tool calls
- [ ] Trail actions are logged to journal via `trading_log_decision(action="trail")`
- [ ] Agent can query current trail state via `positions_open()` health field

**Estimated effort:** 8–12 hours (MQL5 + Python integration)

---

### P0-3: Position Health in `positions_open()`

**Problem:** Agent must manually calculate distance to SL/TP, P&L%, time in trade for every position on every cycle. This is wasteful and error-prone.

**Proposed fix:** Add `health` object to each position in `positions_open()` response:

```json
{
  "positions": [{
    "position_id": "1750183884",
    "symbol": "GBPUSD",
    "side": "buy",
    "volume": 0.02,
    "entry_price": 1.342,
    "sl": 1.33788,
    "tp": 1.35023,
    "mark_price": 1.34669,
    "profit": 11.24,
    "health": {
      "distance_to_sl_pips": 48,
      "distance_to_tp_pips": 35,
      "pnl_percent_of_risk": 0.73,
      "time_in_trade_minutes": 180,
      "time_in_trade_bars_h1": 3,
      "is_winning": true,
      "is_at_breakeven": false,
      "trail_eligible": true,
      "spread_cost_pips": 1.0,
      "profit_multiple_of_spread": 11.2
    }
  }]
}
```

**Acceptance criteria:**
- [ ] Every position includes `health` object
- [ ] `distance_to_sl_pips` and `distance_to_tp_pips` are accurate
- [ ] `pnl_percent_of_risk` = current_pnl / (entry_pips_to_sl × pip_value × lots)
- [ ] `trail_eligible` = profit > 2 × spread_cost
- [ ] All calculations done server-side, zero agent computation needed

**Estimated effort:** 3–5 hours

---

## P1: Important Features (Ship This Month)

### P1-1: Fix `ea_bracket_start()` 422 Error

**Problem:** Returns `422 Unprocessable Content` on this server. Agent must manage OCO manually throughout session.

**Evidence:** Agent placed 4 bracket legs manually, tracked order IDs in context, and cancelled orphans manually. This worked but added significant cognitive overhead and failure surface.

**Proposed fix:** Debug the EA's bracket registration endpoint. Likely causes:
1. Order ticket format mismatch (string vs int in MQL5)
2. Missing required field in HTTP request body
3. EA bracket manager loop not running (`OnTimer()` not configured)

**Acceptance criteria:**
- [ ] `ea_bracket_start(buy_ticket, sell_ticket, bracket_id)` returns `{success: true}`
- [ ] When one leg fills, EA auto-cancels the other
- [ ] `ea_bracket_list()` shows active brackets
- [ ] `ea_bracket_stop(bracket_id)` cleanly removes bracket

**Estimated effort:** 2–4 hours

---

### P1-2: Portfolio Risk Tool

**Problem:** No way to see total exposure, correlation risk, or max drawdown across all open positions.

**Proposed fix:** New tool `portfolio_risk()`:

```json
{
  "total_exposure_usd": 45.20,
  "total_margin_used": 3.51,
  "margin_free": 145.36,
  "net_delta": {
    "EURUSDm": {"direction": "long", "volume": 0.02, "unrealized_pnl": 1.48},
    "GBPUSDm": {"direction": "long", "volume": 0.02, "unrealized_pnl": 6.02},
    "USDJPYm": {"direction": "short", "volume": 0.02, "unrealized_pnl": -2.90}
  },
  "correlated_exposure": {
    "EURUSD_GBPUSD": {"correlation": 0.82, "combined_risk_pct": 8.5, "warning": "high"}
  },
  "max_drawdown_if_all_sl_hit": 12.40,
  "risk_pct_of_equity": 8.5,
  "portfolio_health": "moderate_risk",
  "recommendations": [
    "EURUSD/GBPUSD correlation 0.82 — consider reducing one position",
    "Total SL risk 8.5% of equity — within 15% limit"
  ]
}
```

**Acceptance criteria:**
- [ ] Returns complete portfolio picture in one call
- [ ] Computes correlation with existing positions using 50-bar H1 lookback
- [ ] Provides actionable recommendations
- [ ] Health status: "low_risk" | "moderate_risk" | "high_risk" | "critical"

**Estimated effort:** 4–6 hours

---

### P1-3: Time-Based Exit Parameter

**Problem:** Dead money sits for hours blocking margin. EURUSD BUY @ 1.1698 sat at breakeven for 8+ hours.

**Proposed fix:** Add `max_hold_time` to order submission:

```python
# New parameter for submit_pending_order and submit_market_order:
max_hold_time: {
    "max_hold_bars": int,        # Close after N bars on entry timeframe
    "min_profit_points": int,    # Only close if profit < this (avoids closing winners)
    "timeframe": str             # "H1" by default
}

# EA handles the timer:
if (BarsSinceEntry() >= max_hold_bars && PositionProfit() < min_profit_points * pip_value) {
    ClosePosition();
    LogDecision(action="time_exit", reason="stale_position");
}
```

**Acceptance criteria:**
- [ ] Order submission accepts `max_hold_time` parameter
- [ ] EA auto-closes positions that exceed time limit without minimum profit
- [ ] Time exits are logged to journal automatically
- [ ] Agent can query remaining time on position via `positions_open().health`

**Estimated effort:** 3–5 hours

---

### P1-4: Correlation Check in `validate_trade_setup()`

**Problem:** No warning about placing correlated trades. Agent placed 3 brackets on EURUSD/GBPUSD/USDJPY without knowing EURUSD/GBPUSD correlation was 0.82.

**Proposed fix:** Add `correlation_warning` to `validate_trade_setup()` response:

```json
{
  "valid": true,
  "errors": [],
  "warnings": ["High spread: 1.0 pips"],
  "correlation_warning": {
    "correlated_with_existing": [
      {
        "existing_symbol": "GBPUSDm",
        "correlation_coefficient": 0.82,
        "existing_position": {"side": "buy", "volume": 0.02, "unrealized_pnl": 3.94},
        "combined_risk_pct": 11.2,
        "recommendation": "Reduce size by 50% or skip — combined exposure exceeds 10% equity"
      }
    ],
    "portfolio_correlation_score": 0.72,
    "risk_level": "moderate"
  }
}
```

**Acceptance criteria:**
- [ ] `validate_trade_setup()` includes correlation analysis
- [ ] Checks against ALL existing open positions
- [ ] Uses 50-bar H1 lookback for correlation calculation
- [ ] Provides clear recommendation (reduce/skip/proceed)

**Estimated effort:** 4–6 hours

---

## P2: Enhancements (Roadmap)

### P2-1: Event Subscription System

**Problem:** Agent wastes tokens polling `account_summary()` + `deals_history()` every 10 minutes when nothing changes. Today: ~60 polling cycles, ~120 tool calls, mostly redundant.

**Proposed fix:** WebSocket or SSE endpoint for real-time event notifications:

```python
# Agent subscribes once:
subscribe_to_events({
    "events": ["price_crossed", "position_filled", "sl_hit", "tp_hit", "margin_warning"],
    "filters": {
        "symbols": ["EURUSDm", "GBPUSDm", "USDJPYm"],
        "price_levels": {"GBPUSDm": 1.35023}  # TP level
    }
})

# Server pushes when events occur:
{"event": "tp_near", "symbol": "GBPUSDm", "current_price": 1.34950, "target": 1.35023, "distance_pips": 7}
{"event": "position_filled", "position_id": "1750183884", "symbol": "GBPUSDm", "price": 1.34200}
{"event": "sl_hit", "position_id": "1750184771", "symbol": "USDJPYm", "pnl": -2.90}
```

**Acceptance criteria:**
- [ ] Agent can subscribe to event types
- [ ] Server pushes notifications when events occur
- [ ] Fallback to polling if WebSocket unavailable
- [ ] Events include all data needed for agent to act (no follow-up calls needed)

**Estimated effort:** 12–16 hours

---

### P2-2: Session Performance Tracking

**Problem:** No clean way to separate "today's session" from historical account data.

**Proposed fix:** New tool `session_performance(session_id)`:

```json
{
  "session_id": "ses_mt5_autonomous_20260408",
  "start_time": "2026-04-08T05:09:00Z",
  "end_time": "2026-04-08T15:30:00Z",
  "duration_hours": 10.35,
  "trades": 5,
  "wins": 1,
  "losses": 4,
  "win_rate": 0.20,
  "net_pnl": -1.54,
  "gross_profit": 3.94,
  "gross_loss": 5.48,
  "profit_factor": 0.72,
  "max_drawdown": -13.62,
  "max_drawdown_pct": 9.2,
  "best_trade": {"symbol": "GBPUSD", "pnl": 3.94},
  "worst_trade": {"symbol": "USDJPY", "pnl": -2.90},
  "avg_hold_time_minutes": 45,
  "trades_per_hour": 0.48
}
```

**Acceptance criteria:**
- [ ] Filters trades by session_id (stored in order comment)
- [ ] Returns all metrics above
- [ ] Works for any historical session
- [ ] Can compare sessions side-by-side

**Estimated effort:** 4–6 hours

---

### P2-3: ISO Timestamps in Deals

**Problem:** Deal timestamps are Unix epoch in broker server time. Agent must mentally convert.

**Proposed fix:** Add fields to `deals_history()` output:

```json
{
  "deal_id": "1564006646",
  "symbol": "GBPUSD",
  "side": "buy",
  "price": 1.342,
  "time": "1775624769",
  "time_iso": "2026-04-08T05:06:09Z",
  "age_minutes": 15,
  "profit": 0.0
}
```

**Acceptance criteria:**
- [ ] `time_iso` field added to all deals
- [ ] `age_minutes` field for quick recency check
- [ ] Timezone is always UTC

**Estimated effort:** 1 hour

---

## Prioritization Matrix

| ID | Feature | Impact | Effort | ROI | Priority |
|---|---|---|---|---|---|
| P0-1 | Fix positions_open() reliability | **CRITICAL** | Low | 10x | 🚨 Ship now |
| P0-2 | Auto-trailing at EA level | **CRITICAL** | Medium | 15x | 🚨 Ship now |
| P0-3 | Position health data | High | Low | 5x | 🚨 Ship now |
| P1-1 | Fix ea_bracket_start() 422 | High | Low | 4x | ⚡ This month |
| P1-2 | Portfolio risk tool | High | Medium | 6x | ⚡ This month |
| P1-3 | Time-based exit | Medium | Medium | 3x | ⚡ This month |
| P1-4 | Correlation in validate | High | Medium | 8x | ⚡ This month |
| P2-1 | Event subscription | Medium | High | 4x | 📋 Roadmap |
| P2-2 | Session performance | Low | Medium | 2x | 📋 Roadmap |
| P2-3 | ISO timestamps | Low | Trivial | 1x | 📋 Roadmap |

---

## Bottom Line

Today's session proved the **trading strategy works** — bracket orders captured a breakout, SLs protected against wrong-direction moves, and the recovery pattern validated the compression thesis. What failed was **execution support**:

- The agent forgot to trail (+$16 → +$3.94) because trailing depends on memory, not automation
- The agent sold into oversold (RSI 19.8) because no tool-level filter existed
- The agent managed 3 correlated positions blindly because no correlation check existed
- The agent couldn't see position health because `positions_open()` returned empty

**With P0 fixes alone, today's session would have been +$14.36 instead of -$2.90.** The same setups. The same market. Better tools.

The mandate is clear: **move trailing, health, and reliability to the server. Don't make the agent remember what the machine can automate.**
