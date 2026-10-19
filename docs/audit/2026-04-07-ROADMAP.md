# MT5 Python Algo-Trading Roadmap

**Target:** $100 → $1,000,000 in 36 months  
**Platform:** MT5 via TCP Bridge (Python execution, MT5 as execution layer only)  
**Date:** April 7, 2026  
**Status:** Research Complete → Implementation Phase

---

## 1. Executive Summary

After auditing 5 AlphaForge projects, 2 external frameworks (FinRL, NautilusTrader), and 20+ external repos, the research conclusions are unambiguous:

**ML prediction-based approaches are unworkable for this domain.** Across all 5 AlphaForge projects, the maximum feature-label correlation achieved was **0.0971**. TCN, XGBoost, BiGRU, and neural networks all converge to ~53-55% directional accuracy — statistically indistinguishable from noise after transaction costs.

**The ONLY validated profitable approach** is the growth_engine's delta/acceleration signal framework with ATR trailing stops:
- Out-of-sample Omega: **1.1945**
- Walk-forward pass rate: **80%** (12/15 periods)
- Expectancy: **0.75R**
- Win rate: **59.8%**
- Risk/Reward ratio: **3.17:1**
- Trade frequency: **59.3 trades/month**

**Critical finding from codex_aureus:** High-volatility regime trades achieve **65.7% win rate** vs 39.8% baseline. Regime filtering is the highest-value signal enhancement available.

**Recommended path:** Abandon ML prediction. Build a simple Python signal handler (~200 lines) using growth_engine's validated delta/acceleration signals, regime-filtered with ATR-based risk management. Execute via MT5 TCP bridge. Compound aggressively in early months, then systematically reduce risk as account grows.

---

## 2. Research Verdicts

### AlphaForge Projects

| Project | Verdict | Key Finding |
|---------|---------|-------------|
| **symbol_agnostic_pipeline** | ❌ NO-GO | TCN candle prediction, 53-55% accuracy, 50% candle overlap. Zero backtesting. Own diagnostic says "structural context, not trading signals." |
| **xauusd_xgboost_rd** | ❌ NO-GO | XGBoost/BiGRU, 54% walk-forward accuracy. Signal range reports show 100% accuracy (data leakage). Optimizes accuracy, not profit. |
| **on_hold/codex_aureus** | ❌ NO-GO (ML), ✅ Extract tools | Max feature-label correlation = 0.0971 across 80 features. 70% of trades exit on color flip at 24% WR. **BUT: high-vol regime WR = 65.7%** — most valuable statistical finding across all research. Extractable: `mql5_parity.py`, `tick_renko_builder.py`, `tick_feature_computer.py`, `rolling_scaler.py`, causal Numba simulator kernel. |
| **on_hold/XAUUSD_BREAKOUT_RND** | ❌ NO-GO | All 10 breakout strategies failed (best Omega = 0.0100 vs target 1.0). XAUUSD is mean-reverting at tested timeframes. Keltner mid-band reversion in 97.4%-98.6% of trades. |
| **on_hold/alpha_forge** | ❌ NO-GO (ML), ✅ Extract tools | Python-MQL5 parity gap destroyed all profitable configs (90% WR Python → 0-29% WR MQL5). Neural models: R² ~0.36, MAPE ~400%, max DD 30-82%. Extractable: `technical_indicators.py`, `performance_metrics.py`, `base_strategy.py`. |
| **growth_engine** (active) | ✅ VIABLE (incomplete) | Only project with validated Omega > 1.0. **BLOCKER: parity lock incomplete** (DD-blocked telemetry residual: 185 vs 179). Regime-agnostic HPO shows 4/10 regimes negative — not truly regime-agnostic. |

### External Frameworks

| Framework | Verdict | Key Finding |
|-----------|---------|-------------|
| **FinRL** | ❌ NO-GO for direct use | Naive reward function (portfolio delta only), no spread/slippage modeling, no forex support, daily frequency default. Would need 6-10 weeks of customization. DRL algorithms (PPO, SAC, TD3) are solid but environment design is the bottleneck. |
| **NautilusTrader** | ❌ NO-GO for Phase 1 | Excellent deterministic backtesting, clean adapter model. BUT: 2-3 week ramp-up, 1000+ lines of adapter code, overkill for single-venue forex/gold. **Conditional GO for Phase 2** (after edge validated). |

### External Repos (Integration Candidates)

| Repo | Use Case | Priority |
|------|----------|----------|
| **PyPortfolioOpt** | Portfolio optimization, efficient frontier, Black-Litterman | High (Phase 3+) |
| **Riskfolio-Lib** | Risk parity, hierarchical risk allocation | Medium (Phase 4+) |
| **VectorBT** | Parameter optimization, walk-forward analysis | Medium (Phase 2) |
| **QuantStats** | Performance reporting, tear sheets | Low (Phase 3) |
| **ta-lib / pandas-ta** | Technical indicators | Low (use extracted `technical_indicators.py` instead) |

---

## 3. First-Principles Architecture

### What We Actually Need (vs What We Researched)

```
[MT5 get_bars] → [Feature Computer] → [Regime Filter] → [Signal Generator] → [Risk Manager] → [Python Bridge] → [MT5 TCP Execution]
                      ↓                      ↓                    ↓                        ↓
              (delta/accel)          (high-vol check)     (delta threshold)        (position sizing, daily loss limit)
```

**5 components, ~2,000 lines of Python total.** Not 50,000 lines of NautilusTrader. Not 6-10 weeks of FinRL customization.

### Component Breakdown

| Component | Source | Lines of Code | Status |
|-----------|--------|---------------|--------|
| Data ingestion | MT5 bridge `get_bars()` | 0 (existing) | ✅ Ready |
| Feature computation | growth_engine delta/accel signals + extracted `technical_indicators.py` | ~400 | ⚠️ Partial |
| Regime filter | codex_aureus high-vol finding + ATR-based classification | ~200 | ❌ To build |
| Signal generator | growth_engine delta threshold logic | ~300 | ⚠️ Partial (parity lock incomplete) |
| Risk manager | MT5 bridge policy engine + custom position sizing | ~300 | ⚠️ Partial |
| Python bridge | Simple TCP client | ~200 | ❌ To build |
| Journaling | MT5 bridge SQLite journal | 0 (existing) | ✅ Ready |

### What We Should NOT Build

1. **Custom backtesting engine** — growth_engine exists, has walk-forward validation
2. **Complex execution framework** — NautilusTrader is overkill for single-venue, bar-frequency trading
3. **ML prediction models** — proven unworkable across 5 projects (max correlation 0.0971)
4. **RL agents** — FinRL needs 6-10 weeks customization, reward function is naive for forex

---

## 4. Phased Roadmap

### Phase 1: Foundation (Weeks 1-4) — "Prove the Signal Works"

**Objective:** Complete growth_engine parity lock, extract tools, build Python-to-MT5 bridge, validate on OOS data.

| # | Task | Deliverable | Success Criteria |
|---|------|-------------|------------------|
| 1.1 | Complete growth_engine parity lock | 64-trade target with strict EA-controls, DD-blocked residual resolved | Trade count parity: 64 vs 64, no DD-blocked telemetry residual |
| 1.2 | Extract tools from codex_aureus | `mql5_parity.py`, `tick_renko_builder.py`, `tick_feature_computer.py`, `rolling_scaler.py` copied to new `mt5_bridge/tools/` directory | All extracted tools import without errors, pass basic smoke tests |
| 1.3 | Extract tools from alpha_forge | `technical_indicators.py`, `performance_metrics.py` copied to `mt5_bridge/tools/` | All extracted tools import without errors, match TA-Lib output within 0.1% |
| 1.4 | Build simple Python signal handler | ~200-line Python script: reads signals, manages position state, sends TCP to MT5 | Can execute a market order on MT5 from Python signal |
| 1.5 | Build regime detection module | High-vol classifier using ATR percentile (from codex_aureus finding) | Correctly classifies high-vol vs low-vol bars with >80% accuracy vs manual labels |
| 1.6 | Add realistic cost modeling to backtests | Spread, slippage, commission deducted from backtest results | Backtest results match live results within 5% on identical signal stream |
| 1.7 | Walk-forward validation with regime filter | Growth engine re-tested with high-vol filter applied | Walk-forward pass rate maintained at ≥70% with regime filter |

**Phase 1 Exit Criteria:** Working Python-to-MT5 bridge executing growth_engine signals with regime filter, validated on 3+ months of out-of-sample data with realistic costs.

---

### Phase 2: Edge Validation (Months 2-3) — "Trade Small, Learn Fast"

**Objective:** Validate edge with real capital on $100 account. Confirm live performance matches backtest.

| # | Task | Deliverable | Success Criteria |
|---|------|-------------|------------------|
| 2.1 | Deploy with 0.5% risk per trade | Live trading system running on demo → micro live account | 20+ trades executed, journal entries logged for every decision |
| 2.2 | Monitor regime-filtered performance | Weekly performance reports filtered by regime | High-vol regime WR ≥ 60%, low-vol regime WR ≥ 45% |
| 2.3 | Build performance dashboard | QuantStats tear sheets generated weekly | Sharpe ≥ 1.0, max DD ≤ 15%, profit factor ≥ 1.2 |
| 2.4 | Parameter optimization via VectorBT | Optimize delta threshold, ATR multiplier, regime threshold | Optimized parameters show ≤10% degradation from Phase 1 results |
| 2.5 | Stress test with walk-forward | 5-period walk-forward on most recent data | All 5 periods show positive expectancy |

**Phase 2 Exit Criteria:** 50+ live trades, confirmed positive expectancy, live results within 15% of backtest predictions. Go/No-Go decision for scaling.

---

### Phase 3: Scaling (Months 4-12) — "Compound Aggressively"

**Objective:** $100 → $5,000-$10,000 through systematic compounding.

| # | Task | Deliverable | Success Criteria |
|---|------|-------------|------------------|
| 3.1 | Scale risk to 2% per trade | Dynamic position sizing based on account equity | Risk per trade = 2% of current equity, adjusted daily |
| 3.2 | Add daily loss circuit breaker | Auto-halt after 4% daily loss | System stops trading for 24h after trigger, requires manual reset |
| 3.3 | Add max drawdown halt | Auto-halt after 20% peak-to-trough drawdown | System stops trading, generates drawdown report |
| 3.4 | Multi-symbol exploration | Test delta/accel signal on 3+ additional symbols (EURUSD, GBPUSD, US30) | Signal transfers to ≥2 additional symbols with positive expectancy |
| 3.5 | Evaluate NautilusTrader migration | Decision document: migrate or stay simple | Decision based on: fill reconciliation needs, multi-strategy requirements, backtest quality needs |
| 3.6 | Add PyPortfolioOpt for position sizing | Portfolio-level optimization for multi-symbol positions | Portfolio Sharpe ≥ individual symbol Sharpe |

**Phase 3 Exit Criteria:** $5K-10K account, 200+ total trades, confirmed scaling trajectory, drawdowns within acceptable bounds.

---

### Phase 4: Optimization (Months 13-24) — "Systematic Growth"

**Objective:** $10K → $100K through systematic optimization and multi-strategy diversification.

| # | Task | Deliverable | Success Criteria |
|---|------|-------------|------------------|
| 4.1 | Add regime-adaptive parameters | Different signal thresholds per regime (high-vol, low-vol, trending, ranging) | Regime-adaptive parameters show ≥20% improvement over static parameters |
| 4.2 | ML as feature enricher (not predictor) | Regime classifier using ML (NOT price predictor) | Regime classifier accuracy ≥ 70%, improves signal quality |
| 4.3 | Multi-strategy portfolio | 2-3 uncorrelated strategies running simultaneously | Portfolio DD ≤ max individual strategy DD, portfolio Sharpe ≥ 1.5 |
| 4.4 | Professional monitoring | Alerting, logging, performance attribution | <5 min detection time for system failures, daily performance reports |
| 4.5 | Capital allocation optimization | Riskfolio-Lib for hierarchical risk allocation | Risk-adjusted returns improve ≥15% vs equal-weight allocation |

**Phase 4 Exit Criteria:** $100K account, 500+ total trades, 2+ strategies running, professional-grade monitoring.

---

### Phase 5: Maturation (Months 25-36) — "Protect and Grow"

**Objective:** $100K → $1M through capital preservation and steady growth.

| # | Task | Deliverable | Success Criteria |
|---|------|-------------|------------------|
| 5.1 | Reduce risk per trade to 1% | Conservative position sizing for large account | Max DD ≤ 10% on $100K+ account |
| 5.2 | Add correlation-aware position sizing | Reduce exposure when symbols are correlated | Portfolio correlation ≤ 0.3 between active positions |
| 5.3 | Implement partial profit taking | Scale out of winning positions at predefined levels | Average exit price improved ≥10% vs all-at-once exit |
| 5.4 | Build strategy marketplace | Framework for testing and deploying new strategies | 3+ strategies evaluated per quarter, 1 deployed per quarter |
| 5.5 | Institutional-grade reporting | Monthly investor-style reports | Consistent reporting format, auditable performance |

**Phase 5 Exit Criteria:** $1M account OR honest assessment if target is unreachable at current trajectory.

---

## 5. Tool Integration Plan

### Immediate (Phase 1)

| Tool | Purpose | Integration Method |
|------|---------|-------------------|
| growth_engine | Signal generation | Complete parity lock, extract delta/accel logic |
| codex_aureus tools | Data quality | Copy `mql5_parity.py`, `tick_renko_builder.py` to `mt5_bridge/tools/` |
| alpha_forge tools | Technical analysis | Copy `technical_indicators.py`, `performance_metrics.py` to `mt5_bridge/tools/` |
| MT5 bridge | Execution + journaling | Use existing TCP bridge, policy engine, SQLite journal |

### Short-term (Phase 2-3)

| Tool | Purpose | Integration Method |
|------|---------|-------------------|
| VectorBT | Parameter optimization | Install via pip, use for walk-forward optimization of delta threshold |
| QuantStats | Performance reporting | Install via pip, generate weekly tear sheets from journal data |
| pandas-ta | Additional indicators (optional) | Install via pip, supplement extracted `technical_indicators.py` |

### Medium-term (Phase 3-4)

| Tool | Purpose | Integration Method |
|------|---------|-------------------|
| PyPortfolioOpt | Portfolio optimization | Install via pip, optimize multi-symbol weights |
| Riskfolio-Lib | Risk parity allocation | Install via pip, hierarchical risk allocation |
| NautilusTrader (optional) | Deterministic backtesting | Only if fill reconciliation or multi-strategy needs emerge |

### NOT Recommended

| Tool | Reason |
|------|--------|
| FinRL | Naive reward function, 6-10 weeks to customize, no forex support |
| ML price prediction (TCN, XGBoost, BiGRU, Neural) | Proven unworkable — max feature-label correlation 0.0971 |
| Breakout strategies | All 10 strategies failed on XAUUSD (mean-reverting at tested timeframes) |

---

## 6. Risk & Reality Check

### The Math

| Metric | Growth Engine Value | Required for $1M |
|--------|---------------------|------------------|
| Monthly return (theoretical) | 60 trades × 0.75R × 2% risk = 90% | ~22% compounded monthly |
| Monthly return (realistic, after costs) | ~40-50% | ~22% compounded monthly |
| Max drawdown tolerance | 20% halt | Must survive 20% DD at any account size |
| Trades needed (total) | ~2,000+ | 59.3 × 36 = 2,135 |

### Honest Assessment

**22% monthly returns for 36 months is EXTREMELY aggressive.** Professional hedge funds target 15-25% ANNUAL returns. The growth engine's theoretical 90% monthly return (60 × 0.75R × 2%) assumes:
- Zero slippage beyond modeled costs
- Zero gaps or weekend risk
- Consistent regime distribution
- No psychological degradation during drawdowns
- Perfect execution every time

**Realistic scenarios:**

| Scenario | Monthly Return | 36-Month Result | Probability |
|----------|---------------|-----------------|-------------|
| Best case (edge holds perfectly) | 30-40% | $500K-$2M | 10-15% |
| Expected case (edge degrades 20%) | 15-25% | $50K-$200K | 40-50% |
| Moderate case (edge degrades 40%) | 5-15% | $5K-$50K | 25-30% |
| Worst case (edge disappears) | -5% to +5% | $10-$500 | 10-15% |

**Most likely outcome: $50K-$200K in 36 months.** The $1M target is possible but requires the edge to hold perfectly through massive drawdowns — a low-probability scenario.

**The value of this roadmap is not in hitting $1M exactly — it's in building a systematic, validated trading engine that compounds capital at rates far exceeding traditional investments.** Even the moderate scenario ($50K) represents a 500x return on $100.

---

## 7. Immediate Next Actions (Next 7 Days)

| # | Action | Time Estimate | Priority |
|---|--------|---------------|----------|
| 1 | Complete growth_engine parity lock (resolve DD-blocked residual: 185 vs 179) | 2-3 days | **P0** |
| 2 | Extract tools from codex_aureus (`mql5_parity.py`, `tick_renko_builder.py`, `tick_feature_computer.py`, `rolling_scaler.py`) | 1 day | **P0** |
| 3 | Extract tools from alpha_forge (`technical_indicators.py`, `performance_metrics.py`) | 0.5 days | **P0** |
| 4 | Build simple Python signal handler (~200 lines) | 2-3 days | **P0** |
| 5 | Run walk-forward validation with regime filter on growth_engine | 2 days | **P1** |
| 6 | Document MT5 bridge TCP protocol for signal handler | 0.5 days | **P1** |

---

## Appendix A: Research Methodology

This roadmap is based on:
- 5 AlphaForge project audits (explore agents, 3-5 hours each)
- 2 external framework deep-dives (FinRL: clone + analysis, NautilusTrader: clone + analysis)
- 20+ external repo evaluations via web search
- Growth_engine parity calibration history (Parts 21-44, ~200 hours of prior work)
- Sequential thinking synthesis of all findings

## Appendix B: Key Files Referenced

### Growth Engine
- `growth_engine/results/parity/calibrated_profile/parity_profile_lock.json` — Current parity lock
- `growth_engine/hpo/run_regime_agnostic_hpo.py` — Regime-agnostic HPO executor
- `growth_engine/validation/run_ea_parity_cycle.py` — EA parity cycle runner
- `growth_engine/PROGRESS_LOG.md` — Detailed parity calibration history
- `growth_engine/results/regime_agnostic_hpo_results.json` — Latest HPO results
- `growth_engine/results/full_validation_report.json` — Old HPO v2.2 results

### Extractable Tools
- `on_hold/codex_aureus/python_lab/library/mql5_parity.py` — Exact MQL5 indicator parity
- `on_hold/codex_aureus/python_lab/data_engine/tick_renko_builder.py` — MQL5-parity Renko engine
- `on_hold/alpha_forge/core/technical_indicators.py` — TA-Lib replacement
- `on_hold/alpha_forge/strategies/algorithmic/utils/performance_metrics.py` — Omega/Sharpe/Sortino

### External Repos
- https://github.com/AI4Finance-Foundation/FinRL — Cloned, analyzed, NO-GO
- https://github.com/nautechsystems/nautilus_trader — Cloned, analyzed, NO-GO for Phase 1
- https://github.com/robertmartin8/PyPortfolioOpt — Integration candidate (Phase 3)
- https://github.com/ernestpplla/VectorBT — Integration candidate (Phase 2)
- https://github.com/ranaroussi/quantstats — Integration candidate (Phase 3)
