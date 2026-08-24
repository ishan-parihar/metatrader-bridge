"""
Analysis MCP tools for the standalone MetaTraderBridge Analysis server.

Uses BridgeProtocol to communicate with the TCP bridge (port 8026)
instead of the in-process shared.py singleton. This makes the analysis
server fully independent — its tools fetch raw OHLCV data over TCP
and run computation locally.

Each tool is self-contained and expects no external singletons.
"""

import logging
from typing import Optional

from mt5_mcp.mcp_bridge_protocol import BridgeProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bridge singleton for this module
# ---------------------------------------------------------------------------

_bridge: Optional[BridgeProtocol] = None


async def _get_bridge() -> BridgeProtocol:
    """Get or create the singleton bridge connection.

    Connects lazily on first use so that importing this module does not
    immediately open a TCP connection.
    """
    global _bridge
    if _bridge is None:
        _bridge = BridgeProtocol(host="127.0.0.1", port=8026, timeout=5.0)
        await _bridge.connect()
    return _bridge


async def _fetch_bars(symbol: str, timeframe: str = "H1", count: int = 100) -> list:
    """Fetch OHLCV bar data via the bridge protocol.

    Returns a list of bar dicts, each with keys like ``close``, ``high``,
    ``low``, ``open``, ``time``. Returns an empty list on failure.
    """
    bridge = await _get_bridge()
    result = await bridge.send_and_await(
        "get_bars", {"symbol": symbol, "timeframe": timeframe, "count": count}
    )
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"_fetch_bars({symbol}): {result['error']}")
        return []
    # The bridge returns ``{"data": [...]}``; fall back to returning the
    # whole result if it is already a list.
    return result.get("data", result if isinstance(result, list) else [])


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_tools(mcp):
    """Register all analysis tools on the given FastMCP instance.

    Usage::

        from apps.mcp_analysis.tools_analysis import register_tools
        register_tools(mcp)
    """

    @mcp.tool(name="mt5_analysis_regime")
    async def mt5_analysis_regime(
        symbol: str = "EURUSD", timeframe: str = "H1"
    ) -> dict:
        """Detect market regime (trending / ranging / compressing) for a symbol.

        Uses EMA slope and ATR percentage to classify the current market
        state. Helps determine whether to use trend-following or
        mean-reversion strategies.

        Args:
            symbol: MT5 symbol name, e.g. ``"EURUSD"``. (default: ``"EURUSD"``)
            timeframe: Timeframe string — ``"M1"``, ``"M5"``, ``"H1"``, ``"D1"``, etc. (default: ``"H1"``)

        Returns:
            A dict with ``symbol``, ``timeframe``, ``regime`` (``"trending"`` /
            ``"ranging"`` / ``"compressing"``), ``direction``, ``metrics``,
            and ``bar_count``.
        """
        try:
            bars = await _fetch_bars(symbol, timeframe)
            if not bars:
                return {"error": f"No bars available for {symbol}"}

            closes = [b.get("close", b.get("Close", 0)) for b in bars]
            highs = [b.get("high", b.get("High", 0)) for b in bars]
            lows = [b.get("low", b.get("Low", 0)) for b in bars]

            if len(closes) < 20:
                return {"error": "Need at least 20 bars"}

            # --- Regime detection ---
            # Fast EMA (9) vs Slow EMA (21) → slope direction
            ema_fast = sum(closes[-9:]) / 9
            ema_slow = sum(closes[-21:]) / 21

            # ATR as % of price
            atr_values = []
            for i in range(1, min(15, len(highs))):
                atr_values.append(highs[-i] - lows[-i])
            atr = sum(atr_values) / len(atr_values) if atr_values else 0
            avg_price = sum(closes[-5:]) / 5

            slope = (ema_fast - ema_slow) / ema_slow * 100 if ema_slow else 0
            atr_pct = atr / avg_price * 100 if avg_price else 0

            if abs(slope) > 0.1 and atr_pct > 0.3:
                regime = "trending"
                direction = "bullish" if slope > 0 else "bearish"
            elif atr_pct < 0.15:
                regime = "compressing"
                direction = "neutral"
            else:
                regime = "ranging"
                direction = "neutral"

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "regime": regime,
                "direction": direction,
                "metrics": {
                    "ema_fast_slope_pct": round(slope, 4),
                    "atr_pct": round(atr_pct, 4),
                },
                "bar_count": len(closes),
            }
        except Exception as e:
            logger.error(f"Regime detection error: {e}")
            return {"error": str(e)}

    @mcp.tool(name="mt5_analysis_structure")
    async def mt5_analysis_structure(
        symbol: str = "EURUSD",
        timeframe: str = "H1",
        swing_lookback: int = 5,
    ) -> dict:
        """Detect market structure — swing highs, swing lows, and trend direction.

        Identifies significant swing points in price action and classifies
        the overall trend as uptrend, downtrend, or ranging.

        Args:
            symbol: MT5 symbol name, e.g. ``"EURUSD"``. (default: ``"EURUSD"``)
            timeframe: Timeframe string. (default: ``"H1"``)
            swing_lookback: Number of bars to look left and right when
                detecting swing points. Higher values = fewer, more
                significant swings. (default: ``5``)

        Returns:
            A dict with ``symbol``, ``timeframe``, ``trend``,
            ``swing_highs`` count, ``swing_lows`` count,
            ``last_swing_high``, ``last_swing_low``, and ``current_price``.
        """
        try:
            bars = await _fetch_bars(symbol, timeframe)
            if not bars or len(bars) < swing_lookback * 3:
                return {"error": f"Insufficient bars for {symbol}"}

            highs = [b.get("high", b.get("High", 0)) for b in bars]
            lows = [b.get("low", b.get("Low", 0)) for b in bars]

            # Identify swing highs and lows using local minima/maxima
            swing_highs = []
            swing_lows = []
            n = len(bars)
            for i in range(swing_lookback, n - swing_lookback):
                window_high = highs[i - swing_lookback : i + swing_lookback + 1]
                if highs[i] == max(window_high):
                    swing_highs.append({"index": i, "price": highs[i]})
                window_low = lows[i - swing_lookback : i + swing_lookback + 1]
                if lows[i] == min(window_low):
                    swing_lows.append({"index": i, "price": lows[i]})

            # Determine trend from successive swing points
            trend = "insufficient_data"
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_highs = swing_highs[-2:]
                last_lows = swing_lows[-2:]
                higher_highs = last_highs[1]["price"] > last_highs[0]["price"]
                higher_lows = last_lows[1]["price"] > last_lows[0]["price"]

                if higher_highs and higher_lows:
                    trend = "uptrend"
                elif not higher_highs and not higher_lows:
                    trend = "downtrend"
                else:
                    trend = "ranging"

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "trend": trend,
                "swing_highs": len(swing_highs),
                "swing_lows": len(swing_lows),
                "last_swing_high": swing_highs[-1]["price"] if swing_highs else None,
                "last_swing_low": swing_lows[-1]["price"] if swing_lows else None,
                "current_price": highs[-1] if highs else 0,
            }
        except Exception as e:
            logger.error(f"Structure detection error: {e}")
            return {"error": str(e)}

    @mcp.tool(name="mt5_analysis_market_snapshot")
    async def mt5_analysis_market_snapshot(symbol: str = "EURUSD") -> dict:
        """Get a quick market snapshot with current price, spread, and recent movement.

        Combines tick data with short-term and daily OHLCV bars to produce
        a lightweight market overview — useful for initial symbol assessment
        before deeper analysis.

        Args:
            symbol: MT5 symbol name, e.g. ``"EURUSD"``. (default: ``"EURUSD"``)

        Returns:
            A dict with ``symbol``, ``bid``, ``ask``, ``spread``,
            ``hourly_change_pct``, ``hourly_high``, ``hourly_low``,
            and ``daily_change_pct``.
        """
        try:
            bridge = await _get_bridge()

            # Current tick
            tick_result = await bridge.send_and_await(
                "get_ticks", {"symbol": symbol, "count": 1}
            )

            # Short-term bars (5-minute, last hour)
            bars = await _fetch_bars(symbol, "M5", 12)
            # Daily bars
            daily_bars = await _fetch_bars(symbol, "D1", 2)

            ticks = (
                tick_result.get("data", [])
                if isinstance(tick_result, dict)
                else (tick_result if isinstance(tick_result, list) else [])
            )

            result: dict = {"symbol": symbol}

            if ticks:
                last_tick = ticks[0] if isinstance(ticks, list) else ticks
                result["bid"] = last_tick.get("bid", last_tick.get("Bid", 0))
                result["ask"] = last_tick.get("ask", last_tick.get("Ask", 0))
                result["spread"] = abs(float(result.get("ask", 0)) - float(result.get("bid", 0)))
                result["timestamp"] = last_tick.get("time", last_tick.get("Time", ""))

            if bars:
                closes = [b.get("close", b.get("Close", 0)) for b in bars]
                first_close = float(closes[0]) if closes[0] else 0
                last_close = float(closes[-1]) if closes[-1] else 0
                result["hourly_change_pct"] = round(
                    (last_close - first_close) / first_close * 100 if first_close else 0,
                    2,
                )
                result["hourly_high"] = max(
                    float(b.get("high", b.get("High", 0))) for b in bars
                )
                result["hourly_low"] = min(
                    float(b.get("low", b.get("Low", 0))) for b in bars
                )

            if daily_bars and len(daily_bars) >= 2:
                prev_close = float(
                    daily_bars[-2].get("close", daily_bars[-2].get("Close", 0))
                )
                current_open = float(
                    daily_bars[-1].get("open", daily_bars[-1].get("Open", 0))
                )
                result["daily_change_pct"] = round(
                    (current_open - prev_close) / prev_close * 100 if prev_close else 0,
                    2,
                )

            return result
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
            return {"error": str(e)}
