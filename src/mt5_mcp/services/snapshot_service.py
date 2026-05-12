"""Symbol Snapshot Service — authoritative one-call market-and-viability surface.

Combines 5+ separate API calls (bars, indicators, order book, symbol info,
coaching) into a single assembled snapshot payload.

Usage:
    service = SymbolSnapshotService(
        coach=TradingCoach(),
        reconciliation_service=recon_svc,
    )
    snapshot = service.build(
        symbol="XAUUSD",
        timeframe="H1",
        bars_data=[...],
        indicator_data={"atr": ..., "rsi": ..., "ema_fast": ..., "ema_slow": ..., "macd": ...},
        order_book_data={"bids": [...], "asks": [...]},
        symbol_info_data={...},
        positions=[...],
        include_coaching=True,
        session_id="sess-123",
        strategy_id="scalp",
    )
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mt5_mcp.services.market_context import build_context
from mt5_mcp.services.market_regime import detect_regime
from mt5_mcp.services.trading_coach import TradingCoach, CoachingAdvice
from mt5_mcp.services.session_service import get_session_context, get_session_for_pair
from mt5_mcp.services.economic_calendar import get_upcoming_events
from mt5_mcp.services.reconciliation import ReconciliationService


class SymbolSnapshotService:
    """Aggregates all market context into a single snapshot call.

    The service does NOT call the EA directly — it orchestrates existing
    services using raw data provided by the caller (typically an MCP endpoint
    that handles the bridge communication).
    """

    def __init__(
        self,
        coach: TradingCoach | None = None,
        reconciliation_service: ReconciliationService | None = None,
    ):
        self.coach = coach or TradingCoach()
        self.reconciliation_service = reconciliation_service

    def build(
        self,
        *,
        symbol: str,
        timeframe: str,
        # From bars endpoint
        bars_data: list[dict] | None = None,
        # From indicators endpoint
        atr_value: float | None = None,
        atr_percentile: float | None = None,
        rsi: float | None = None,
        ema_fast: float | None = None,
        ema_slow: float | None = None,
        macd: dict | None = None,
        bbands: dict | None = None,
        # From order book endpoint
        order_book_data: dict | None = None,
        # From ticks (for spread)
        bid: float | None = None,
        ask: float | None = None,
        # From symbol info endpoint
        symbol_info_data: dict | None = None,
        # From positions/bridge
        positions: list[dict] | None = None,
        # Options
        include_coaching: bool = True,
        session_id: str | None = None,
        strategy_id: str | None = None,
    ) -> dict:
        """Build a complete snapshot payload for a symbol.

        Returns a dict with all market context, viability warnings,
        coaching advice, and current exposure.
        """
        symbol_upper = symbol.upper()
        warnings = self._validate_inputs(
            symbol_upper=symbol_upper,
            bars_data=bars_data,
            atr_value=atr_value,
            rsi=rsi,
            bid=bid,
            ask=ask,
        )

        result: dict[str, Any] = {
            "symbol": symbol_upper,
            "timeframe": timeframe,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data_quality": warnings if warnings else None,
        }

        # ====== 1. PRICE DATA ======
        spread_points: float | None = None
        spread_ratio_atr: float | None = None

        if bid is not None and ask is not None and bid > 0 and ask > 0:
            spread_points = round(ask - bid, 6)
            result["price"] = {
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 6),
                "spread_points": spread_points,
            }
        else:
            result["price"] = {
                "bid": bid,
                "ask": ask,
                "spread_points": None,
                "spread_ratio_atr": None,
            }

        # ====== 2. BARS SUMMARY ======
        bars_summary = self._summarize_bars(bars_data or [], atr_value)
        result["bars"] = bars_summary

        # ====== 3. INDICATORS ======
        indicators = self._summarize_indicators(
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            macd=macd,
            bbands=bbands,
            symbol=symbol_upper,
        )
        result["indicators"] = indicators

        # Spread ratio (spread / atr)
        if spread_points is not None and atr_value and atr_value > 0:
            spread_ratio_atr = round(spread_points / atr_value, 4)
            result["price"]["spread_ratio_atr"] = spread_ratio_atr

        # ====== 4. REGIME ======
        regime = detect_regime(
            bars=bars_data or [],
            atr_value=atr_value or 0,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )
        result["regime"] = regime

        # ====== 5. SUPPORT / RESISTANCE ======
        result["support_resistance"] = self._extract_support_resistance(bars_data or [])

        # ====== 6. ORDER BOOK ======
        result["order_book"] = self._summarize_order_book(order_book_data or {})

        # ====== 7. SESSION CONTEXT ======
        try:
            session_ctx = get_session_context()
            pair_ctx = get_session_for_pair(symbol_upper)
            result["session_context"] = {
                "current_sessions": session_ctx.current_sessions,
                "active_overlaps": session_ctx.active_overlaps,
                "is_market_open": session_ctx.is_market_open,
                "volatility_regime": session_ctx.volatility_regime,
                "spread_quality": session_ctx.spread_quality,
                "volume_concentration_pct": round(
                    session_ctx.volume_concentration * 100, 1
                ),
                "time_to_next_session_minutes": session_ctx.time_to_next_session,
                "time_to_session_close_minutes": session_ctx.time_to_session_close,
                "day_of_week": session_ctx.day_of_week,
                "day_of_week_factor": session_ctx.day_of_week_factor,
                "pair_quality_score": pair_ctx.get("quality_score", 0),
                "pair_is_optimal": pair_ctx.get("is_optimal", False),
                "pair_warnings": pair_ctx.get("warnings", []),
            }
        except Exception:
            result["session_context"] = {"error": "unavailable"}

        # ====== 8. CALENDAR ======
        result["calendar"] = self._calendar_context(symbol_upper)

        # ====== 9. COACHING ======
        if include_coaching:
            result["coaching"] = self._build_coaching(
                symbol=symbol_upper,
                bars_data=bars_data or [],
                atr_value=atr_value,
                atr_percentile=atr_percentile,
                rsi=rsi,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                spread_points=spread_points,
                regime=regime.get("regime"),
                position_in_range=regime.get("price_position_pct"),
                bid=bid,
                ask=ask,
            )

        # ====== 10. VIABILITY WARNINGS ======
        result["viability_warnings"] = self._viability_warnings(
            symbol=symbol_upper,
            spread_points=spread_points,
            spread_ratio_atr=spread_ratio_atr,
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            regime=regime.get("regime"),
            calendar=result.get("calendar", {}),
            session=result.get("session_context", {}),
        )

        # ====== 11. CURRENT EXPOSURE ======
        result["current_exposure"] = self._current_exposure(
            symbol_upper, positions or []
        )

        return result

    # ----------------------------------------------------------------
    # Data validation
    # ----------------------------------------------------------------

    def _validate_inputs(
        self,
        *,
        symbol_upper: str,
        bars_data: list[dict] | None,
        atr_value: float | None,
        rsi: float | None,
        bid: float | None,
        ask: float | None,
    ) -> list[str]:
        warnings: list[str] = []

        if bars_data:
            required = {"open", "high", "low", "close"}
            bad_bars = [
                i for i, b in enumerate(bars_data) if not required.issubset(b.keys())
            ]
            if bad_bars:
                warnings.append(
                    f"{len(bad_bars)} bar(s) missing OHLC fields — indicators may be inaccurate"
                )
            if len(bars_data) < 50:
                warnings.append(
                    f"Only {len(bars_data)} bars provided — indicators (EMA, MACD) need 50+ for accuracy"
                )

        if atr_value is not None and atr_value <= 0:
            warnings.append(f"ATR value non-positive: {atr_value}")

        if rsi is not None and (rsi < 0 or rsi > 100):
            warnings.append(f"RSI out of range [0,100]: {rsi}")

        if bid is not None and ask is not None and bid > ask:
            warnings.append(f"Bid ({bid}) exceeds ask ({ask}) — possible stale data")

        return warnings

    # ----------------------------------------------------------------
    # Private helpers
    # ----------------------------------------------------------------

    def _summarize_bars(self, bars: list[dict], atr_value: float | None) -> dict:
        """Summarize recent bars: last 3 OHLC, range, direction."""
        if not bars:
            return {
                "count": 0,
                "last_bars": [],
                "recent_range": None,
                "direction": None,
            }

        recent = bars[-3:] if len(bars) >= 3 else bars
        last_bars = []
        for b in recent:
            last_bars.append(
                {
                    "open": b.get("open"),
                    "high": b.get("high"),
                    "low": b.get("low"),
                    "close": b.get("close"),
                    "volume": b.get("volume"),
                    "time": b.get("time"),
                }
            )

        # Recent range (high - low of last N bars)
        highs = [b.get("high", 0) for b in bars[-20:] if b.get("high") is not None]
        lows = [b.get("low", 0) for b in bars[-20:] if b.get("low") is not None]
        recent_range = None
        if highs and lows:
            recent_range = round(max(highs) - min(lows), 6)

        # Direction (last bar)
        direction = None
        if bars:
            last = bars[-1]
            close = last.get("close", 0)
            open_ = last.get("open", 0)
            if close > open_:
                direction = "bullish"
            elif close < open_:
                direction = "bearish"
            else:
                direction = "doji"

        # ATR context
        bar_atr_context = None
        if atr_value and atr_value > 0 and bars:
            last = bars[-1]
            last_range = (last.get("high", 0) or 0) - (last.get("low", 0) or 0)
            if last_range > 0:
                bar_atr_context = round(last_range / atr_value, 2)

        return {
            "count": len(bars),
            "last_bars": last_bars,
            "recent_range_points": recent_range,
            "direction": direction,
            "last_bar_vs_atr": bar_atr_context,
        }

    def _summarize_indicators(
        self,
        *,
        atr_value: float | None,
        atr_percentile: float | None,
        rsi: float | None,
        ema_fast: float | None,
        ema_slow: float | None,
        macd: dict | None,
        bbands: dict | None = None,
        symbol: str,
    ) -> dict:
        """Summarize indicator values with context."""
        indicators: dict[str, Any] = {}

        # ATR
        if atr_value is not None:
            atr_info: dict[str, Any] = {"value": atr_value}
            # Convert to pips for forex
            from mt5_mcp.services.market_context import (
                _price_to_pips,
                _get_symbol_spec,
            )

            pip_size = _get_symbol_spec(symbol)["pip_size"]
            atr_pips = atr_value / pip_size if pip_size > 0 else atr_value
            atr_info["pips"] = round(atr_pips, 2)
            if atr_percentile is not None:
                atr_info["percentile"] = atr_percentile
            indicators["atr"] = atr_info

        # RSI
        if rsi is not None:
            rsi_state = (
                "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
            )
            indicators["rsi"] = {"value": round(rsi, 1), "state": rsi_state}

        # EMAs
        if ema_fast is not None:
            indicators["ema_fast"] = ema_fast
        if ema_slow is not None:
            indicators["ema_slow"] = ema_slow
        if ema_fast is not None and ema_slow is not None:
            indicators["ema_alignment"] = (
                "bullish" if ema_fast > ema_slow else "bearish"
            )

        # MACD
        if macd is not None:
            indicators["macd"] = macd

        # Bollinger Bands
        if bbands is not None:
            indicators["bbands"] = bbands

        return indicators

    def _extract_support_resistance(self, bars: list[dict]) -> dict:
        """Extract approximate support/resistance from recent bar data."""
        if not bars:
            return {"support": [], "resistance": [], "method": "none"}

        # Use recent highs/lows as S/R levels
        highs = []
        lows = []
        for b in bars:
            h = b.get("high")
            l = b.get("low")
            if h is not None:
                highs.append(h)
            if l is not None:
                lows.append(l)

        if not highs or not lows:
            return {"support": [], "resistance": [], "method": "none"}

        # Simple: top 3 resistance = highest highs, bottom 3 support = lowest lows
        sorted_highs = sorted(set(round(h, 4) for h in highs), reverse=True)[:3]
        sorted_lows = sorted(set(round(l, 4) for l in lows))[:3]

        return {
            "resistance": sorted_highs,
            "support": sorted_lows,
            "method": "recent_highs_lows",
        }

    def _summarize_order_book(self, book_data: dict) -> dict:
        """Summarize order book depth."""
        bids = book_data.get("bids", [])
        asks = book_data.get("asks", [])

        bid_depth = len(bids)
        ask_depth = len(asks)

        bid_volume = sum(b.get("volume", 0) for b in bids) if bids else 0
        ask_volume = sum(a.get("volume", 0) for a in asks) if asks else 0

        total_volume = bid_volume + ask_volume

        return {
            "bid_count": bid_depth,
            "ask_count": ask_depth,
            "bid_volume": round(bid_volume, 4),
            "ask_volume": round(ask_volume, 4),
            "bid_ask_volume_ratio": round(bid_volume / total_volume, 2)
            if total_volume > 0
            else None,
            "depth_summary": f"{bid_depth} bid levels, {ask_depth} ask levels",
        }

    def _calendar_context(self, symbol: str) -> dict:
        """Get upcoming high-impact events for this symbol's currencies."""
        # Extract currencies from symbol
        sym_clean = symbol.upper().replace("/", "")
        currencies = set()
        if len(sym_clean) >= 6:
            currencies.add(sym_clean[:3])
            currencies.add(sym_clean[3:6])

        try:
            events = get_upcoming_events(hours_ahead=4, min_impact="HIGH")
            relevant = [
                e.to_dict()
                for e in events
                if not currencies or e.currency in currencies
            ]
            return {
                "is_blackout": any(e.is_in_blackout for e in events),
                "upcoming_events": relevant[:5],
                "event_count_next_4h": len(relevant),
            }
        except Exception:
            return {"error": "unavailable"}

    def _build_coaching(
        self,
        *,
        symbol: str,
        bars_data: list[dict],
        atr_value: float | None,
        atr_percentile: float | None,
        rsi: float | None,
        ema_fast: float | None,
        ema_slow: float | None,
        spread_points: float | None,
        regime: str | None,
        position_in_range: float | None,
        bid: float | None,
        ask: float | None,
    ) -> dict:
        """Build coaching advice for both sides (buy and sell)."""
        current_price = (bid + ask) / 2 if bid and ask else None
        last_bar_range = None
        last_bar_body = None
        last_bar_direction = None

        if bars_data:
            last = bars_data[-1]
            last_bar_range = (last.get("high", 0) or 0) - (last.get("low", 0) or 0)
            last_bar_body = (last.get("close", 0) or 0) - (last.get("open", 0) or 0)
            if last_bar_body > 0:
                last_bar_direction = "bullish"
            elif last_bar_body < 0:
                last_bar_direction = "bearish"
            else:
                last_bar_direction = "doji"

        # Compression ratio
        recent_compression = None
        if atr_value and atr_value > 0 and len(bars_data) >= 3:
            recent_ranges = [
                (b.get("high", 0) or 0) - (b.get("low", 0) or 0) for b in bars_data[-3:]
            ]
            if recent_ranges:
                avg_recent = sum(recent_ranges) / len(recent_ranges)
                recent_compression = avg_recent / atr_value

        # Build coaching for both sides
        buy_advice: CoachingAdvice = self.coach.evaluate(
            symbol=symbol,
            side="buy",
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            current_price=current_price,
            spread_points=spread_points,
            last_bar_range=last_bar_range,
            last_bar_body=last_bar_body,
            last_bar_direction=last_bar_direction,
            recent_bars_compression=recent_compression,
            regime=regime,
            position_in_range=position_in_range,
        )

        sell_advice: CoachingAdvice = self.coach.evaluate(
            symbol=symbol,
            side="sell",
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            current_price=current_price,
            spread_points=spread_points,
            last_bar_range=last_bar_range,
            last_bar_body=last_bar_body,
            last_bar_direction=last_bar_direction,
            recent_bars_compression=recent_compression,
            regime=regime,
            position_in_range=position_in_range,
        )

        return {
            "buy": {
                "recommendation": buy_advice.recommendation,
                "warnings": buy_advice.warnings,
                "insights": buy_advice.insights,
                "raw_metrics": buy_advice.raw_metrics,
            },
            "sell": {
                "recommendation": sell_advice.recommendation,
                "warnings": sell_advice.warnings,
                "insights": sell_advice.insights,
                "raw_metrics": sell_advice.raw_metrics,
            },
        }

    def _viability_warnings(
        self,
        *,
        symbol: str,
        spread_points: float | None,
        spread_ratio_atr: float | None,
        atr_value: float | None,
        atr_percentile: float | None,
        regime: str | None,
        calendar: dict,
        session: dict,
    ) -> list[str]:
        """Generate viability warnings for trading this symbol right now."""
        warnings: list[str] = []

        # Spread too wide (spread > 10% of ATR)
        if spread_ratio_atr is not None and spread_ratio_atr > 0.10:
            warnings.append(
                f"Spread is {spread_ratio_atr * 100:.0f}% of ATR — transaction costs "
                f"may erode edge. Consider waiting for tighter spreads."
            )

        # ATR too low (compressed market)
        if atr_percentile is not None and atr_percentile < 20:
            warnings.append(
                f"ATR at {atr_percentile:.0f}th percentile — very compressed. "
                f"Low volatility may mean no clear direction."
            )

        # Calendar blackout
        if calendar.get("is_blackout"):
            event_count = calendar.get("event_count_next_4h", 0)
            warnings.append(
                f"Economic calendar blackout — {event_count} high-impact event(s) "
                f"in next 4 hours. Spreads may widen, price may gap."
            )

        # Market closed
        if session and not session.get("is_market_open", True):
            warnings.append("Market is closed — no trading possible.")

        # Bad session quality
        if session:
            pair_warnings = session.get("pair_warnings", [])
            if pair_warnings:
                warnings.extend(pair_warnings)

        # Ranging market warning for directional entries
        if regime == "ranging":
            warnings.append(
                "Market is ranging — bracket orders preferred over directional entries."
            )
        elif regime == "compressing":
            warnings.append(
                "Market is compressing — wait for breakout before entering."
            )

        return warnings

    def _current_exposure(self, symbol: str, positions: list[dict]) -> dict:
        """Summarize current positions for this symbol."""
        symbol_positions = [
            p for p in positions if p.get("symbol", "").upper() == symbol
        ]

        total_volume = sum(float(p.get("volume", 0)) for p in symbol_positions)
        buy_volume = sum(
            float(p.get("volume", 0))
            for p in symbol_positions
            if p.get("side", "").lower() == "buy"
        )
        sell_volume = sum(
            float(p.get("volume", 0))
            for p in symbol_positions
            if p.get("side", "").lower() == "sell"
        )
        total_unrealized_pnl = sum(
            float(p.get("unrealized_pnl", 0)) for p in symbol_positions
        )

        return {
            "position_count": len(symbol_positions),
            "total_volume_lots": round(total_volume, 4),
            "buy_volume_lots": round(buy_volume, 4),
            "sell_volume_lots": round(sell_volume, 4),
            "net_exposure_lots": round(buy_volume - sell_volume, 4),
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
            "positions": [
                {
                    "position_id": p.get("position_id"),
                    "side": p.get("side"),
                    "volume": p.get("volume"),
                    "entry_price": p.get("entry_price"),
                    "sl": p.get("sl"),
                    "tp": p.get("tp"),
                    "unrealized_pnl": p.get("unrealized_pnl"),
                    "strategy_id": p.get("strategy_id"),
                }
                for p in symbol_positions
            ],
        }
