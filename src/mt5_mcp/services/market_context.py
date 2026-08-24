"""Market-derived Trading Context — live composure report for the AI agent.

Combines:
1. LIVE market data (ATR, order book, indicators, recent bars)
2. Historical context (ATR percentiles, volatility trends)
3. Baseline knowledge (point values, typical ranges)

The agent calls this when it needs to understand:
- "Is 200 points a lot on this symbol RIGHT NOW?"
- "How does current volatility compare to the last 30 days?"
- "What's the spread doing? Is liquidity thin?"
- "What's normal vs abnormal for this symbol today?"

This gives the agent composure through DATA, not platitudes.

CRITICAL: Unit normalization.
- MT5 returns all indicator values (ATR, etc.) in PRICE units.
- Baseline typical_atr_h1 is in PIPS.
- "Points" in the MetaTraderBridge system = smallest price increment (5th decimal for forex).
- 1 pip = 10 points (for 5-digit forex).
- All calculations normalize to PIPS for comparison, then convert back to points for display.
"""

from __future__ import annotations

from typing import Optional

# Symbol specs: pip_size, point_size, and baseline knowledge
# pip_size = value of 1 pip in price units
# point_size = smallest tick increment (what MT5 calls "point")

SYMBOL_SPECS: dict[str, dict] = {
    "BTCUSD": {
        "pip_size": 1.0,  # 1 pip = $1.00
        "point_size": 1.0,  # 1 point = $1.00 (same as pip for BTC)
        "digits": 2,
    },
    "XAUUSD": {
        "pip_size": 0.1,  # 1 pip = $0.10 (gold 1-pip = 10 cents)
        "point_size": 0.01,  # 1 point = $0.01
        "digits": 2,
    },
    "EURUSD": {
        "pip_size": 0.0001,  # 1 pip = 0.0001 (standard forex pip)
        "point_size": 0.00001,  # 1 point = 0.00001 (5th decimal)
        "digits": 5,
    },
    "GBPUSD": {
        "pip_size": 0.0001,
        "point_size": 0.00001,
        "digits": 5,
    },
    "USDJPY": {
        "pip_size": 0.01,  # JPY pairs: pip = 2nd decimal
        "point_size": 0.001,  # point = 3rd decimal
        "digits": 3,
    },
    "AUDUSD": {
        "pip_size": 0.0001,
        "point_size": 0.00001,
        "digits": 5,
    },
    "USDCAD": {
        "pip_size": 0.0001,
        "point_size": 0.00001,
        "digits": 5,
    },
    "NZDUSD": {
        "pip_size": 0.0001,
        "point_size": 0.00001,
        "digits": 5,
    },
}

# Baseline knowledge — typical ATR values in PIPS
SYMBOL_BASELINE: dict[str, dict] = {
    "BTCUSD": {
        "name": "Bitcoin vs US Dollar",
        "asset_class": "Cryptocurrency",
        "description": "Bitcoin is the largest cryptocurrency. 24/7 trading, high volatility.",
        "point_value_per_0_01_lot": 0.01,
        "point_value_per_0_1_lot": 0.10,
        "point_value_per_1_lot": 1.00,
        "point_explanation": "1 point = $1.00 price movement. On 0.01 lots, each point = $0.01.",
        "typical_atr_h1_pips": 400,  # $400 per pip
        "typical_atr_d1_pips": 2000,
        "normal_daily_range_points": "1500-3000",
        "reasonable_sl_1x_atr_h1": "~400 points",
        "reasonable_tp_2x_atr_h1": "~800 points",
        "common_pitfalls": [
            "Exiting on 100-200pt pullbacks — that's 25-50% of H1 ATR, normal noise",
            "Moving SL to BE before 1x ATR in profit — gets whipsawed",
            "Trading during low-liquidity periods (weekends, Asian session)",
            "Ignoring D1 trend — always check higher timeframe first",
        ],
        "wisdom": "BTCUSD rewards patience. Set SL at 1x ATR, then step away. Checking every minute causes you to exit good trades on normal noise.",
    },
    "XAUUSD": {
        "name": "Gold vs US Dollar",
        "asset_class": "Commodity / Precious Metal",
        "description": "Gold is a safe-haven asset. Moves on geopolitical news, USD strength, inflation.",
        "point_value_per_0_01_lot": 0.01,
        "point_value_per_0_1_lot": 0.10,
        "point_value_per_1_lot": 1.00,
        "point_explanation": "1 point = $0.01 price movement. On 0.01 lots, each point = $0.01.",
        "typical_atr_h1_pips": 30,  # 30 pips = $3.00
        "typical_atr_d1_pips": 200,  # 200 pips = $20.00
        "normal_daily_range_points": "150-300",
        "reasonable_sl_1x_atr_h1": "~30 points",
        "reasonable_tp_2x_atr_h1": "~60 points",
        "common_pitfalls": [
            "Trading before major news (NFP, FOMC) — spreads widen, price jumps",
            "Ignoring DXY correlation — gold moves inversely to USD",
            "Entering during Asian session — low volume, choppy",
        ],
        "wisdom": "Gold respects technical levels well. Trade London/NY overlap for best liquidity.",
    },
    "EURUSD": {
        "name": "Euro vs US Dollar",
        "asset_class": "Forex",
        "description": "Most traded currency pair. Low volatility, tight spreads, respects TA well.",
        "point_value_per_0_01_lot": 0.10,
        "point_value_per_0_1_lot": 1.00,
        "point_value_per_1_lot": 10.00,
        "point_explanation": "10 points = 1 pip (0.0001). On 0.01 lots, 1 pip = $0.10.",
        "typical_atr_h1_pips": 10,  # 10 pips = 0.001
        "typical_atr_d1_pips": 80,  # 80 pips = 0.008
        "normal_daily_range_points": "60-100",
        "reasonable_sl_1x_atr_h1": "~10 points (1 pip)",
        "reasonable_tp_2x_atr_h1": "~20 points (2 pips)",
        "common_pitfalls": [
            "Overtrading — tight spreads invite too many small trades",
            "Ignoring economic calendar — ECB, Fed announcements move price",
            "Using BTCUSD-style SL distances — 200pts on EURUSD is 2-3x DAILY RANGE",
        ],
        "wisdom": "EURUSD is patient. Wait for clear setups. Best moves during London/NY overlap (8am-12pm EST).",
    },
    "GBPUSD": {
        "name": "British Pound vs US Dollar",
        "asset_class": "Forex",
        "description": "More volatile than EURUSD. Known as 'Cable'.",
        "point_value_per_0_01_lot": 0.10,
        "point_value_per_0_1_lot": 1.00,
        "point_value_per_1_lot": 10.00,
        "point_explanation": "10 points = 1 pip. On 0.01 lots, 1 pip = $0.10.",
        "typical_atr_h1_pips": 18,  # ~18 pips
        "typical_atr_d1_pips": 120,  # ~120 pips
        "normal_daily_range_points": "80-150",
        "reasonable_sl_1x_atr_h1": "~18 points (~2 pips)",
        "reasonable_tp_2x_atr_h1": "~36 points (~4 pips)",
        "common_pitfalls": [
            "Using EURUSD SL distances — GBPUSD needs ~1.5x wider stops",
            "Ignoring UK political news — sudden spikes",
        ],
        "wisdom": "GBPUSD trends well but whipsaws more than EURUSD. Use wider SL.",
    },
}


def _normalize_symbol_for_spec(symbol: str) -> str:
    """Strip broker suffixes (m, M, micro, etc.) to match base symbol specs."""
    upper = symbol.upper().rstrip("M")
    return upper


def _get_symbol_spec(symbol: str) -> dict:
    """Get symbol specs (pip_size, point_size). Falls back to 5-digit forex defaults."""
    base = _normalize_symbol_for_spec(symbol)
    return SYMBOL_SPECS.get(
        base, {"pip_size": 0.0001, "point_size": 0.00001, "digits": 5}
    )


def _price_to_pips(price_value: float, symbol: str) -> float:
    """Convert a price-difference to pips."""
    pip_size = _get_symbol_spec(symbol)["pip_size"]
    return price_value / pip_size


def _price_to_points(price_value: float, symbol: str) -> float:
    """Convert a price-difference to points."""
    point_size = _get_symbol_spec(symbol)["point_size"]
    return price_value / point_size


def _pips_to_price(pip_value: float, symbol: str) -> float:
    """Convert pips to price-difference."""
    pip_size = _get_symbol_spec(symbol)["pip_size"]
    return pip_value * pip_size


def build_context(
    *,
    symbol: str,
    # LIVE market data (these are the inputs that make it data-driven)
    current_atr: Optional[float] = None,
    current_price: Optional[float] = None,
    spread_points: Optional[float] = None,
    rsi: Optional[float] = None,
    ema_fast: Optional[float] = None,
    ema_slow: Optional[float] = None,
    last_bar_range: Optional[float] = None,
    last_bar_direction: Optional[str] = None,
    # Historical context
    atr_30d_avg: Optional[float] = None,
    atr_7d_avg: Optional[float] = None,
    recent_volatility_trend: Optional[str] = None,  # "rising", "falling", "stable"
    # Order book
    bid_depth: Optional[float] = None,
    ask_depth: Optional[float] = None,
) -> dict:
    """Build a live context report combining baseline knowledge with real-time data.

    All unit conversions are handled here to ensure consistent calculations:
    - MT5 ATR → converted to PIPS for comparison with baseline
    - Spread (raw price) → converted to POINTS and PIPS for display
    - "200 points" → converted to price before any ratio calculations
    """
    symbol_upper = symbol.upper()
    baseline = SYMBOL_BASELINE.get(symbol_upper, None)
    spec = _get_symbol_spec(symbol_upper)
    pip_size = spec["pip_size"]
    point_size = spec["point_size"]

    result: dict = {
        "symbol": symbol_upper,
        "symbol_info": {},
        "point_context": {},
        "volatility_assessment": {},
        "market_state": {},
        "composure_notes": [],
    }

    # ====== BASELINE INFO ======
    if baseline:
        result["symbol_info"] = {
            "name": baseline["name"],
            "asset_class": baseline["asset_class"],
            "description": baseline["description"],
        }
        result["point_context"] = {
            "per_0_01_lot": f"${baseline['point_value_per_0_01_lot']}",
            "per_0_1_lot": f"${baseline['point_value_per_0_1_lot']}",
            "per_1_lot": f"${baseline['point_value_per_1_lot']}",
            "explanation": baseline["point_explanation"],
        }
        result["baseline"] = {
            "typical_atr_h1_pips": baseline["typical_atr_h1_pips"],
            "typical_atr_d1_pips": baseline["typical_atr_d1_pips"],
            "normal_daily_range": baseline["normal_daily_range_points"],
            "reasonable_sl": baseline["reasonable_sl_1x_atr_h1"],
            "reasonable_tp": baseline["reasonable_tp_2x_atr_h1"],
            "common_pitfalls": baseline["common_pitfalls"],
            "wisdom": baseline["wisdom"],
        }
    else:
        result["symbol_info"] = {"name": symbol_upper, "asset_class": "Unknown"}
        result["point_context"] = {
            "explanation": "Check symbol_info for exact point value."
        }
        result["baseline"] = {"note": "No baseline data. Use ATR-based analysis."}

    # ====== LIVE VOLATILITY ASSESSMENT ======
    current_atr_pips = None  # Normalized: ATR in pips

    if current_atr is not None:
        current_atr_pips = _price_to_pips(current_atr, symbol_upper)
        result["volatility_assessment"] = {
            "current_atr_price": current_atr,
            "current_atr_pips": round(current_atr_pips, 2),
        }

    if current_atr_pips is not None and baseline:
        typical_pips = baseline["typical_atr_h1_pips"]
        atr_ratio = current_atr_pips / typical_pips if typical_pips > 0 else 1.0

        result["volatility_assessment"]["typical_atr_h1_pips"] = typical_pips
        result["volatility_assessment"]["atr_vs_typical"] = round(atr_ratio, 2)

        # Compose the critical composure note
        if current_price is not None and current_price > 0:
            atr_pct = current_atr / current_price * 100
            result["volatility_assessment"]["atr_percent_of_price"] = round(atr_pct, 3)

            # "Is 200 points a lot?" — convert 200 points to price, then to ATR ratio
            points_200 = 200
            points_200_price = points_200 * point_size
            points_200_as_atr = points_200_price / current_atr if current_atr > 0 else 0
            points_200_as_pct = points_200_price / current_price * 100

            result["composure_notes"].append(
                f"200 points = {points_200_as_atr:.2f}x ATR = {points_200_as_pct:.2f}% of price. "
                f"{'This is well within normal noise.' if points_200_as_atr < 1.0 else 'This is a significant move.'}"
            )

        if atr_ratio > 1.5:
            result["volatility_assessment"]["status"] = "elevated"
            result["composure_notes"].append(
                f"ATR is {atr_ratio:.1f}x typical — elevated volatility. "
                f"Price swings will be larger than usual. Use wider SL."
            )
        elif atr_ratio < 0.5:
            result["volatility_assessment"]["status"] = "compressed"
            result["composure_notes"].append(
                f"ATR is {atr_ratio:.1f}x typical — compressed. "
                f"Market is coiling. A breakout is likely. "
                f"Consider bracket orders to catch the move in either direction."
            )
        else:
            result["volatility_assessment"]["status"] = "normal"
            result["composure_notes"].append(
                f"ATR is {atr_ratio:.1f}x typical — within normal range. "
                f"Standard SL/TP distances apply."
            )

        # Historical trend
        if atr_7d_avg is not None and atr_30d_avg is not None:
            result["volatility_assessment"]["atr_7d_avg"] = atr_7d_avg
            result["volatility_assessment"]["atr_30d_avg"] = atr_30d_avg
            if recent_volatility_trend:
                result["volatility_assessment"]["trend"] = recent_volatility_trend

    elif current_atr is not None:
        result["volatility_assessment"] = {
            "current_atr_price": current_atr,
            "current_atr_pips": round(current_atr_pips, 2)
            if current_atr_pips
            else None,
            "status": "unknown_baseline",
        }
        if current_price and current_price > 0:
            result["volatility_assessment"]["atr_percent_of_price"] = round(
                current_atr / current_price * 100, 3
            )

    # ====== LIVE MARKET STATE ======
    state: dict = {}
    if rsi is not None:
        state["rsi"] = round(rsi, 1)
        if rsi > 70:
            state["rsi_state"] = "overbought"
        elif rsi < 30:
            state["rsi_state"] = "oversold"
        else:
            state["rsi_state"] = "neutral"

    if ema_fast is not None and ema_slow is not None:
        state["ema_fast"] = ema_fast
        state["ema_slow"] = ema_slow
        state["ema_alignment"] = "bullish" if ema_fast > ema_slow else "bearish"

    if last_bar_range is not None:
        state["last_bar_range_pips"] = round(
            _price_to_pips(last_bar_range, symbol_upper), 2
        )
        state["last_bar_range_points"] = round(
            _price_to_points(last_bar_range, symbol_upper), 1
        )
        if last_bar_direction:
            state["last_bar_direction"] = last_bar_direction

        if current_atr is not None and current_atr > 0:
            bar_atr_ratio = last_bar_range / current_atr
            state["bar_vs_atr"] = round(bar_atr_ratio, 2)
            if bar_atr_ratio > 1.5:
                state["bar_assessment"] = "wide — high momentum candle"
            elif bar_atr_ratio < 0.3:
                state["bar_assessment"] = "tiny — indecision/compression"
            else:
                state["bar_assessment"] = "normal range"

    if spread_points is not None:
        # spread_points is actually a raw price difference from ask - bid
        spread_price = spread_points
        spread_pips = _price_to_pips(spread_price, symbol_upper)
        spread_pts = _price_to_points(spread_price, symbol_upper)
        state["spread_pips"] = round(spread_pips, 1)
        state["spread_points"] = round(spread_pts, 1)
        state["spread_price"] = spread_price
        if current_atr is not None and current_atr > 0:
            state["spread_as_atr_pct"] = round(spread_price / current_atr * 100, 1)

    if bid_depth is not None and ask_depth is not None:
        total = bid_depth + ask_depth
        if total > 0:
            state["bid_ask_ratio"] = round(bid_depth / total, 2)
            if bid_depth / total > 0.65:
                state["liquidity_bias"] = "bid-heavy (more buying interest)"
            elif bid_depth / total < 0.35:
                state["liquidity_bias"] = "ask-heavy (more selling interest)"
            else:
                state["liquidity_bias"] = "balanced"

    result["market_state"] = state

    # ====== CROSS-SYMBOL COMPARISON ======
    result["comparison"] = []
    for sym, bl in SYMBOL_BASELINE.items():
        result["comparison"].append(
            {
                "symbol": sym,
                "typical_atr_h1_pips": bl["typical_atr_h1_pips"],
                "normal_daily_range": bl["normal_daily_range_points"],
                "sl_1x_atr": bl["reasonable_sl_1x_atr_h1"],
            }
        )

    return result
