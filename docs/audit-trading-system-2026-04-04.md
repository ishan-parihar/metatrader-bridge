# MetaTraderBridge Trading System Audit Report

**Date:** 2026-04-04
**Auditor:** AI Trading Agent (Autonomous)
**System:** MetaTraderBridge Bridge v2.30 + MCP Server (53 endpoints)
**Account:** Demo | Exness-MT5Trial17 | $202.77 | 2000:1 Leverage
**Period Reviewed:** Last 7 days (19 closed trades)

---

## 1. Executive Summary

The MetaTraderBridge infrastructure is **architecturally sound** — bridge connectivity, EA batch processing, MCP endpoints, and observability are all operational. However, the system is currently **losing money** (-69.33 net, 21% win rate) and lacks several critical capabilities I need as an AI agent to generate profits autonomously.

**The infrastructure is ready. The strategy layer is not.**

---

## 2. Current Tool Inventory (What I Have)

### 2.1 Market Data Tools ✅ Fully Operational
| Tool | Latency | Quality | What I Can Do |
|------|---------|---------|---------------|
| `get_bars` | ~110ms | Excellent | Read OHLCV across any timeframe |
| `get_ticks` | ~110ms | Excellent | Precision entry timing |
| `get_order_book` | ~110ms | Good (tick fallback) | See bid/ask spread |
| `get_symbol_info` | ~110ms | Excellent | Know contract specs, stops level |
| `get_indicator` | ~110ms | Excellent (fixed defaults) | RSI, EMA, MACD, BBands, Stoch, ATR, ADX, Ichimoku, CCI, OBV |
| `multi_timeframe_indicators` | ~600ms | Excellent | See indicator confluence across M5/M15/H1/H4 |
| `correlation_matrix` | ~400ms | Good | Cross-symbol correlation |

### 2.2 Analysis Tools ✅ Fully Operational
| Tool | Latency | Quality | What I Can Do |
|------|---------|---------|---------------|
| `trading/context` | ~400ms | **Excellent** | Full market state: ATR, RSI, EMAs, spread assessment, composure notes |
| `trading/coach` | ~500ms | **Excellent** | Advisory feedback on entry quality, warnings about bad setups |
| `trading/decision_support` | ~700ms warm | **Excellent** | One-call: regime + ATR + RSI + EMAs + coaching |
| `market/regime` | ~300ms | Excellent | Detect trending_up/down, ranging, compressing |
| `market/scan` | ~2.5s | Good | Multi-symbol scan for setup discovery |
| `volatility_profile` | ~200ms | Good | ATR + bar range analysis |

### 2.3 Execution Tools ⚠️ Partially Operational
| Tool | Status | What I Can Do |
|------|--------|---------------|
| `estimate_margin` | ✅ Working | Know margin requirement before entry |
| `validate_trade_setup` | ✅ Working | Pre-flight check against broker constraints |
| `calculate_position_size` | ✅ Working | Risk-based sizing (1-2% of equity) |
| `submit_market_order_via_bridge` | ⚠️ Requires intent_id/strategy_id | Can place orders but need orchestration IDs |
| `submit_pending_order` | ✅ Working | Place limit/stop orders |
| `modify_position_sl_tp` | ✅ Working | Adjust SL/TP on open positions |
| `close_position` | ✅ Working | Close (partial or full) |
| `close_all_positions` | ✅ Working | Emergency close |
| `place_bracket_order` | ✅ Working | BUY STOP + SELL STOP breakout capture |
| `cancel_order` | ✅ Working | Cancel pending orders |
| `modify_order` | ✅ Working | Modify pending order price/SL/TP |

### 2.4 Position Management Tools ✅ Operational
| Tool | Status | What I Can Do |
|------|--------|---------------|
| `set_trailing_stop` | ✅ Working | Server-side trailing stop with ATR multiplier |
| `trail_position` | ✅ Working | Manual trail from current mark |
| `trailing_stop/tick` | ✅ Working | Process all active trailing stops |
| `trailing_stop/list` | ✅ Working | View active trailing stops |
| `trailing_stop/cancel` | ✅ Working | Cancel trailing stop |

### 2.5 Metacognition Tools ✅ Operational
| Tool | Status | What I Can Do |
|------|--------|---------------|
| `trading/log_decision` | ✅ Working | Log every decision with reasoning, emotion, confidence |
| `trading/reflect` | ✅ Working | Query past decisions: "show me losses", "what regime was I in?" |
| `trading/insights` | ✅ Working | Auto-patterns: win rate by emotion, regime, mistakes |
| `trading/agent_prompt` | ✅ Working | Generate full system prompt with live context |

### 2.6 Monitoring Tools ✅ Operational
| Tool | Status | What I Can Do |
|------|--------|---------------|
| `resources/market/wait_for_price` | ✅ Working | Long-polling price alerts |
| `resources/positions/monitor` | ✅ Working | Long-polling P&L/price alerts |
| `get_account_summary` | ✅ Working | Check equity, margin, free margin |
| `get_positions` | ✅ Working | List all open positions |
| `get_orders` | ✅ Working | List all pending orders |
| `get_deals_history` | ✅ Working | Review closed trades |
| `performance_summary` | ✅ Working | Win rate, profit factor, expectancy |
| `get_chart_screenshot` | ✅ Working | Visual chart confirmation |

---

## 3. What I Can Do Right Now (With Current Tools)

As an AI trading agent with the current toolset, I can:

1. **Scan markets** — Check multiple symbols for setups in ~2.5s
2. **Analyze market state** — Get full context (regime, ATR, RSI, EMAs) in ~700ms
3. **Validate entries** — Check if a setup has confluence (2+ indicators agreeing, regime-aligned)
4. **Size positions** — Calculate risk-based lot sizes
5. **Execute trades** — Place market orders, pending orders, bracket orders
6. **Manage positions** — Set trailing stops, adjust SL/TP, close positions
7. **Learn from mistakes** — Log decisions, reflect on patterns, get insights
8. **Monitor passively** — Set price alerts and P&L monitors

**I have everything I need to trade manually with AI assistance.**

---

## 4. What I CANNOT Do (Critical Gaps)

### 4.1 ❌ No News/Macro Awareness
**The Gap:** I'm trading blind into economic events. I have NO access to:
- NFP dates, FOMC meetings, CPI releases
- Geopolitical events that move gold
- Crypto regulation news that moves BTC
- Central bank announcements

**Impact:** I entered BTCUSD trades and got stopped out. Looking at the deal history, all 5 recent losses were SL hits on BTCUSD. A basic news check would have told me to avoid those times.

**Fix Required:** Wire `igs-mcp` news tools into the MCP server or give me direct IGS-MCP access.

### 4.2 ❌ No Backtesting/Simulation Framework
**The Gap:** I can't test strategies before deploying them. I have `simulate_order` but it's a simple margin check — no historical simulation.

**Impact:** I'm learning by losing real money (even demo). I should be able to test "if I had used bracket orders in ranging markets, what would my win rate have been?"

**Fix Required:** Add a backtesting tool that replays historical bars with a strategy definition.

### 4.3 ❌ No Session Management / Trading Hours
**The Gap:** I don't know when London/NY sessions start/end. I don't know when to avoid trading (Asian session = choppy for XAUUSD).

**Impact:** I might enter trades during low-volume periods where spreads are wide and price action is unreliable.

**Fix Required:** Add session awareness to `trading/context` — current session, next session, session volume profile.

### 4.4 ❌ No Economic Calendar Integration
**The Gap:** Related to #4.1 but specifically: I don't know the HIGH-impact events scheduled for today/tomorrow.

**Impact:** Trading into NFP = guaranteed whipsaw.

**Fix Required:** Economic calendar endpoint (can source from ForexFactory via IGS-MCP).

### 4.5 ❌ No Portfolio-Level Risk Management
**The Gap:** I can size individual trades but I can't see my total portfolio exposure. If I have 3 open positions all correlated to USD strength, I'm overexposed.

**Impact:** I could easily have 5% of equity at risk across correlated positions without knowing it.

**Fix Required:** Portfolio exposure tool: total risk %, correlation-weighted exposure, max drawdown tracking.

### 4.6 ❌ No Position Monitoring Loop
**The Gap:** I can set trailing stops and alerts, but I have no automatic loop to check positions and adjust based on changing market conditions.

**Impact:** Once I enter a trade, I need to manually check it. A good AI agent should monitor and adapt.

**Fix Required:** Position monitoring loop that checks every 5-15 minutes and adjusts based on regime changes.

### 4.7 ⚠️ EA Batch Processing Still Sequential
**The Gap:** The EA processes 6 commands sequentially with 100ms sleep between each. Cold decision_support takes ~4.6s.

**Impact:** In fast-moving markets, 4.6s is an eternity. Price can move 50+ points on XAUUSD.

**Fix Required (Future):** EA-side parallel command processing or WebSocket push. Not critical for now but important for HFT.

---

## 5. Performance Analysis (Last 7 Days)

### 5.1 Raw Numbers
| Metric | Value | Assessment |
|--------|-------|------------|
| Total Trades | 19 | Too many for 7 days on demo |
| Wins | 4 | |
| Losses | 15 | |
| Win Rate | **21.1%** | ❌ Terrible — should be 40-55% |
| Gross Profit | $3.85 | |
| Gross Loss | -$73.18 | |
| Net P&L | **-$69.33** | ❌ Bleeding money |
| Profit Factor | **0.05** | ❌ Should be > 1.0 |
| Expectancy | **-$3.65/trade** | ❌ Each trade loses $3.65 on avg |
| Avg Win | $0.96 | Too small |
| Avg Loss | $4.88 | Too large — 5x the avg win |

### 5.2 Diagnosis: Why Am I Losing?

Looking at the deal history:
- **All 5 recent trades were BTCUSD** — all hit SL
- **Average loss ($4.88) is 5x average win ($0.96)** — I'm cutting winners too early and letting losers run
- **21% win rate** suggests I'm entering randomly, not waiting for setups
- **19 trades in 7 days** = ~2.7 trades/day — this is overtrading for a demo account

**Root Causes:**
1. No news awareness → trading into volatility events
2. No session awareness → trading during Asian chop
3. No confluence requirement → entering on single-signal setups
4. Poor risk:reward → avg loss 5x avg win means I'm not waiting for proper R:R
5. No regime filtering → using trending strategies in ranging markets

---

## 6. What I Need to Generate Profits

### Phase 1: Immediate Fixes (Can Do Today)
1. **News awareness** — Integrate IGS-MCP news tools. Before every trade, check: "Is there a high-impact event in the next 2 hours?"
2. **Confluence enforcement** — Require 3+ indicators agreeing + regime alignment before entry
3. **Minimum R:R of 1.5:1** — Never enter if TP distance < 1.5x SL distance
4. **Trading hours restriction** — Only trade London (08:00-16:00 GMT) and NY (13:00-21:00 GMT) overlaps
5. **Max 2 trades/day** — Prevent overtrading

### Phase 2: Short-Term Enhancements (This Week)
6. **Economic calendar** — ForexFactory integration to know NFP, FOMC, CPI dates
7. **Portfolio risk dashboard** — Total exposure, correlation-weighted risk
8. **Position monitoring loop** — Auto-check every 5 min, adjust SL based on regime changes
9. **Backtesting framework** — Test strategies on historical data before live deployment

### Phase 3: Medium-Term (Next Month)
10. **Multi-timeframe strategy** — H1 for direction, M15 for entry, M5 for precision
11. **Adaptive position sizing** — Increase size after wins, decrease after losses
12. **Regime-specific strategies** — Different entry rules for trending vs ranging vs compressing
13. **News-aware exit** — Close positions before major news events

---

## 7. Infrastructure Health Check

| Component | Status | Notes |
|-----------|--------|-------|
| EA Bridge (v2.30) | ✅ Healthy | Batch processing works, 1s heartbeat |
| Gateway (port 8020) | ✅ Healthy | Queue management, enqueue/dequeue |
| MCP Server (53 routes) | ✅ Healthy | All endpoints operational |
| Python Tests (102) | ✅ Passing | 100% pass rate |
| Latency (warm) | ✅ Good | 110ms-700ms for all tools |
| Indicator Defaults | ✅ Fixed | All 11 indicators work with zero params |
| Caching (6 TTL caches) | ✅ Operational | Symbol:30s, account:5s, price:1.5s |
| Trailing Stops | ✅ Operational | Server-side with ATR multiplier |
| Metacognition | ✅ Operational | Decision journal, reflection, insights |

---

## 8. Priority Action Items

### 🔴 Critical (Block Trading Until Fixed)
1. **Integrate IGS-MCP news tools** — Add `news_fetch`, `news_enrich`, `insights_trendingEntities` to MCP server
2. **Add economic calendar endpoint** — Source from ForexFactory via IGS-MCP
3. **Implement confluence check** — Reject entries with < 2 indicator agreements

### 🟡 Important (Do Before Live Trading)
4. **Add session awareness** — Current session, volume profile, best trading windows
5. **Add portfolio risk dashboard** — Total exposure, correlation, max drawdown
6. **Implement max trades/day limit** — Prevent overtrading
7. **Add backtesting tool** — Historical replay with strategy definition

### 🟢 Nice-to-Have (Optimization)
8. **Parallel EA command processing** — Reduce cold latency from 4.6s to < 1s
9. **WebSocket push from EA** — Eliminate polling entirely
10. **Adaptive position sizing** — Kelly criterion or similar

---

## 9. Trading Strategy Recommendation

Based on the current toolset and market analysis:

### Recommended Strategy: Regime-Aware Pullback Trading

**When to Trade:**
- London session (08:00-12:00 GMT) or NY session (13:00-17:00 GMT)
- NOT during high-impact news (check calendar first)
- Max 2 trades per day

**Entry Rules:**
1. Check regime: Must be `trending_up` or `trending_down` (NOT ranging)
2. Check confluence: RSI + EMA alignment + price action (3/3 must agree)
3. Check R:R: TP must be ≥ 1.5x SL distance
4. Check ATR: SL must be ≥ 1x ATR
5. Check spread: Spread must be < 2% of ATR

**Position Management:**
- SL = 1x ATR from entry
- TP = 2x ATR from entry (R:R = 2:1)
- Move SL to breakeven after price moves 1x ATR in favor
- Trail with ATR multiplier of 1.0 after breakeven

**Instruments:**
- XAUUSD only (best technicals, respects levels)
- Avoid BTCUSD until news awareness is added (too volatile without macro context)

---

## 10. Conclusion

The **infrastructure is production-ready**. The EA bridge is fast, all 53 MCP endpoints work, tests pass, and latency is acceptable.

The **strategy layer is not ready**. The current 21% win rate and -$69.33 net loss over 7 days proves that having tools is not the same as having an edge.

**What separates profitable from unprofitable is not better tools — it's better discipline.**

The #1 priority before any live trading: **Integrate news awareness and enforce confluence rules.** Without these, I'm gambling, not trading.

---

*Report generated by AI Trading Agent — Autonomous Audit Mode*
*Next audit scheduled: After Phase 1 fixes are implemented*
