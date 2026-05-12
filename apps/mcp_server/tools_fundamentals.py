"""MCP Fundamentals Tools — economic risk, market screening, session intelligence, and news sentiment.

Provides AI agents with fundamental analysis capabilities:
economic calendar risk assessment, multi-symbol market screening,
forex session activity analysis, and news sentiment aggregation.
All tools are read-only (no destructive operations) and return dicts.

Dependencies:
    - mt5_mcp.services.economic_calendar (get_upcoming_events, is_blackout_now, get_blackout_windows)
    - mt5_mcp.services.session_service (get_session_context)
    - mt5_mcp.services.news_service (fetch_news)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from mcp.types import ToolAnnotations

from . import mcp
from .shared import (
    _tcp_send_and_await,
    _parse_payload_dict,
)
from mt5_mcp.adapters.common.symbol_utils import normalize_symbol, denormalize_symbol
from mt5_mcp.services.economic_calendar import (
    get_upcoming_events,
    is_blackout_now,
    get_blackout_windows,
)
from mt5_mcp.services.session_service import (
    get_session_context,
)
from mt5_mcp.services.news_service import fetch_news

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentiment keyword lists for news analysis
# ---------------------------------------------------------------------------
_bullish_words = [
    "surge", "gain", "rally", "growth", "beat", "rise", "up",
    "bullish", "positive", "strong", "recovery", "expansion",
    "outperform", "upgrade",
]
_bearish_words = [
    "slump", "drop", "fall", "decline", "miss", "down",
    "bearish", "negative", "weak", "recession", "slowdown",
    "downgrade", "selloff", "crash",
]

# Category keywords for news article classification
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "economic": [
        "gdp", "cpi", "inflation", "employment", "nfp", "pmi",
        "retail sales", "trade", "unemployment", "consumer",
        "manufacturing", "services", "industrial", "production",
        "housing", "durable goods", "economic", "economy", "growth",
        "data", "output", "spending",
    ],
    "central_banks": [
        "fed", "fomc", "ecb", "boe", "boj", "rba",
        "central bank", "interest rate", "rate decision",
        "monetary policy", "powell", "lagarde", "bank of england",
        "bank of japan", "federal reserve", "rate cut", "rate hike",
        "quantitative easing", "tightening",
    ],
    "earnings": [
        "earnings", "profit", "revenue", "quarterly", "fiscal",
        "eps", "dividend", "buyback",
    ],
    "geopolitical": [
        "war", "sanction", "election", "geopolitical", "conflict",
        "tension", "tariff", "trade war", "military", "diplomatic",
        "sanctions", "political", "policy", "defense", "security",
    ],
    "crypto": [
        "bitcoin", "crypto", "blockchain", "ethereum", "btc", "eth",
        "defi", "cryptocurrency", "digital asset", "token", "altcoin",
        "nft",
    ],
    "commodities": [
        "gold", "oil", "silver", "copper", "crude", "natural gas",
        "commodity", "commodities", "palladium", "platinum",
        "wheat", "corn", "lithium", "uranium",
    ],
}

# Forex session open/close times (approximate, used for display)
_SESSION_TIMES: dict[str, tuple[str, str]] = {
    "sydney": ("21:00", "06:00"),
    "tokyo": ("00:00", "09:00"),
    "london": ("08:00", "17:00"),
    "new_york": ("13:00", "22:00"),
}


# ===================================================================
# Helper: compute article-level sentiment
# ===================================================================


def _compute_article_sentiment(text: str) -> dict:
    """Simple keyword-based sentiment for a single text string.

    Returns dict with 'label' ('bullish'/'bearish'/'neutral'),
    'score' (-1 to 1), and keyword breakdown.
    """
    text_lower = text.lower()
    bullish_count = sum(
        1 for w in _bullish_words if w in text_lower
    )
    bearish_count = sum(
        1 for w in _bearish_words if w in text_lower
    )
    total = bullish_count + bearish_count
    score = (bullish_count - bearish_count) / max(total, 1)

    if score > 0.2:
        label = "bullish"
    elif score < -0.2:
        label = "bearish"
    else:
        label = "neutral"

    return {
        "label": label,
        "score": round(score, 4),
        "bullish_keywords": bullish_count,
        "bearish_keywords": bearish_count,
    }


def _categorize_article(title: str, snippet: str) -> str:
    """Assign a category to an article based on keyword matching."""
    text = f"{title} {snippet}".lower()
    scores: dict[str, int] = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        scores[category] = sum(1 for kw in keywords if kw in text)
    if not scores or max(scores.values()) == 0:
        return "uncategorized"
    return max(scores, key=scores.get)  # type: ignore[arg-type]


def _format_timedelta(minutes: int) -> str:
    """Format minutes to a human-readable string like '3h 15m'."""
    if minutes <= 0:
        return "now"
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
    return f"{mins}m"


# ===================================================================
# Tool 1: mt5_economic_risk_assessment
# ===================================================================


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_economic_risk_assessment() -> dict:
    """Comprehensive economic risk snapshot — upcoming events, blackout periods, forex sessions, and computed risk level.

    Use this tool before making trading decisions to understand the current
    macro risk environment. It aggregates economic calendar events, news
    blackout windows, and forex session activity into a single risk score
    with actionable risk factors.

    Args:
        None

    Returns:
        dict: Contains "overall_risk" ("low"|"moderate"|"high"|"extreme"),
              "risk_score", "high_impact_events", "medium_impact_events",
              "blackout_active", "blackout_windows", "active_sessions",
              "active_overlaps", "risk_factors", "session_context",
              and "timestamp". On error, returns {"error": str}.

    Example:
        mt5_economic_risk_assessment() →
          {"overall_risk": "moderate", "risk_score": 2.5,
           "high_impact_events": [...], "blackout_active": false, ...}

    Note:
        Risk is computed from: number of high-impact events in next 4 hours,
        active news blackout windows, session overlaps, and day-of-week
        factors. All calendar data is rule-based (no live data feed).
    """
    try:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        next_4h = now_utc + timedelta(hours=4)

        # ---- 1. Upcoming economic events (24h window) ----
        events = get_upcoming_events()

        # Separate by impact level
        high_impact_events: list[dict] = []
        medium_impact_events: list[dict] = []
        for e in events:
            ed = e.to_dict()
            if e.impact in ("CRITICAL", "HIGH"):
                high_impact_events.append(ed)
            elif e.impact == "MEDIUM":
                medium_impact_events.append(ed)

        # Count high-impact events within the next 4 hours
        high_impact_next_4h = [
            e for e in events
            if e.impact in ("CRITICAL", "HIGH") and now_utc <= e.event_date <= next_4h
        ]
        high_count_4h = len(high_impact_next_4h)

        # ---- 2. Blackout windows ----
        blackout_info = is_blackout_now()
        blackout_active = blackout_info.get("is_blackout", False)
        blackout_windows = get_blackout_windows()

        # ---- 3. Forex session context ----
        session = get_session_context()
        session_dict = (
            session.to_dict() if hasattr(session, "to_dict") else {}
        )
        active_sessions = session_dict.get("current_sessions", [])
        active_overlaps = session_dict.get("active_overlaps", [])
        overlap_count = len(active_overlaps)
        day_of_week = session_dict.get("day_of_week", "")

        # ---- 4. Compute overall risk ----
        risk_factors: list[str] = []
        risk_score = 0.0

        # Factor: high-impact events in next 4h
        if high_count_4h >= 3:
            risk_score += 3.0
            risk_factors.append(
                f"{high_count_4h} high-impact events within 4 hours (elevated volatility risk)"
            )
        elif high_count_4h >= 1:
            risk_score += 2.0
            risk_factors.append(
                f"{high_count_4h} high-impact event(s) in next 4 hours"
            )

        # Factor: active blackout
        if blackout_active:
            risk_score += 2.0
            risk_factors.append(
                "Active news blackout window — avoid new entries near event times"
            )

        # Factor: session overlap (liquidity + volatility)
        if overlap_count >= 2:
            risk_score += 1.5
            risk_factors.append(
                "Multiple session overlaps — extreme liquidity and volatility"
            )
        elif overlap_count == 1:
            risk_score += 0.5
            risk_factors.append("Session overlap active — increased liquidity")

        # Factor: day-of-week patterns
        if day_of_week == "Friday":
            risk_score += 0.5
            risk_factors.append("Friday — late-week liquidity decline, position squaring")
        elif day_of_week == "Monday":
            risk_score += 0.25
            risk_factors.append("Monday — potential gap risk from weekend close")
        elif day_of_week == "Sunday":
            risk_score += 0.5
            risk_factors.append("Sunday — low liquidity, wide spreads at market open")

        # Determine overall risk level
        if risk_score >= 5.0:
            overall_risk = "extreme"
        elif risk_score >= 3.0:
            overall_risk = "high"
        elif risk_score >= 1.0:
            overall_risk = "moderate"
        else:
            overall_risk = "low"

        return {
            "overall_risk": overall_risk,
            "risk_score": round(risk_score, 2),
            "high_impact_events": high_impact_events,
            "high_impact_next_4h_count": high_count_4h,
            "medium_impact_events": medium_impact_events,
            "total_upcoming_events": len(events),
            "blackout_active": blackout_active,
            "blackout_info": blackout_info,
            "blackout_windows": blackout_windows,
            "active_sessions": active_sessions,
            "active_overlaps": active_overlaps,
            "overlap_count": overlap_count,
            "risk_factors": risk_factors,
            "session_context": session_dict,
            "timestamp": time.time(),
        }
    except Exception as e:
        logger.error(f"mt5_economic_risk_assessment failed: {e}", exc_info=True)
        return {"error": str(e)}


# ===================================================================
# Tool 2: mt5_market_screener
# ===================================================================


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_market_screener(
    symbols: str,
    min_volume: Optional[float] = None,
    min_volatility: Optional[float] = None,
    max_spread: Optional[int] = None,
    min_volume_ma: Optional[float] = None,
    limit: Optional[int] = 20,
) -> dict:
    """Screen multiple symbols by volume, volatility, and spread criteria.

    Use this tool to filter a list of symbols based on activity metrics.
    Fetches 100 H1 bars and symbol info for each symbol, computes average
    volume and ATR (14-bar high-low range), then applies the provided
    filters. Results are scored and ranked by combined volume + volatility
    percentile.

    Args:
        symbols: Comma-separated list of symbols to screen
                 (e.g. "EURUSD,GBPUSD,XAUUSD,BTCUSD").
        min_volume: Minimum average H1 volume (float, optional).
        min_volatility: Minimum ATR value in points (float, optional).
        max_spread: Maximum spread in points (int, optional).
        min_volume_ma: Minimum volume moving average (float, optional).
        limit: Maximum number of results to return (default: 20).

    Returns:
        dict: Contains "total_screened" (count processed), "passed" (count
              after filters), "results" (list of scored symbols), "criteria"
              (applied filters), and "timestamp".
              On error, returns {"error": str}.

    Example:
        mt5_market_screener(
            symbols="EURUSD,GBPUSD,USDJPY,XAUUSD,AUDUSD",
            min_volume=1000.0,
            max_spread=20,
            limit=5
        )

    Note:
        ATR is approximated as the average high-low range over the most
        recent 14 H1 bars. Requires an active TCP bridge connection to MT5.
    """
    try:
        raw_symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        if not raw_symbols:
            return {"error": "No symbols provided", "total_screened": 0, "passed": 0, "results": []}

        criteria = {
            k: v for k, v in {
                "min_volume": min_volume,
                "min_volatility": min_volatility,
                "max_spread": max_spread,
                "min_volume_ma": min_volume_ma,
            }.items() if v is not None
        }

        results: list[dict] = []
        symbols_to_scan = raw_symbols[:50]  # Safety cap

        for sym in symbols_to_scan:
            try:
                sym_norm = normalize_symbol(sym)

                # Fetch H1 bars (last 100)
                bars_result = _tcp_send_and_await(
                    "get_bars",
                    {"symbol": sym_norm, "timeframe": "H1", "count": 100},
                )
                bars_data = (
                    _parse_payload_dict(bars_result).get("data", [])
                    if bars_result else []
                )
                if not bars_data:
                    logger.debug(f"screener: no bars for {sym_norm}")
                    continue

                # Fetch symbol info for spread
                info_result = _tcp_send_and_await(
                    "get_symbol_info", {"symbol": sym_norm},
                )
                info_data = (
                    _parse_payload_dict(info_result) if info_result else {}
                )

                # ---- Compute metrics ----
                # Average volume
                volumes = [
                    float(b.get("volume", 0) or 0) for b in bars_data
                ]
                avg_volume = sum(volumes) / len(volumes) if volumes else 0.0

                # ATR approximation: avg high-low range over last 14 bars
                recent = bars_data[-14:] if len(bars_data) >= 14 else bars_data
                ranges = [
                    float(b["high"]) - float(b["low"])
                    for b in recent
                    if b.get("high") is not None and b.get("low") is not None
                ]
                atr_value = sum(ranges) / len(ranges) if ranges else 0.0

                # Current spread
                raw_spread = info_data.get("spread")
                spread = int(raw_spread) if raw_spread is not None else None

                # ---- Apply filters ----
                if min_volume is not None and avg_volume < min_volume:
                    continue
                if min_volatility is not None and atr_value < min_volatility:
                    continue
                if max_spread is not None and (spread is None or spread > max_spread):
                    continue
                if min_volume_ma is not None and avg_volume < min_volume_ma:
                    continue

                results.append({
                    "symbol": denormalize_symbol(sym_norm),
                    "avg_volume": round(avg_volume, 2),
                    "atr": round(atr_value, 5),
                    "spread": spread,
                    "score": 0.0,
                    "volume_percentile": 0.0,
                    "volatility_percentile": 0.0,
                })
            except Exception as e:
                logger.debug(f"screener: error processing {sym}: {e}")
                continue

        # ---- Compute percentiles and scores ----
        if results:
            max_volume = max(r["avg_volume"] for r in results) or 1.0
            max_atr = max(r["atr"] for r in results) or 0.00001

            for r in results:
                vol_pct = (r["avg_volume"] / max_volume) * 100.0
                vola_pct = (r["atr"] / max_atr) * 100.0
                r["volume_percentile"] = round(vol_pct, 1)
                r["volatility_percentile"] = round(vola_pct, 1)
                r["score"] = round((vol_pct + vola_pct) / 2.0, 1)

            # Sort by score descending
            results.sort(key=lambda x: x["score"], reverse=True)

        # Apply limit
        if limit is not None and limit > 0:
            results = results[:limit]

        return {
            "total_screened": len(symbols_to_scan),
            "passed": len(results),
            "results": results,
            "criteria": criteria,
            "timestamp": time.time(),
        }
    except Exception as e:
        logger.error(f"mt5_market_screener failed: {e}", exc_info=True)
        return {"error": str(e)}


# ===================================================================
# Tool 4: mt5_news_sentiment_composite
# ===================================================================


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_news_sentiment_composite(
    pool: Optional[str] = None,
    hours_back: Optional[int] = 24,
    limit: Optional[int] = 20,
) -> dict:
    """Aggregate news sentiment across an RSS news pool — article-level analysis, category grouping, and overall score.

    Use this tool to gauge market sentiment from financial news headlines
    without requiring API keys or external NLP services. Sentiment is
    computed via keyword matching (bullish/bearish word lists) and articles
    are categorized by economic theme. Results can be filtered by news pool.

    Args:
        pool: News pool to fetch from (optional). Available pools:
              "FOREX_MAJOR", "CENTRAL_BANKS", "MACRO_ECONOMIC",
              "CRYPTO_FX", "GEOPOLITICAL_FX". If omitted, fetches from
              all pools.
        hours_back: How many hours back to scan for articles (default: 24).
        limit: Maximum number of articles to analyze (default: 20).

    Returns:
        dict: Contains "articles_analyzed", "overall_sentiment"
              ("bullish"/"bearish"/"neutral"), "sentiment_score" (-1 to 1),
              "by_category" (per-category breakdown), "top_articles"
              (most significant), "time_window_hours", and "meta".
              On error, returns {"error": str}.

    Example:
        mt5_news_sentiment_composite(pool="CENTRAL_BANKS", hours_back=48) →
          {"articles_analyzed": 15, "overall_sentiment": "neutral",
           "sentiment_score": 0.12, "by_category": {...}, ...}

    Note:
        Sentiment uses simple keyword matching — not NLP-based. Results
        are approximate. RSS feeds have varying reliability; unavailable
        sources are silently skipped. Cached for 10 minutes internally.
    """
    try:
        # ---- 1. Fetch news articles ----
        pool_list = [pool.upper()] if pool else None
        # Fetch extra articles to have enough after enrichment
        fetch_limit = max(50, (limit or 20) * 2)

        news_data = fetch_news(
            pools=pool_list,
            hours_back=hours_back or 24,
            limit=fetch_limit,
        )

        raw_items = news_data.get("items", [])
        if not raw_items:
            return {
                "articles_analyzed": 0,
                "overall_sentiment": "neutral",
                "sentiment_score": 0.0,
                "by_category": {},
                "top_articles": [],
                "time_window_hours": hours_back or 24,
                "meta": news_data.get("meta", {}),
            }

        # ---- 2. Analyze each article ----
        analyzed: list[dict] = []
        for item in raw_items:
            title = item.get("title", "")
            snippet = item.get("content_snippet", "") or item.get("summary", "")
            if not title and not snippet:
                continue

            text = f"{title} {snippet}"
            sentiment = _compute_article_sentiment(text)
            category = _categorize_article(title, snippet)

            analyzed.append({
                "title": title,
                "source": item.get("source_name", "unknown"),
                "pub_date": item.get("pub_date", ""),
                "link": item.get("link", ""),
                "sentiment": sentiment["label"],
                "sentiment_score": sentiment["score"],
                "category": category,
            })

        # ---- 3. Group by category ----
        by_category: dict[str, dict] = {}
        for article in analyzed:
            cat = article["category"]
            if cat not in by_category:
                by_category[cat] = {
                    "count": 0,
                    "sentiment_scores": [],
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                }
            by_category[cat]["count"] += 1
            by_category[cat]["sentiment_scores"].append(article["sentiment_score"])
            if article["sentiment"] == "bullish":
                by_category[cat]["bullish_count"] += 1
            elif article["sentiment"] == "bearish":
                by_category[cat]["bearish_count"] += 1
            else:
                by_category[cat]["neutral_count"] += 1

        # Compute per-category sentiment
        category_summary: dict[str, dict] = {}
        for cat, data in by_category.items():
            scores = data["sentiment_scores"]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            if avg_score > 0.2:
                sentiment_label = "bullish"
            elif avg_score < -0.2:
                sentiment_label = "bearish"
            else:
                sentiment_label = "neutral"
            category_summary[cat] = {
                "count": data["count"],
                "sentiment": sentiment_label,
                "score": round(avg_score, 4),
                "bullish": data["bullish_count"],
                "bearish": data["bearish_count"],
                "neutral": data["neutral_count"],
            }

        # ---- 4. Compute overall sentiment ----
        total_scores = [a["sentiment_score"] for a in analyzed]
        overall_score = (
            sum(total_scores) / len(total_scores) if total_scores else 0.0
        )
        if overall_score > 0.2:
            overall_label = "bullish"
        elif overall_score < -0.2:
            overall_label = "bearish"
        else:
            overall_label = "neutral"

        # ---- 5. Top articles (by sentiment magnitude, highest absolute score) ----
        sorted_articles = sorted(
            analyzed,
            key=lambda a: abs(a["sentiment_score"]),
            reverse=True,
        )
        top_articles = sorted_articles[: (limit or 20)]

        return {
            "articles_analyzed": len(analyzed),
            "overall_sentiment": overall_label,
            "sentiment_score": round(overall_score, 4),
            "by_category": category_summary,
            "top_articles": top_articles,
            "time_window_hours": hours_back or 24,
            "pool_used": pool or "ALL",
            "meta": {
                "sources_queried": news_data.get("meta", {}).get("sources_queried", 0),
                "sources_succeeded": news_data.get("meta", {}).get("sources_succeeded", 0),
                "fetched_at": news_data.get("meta", {}).get("fetched_at", ""),
            },
            "timestamp": time.time(),
        }
    except Exception as e:
        logger.error(f"mt5_news_sentiment_composite failed: {e}", exc_info=True)
        return {"error": str(e)}
