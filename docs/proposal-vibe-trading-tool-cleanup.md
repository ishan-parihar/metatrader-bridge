# Proposal: MetaTraderBridge MCP Tool Consolidation with Vibe-Trading Integration

**Date:** 2026-04-14
**Branch:** `feature/vibe-trading-integration`
**Status:** Pending Review

---

## Executive Summary

MetaTraderBridge now has Vibe-Trading integrated as a research/strategy/backtesting backend. This creates overlap with ~18 existing MetaTraderBridge tools. This proposal identifies which tools to remove, which to keep, and — critically — assesses whether Vibe-Trading's data capabilities are production-grade enough to justify replacing MetaTraderBridge-native tools.

**Bottom-line recommendation:** Remove 2 files (18 tools), keep all 56 MT5-native tools. Vibe-Trading is excellent for research and swing-strategy development, but its data pipeline is NOT production-grade for real-time execution decisions.

---

## 1. Current Tool Inventory

| Category | File | Tools | Status |
|----------|------|-------|--------|
| **Execution** | `tools_trading.py` | 16 | ✅ Core — KEEP |
| **Live Market Data** | `tools_market_data.py` | 11 | ✅ Core — KEEP |
| **Account/Positions** | `tools_resources.py` | 8 | ✅ Core — KEEP |
| **Trading Context** | `tools_context.py` | 3 | ✅ Core — KEEP |
| **Portfolio/Risk** | `tools_portfolio.py` | 5 | ✅ Core — KEEP |
| **System Management** | `tools_management.py` | 5 | ✅ Core — KEEP |
| **Trade Journal** | `tools_metacognition.py` | 3 | ✅ Core — KEEP |
| **Vibe-Trading Proxy** | `tools_vibe.py` | 6 | ✅ New — KEEP |
| **Local Data Store** | `tools_data.py` | 5 | ⚠️ Low-use — REVIEW |
| **Analysis** | `tools_analysis.py` | 15 | 🔴 Redundant + broken — REMOVE |
| **ML/ONNX** | `tools_ml.py` | 3 | 🔴 Placeholder — REMOVE |
| **Total** | 11 files | **80 tools** | |

---

## 2. Vibe-Trading Data Capability Assessment

### 2.1 What Vibe-Trading Actually Provides

Vibe-Trading is a **research-grade** system, not a live-data system. Its data pipeline consists of:

| Data Source | Markets Supported | Max Resolution | Auth Required | Real-Time? |
|---|---|---|---|---|
| **AKShare** | A-shares, US, HK, futures, macro | 1D (daily) | No | ❌ EOD only |
| **yfinance** | US equities, HK equities | 1H (hourly) | No | ❌ 15min delay |
| **Tushare** | A-shares (intraday) | 1m-1H | ✅ Token required | ❌ Historical only |
| **OKX** | Crypto only | 1m | No | Near-real-time |
| **CCXT** | Crypto (100+ exchanges) | 1m | No | Near-real-time |

### 2.2 Critical Gaps for Forex & Commodities

#### Forex Data: BROKEN

The AKShare loader declares `markets = {"forex", ...}` but has **no `_fetch_forex` method**. When a forex symbol like `EURUSD` is requested:

```python
# In akshare_loader.py _fetch_one():
if _is_a_share(code):  # False for EURUSD
if _is_us(code):       # False for EURUSD
if _is_hk(code):       # False for EURUSD
return self._fetch_a_share(...)  # Wrong method → returns None
```

The fallback chain `forex → ["akshare", "yfinance"]` doesn't help because:
1. AKShare is "available" (library installed) but returns empty for forex codes
2. yfinance is registered only for `us_equity` and `hk_equity` markets — not forex
3. Result: **No forex data is actually fetched**

#### Commodity Data: PARTIAL

Global futures engine exists (GC gold, CL oil, SI silver, etc.) with proper contract multipliers and margin tables. BUT:
- Data loading depends on AKShare (futures loader is A-share/CN-focused)
- No live tick data — daily bars only
- No continuous contract roll logic
- Symbol mapping to MT5 conventions is manual (GC → XAUUSD is not handled)

### 2.3 Real-Time Capability: NONE

| Capability | MetaTraderBridge | Vibe-Trading |
|---|---|---|
| Live bid/ask prices | ✅ Via EA (15-25ms) | ❌ No |
| Tick-by-tick data | ✅ Via `mt5_get_ticks` | ❌ No |
| Real-time indicators | ✅ Via `mt5_get_indicator` | ❌ No |
| Current spread | ✅ Via `mt5_get_order_book` | ❌ No |
| Account equity live | ✅ Via `mt5_account_summary` | ❌ No |
| Position P&L live | ✅ Via `mt5_positions_open` | ❌ No |
| End-of-day bars | ✅ Via EA | ✅ Via AKShare/yfinance |
| Historical backtest | ❌ No engine | ✅ 7 market engines |
| Multi-agent analysis | ❌ No | ✅ 29 swarm presets |
| Strategy generation | ❌ No | ✅ 69 skills |

### 2.4 Verdict: Vibe-Trading Data is NOT Production-Grade for Execution

**Vibe-Trading's data is suitable for:**
- ✅ Pre-market research and analysis (daily/weekly timeframes)
- ✅ Backtesting strategy ideas on historical data
- ✅ Multi-agent swarm debate on market outlook
- ✅ Cross-market research (A-shares + crypto + US equities)
- ✅ Strategy generation and code export (Pine Script, MQL5)

**Vibe-Trading's data is NOT suitable for:**
- ❌ Real-time trade entry/exit decisions
- ❌ Live position management and trailing stops
- ❌ Intraday signal generation (M5, M15, M30)
- ❌ Forex pair analysis (data pipeline is broken)
- ❌ Commodities live analysis (daily bars only, no live feeds)
- ❌ Any decision requiring current spread, slippage, or order book data

---

## 3. Tools to Remove (18 tools, 2 files)

### 3.1 `tools_ml.py` — 3 tools

| Tool | Function | Why Remove |
|------|----------|------------|
| `mt5_ml_predict` | ONNX model inference | No models shipped. Requires manual `.onnx` files. Always returns "not available". |
| `mt5_ml_models` | List loaded ML models | Returns empty dict. Hint tells user to install onnxruntime. |
| `mt5_ml_models_reload` | Reload ML models | No models exist to reload. |

**Replacement:** None needed. This was a placeholder for future ML integration. Vibe-Trading's swarm analysis provides far superior reasoning than a static ONNX model would.

### 3.2 `tools_analysis.py` — 15 tools

| Tool | Function | Why Remove |
|------|----------|------------|
| `mt5_volatility_profile` | ATR + volatility from bars | Vibe has `volatility` skill |
| `mt5_divergence` | MACD/RSI divergence | Vibe `pattern_recognition` |
| `mt5_multi_bar_patterns` | Candlestick + Fib | Vibe `candlestick`, `harmonic` |
| `mt5_volume_profile` | Volume anomaly | Vibe backtest volume analysis |
| `mt5_momentum_check` | RSI + ATR momentum | Vibe `technical-basic` |
| `mt5_multi_timeframe_indicators` | Indicator across TFs | Vibe multi-TF analysis |
| `mt5_correlation_matrix` | Cross-symbol correlation | Vibe `correlation-analysis` |
| `mt5_market_structure` | BOS/CHOCH | Vibe `smc` skill |
| `mt5_strategy_selector` | Regime-based strategy | Vibe `strategy-generate` |
| `mt5_vwap` | VWAP + bands | Vibe `technical-basic` |
| `mt5_volume_at_price` | Volume-at-price POC | Vibe backtest |
| `mt5_setup_probability` | Historical win rate | `mt5_trading_insights` covers this |
| `mt5_support_resistance` | Swing points | Vibe `technical-basic`, `ichimoku` |
| `mt5_market_regime` | Trending/ranging | Vibe swarm regime detection |
| `mt5_market_scan` | Multi-symbol scan | Vibe `get_market_data` + swarm |
| `mt5_opportunity_rank` | Symbol scoring | Vibe `vibe_swarm_to_signal` |

**Critical:** `tools_analysis.py` crashes on Python 3.14 due to FastMCP type annotation incompatibility (`issubclass()` on generic types like `list[str] = None`). This was a pre-existing bug.

---

## 4. Tools to Keep (56 tools, 8 files)

### 4.1 Must Keep — MT5 Execution Bridge

These are MetaTraderBridge's core value proposition. Vibe-Trading cannot replace these:

| Category | Tools | Why |
|----------|-------|-----|
| **Order Execution** | `mt5_submit_market_order`, `mt5_submit_market_order_via_bridge`, `mt5_submit_pending_order`, `mt5_close_position`, `mt5_close_all_positions`, `mt5_cancel_order`, `mt5_cancel_all_orders`, `mt5_modify_order`, `mt5_modify_position_sl_tp`, `mt5_trail_position` | Direct MT5 execution via EA bridge |
| **Live Market Data** | `mt5_get_bars`, `mt5_get_indicator`, `mt5_get_ticks`, `mt5_get_order_book`, `mt5_get_symbol_info`, `mt5_get_deals_history`, `mt5_get_account_summary`, `mt5_get_positions`, `mt5_get_orders`, `mt5_get_chart_screenshot` | Real-time data from MT5 terminal |
| **Trading Context** | `mt5_trading_context`, `mt5_trading_coach`, `mt5_decision_support` | Pre-trade validation using LIVE data |
| **Portfolio/Risk** | `mt5_portfolio_exposure`, `mt5_portfolio_risk`, `mt5_pre_trade_gate`, `mt5_reconcile`, `mt5_custom_indicator` | Risk management on live positions |
| **Position Sizing** | `mt5_calculate_position_size`, `mt5_validate_trade_setup` | Trade validation against live account |
| **Market Snapshot** | `mt5_market_snapshot`, `mt5_chart_intelligence` | Live chart analysis |
| **Account/Positions** | `mt5_terminal_status`, `mt5_account_summary`, `mt5_symbol_info`, `mt5_deals_history`, `mt5_performance_summary`, `mt5_positions_open`, `mt5_orders_pending`, `mt5_bridge_status` | Account state from MT5 |
| **Management** | `mt5_health`, `mt5_tool_status`, `mt5_freeze_status`, `mt5_thaw`, `mt5_safe_shutdown` | System lifecycle control |
| **Trade Journal** | `mt5_log_trade_decision`, `mt5_reflect_on_trades`, `mt5_trading_insights` | Decision logging and reflection |
| **News** | `mt5_news_fetch`, `mt5_news_enrich`, `mt5_news_pools`, `mt5_economic_calendar` | Market news and events |
| **Vibe-Trading** | `vibe_list_skills`, `vibe_get_market_data`, `vibe_run_swarm`, `vibe_backtest`, `vibe_swarm_to_signal`, `vibe_web_search` | Research and strategy via Vibe |

### 4.2 Review — `tools_data.py` (5 tools)

| Tool | Function | Status |
|------|----------|--------|
| `mt5_data_import` | Import bars/ticks/deals | Low-use — local data cache |
| `mt5_data_bars` | Query stored bars | Low-use |
| `mt5_data_ticks` | Query stored ticks | Low-use |
| `mt5_data_deals` | Query stored deals | Low-use |
| `mt5_data_stats` | Data store stats | Low-use |

These tools manage a local SQLite data store for imported historical data. They don't overlap with Vibe-Trading but are rarely used. **Recommendation: Keep for now, consider deprecation in a future release.**

---

## 5. Architecture After Cleanup

```
MetaTraderBridge MCP Server (Port 8010)
├── MT5 Execution Layer (16 tools)    ← Real-time order execution via EA
├── Live Market Data (11 tools)       ← Real-time bars, indicators, ticks
├── Account & Positions (8 tools)     ← Live MT5 terminal state
├── Trading Context (3 tools)         ← Pre-trade validation
├── Portfolio Risk (5 tools)          ← Position risk management
├── System Management (5 tools)       ← Health, shutdown, freeze
├── Trade Journal (3 tools)           ← Decision logging
├── News & Calendar (4 tools)         ← Market events
├── Data Store (5 tools)              ← Historical data cache
└── Vibe-Trading Proxy (6 tools)      ← Research, strategy, backtest

Total: 66 tools (down from 80)
```

---

## 6. Impact Analysis

### 6.1 What Breaks
- Any agent workflows calling `mt5_ml_predict`, `mt5_ml_models`, `mt5_ml_models_reload`
- Any workflows using `mt5_market_scan`, `mt5_opportunity_rank`, or `mt5_strategy_selector`
- `mt5_setup_probability` (trade journal provides equivalent via `mt5_trading_insights`)

### 6.2 What Improves
- Eliminates Python 3.14 crash in `tools_analysis.py`
- Reduces MCP server startup time (fewer module imports)
- Reduces tool confusion for AI agents (fewer tools to choose from)
- Clear separation: MetaTraderBridge = execution, Vibe-Trading = research

### 6.3 Migration Path for Removed Tools

| Old Tool | Replacement | Notes |
|----------|-------------|-------|
| `mt5_ml_predict` | None needed | Was non-functional |
| `mt5_market_scan` | `mt5_get_order_book` + `vibe_get_market_data` | Live data + Vibe research |
| `mt5_opportunity_rank` | `vibe_run_swarm` + `vibe_swarm_to_signal` | Superior multi-agent analysis |
| `mt5_strategy_selector` | `vibe_run_swarm("investment_committee", ...)` | Vibe has 69 skills vs static rules |
| `mt5_setup_probability` | `mt5_trading_insights` | Already covers win-rate analysis |
| `mt5_market_regime` | `mt5_market_snapshot` | Snapshot includes regime info |
| `mt5_correlation_matrix` | `mt5_market_snapshot` on multiple symbols | Compute from live bar data |

---

## 7. Vibe-Trading Production Readiness Summary

| Dimension | Rating | Details |
|-----------|--------|---------|
| **Strategy Research** | ⭐⭐⭐⭐⭐ | Excellent. 69 skills, 29 swarm presets, multi-agent debate |
| **Historical Backtesting** | ⭐⭐⭐⭐ | Strong. 7 market engines, statistical validation, composite portfolios |
| **Code Generation** | ⭐⭐⭐⭐⭐ | Excellent. Strategy code → Pine Script / TDX / MQL5 export |
| **Forex Data** | ⭐ | Broken. AKShare claims support but has no implementation |
| **Commodities Data** | ⭐⭐ | Partial. Engine exists but data loading is A-share focused |
| **Real-Time Data** | ⭐ | None. EOD daily bars only, 15min delay on yfinance |
| **Production Trading** | ⭐ | Not suitable. No live feeds, no tick data, no order book |

### Recommended Usage Pattern

```
Phase 1 — Vibe-Trading (Research):
  vibe_run_swarm("macro_rates_fx_desk", {goal: "XAUUSD outlook"})
  vibe_backtest(run_dir="/path/to/strategy")
  vibe_swarm_to_signal(report) → generates trade hypothesis

Phase 2 — MetaTraderBridge (Execution):
  mt5_get_bars(symbol="XAUUSD", timeframe="H1") → verify current price action
  mt5_trading_context(symbol="XAUUSD") → check live conditions
  mt5_calculate_position_size(...) → compute lot size
  mt5_validate_trade_setup(...) → pre-trade validation
  mt5_submit_market_order(...) → EXECUTE
  mt5_log_trade_decision(...) → journal
```

Vibe-Trading generates the hypothesis. MetaTraderBridge validates it against live data and executes.

---

## 8. Proposed Changes

### Step 1: Remove `tools_ml.py` (3 tools)
- Delete `apps/mcp_server/tools_ml.py`
- Remove `tools_ml` import from `apps/mcp_server/__init__.py`

### Step 2: Remove `tools_analysis.py` (15 tools)
- Delete `apps/mcp_server/tools_analysis.py`
- Remove `tools_analysis` import from `apps/mcp_server/__init__.py`

### Step 3: Update `__init__.py`
Remove these lines from `create_mcp_server()`:
```python
tools_analysis,   # REMOVED
tools_ml,         # REMOVED
```

### Step 4: Update tests
- Remove any tests referencing removed tools
- Update test counts

### NOT in Scope
- No changes to `tools_data.py` (low priority, defer)
- No changes to any MT5-native tools
- No changes to Vibe-Trading integration code
- No changes to gateway routes or lifecycle management

---

## 9. Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Agent workflows break from removed tools | Medium | Document replacements; agents can adapt to 66 tools |
| `tools_analysis.py` removal loses unique features | Low | All features covered by Vibe or existing MetaTraderBridge tools |
| Forex data gap in Vibe-Trading not addressed | High | Out of scope — MetaTraderBridge handles forex live data natively |
| Python 3.14 compatibility issues persist | Low | Removing `tools_analysis.py` eliminates the known crash |

---

## 10. Decision Required

- [ ] Approve removal of `tools_ml.py` (3 tools)
- [ ] Approve removal of `tools_analysis.py` (15 tools)
- [ ] Defer `tools_data.py` review (keep for now)
- [ ] Proceed with implementation on this branch
