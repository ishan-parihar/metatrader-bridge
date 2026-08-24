# R&D Recommendation: Analysis Engine Enhancements

**Source:** Legacy Chinese Quant System Audit → MetaTraderBridge Integration Opportunities  
**Date:** April 9, 2026  
**Status:** Recommendations for review

---

## Executive Summary

A full audit of the legacy Chinese quant system (`~/.agents/skills/trading-quant/`, 1011-line CLI with 25 tools across scoring, sentiment, capital flow, and market analysis) reveals **5 high-value features** worth porting to MetaTraderBridge and **12 features already surpassed** by the current MetaTraderBridge implementation.

**Bottom line:** MetaTraderBridge is the more capable system overall. The legacy system contributes specific analytical patterns — not architecture, not infrastructure, not execution logic — but **detection algorithms** that operate purely on OHLCV data and require zero MQL5 EA changes.

**Estimated effort:** ~800 lines of Python across 3 new services + 3 new MCP endpoints.

---

## 1. What to Port (Ranked by ROI)

### Tier 1: P0 — High Value, Low Effort

#### 1.1 Divergence Detection Engine

**What it does:** Detects when price makes new highs/lows but momentum indicators (MACD, RSI) do not confirm — a high win-rate reversal signal.

| Signal | Logic | Score Impact |
|--------|-------|-------------|
| MACD-Price Bullish Divergence | 10-bar window: price makes new low, MACD does not | +5 |
| MACD-Price Bearish Divergence | 10-bar window: price makes new high, MACD does not | -5 |
| RSI-Price Bullish Divergence | Same logic on RSI(14) | +4 |
| RSI-Price Bearish Divergence | Same logic on RSI(14) | -4 |

**Why it matters:** MetaTraderBridge has zero divergence detection. This is a pure pandas operation on `get_bars()` + `get_indicator()` output. No EA changes needed.

**Portability:** **As-is.** Indicator-agnostic math.

**Where it goes:** `src/mt5_mcp/services/divergence.py` → new endpoint `POST /tools/analysis/divergence`

**Lines of code:** ~120

---

#### 1.2 Multi-Bar Pattern Recognition

**What it does:** Detects structural chart patterns that span multiple bars — beyond MetaTraderBridge's existing 5 single-bar candlestick patterns.

| Pattern | Detection Logic | Score Impact |
|---------|----------------|-------------|
| W-Bottom | Double bottom within 3% tolerance, neckline break confirmed | +5 (confirmed) / +2 (forming) |
| M-Top | Double top within 3% tolerance, neckline break confirmed | -5 (confirmed) / -2 (forming) |
| Bollinger Squeeze | Bandwidth < 50% of 20-bar average = impending volatility expansion | +3 |
| Breakout Detection | 20-bar high/low breach with volume confirmation | ±3 |
| Gap Detection | >1% gap up/down between consecutive bars | ±2 |
| Fibonacci Retracement | 0.618 retracement + RSI < 45 = high win-rate support zone | +6 |
| Fibonacci Extension | 1.618 extension as profit target | +3 (if reached) / +4 (if holding) |

**Why it matters:** MetaTraderBridge's `chart_intelligence.py` only detects: doji, hammer, shooting_star, engulfing, inside_bar. These multi-bar patterns capture swing structures that single-bar patterns miss entirely.

**Portability:** **As-is.** Pure OHLCV math. Fibonacci uses swing high/low points from existing `support_resistance()` data.

**Where it goes:** Extend `src/mt5_mcp/services/chart_intelligence.py` with `detect_patterns()` method → exposed via existing `POST /tools/market/chart_intelligence`

**Lines of code:** ~200

---

#### 1.3 Momentum Anti-Chase System

**What it does:** Penalizes entries at extreme moves to prevent buying tops and selling bottoms — behavioral risk management baked into the scoring system.

| Condition | Penalty |
|-----------|---------|
| Price up ≥ 9.5% (limit-up equivalent) | -12 points |
| Price up ≥ 7% | -6 points |
| Price up ≥ 5% | -3 points |
| Price down ≤ -9.5% (limit-down equivalent) | +8 points (contrarian bounce probability) |
| Price down ≤ -7% | +4 points |
| RSI > 80 + STRONG_BUY signal | Downgrade to WATCH |
| RSI < 20 + STRONG_SELL signal | Upgrade to WATCH |

**Why it matters:** MetaTraderBridge's `opportunity_rank.py` scores opportunity quality but does not penalize chasing. This is a simple post-score adjustment that prevents the AI agent from entering at exhaustion points.

**Portability:** **Needs adaptation.** The ±9.5% thresholds are A-share-specific (price limit mechanism). For MT5 forex/CFD, replace with:
- ATR-based thresholds: price move > 2× ATR(14) from session open = chase risk
- Percentile-based: current bar range > 95th percentile of last 50 bars = exhaustion

**Where it goes:** Add `momentum_penalty` factor to `src/mt5_mcp/services/opportunity_rank.py` OR new `src/mt5_mcp/services/momentum.py` → integrate into `trading/coach` scoring

**Lines of code:** ~80

---

### Tier 2: P1 — High Value, Moderate Effort

#### 1.4 Volume Anomaly Scoring

**What it does:** Multi-tier volume analysis that detects unusual activity beyond simple volume ratios.

| Metric | A-Share Threshold | MT5-Adapted Threshold | Score Impact |
|--------|------------------|----------------------|-------------|
| Volume Ratio > 5× | +8 | > 3× avg | +6 |
| Volume Ratio > 3× | +5 | > 2× avg | +4 |
| Volume Ratio > 1.5× | +2 | > 1.3× avg | +2 |
| Volume Ratio < 0.5× | -3 | < 0.5× avg | -3 |
| Amount > 3× 5-day avg | +5 | Volume > 3× 20-bar avg | +4 |
| Price +2% but vol < 0.8× | -4 | Price up but vol declining | -3 (weakness) |
| Price -2% and vol > 3× | -3 | Price down + vol surge | -4 (distribution) |
| Volume + big move correction | -3 to -5 | Same concept | Chase risk penalty |

**Why it matters:** MetaTraderBridge has `volatility_profile` (ATR-based) but nothing on volume anomalies. Volume confirms or invalidates price moves — a critical missing dimension.

**Portability:** **Needs adaptation.** A-share volume is tick volume (trade count), same as MT5. Only the threshold tiers need recalibration per symbol.

**Where it goes:** `src/mt5_mcp/services/volume_analysis.py` → new endpoint `POST /tools/analysis/volume_profile`

**Lines of code:** ~150

---

#### 1.5 Sector-Aware PE Valuation

**What it does:** PE bands vary by sector — a PE of 30 is cheap for tech but expensive for banks. The legacy system has 16 industry profiles with undervalued/fair/high/extreme thresholds.

| Sector | Undervalued | Fair | High | Extreme |
|--------|------------|------|------|---------|
| Banks | PE < 5 | 5-8 | 8-12 | > 12 |
| Tech | PE < 15 | 15-35 | 35-60 | > 60 |
| Consumer | PE < 15 | 15-25 | 25-40 | > 40 |
| Energy | PE < 6 | 6-12 | 12-18 | > 18 |

**Why it matters:** MetaTraderBridge has zero fundamental analysis. For CFD/forex, this maps to:
- **Forex pairs:** Real interest rate differentials (carry trade valuation)
- **Indices:** Aggregate sector PE from constituent data
- **Commodities:** Forward curve positioning (contango/backwardation as valuation signal)

**Portability:** **Needs significant adaptation.** The scoring logic is universal; the data sources and industry classifications are entirely China-specific.

**Where it goes:** `src/mt5_mcp/services/valuation.py` → new endpoint `POST /tools/analysis/valuation`

**Lines of code:** ~200 (plus data source integration)

---

### Tier 3: P2 — Investigate, Don't Commit

| Feature | Value | Effort | Verdict |
|---------|-------|--------|---------|
| Gold-silver ratio analysis (60-80 bands) | Moderate — universal for commodity pairs | Low | **Worth porting** if you trade XAU/XAG pair |
| FinBERT sentiment pipeline | Moderate — MetaTraderBridge already has keyword-based sentiment in `news_service.py` | Medium (swap CN model → ProsusAI/finbert) | **Skip** unless you want deep NLP sentiment |
| Dragon-Tiger list equivalent | Low for forex, high for equities | High (needs SEC Form 4 / 13F / dark pool data) | **Skip** for current MT5 scope |
| Northbound flow equivalent | Moderate — maps to ETF flow / institutional positioning | Medium (needs external data) | **Defer** to Phase 3+ when multi-asset |

---

## 2. What NOT to Port

| Legacy Feature | Why Skip | MetaTraderBridge Already Has |
|---|---|---|
| Limit up/down pool scanning | China-specific ±10%/±20% price limits. No equivalent in forex/CFD. | `market/regime` (compressing regime captures squeeze) |
| Fried plate pool (failed limit-up) | Only exists in price-limited markets | — |
| Northbound flow via HK Stock Connect | Specific mechanism, not transferable | `economic_calendar` + `news_fetch` |
| Chinese FinBERT (ProsusAI/Chinese) | Wrong language | `news_service.py` keyword-based sentiment |
| Fallback chain + circuit breaker | Generic reliability pattern | TCP → HTTP bridge failover |
| SQLite kline cache | Generic caching | `services/data_store.py` |
| AKShare data source | China-only library | — |
| EastMoney news aggregation | China-only sources | `news_service.py` (16 sources, 5 pools) |
| THS market scanner | China-only API | — |
| CapitalFlowManager 6-level cascade | All data sources are CN-specific | — |

---

## 3. Architecture Impact Assessment

### Zero EA Changes Required

All Tier 1 and Tier 2 features are **pure-Python analysis** operating on data already available via MetaTraderBridge tools:

| Feature | Input Data Source | Existing MetaTraderBridge Tool |
|---------|-----------------|----------------------|
| Divergence | Bars + MACD/RSI indicators | `get_bars()` + `get_indicator()` |
| Pattern Recognition | Bars (OHLCV) | `get_bars()` |
| Momentum Anti-Chase | Price change, RSI | `get_bars()` + `get_indicator(rsi)` |
| Volume Anomaly | Volume from bars | `get_bars()` |
| Valuation | External data (PE, rates) | Not currently available |

### New Endpoints Required

| Endpoint | Request | Response | Estimated Effort |
|----------|---------|----------|-----------------|
| `POST /tools/analysis/divergence` | `{symbol, timeframe, indicators=["macd","rsi"], lookback}` | `{bullish: [...], bearish: [...], signals: [...]}` | 2 hours |
| `POST /tools/analysis/volume_profile` | `{symbol, timeframe, lookback}` | `{volume_ratio, volume_tier, anomalies: [...], score}` | 3 hours |
| Extend `POST /tools/market/chart_intelligence` | No schema change | Add `patterns: {w_bottom, m_top, squeeze, breakout, gap, fibonacci}` | 4 hours |
| Modify `POST /tools/trading/coach` | No schema change | Add `momentum_penalty` to advisory section | 1 hour |

**Total estimated effort:** ~10 hours of Python development.

---

## 4. Integration Strategy

### Phase A: Divergence + Patterns (Week 1)

```
1. Create src/mt5_mcp/services/divergence.py
   - detect_macd_divergence(bars, macd_data, lookback=10)
   - detect_rsi_divergence(bars, rsi_data, lookback=10)

2. Extend src/mt5_mcp/services/chart_intelligence.py
   - detect_w_bottom(bars, tolerance=0.03)
   - detect_m_top(bars, tolerance=0.03)
   - detect_bollinger_squeeze(bars, bb_data, threshold=0.5)
   - detect_breakout(bars, period=20)
   - detect_fibonacci_levels(bars, sr_levels)

3. Add endpoint in apps/mcp_server/main.py
   - POST /tools/analysis/divergence
   - Extend POST /tools/market/chart_intelligence response
```

### Phase B: Volume + Momentum (Week 2)

```
4. Create src/mt5_mcp/services/volume_analysis.py
   - compute_volume_ratio(symbol, timeframe, lookback)
   - detect_volume_anomalies(bars)
   - score_volume_confirmation(price_move, volume_change)

5. Modify src/mt5_mcp/services/opportunity_rank.py OR trading/coach
   - Add momentum_penalty_factor() using ATR-based thresholds
   - Integrate RSI-based signal degradation

6. Add endpoint POST /tools/analysis/volume_profile
```

### Phase C: Valuation (Defer to Phase 3+)

```
7. Research MT5-compatible fundamental data sources
   - Forex: Real interest rate APIs (FRED, ECB)
   - Indices: Constituent PE aggregation
   - Commodities: Forward curve data

8. Create src/mt5_mcp/services/valuation.py
   - Only if data source identified and reliable
```

---

## 5. Alignment with Existing Roadmap

### ROADMAP.md Compatibility

| Existing Roadmap Item | Enhancement Contribution |
|---|---|
| Phase 1.5: Regime detection module | Divergence + patterns enrich regime classification (divergence = regime transition signal) |
| Phase 1.7: Walk-forward validation | Volume anomaly scoring provides additional filter for entry quality |
| Phase 4.1: Regime-adaptive parameters | Momentum anti-chase is inherently regime-adaptive (penalties scale with volatility) |
| ROADMAP codex_aureus finding: "high-vol regime WR = 65.7%" | Volume anomaly scoring directly measures high-vol regime entry quality |

### No Conflicts

None of the proposed features conflict with the ROADMAP's core thesis:
- ✅ No ML price prediction (pure technical analysis)
- ✅ No ML reward optimization (rule-based scoring)
- ✅ No backtesting engine duplication (enhances existing tools)
- ✅ No complex execution framework (pure analysis services)

---

## 6. Success Metrics

| Feature | Success Criteria | Measurement |
|---------|-----------------|-------------|
| Divergence Detection | > 55% win rate on divergent entries vs 50% baseline | Journal analysis: filter trades taken at divergence signals |
| Pattern Recognition | Patterns detected match visual inspection > 80% of the time | Manual review of 50 chart screenshots vs pattern detection output |
| Volume Anomaly Scoring | Volume-confirmed entries have 10% higher profit factor | Compare PF of trades with volume_score > 70 vs volume_score < 50 |
| Momentum Anti-Chase | Reduces max drawdown by 15% on chase-heavy strategies | Backtest with vs without momentum penalty |

---

## 7. Decision Matrix

| Feature | Port? | Priority | Effort | Depends On |
|---------|-------|----------|--------|-----------|
| Divergence Detection | **YES** | P0 | ~120 LOC | None |
| Multi-Bar Pattern Recognition | **YES** | P0 | ~200 LOC | None |
| Momentum Anti-Chase | **YES** | P1 | ~80 LOC | None |
| Volume Anomaly Scoring | **YES** | P1 | ~150 LOC | None |
| Sector-Aware Valuation | **INVESTIGATE** | P2 | ~200 LOC | External data source |
| Gold-Silver Ratio Analysis | **OPTIONAL** | P2 | ~50 LOC | Only if trading XAU/XAG |
| FinBERT Sentiment Pipeline | **NO** | — | High | Already have keyword sentiment |
| Dragon-Tiger Equivalent | **NO** | — | Very High | Wrong market structure |
| China-Specific Tools | **NO** | — | N/A | Non-portable |

---

## 8. Recommendation

**Start with Phase A (Divergence + Patterns).** These two features:
1. Require zero EA changes
2. Use data already available from existing MetaTraderBridge tools
3. Add genuinely new analytical capability (not present anywhere in current codebase)
4. Can be validated by comparing detected signals against visual chart inspection
5. Total ~320 lines of Python, completable in one focused session

**Skip the infrastructure.** The legacy system's fallback chains, caching layers, and data source managers are all superseded by MetaTraderBridge's TCP bridge, data_store.py, and adapter architecture.

**Treat the scoring system as inspiration, not a blueprint.** The legacy 5-dimension weighted scoring (25/30/10/20/15) is a good framework, but MetaTraderBridge's 7-factor `opportunity_rank` with regime-aware skip conditions is more sophisticated. Port the *individual analytical components* (divergence, patterns, volume) into the existing `opportunity_rank` framework rather than building a parallel scoring engine.
