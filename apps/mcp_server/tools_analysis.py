"""MCP tools for computed market analysis — regime, structure, divergence, VWAP, momentum, etc.

All tools are read-only, pure-Python analysis that fetches OHLCV bar data and indicators
via the bridge, then runs analysis locally. No EA calls needed after data fetch.
"""

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

from . import mcp
from .shared import (
    get_gateway,
    get_http_client,
    get_settings_cached,
    _tcp_send_and_await,
    _batch_enqueue_and_await,
    _await_result,
    _parse_payload,
    _parse_payload_dict,
    _parse_indicator_value,
    _parse_indicator_value_from_data,
    _first_bid_ask,
    INDICATOR_DEFAULTS,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


# ---------------------------------------------------------------------------
# Helper: fetch bars via TCP-first pattern with HTTP fallback
# ---------------------------------------------------------------------------


def _fetch_bars(
    symbol_norm: str, timeframe: str, count: int
) -> tuple[list[dict], str]:
    """Fetch OHLCV bars via TCP bridge with HTTP fallback.

    Returns (bars_list, source_string).
    """
    result = _tcp_send_and_await(
        "get_bars", {"symbol": symbol_norm, "timeframe": timeframe, "count": count}
    )
    source = "bridge"
    if result is None:
        source = "tcp_bridge"
        client = get_http_client()
        settings = get_settings_cached()
        req = client.post(
            f"{settings.gateway_url}/bridge/commands/enqueue",
            params={
                "type": "get_bars",
                "symbol": symbol_norm,
                "timeframe": timeframe,
                "count": count,
            },
        )
        req.raise_for_status()
        req_id = req.json().get("id") or req.json().get("request_id")
        result = _await_result(req_id)

    data = _parse_payload_dict(result) if result else {}
    if "error" in data or "data" not in data:
        return [], source
    bars_data = data.get("data", [])
    return bars_data, source


def _fetch_indicator(
    symbol_norm: str,
    timeframe: str,
    indicator: str,
    **kwargs: Any,
) -> dict | None:
    """Fetch a single indicator value via TCP bridge with HTTP fallback.

    Returns the raw result dict (suitable for _parse_indicator_value)
    or None if unavailable.
    """
    params: dict[str, Any] = {
        "type": "get_indicator",
        "symbol": symbol_norm,
        "timeframe": timeframe,
        "indicator": indicator.lower(),
    }
    params.update(kwargs)
    result = _tcp_send_and_await(
        params["type"],
        {k: v for k, v in params.items() if k != "type"},
    )
    if result is None:
        client = get_http_client()
        settings = get_settings_cached()
        req = client.post(
            f"{settings.gateway_url}/bridge/commands/enqueue",
            params=params,
        )
        req.raise_for_status()
        req_id = req.json().get("id") or req.json().get("request_id")
        result = _await_result(req_id)
    return result


# ---------------------------------------------------------------------------
# Tool 1: Market Regime
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_market_regime", annotations=_READ_ANNOTATIONS)
def mt5_market_regime(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 100,
    atr_period: int = 14,
) -> dict:
    """Detect the current market regime (trending, ranging, compressing, etc.) for a symbol.

    Use this tool when you need to know whether the market is trending, ranging, compressing,
    experiencing a momentum push, mean reversion setup, or volatile expansion — essential for
    selecting the right trading strategy. Regime is computed from OHLCV bar data and ATR using
    range-to-ATR ratio, ADX, RSI, Bollinger Band proximity, and volatility percentile analysis.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD" or "XAUUSD". Normalized automatically.
        timeframe (str): Timeframe string — "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: "H1")
        lookback (int): Number of bars to analyse for regime classification. (default: 100)
        atr_period (int): Period for ATR calculation used in volatility analysis. (default: 14)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "regime" (str — one of "ranging", "trending_up", "trending_down", "compressing", "momentum_push", "mean_reversion", "volatile_expansion", "low_volatility_consolidation", "unknown"), "confidence" (float 0-1), "atr" (float), "avg_range" (float), "recommendation" (str), "strategy_hints" (dict), "rsi" (float or None), "adx" (float), "atr_percentile" (float), and more. On error, returns {"error": str}.

    Example: mt5_market_regime(symbol="EURUSD", timeframe="H1", lookback=100, atr_period=14)

    Note: Regime detection is purely Python-based using standard deviation and ATR ratios — no EA calls beyond initial data fetch. The regime is used by mt5_strategy_selector to recommend the appropriate trading strategy. Regime is recalculated on every call (no caching).
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)

        atr_result = _fetch_indicator(
            symbol_norm, timeframe, "atr", period=atr_period
        )
        atr_value = _parse_indicator_value(atr_result)

        from mt5_mcp.services.market_regime import detect_regime

        result = detect_regime(
            bars=bars,
            atr_value=atr_value if atr_value is not None else 0.0,
            atr_period=atr_period,
        )
        result["symbol"] = denormalize_symbol(symbol_norm)
        result["timeframe"] = timeframe
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: Market Structure
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_market_structure", annotations=_READ_ANNOTATIONS)
def mt5_market_structure(
    symbol: str,
    timeframe: str = "H1",
    swing_lookback: int = 5,
    confirm_bos_pips: float = 0.0,
) -> dict:
    """Detect market structure — swing highs/lows, BOS (Break of Structure), ChoCh (Change of Character).

    Use this tool when you need to understand the current market structure: whether price is
    making higher highs/higher lows (bullish), lower highs/lower lows (bearish), ranging, or
    transitioning. Identifies swing points, break of structure events, and trend health (strong,
    weakening, exhausted).

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        swing_lookback (int): Number of bars on each side to confirm a swing point. Higher values = fewer but more reliable swings. (default: 5)
        confirm_bos_pips (float): Minimum pips beyond a swing high/low to confirm a Break of Structure. 0.0 means any breach counts. (default: 0.0)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "structure" (str — "bullish", "bearish", "ranging", "transitioning"), "swing_points" (list of dicts with index/price/kind), "last_bos" (dict or None — most recent Break of Structure), "last_choch" (dict or None — most recent Change of Character), "trend_health" (str — "strong", "weakening", "exhausted", "unknown"), "recent_structure" (list of str — HH/HL/LH/LL labels). On error, returns {"error": str}.

    Example: mt5_market_structure(symbol="EURUSD", timeframe="H1", swing_lookback=5)

    Note: Requires at least swing_lookback*2+3 bars of data. Market structure is computed entirely in Python from OHLCV bar data — no EA calls needed after the initial bar fetch. Good for identifying trend reversals and continuations.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, 100)

        from mt5_mcp.services.market_structure import detect_market_structure

        ms = detect_market_structure(
            bars=bars,
            swing_lookback=swing_lookback,
            confirm_bos_pips=confirm_bos_pips,
        )
        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "structure": ms.structure,
            "swing_points": ms.swing_points,
            "last_bos": ms.last_bos,
            "last_choch": ms.last_choch,
            "trend_health": ms.trend_health,
            "recent_structure": ms.recent_structure,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: Market Scan
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_market_scan", annotations=_READ_ANNOTATIONS)
def mt5_market_scan(
    symbols: list[str],
    timeframe: str = "H1",
    lookback: int = 100,
    atr_period: int = 14,
) -> dict:
    """Scan multiple symbols to detect market regimes in a single call.

    Use this tool when you need a quick overview of market conditions across a watchlist
    of symbols. Fetches bars and ATR for each symbol and runs regime detection, returning
    a consolidated table with regime, confidence, ATR, and trade recommendation per symbol.

    Args:
        symbols (list[str]): List of MT5 symbol names, e.g. ["EURUSD", "GBPUSD", "XAUUSD"]. Each is normalized automatically.
        timeframe (str): Timeframe string for analysis. (default: "H1")
        lookback (int): Number of bars to analyse per symbol. (default: 100)
        atr_period (int): ATR period for volatility analysis. (default: 14)

    Returns:
        dict: With key "symbols" containing a list of dicts, each with "symbol" (str), "regime" (str), "confidence" (float), "atr" (float), "recommendation" (str). On error, returns {"error": str}.

    Example: mt5_market_scan(symbols=["EURUSD", "GBPUSD", "XAUUSD"], timeframe="H1")

    Note: Each symbol is analysed sequentially (not parallel). For large watchlists, consider reducing lookback to keep response time reasonable. Regime values and recommendations match mt5_market_regime output.
    """
    try:
        from mt5_mcp.services.market_regime import detect_regime

        results = []
        for raw_sym in symbols:
            try:
                sym_norm = normalize_symbol(raw_sym)
                bars, _source = _fetch_bars(sym_norm, timeframe, lookback)
                atr_result = _fetch_indicator(
                    sym_norm, timeframe, "atr", period=atr_period
                )
                atr_value = _parse_indicator_value(atr_result)
                regime = detect_regime(
                    bars=bars,
                    atr_value=atr_value if atr_value is not None else 0.0,
                    atr_period=atr_period,
                )
                results.append(
                    {
                        "symbol": denormalize_symbol(sym_norm),
                        "regime": regime.get("regime", "unknown"),
                        "confidence": regime.get("confidence", 0.0),
                        "atr": regime.get("atr", 0.0),
                        "recommendation": regime.get("recommendation", "wait_for_data"),
                    }
                )
            except Exception as sym_err:
                results.append(
                    {
                        "symbol": raw_sym,
                        "regime": "unknown",
                        "confidence": 0.0,
                        "atr": 0.0,
                        "recommendation": "error",
                        "error": str(sym_err),
                    }
                )

        return {"symbols": results, "count": len(results)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 4: Opportunity Rank
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_opportunity_rank", annotations=_READ_ANNOTATIONS)
def mt5_opportunity_rank(
    symbols: list[str],
    min_score: float = 50.0,
) -> dict:
    """Rank a list of symbols by trading opportunity quality across 7 weighted factors.

    Use this tool when you have a watchlist and need to know which symbol deserves your
    attention NOW. Scores each symbol on regime clarity, spread/ATR ratio, volatility
    usability, session quality, indicator confluence, portfolio overlap, and calendar risk.
    Returns ranked results with scores, reasons, skip flags, and trade/watch/skip recommendations.

    Args:
        symbols (list[str]): List of MT5 symbol names to rank, e.g. ["EURUSD", "GBPUSD", "XAUUSD"]. Each is normalized automatically.
        min_score (float): Minimum score (0-100) to include in results. Symbols below threshold are still returned with a skip_reason. (default: 50.0)

    Returns:
        dict: With keys "results" (list of ranked symbol dicts with symbol/score/factors/reasons/skip_reason/recommendation/weights_used) and "weights_used" (dict — the weight configuration applied). On error, returns {"error": str}.

    Example: mt5_opportunity_rank(symbols=["EURUSD", "GBPUSD", "XAUUSD"], min_score=50.0)

    Note: This tool fetches full market snapshots (bars + multiple indicators) for each symbol using batch enqueue for efficiency. Uses the OpportunityRanker which internally calls SymbolSnapshotService.build(). The 7 scoring factors are: regime_clarity (20%), spread_atr_ratio (15%), volatility_usability (15%), session_quality (15%), confluence (15%), portfolio_overlap (10%), and calendar (10%). Symbols already in open positions get score=0.
    """
    try:
        from mt5_mcp.services.opportunity_rank import OpportunityRanker
        from mt5_mcp.services.snapshot_service import SymbolSnapshotService
        from mt5_mcp.services.trading_coach import TradingCoach

        ranker = OpportunityRanker()
        weights_used = ranker.get_default_weights()
        snapshots: dict[str, dict] = {}

        for raw_sym in symbols:
            try:
                sym_norm = normalize_symbol(raw_sym)
                sym_denorm = denormalize_symbol(sym_norm)

                commands = [
                    {"type": "get_bars", "symbol": sym_norm, "timeframe": "H1", "count": 100},
                    {"type": "get_indicator", "symbol": sym_norm, "timeframe": "H1", "indicator": "atr", "period": 14},
                    {"type": "get_indicator", "symbol": sym_norm, "timeframe": "H1", "indicator": "rsi", "period": 14},
                    {"type": "get_indicator", "symbol": sym_norm, "timeframe": "H1", "indicator": "ema", "period": 20},
                    {"type": "get_indicator", "symbol": sym_norm, "timeframe": "H1", "indicator": "ema", "period": 50},
                    {"type": "get_indicator", "symbol": sym_norm, "timeframe": "H1", "indicator": "macd", "fast": 12, "slow": 26, "signal": 9},
                    {"type": "get_order_book", "symbol": sym_norm},
                    {"type": "get_symbol_info", "symbol": sym_norm},
                ]

                results = _batch_enqueue_and_await(commands, timeout_s=20.0)

                bars_data = _parse_payload_dict(results[0]).get("data", []) if len(results) > 0 and results[0] else []
                atr_value = _parse_indicator_value(results[1]) if len(results) > 1 else None
                rsi = _parse_indicator_value(results[2]) if len(results) > 2 else None
                ema_fast = _parse_indicator_value(results[3]) if len(results) > 3 else None
                ema_slow = _parse_indicator_value(results[4]) if len(results) > 4 else None
                macd_payload = _parse_payload_dict(results[5]) if len(results) > 5 and results[5] else {}
                macd_data = macd_payload.get("value") or macd_payload
                book_data = _parse_payload_dict(results[6]) if len(results) > 6 and results[6] else {}
                symbol_info_data = _parse_payload_dict(results[7]) if len(results) > 7 and results[7] else {}

                bid, ask = _first_bid_ask(book_data)

                snapshot_svc = SymbolSnapshotService(coach=TradingCoach(), reconciliation_service=None)
                snapshot = snapshot_svc.build(
                    symbol=sym_denorm,
                    timeframe="H1",
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
                    positions=[],
                    include_coaching=False,
                )
                snapshots[sym_denorm] = snapshot
            except Exception as sym_err:
                snapshots[raw_sym] = {"error": str(sym_err)}

        ranked = ranker.rank(
            symbols=list(snapshots.keys()),
            snapshots=snapshots,
            portfolio_positions=[],
            min_score=min_score,
        )

        return {"results": ranked, "weights_used": weights_used}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: Divergence Analysis
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_analysis_divergence", annotations=_READ_ANNOTATIONS)
def mt5_analysis_divergence(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 50,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal_period: int = 9,
    rsi_period: int = 14,
    swing_window: int = 5,
) -> dict:
    """Detect MACD-price and RSI-price divergences — bullish, bearish, and hidden divergences.

    Use this tool when you need to identify potential trend reversals or continuations through
    divergence analysis. Bullish divergence (price makes lower low, indicator makes higher low)
    signals potential upward reversal. Bearish divergence (price makes higher high, indicator
    makes lower high) signals potential downward reversal. Hidden divergences signal trend
    continuation. All computation is pure Python from OHLCV bars.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of most-recent bars to analyse for divergences. (default: 50)
        macd_fast (int): Fast EMA period for MACD calculation. (default: 12)
        macd_slow (int): Slow EMA period for MACD calculation. (default: 26)
        macd_signal_period (int): Signal line EMA period for MACD. (default: 9)
        rsi_period (int): Wilder's RSI period for RSI-price divergence detection. (default: 14)
        swing_window (int): Sliding window size for finding local extrema in price and indicators. (default: 5)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "bullish" (list of bullish divergence dicts), "bearish" (list of bearish divergence dicts), "bullish_hidden" (list), "bearish_hidden" (list), "summary" (dict with total counts, strongest_signal, divergence_score on -10 to +10 scale). On error, returns {"error": str}.

    Example: mt5_analysis_divergence(symbol="EURUSD", timeframe="H1", lookback=50)

    Note: Requires sufficient bar data (at least macd_slow + macd_signal_period + rsi_period + swing_window*2 bars). Divergence signals are computed purely in Python from OHLCV data — no EA indicator calls needed. The divergence_score ranges from -10 (strong bearish) to +10 (strong bullish).
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)

        from mt5_mcp.services.divergence import detect_divergence

        result = detect_divergence(
            bars=bars,
            lookback=lookback,
            macd_fast=macd_fast,
            macd_slow=macd_slow,
            macd_signal_period=macd_signal_period,
            rsi_period=rsi_period,
            swing_window=swing_window,
        )
        result["symbol"] = denormalize_symbol(symbol_norm)
        result["timeframe"] = timeframe
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: Multi-Bar Patterns
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_analysis_multi_bar_patterns", annotations=_READ_ANNOTATIONS)
def mt5_analysis_multi_bar_patterns(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 100,
    period: int = 20,
    fib_lookback: int = 50,
) -> dict:
    """Detect multi-bar chart patterns — W-Bottom, M-Top, Bollinger Squeeze, Breakout, Gaps, Fibonacci.

    Use this tool when you need comprehensive pattern recognition on a symbol. Detects six
    pattern types: W-Bottom/Double Bottom reversal, M-Top/Double Top reversal, Bollinger Band
    squeeze (volatility contraction), price breakouts from recent range (with volume confirmation),
    price gaps between consecutive bars, and Fibonacci retracement/extension levels with proximity
    alerts. All computation is pure Python from OHLCV bars.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of bars to fetch for pattern detection. (default: 100)
        period (int): Lookback period for Bollinger Bands and other period-based patterns. (default: 20)
        fib_lookback (int): Number of bars to scan for Fibonacci swing high/low points. (default: 50)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "w_bottom" (dict), "m_top" (dict), "bollinger_squeeze" (dict), "breakout" (dict), "gaps" (dict), "fibonacci" (dict), and "summary" (dict with total_bullish_signals, total_bearish_signals, net_pattern_score). Each sub-dict contains status/score/price details. On error, returns {"error": str}.

    Example: mt5_analysis_multi_bar_patterns(symbol="EURUSD", timeframe="H1", lookback=100)

    Note: All six patterns are detected purely in Python from OHLCV bars — no EA calls needed. The net_pattern_score aggregates all individual pattern scores (positive = bullish bias, negative = bearish bias). Pattern detection is recalculated on every call.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)

        from mt5_mcp.services.multi_bar_patterns import detect_multi_bar_patterns

        result = detect_multi_bar_patterns(
            bars=bars,
            period=period,
            fib_lookback=fib_lookback,
        )
        result["symbol"] = denormalize_symbol(symbol_norm)
        result["timeframe"] = timeframe
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 7: Volume Profile
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_analysis_volume_profile", annotations=_READ_ANNOTATIONS)
def mt5_analysis_volume_profile(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 50,
) -> dict:
    """Analyse tick volume from OHLCV bars — volume tier, trend, price-volume signals, anomalies.

    Use this tool when you need to understand volume dynamics: whether volume is surging or
    drying up, whether price movement is supported by volume (accumulation/distribution), and
    whether any anomalous volume spikes exist. Volume analysis uses tick volume from MT5 bars
    (real volume is not available in forex CFDs).

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of most-recent bars to analyse. (default: 50)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "current_volume" (float), "avg_volume" (float), "volume_ratio" (float), "volume_tier" (str — "extreme_surge", "strong_surge", "elevated", "normal", "drying_up"), "volume_trend" (str — "increasing", "decreasing", "stable"), "price_volume_signal" (str — "accumulation", "distribution", "selling_exhaustion", "weakness", "none"), "score" (int), "anomalies" (list of dicts). On error, returns {"error": str}.

    Example: mt5_analysis_volume_profile(symbol="EURUSD", timeframe="H1", lookback=50)

    Note: Uses tick_volume from MT5 which is the number of price changes (not contract volume). In forex, tick volume serves as a proxy for market activity. Requires at least a few bars with tick_volume > 0 for meaningful analysis. The price-volume signal identifies accumulation (bullish), distribution (bearish), selling exhaustion, or weakness.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)

        from mt5_mcp.services.volume_analysis import detect_volume_anomalies

        result = detect_volume_anomalies(
            bars=bars,
            lookback=lookback,
            symbol=denormalize_symbol(symbol_norm),
        )
        result["timeframe"] = timeframe
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 8: Momentum Analysis
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_analysis_momentum", annotations=_READ_ANNOTATIONS)
def mt5_analysis_momentum(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 50,
) -> dict:
    """Compute momentum chase penalty and exhaustion risk to prevent chasing moves.

    Use this tool when you need to evaluate whether a strong price move is worth chasing or
    if it risks exhaustion. Analyses three dimensions: ATR-based chase penalty (current bar
    range vs ATR), percentile-based exhaustion risk (current bar range vs 95th percentile),
    and RSI-based signal degradation (overbought/oversold adjustments). Returns a total
    penalty score and recommendation from "normal" to "avoid_entry".

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of bars used for percentile-based exhaustion analysis. (default: 50)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "chase_tier" (str — "extreme_chase", "strong_chase", "moderate_chase", "normal"), "chase_penalty" (int), "range_atr_ratio" (float), "exhaustion_risk" (bool), "exhaustion_penalty" (int), "rsi_signal" (str — "overbought", "elevated", "oversold", "approaching_oversold", "neutral"), "rsi_penalty" (int), "total_penalty" (int), "recommendation" (str — "avoid_entry", "reduce_size", "caution", "normal"). On error, returns {"error": str}.

    Example: mt5_analysis_momentum(symbol="EURUSD", timeframe="H1", lookback=50)

    Note: A total_penalty <= -10 means "avoid_entry", <= -6 means "reduce_size", <= -3 means "caution". Positive RSI penalties (for oversold conditions) can offset chase penalties. The exhaustion risk flag is set when current bar range exceeds the 95th percentile of the lookback window.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)
        rsi_result = _fetch_indicator(symbol_norm, timeframe, "rsi", period=14)
        atr_result = _fetch_indicator(symbol_norm, timeframe, "atr", period=14)

        rsi_value = _parse_indicator_value(rsi_result)
        atr_value = _parse_indicator_value(atr_result)

        from mt5_mcp.services.momentum import compute_momentum_penalty

        result = compute_momentum_penalty(
            bars=bars,
            rsi=rsi_value,
            atr=atr_value,
            lookback=lookback,
        )
        result["symbol"] = denormalize_symbol(symbol_norm)
        result["timeframe"] = timeframe
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 9: Multi-Timeframe Indicators
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_multi_timeframe_indicators", annotations=_READ_ANNOTATIONS)
def mt5_multi_timeframe_indicators(
    symbol: str,
    timeframes: Optional[list[str]] = None,
    indicator: str = "rsi",
    period: Optional[int] = None,
    fast: Optional[int] = None,
    slow: Optional[int] = None,
    signal: Optional[int] = None,
) -> dict:
    """Fetch the same indicator across multiple timeframes in a single call.

    Use this tool when you need to compare an indicator (RSI, MACD, ATR, etc.) across
    multiple timeframes — essential for multi-timeframe analysis and trade confirmation.
    Fetches the indicator value for each specified timeframe and returns a consolidated map.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframes (list[str]): List of timeframe strings, e.g. ["M15", "H1", "H4", "D1"]. (default: ["M15", "H1", "H4", "D1"])
        indicator (str): Indicator name — "rsi", "macd", "atr", "ema", "sma", "bbands", "stoch", "adx", "cci", "williams", "momentum". (default: "rsi")
        period (Optional[int]): Main period for the indicator (overrides INDICATOR_DEFAULTS). (default: None)
        fast (Optional[int]): Fast period for dual-line indicators like MACD. (default: None)
        slow (Optional[int]): Slow period for dual-line indicators like MACD. (default: None)
        signal (Optional[int]): Signal period for MACD. (default: None)

    Returns:
        dict: With keys "symbol" (str), "indicator" (str), "readings" (dict mapping timeframe → dict with "value" and "status"). On error, returns {"error": str}.

    Example: mt5_multi_timeframe_indicators(symbol="EURUSD", timeframes=["M15", "H1", "H4", "D1"], indicator="rsi", period=14)

    Note: Fetches each timeframe sequentially (not batched). For MACD, the value is a dict with macd/signal/histogram. For most other indicators, the value is a single float. Default indicator parameters from INDICATOR_DEFAULTS are applied when not explicitly provided.
    """
    try:
        if timeframes is None:
            timeframes = ["M15", "H1", "H4", "D1"]

        symbol_norm = normalize_symbol(symbol)
        indicator_lower = indicator.lower()
        defaults = INDICATOR_DEFAULTS.get(indicator_lower, {})

        params: dict[str, Any] = {
            "type": "get_indicator",
            "indicator": indicator_lower,
        }
        if period is not None:
            params["period"] = period
        elif "period" in defaults:
            params["period"] = defaults["period"]
        if fast is not None:
            params["fast"] = fast
        elif "fast" in defaults:
            params["fast"] = defaults["fast"]
        if slow is not None:
            params["slow"] = slow
        elif "slow" in defaults:
            params["slow"] = defaults["slow"]
        if signal is not None:
            params["signal"] = signal
        elif "signal" in defaults:
            params["signal"] = defaults["signal"]

        readings: dict[str, dict] = {}
        for tf in timeframes:
            try:
                tf_params = {**params, "symbol": symbol_norm, "timeframe": tf}
                result = _tcp_send_and_await(
                    tf_params["type"],
                    {k: v for k, v in tf_params.items() if k != "type"},
                )
                if result is None:
                    client = get_http_client()
                    settings = get_settings_cached()
                    req = client.post(
                        f"{settings.gateway_url}/bridge/commands/enqueue",
                        params=tf_params,
                    )
                    req.raise_for_status()
                    req_id = req.json().get("id") or req.json().get("request_id")
                    result = _await_result(req_id)

                data = _parse_payload_dict(result) if result else {}
                value = _parse_indicator_value_from_data(data, indicator_lower)
                readings[tf] = {
                    "value": value,
                    "status": result.get("status", "unknown") if result else "unknown",
                }
            except Exception as tf_err:
                readings[tf] = {"error": str(tf_err)}

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "indicator": indicator_lower,
            "readings": readings,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 10: Correlation Matrix
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_correlation_matrix", annotations=_READ_ANNOTATIONS)
def mt5_correlation_matrix(
    symbols: list[str],
    timeframe: str = "H1",
    lookback: int = 100,
) -> dict:
    """Compute Pearson correlation matrix between close prices of multiple symbols.

    Use this tool when you need to understand how different symbols move relative to each
    other — essential for portfolio diversification, hedging, and avoiding correlated trades.
    Computes return-based Pearson correlation from OHLCV bar data. A correlation of +1 means
    perfect positive correlation, -1 means perfect negative correlation, 0 means no correlation.

    Args:
        symbols (list[str]): List of MT5 symbol names to correlate, e.g. ["EURUSD", "GBPUSD", "XAUUSD"]. Each is normalized automatically.
        timeframe (str): Timeframe string for bar data used in correlation. (default: "H1")
        lookback (int): Number of bars to fetch for correlation calculation. (default: 100)

    Returns:
        dict: With keys "timeframe" (str), "lookback" (int), "matrix" (dict of dict — matrix[symbol_a][symbol_b] = Pearson r or None if insufficient data). On error, returns {"error": str}.

    Example: mt5_correlation_matrix(symbols=["EURUSD", "GBPUSD", "XAUUSD"], timeframe="H1", lookback=100)

    Note: Correlations are computed from daily returns (percentage changes between consecutive closes). Requires at least 2 bars of data per symbol. Returns None for pairs with insufficient data. Matrix is symmetric: matrix[A][B] == matrix[B][A]. Correlations are recalculated on every call using fresh bar data.
    """
    try:
        from mt5_mcp.services.agent_capabilities import compute_correlation_matrix

        close_series: dict[str, list[float]] = {}
        for raw_sym in symbols:
            sym_norm = normalize_symbol(raw_sym)
            bars, _source = _fetch_bars(sym_norm, timeframe, lookback)
            sym_denorm = denormalize_symbol(sym_norm)
            close_series[sym_denorm] = [
                float(b["close"]) for b in bars if "close" in b
            ]

        matrix = compute_correlation_matrix(close_series)

        return {
            "timeframe": timeframe,
            "lookback": lookback,
            "matrix": matrix,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 11: Strategy Selector
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_strategy_selector", annotations=_READ_ANNOTATIONS)
def mt5_strategy_selector(
    regime: Optional[str] = None,
) -> dict:
    """Select the optimal trading strategy for a given market regime, or list all strategies.

    Use this tool to get a strategy recommendation based on the current market regime detected
    by mt5_market_regime. Each strategy includes entry style, stop type, take profit type,
    maximum positions, risk multiplier, trailing stop behaviour, and description. If no regime
    is provided, returns all available strategies for reference.

    Args:
        regime (Optional[str]): Market regime string — one of "trending_up", "trending_down", "ranging", "compressing", "momentum_push", "mean_reversion", "volatile_expansion", "low_volatility_consolidation". If None, returns all strategies. (default: None)

    Returns:
        dict: With keys "all_strategies" (list of strategy dicts), and optionally "recommended" (dict — the best strategy for the given regime with name/entry_style/stop_type/take_profit_type/max_positions/risk_multiplier/trailing/description). On error, returns {"error": str}.

    Example: mt5_strategy_selector(regime="trending_up")

    Note: No data fetch needed — strategy definitions are static. The recommended strategy is determined by the regime-strategy mapping in the strategy_selector service. Always pair this tool with mt5_market_regime for regime-aware trading.
    """
    try:
        from mt5_mcp.services.strategy_selector import list_strategies, select_strategy

        all_strategies = list_strategies()
        result: dict[str, Any] = {"all_strategies": all_strategies}

        if regime:
            selected = select_strategy(regime)
            result["recommended"] = {
                "name": selected.name,
                "regime": selected.regime,
                "entry_style": selected.entry_style,
                "stop_type": selected.stop_type,
                "take_profit_type": selected.take_profit_type,
                "max_positions": selected.max_positions,
                "risk_multiplier": selected.risk_multiplier,
                "trailing": selected.trailing,
                "description": selected.description,
            }

        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 12: VWAP
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_vwap", annotations=_READ_ANNOTATIONS)
def mt5_vwap(
    symbol: str,
    timeframe: str = "H1",
    bar_count: int = 100,
    std_dev_multiplier: float = 2.0,
) -> dict:
    """Compute Volume-Weighted Average Price (VWAP) with standard deviation bands.

    Use this tool when you need an institutional-grade reference price that accounts for
    both price and volume. VWAP = Sum(typical_price * volume) / Sum(volume), where
    typical_price = (high + low + close) / 3. Includes upper/lower standard deviation
    bands and categorises current price position (above/below/at VWAP).

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        bar_count (int): Number of bars to include in VWAP calculation. (default: 100)
        std_dev_multiplier (float): Number of standard deviations for upper/lower bands. (default: 2.0)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "current_vwap" (float), "vwap_deviation_upper" (float), "vwap_deviation_lower" (float), "distance_from_vwap_pct" (float — percentage distance from VWAP), "price_position" (str — "above_vwap", "below_vwap", "at_vwap"), "volume_type" (str). On error, returns {"error": str}.

    Example: mt5_vwap(symbol="EURUSD", timeframe="H1", bar_count=100, std_dev_multiplier=2.0)

    Note: Uses tick volume as a proxy for real volume (standard for forex CFDs where real volume is unavailable). Requires at least 5 bars of data. VWAP is calculated cumulatively from the oldest to newest bar — the order of bars matters.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, bar_count)

        from mt5_mcp.services.vwap import compute_vwap

        vwap_result = compute_vwap(
            bars=bars,
            std_dev_multiplier=std_dev_multiplier,
        )

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "current_vwap": vwap_result.current_vwap,
            "vwap_deviation_upper": vwap_result.vwap_deviation_upper,
            "vwap_deviation_lower": vwap_result.vwap_deviation_lower,
            "distance_from_vwap_pct": vwap_result.distance_from_vwap_pct,
            "price_position": vwap_result.price_position,
            "volume_type": vwap_result.volume_type,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 13: Volume at Price
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_volume_at_price", annotations=_READ_ANNOTATIONS)
def mt5_volume_at_price(
    symbol: str,
    timeframe: str = "H1",
    bar_count: int = 100,
    num_bins: int = 20,
) -> dict:
    """Compute Volume-at-Price profile — identifies high-volume nodes (Point of Control) and Value Area.

    Use this tool when you need to understand which price levels have the highest trading
    activity. Buckets price levels into bins and aggregates tick volume per bin. Identifies
    the Point of Control (POC — price level with highest volume), Value Area (the range
    containing 70% of total volume), and current price position relative to the value area.
    Essential for identifying support/resistance zones from volume profile.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        bar_count (int): Number of bars to include in the volume profile. (default: 100)
        num_bins (int): Number of price bins to divide the range into. Higher = finer granularity but potentially noisier. (default: 20)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "poc" (float — Point of Control price), "value_area_high" (float), "value_area_low" (float), "value_area_width" (float), "current_price_position" (str — "above_value_area", "below_value_area", "at_poc", "inside_value_area"), "price_distribution" (list of dicts with price_low/price_high/volume/is_poc/is_value_area per bin). On error, returns {"error": str}.

    Example: mt5_volume_at_price(symbol="EURUSD", timeframe="H1", bar_count=100, num_bins=20)

    Note: Uses tick volume as a proxy (standard for forex). Requires at least 10 bars for meaningful analysis. The POC level often acts as strong support/resistance. The Value Area (70% of volume) represents the range of "fair value" — prices outside this zone may be overextended.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, bar_count)

        from mt5_mcp.services.vwap import compute_volume_at_price

        vap_result = compute_volume_at_price(
            bars=bars,
            num_bins=num_bins,
        )

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "poc": vap_result.poc,
            "value_area_high": vap_result.value_area_high,
            "value_area_low": vap_result.value_area_low,
            "value_area_width": vap_result.value_area_width,
            "price_distribution": vap_result.price_distribution,
            "current_price_position": vap_result.current_price_position,
            "volume_type": vap_result.volume_type,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 14: Setup Probability
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_setup_probability", annotations=_READ_ANNOTATIONS)
def mt5_setup_probability(
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
    session: Optional[str] = None,
    min_samples: int = 5,
) -> dict:
    """Estimate the probability of a successful trade based on historical journal data.

    Use this tool when you want a data-driven win rate estimate for the current setup before
    entering a trade. Queries the trade journal database for historical trades matching the
    current symbol, regime, or session context. Returns estimated win rate, sample size,
    confidence level, similar trades, win rates broken down by regime and session, common
    mistakes, and a recommendation (favorable/neutral/caution/avoid/insufficient_data).

    Args:
        symbol (Optional[str]): MT5 symbol name to filter by, e.g. "EURUSD". If None, uses all symbols. (default: None)
        regime (Optional[str]): Market regime to filter by, e.g. "ranging", "trending_up". If None, uses all regimes. (default: None)
        session (Optional[str]): Trading session to filter by, e.g. "London", "NY", "Tokyo". If None, uses all sessions. (default: None)
        min_samples (int): Minimum number of historical trades needed for a reliable estimate. Below this, the estimate blends with the base win rate. (default: 5)

    Returns:
        dict: With keys "estimated_win_rate" (float 0-100), "sample_size" (int), "confidence" (str — "high", "medium", "low", "very_low", "none"), "similar_trades" (list of recent matching trade dicts), "win_rate_by_regime" (dict), "win_rate_by_session" (dict), "common_mistakes" (list), "recommendation" (str — "favorable", "neutral", "caution", "avoid", "insufficient_data"). On error, returns {"error": str}.

    Example: mt5_setup_probability(symbol="EURUSD", regime="ranging", min_samples=5)

    Note: Requires the trade journal database to have historical entries (populated via mt5_log_decision calls). Returns "insufficient_data" if no completed trades are found. Session filtering matches the "session_id" field in the journal. The win rate blends filtered and base rates when sample_size < min_samples.
    """
    try:
        from mt5_mcp.services.trade_journal_db import get_journal_db
        from mt5_mcp.services.setup_probability import estimate_setup_probability

        db = get_journal_db()
        query_kwargs: dict[str, Any] = {"limit": 500}
        if symbol:
            query_kwargs["symbol"] = normalize_symbol(symbol)
        trades = db.query(**query_kwargs)

        result = estimate_setup_probability(
            trades=trades,
            current_regime=regime,
            current_session=session,
            current_symbol=normalize_symbol(symbol) if symbol else None,
            min_samples=min_samples,
        )

        return {
            "estimated_win_rate": result.estimated_win_rate,
            "sample_size": result.sample_size,
            "confidence": result.confidence,
            "similar_trades": result.similar_trades,
            "win_rate_by_regime": result.win_rate_by_regime,
            "win_rate_by_session": result.win_rate_by_session,
            "common_mistakes": result.common_mistakes,
            "recommendation": result.recommendation,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 15: Support & Resistance
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_support_resistance", annotations=_READ_ANNOTATIONS)
def mt5_support_resistance(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 100,
) -> dict:
    """Identify key support and resistance levels from recent price action.

    Use this tool when you need quick support/resistance levels without running full pattern
    analysis. Computed inline from OHLCV bar data by finding the most significant swing lows
    (support) and swing highs (resistance) in the lookback window. Returns the top three
    levels for each side along with the current price for reference.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of bars to scan for swing highs/lows. (default: 100)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "support_levels" (list of float — up to 3 key support levels, sorted ascending), "resistance_levels" (list of float — up to 3 key resistance levels, sorted ascending), "current_price" (float — most recent close). On error, returns {"error": str}.

    Example: mt5_support_resistance(symbol="EURUSD", timeframe="H1", lookback=100)

    Note: This is a lightweight, inline computation — more sophisticated S/R detection is available through mt5_analysis_multi_bar_patterns. Support levels are the lowest swing lows, resistance levels are the highest swing highs in the lookback period. The number of levels returned depends on data availability.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)

        if not bars:
            return {
                "symbol": denormalize_symbol(symbol_norm),
                "timeframe": timeframe,
                "support_levels": [],
                "resistance_levels": [],
                "current_price": None,
            }

        highs = sorted(
            [float(b["high"]) for b in bars if "high" in b], reverse=True
        )
        lows = sorted([float(b["low"]) for b in bars if "low" in b])
        closes = [float(b["close"]) for b in bars if "close" in b]

        # Deduplicate nearby levels (within 0.1% of each other)
        def _deduplicate_levels(
            levels: list[float], threshold_pct: float = 0.001
        ) -> list[float]:
            if not levels:
                return []
            deduped = [levels[0]]
            for level in levels[1:]:
                if abs(level - deduped[-1]) / max(abs(deduped[-1]), 0.0001) > threshold_pct:
                    deduped.append(level)
            return deduped[:3]

        support = _deduplicate_levels(lows)[:3]
        resistance = _deduplicate_levels(highs)[:3]

        return {
            "symbol": denormalize_symbol(symbol_norm),
            "timeframe": timeframe,
            "support_levels": support,
            "resistance_levels": resistance,
            "current_price": closes[-1] if closes else None,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 16: Volatility Profile
# ---------------------------------------------------------------------------


@mcp.tool(name="mt5_volatility_profile", annotations=_READ_ANNOTATIONS)
def mt5_volatility_profile(
    symbol: str,
    timeframe: str = "H1",
    lookback: int = 100,
    atr_period: int = 14,
) -> dict:
    """Build a volatility profile for a symbol — average range, ATR percentage, and bar counts.

    Use this tool when you need a concise summary of a symbol's volatility characteristics.
    Computes average high-low range, ATR as a percentage of price, and other volatility
    metrics. Useful for comparing volatility across symbols, setting stop-loss distances,
    and understanding typical price movement magnitude.

    Args:
        symbol (str): MT5 symbol name, e.g. "EURUSD". Normalized automatically.
        timeframe (str): Timeframe string for bar data. (default: "H1")
        lookback (int): Number of bars to include in the profile. (default: 100)
        atr_period (int): ATR period for the volatility calculation. (default: 14)

    Returns:
        dict: With keys "symbol" (str), "timeframe" (str), "bars_used" (int), "last_close" (float), "atr_value" (float), "average_range" (float), "average_range_percent" (float — average range as % of price), "atr_percent_of_price" (float — ATR as % of price). On error, returns {"error": str}.

    Example: mt5_volatility_profile(symbol="XAUUSD", timeframe="H1", lookback=100, atr_period=14)

    Note: No external service dependency — uses inline computation from OHLCV bars and ATR value. The average_range_percent and atr_percent_of_price values are useful for comparing volatility across symbols with different price scales. Higher percentages indicate more volatile instruments.
    """
    try:
        symbol_norm = normalize_symbol(symbol)
        bars, source = _fetch_bars(symbol_norm, timeframe, lookback)
        atr_result = _fetch_indicator(
            symbol_norm, timeframe, "atr", period=atr_period
        )
        atr_value = _parse_indicator_value(atr_result) or 0.0

        from mt5_mcp.services.agent_capabilities import build_volatility_profile

        return build_volatility_profile(
            symbol=denormalize_symbol(symbol_norm),
            timeframe=timeframe,
            bars=bars,
            atr_value=atr_value,
        )
    except Exception as e:
        return {"error": str(e)}
