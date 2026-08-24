"""Forex News Service — RSS-based news fetcher adapted from IGS architecture.

Mirrors IGS's pool/source configuration but simplified for forex trading.
Uses feedparser for RSS parsing, requests for HTTP, and existing TTLCache.

Pools:
- FOREX_MAJOR: EUR, GBP, USD, JPY, CHF news
- CENTRAL_BANKS: Fed, ECB, BoE, BoJ, RBA, SNB
- MACRO_ECONOMIC: GDP, CPI, employment, PMI
- CRYPTO_FX: BTC/USD, ETH/USD and crypto-fiat
- GEOPOLITICAL_FX: Wars, sanctions, elections affecting currencies

Architecture (adapted from IGS):
- Source configuration as Python dataclasses (not YAML)
- Concurrent fetching with timeouts
- Cache-aware (reuse TTLCache)
- Lightweight enrichment (keyword extraction + sentiment)
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse

import feedparser
import requests

from mt5_mcp.services.cache import TTLCache


# ============================================================
# Data Models
# ============================================================


@dataclass
class NewsSource:
    """A single news source configuration."""

    id: str
    name: str
    url: str
    pools: list[str]
    currencies: list[str] = field(default_factory=list)
    is_active: bool = True
    timeout_seconds: int = 10


@dataclass
class NewsItem:
    """A single news article/item."""

    id: str
    title: str
    link: str
    pub_date: str
    source_name: str
    source_id: str
    pool_id: str
    content_snippet: str = ""
    author: str = ""

    # Enrichment fields (populated by enrich_news)
    sentiment: Optional[dict] = None
    topics: Optional[list[str]] = None
    entities: Optional[list[dict]] = None
    summary: Optional[str] = None
    currency_relevance: Optional[dict] = None  # {currency: relevance_score}

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "link": self.link,
            "pub_date": self.pub_date,
            "source_name": self.source_name,
            "content_snippet": self.content_snippet[:500]
            if self.content_snippet
            else "",
        }
        if self.sentiment:
            d["sentiment"] = self.sentiment
        if self.topics:
            d["topics"] = self.topics
        if self.entities:
            d["entities"] = self.entities
        if self.summary:
            d["summary"] = self.summary
        if self.currency_relevance:
            d["currency_relevance"] = self.currency_relevance
        return d


# ============================================================
# Source Configuration — Forex-specific pools
# ============================================================

NEWS_SOURCES: list[NewsSource] = [
    # ═══════════════════════════════════════════
    # FOREX_MAJOR — EUR, GBP, USD, JPY, CHF
    # ═══════════════════════════════════════════
    NewsSource(
        id="fxstreet",
        name="FXStreet",
        url="https://www.fxstreet.com/rss",
        pools=["FOREX_MAJOR"],
        currencies=["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"],
    ),
    NewsSource(
        id="dailyfx",
        name="DailyFX",
        url="https://www.dailyfx.com/feeds/news/",
        pools=["FOREX_MAJOR"],
        currencies=["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"],
    ),
    NewsSource(
        id="reuters_forex",
        name="Reuters Forex (Google News)",
        url="https://news.google.com/rss/search?q=forex+currency+market&hl=en-US&gl=US&ceid=US:en",
        pools=["FOREX_MAJOR"],
        currencies=["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"],
    ),
    NewsSource(
        id="investing_fx",
        name="Investing.com Forex",
        url="https://www.investing.com/rss/news.rss",
        pools=["FOREX_MAJOR"],
        currencies=["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"],
    ),
    # ═══════════════════════════════════════════
    # CENTRAL_BANKS — Fed, ECB, BoE, BoJ, RBA
    # ═══════════════════════════════════════════
    NewsSource(
        id="fed_news",
        name="Federal Reserve",
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        pools=["CENTRAL_BANKS"],
        currencies=["USD"],
    ),
    NewsSource(
        id="ecb_news",
        name="European Central Bank",
        url="https://www.ecb.europa.eu/press/rss/index.rss",
        pools=["CENTRAL_BANKS"],
        currencies=["EUR"],
    ),
    NewsSource(
        id="boe_news",
        name="Bank of England",
        url="https://www.bankofengland.co.uk/-/media/boe/rss/boe-news.rss",
        pools=["CENTRAL_BANKS"],
        currencies=["GBP"],
    ),
    NewsSource(
        id="rba_news",
        name="Reserve Bank of Australia",
        url="https://news.google.com/rss/search?q=RBA+%22Reserve+Bank+Australia%22+interest+rate&hl=en-US&gl=US&ceid=US:en",
        pools=["CENTRAL_BANKS"],
        currencies=["AUD"],
    ),
    NewsSource(
        id="boj_news",
        name="Bank of Japan",
        url="https://news.google.com/rss/search?q=%22Bank+of+Japan%22+interest+rate&hl=en-US&gl=US&ceid=US:en",
        pools=["CENTRAL_BANKS"],
        currencies=["JPY"],
    ),
    # ═══════════════════════════════════════════
    # MACRO_ECONOMIC — GDP, CPI, PMI
    # ═══════════════════════════════════════════
    NewsSource(
        id="macro_econ",
        name="Macro Economics (Google News)",
        url="https://news.google.com/rss/search?q=GDP+CPI+inflation+employment+economic+data&hl=en-US&gl=US&ceid=US:en",
        pools=["MACRO_ECONOMIC"],
        currencies=["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
    ),
    NewsSource(
        id="bloomberg_econ",
        name="Bloomberg Economics (Google News)",
        url="https://news.google.com/rss/search?q=bloomberg+economics+central+banks&hl=en-US&gl=US&ceid=US:en",
        pools=["MACRO_ECONOMIC"],
        currencies=["USD", "EUR", "GBP", "JPY"],
    ),
    # ═══════════════════════════════════════════
    # CRYPTO_FX — BTC/USD, ETH/USD
    # ═══════════════════════════════════════════
    NewsSource(
        id="coindesk",
        name="CoinDesk",
        url="https://www.coindesk.com/arc/outboundfeeds/rss/",
        pools=["CRYPTO_FX"],
        currencies=["USD"],
    ),
    NewsSource(
        id="cointelegraph",
        name="CoinTelegraph",
        url="https://cointelegraph.com/rss",
        pools=["CRYPTO_FX"],
        currencies=["USD"],
    ),
    NewsSource(
        id="kitco",
        name="Kitco (Gold + Crypto)",
        url="https://www.kitco.com/rss",
        pools=["CRYPTO_FX"],
        currencies=["USD"],
    ),
    # ═══════════════════════════════════════════
    # GEOPOLITICAL_FX — Wars, sanctions, elections
    # ═══════════════════════════════════════════
    NewsSource(
        id="reuters_world",
        name="Reuters World",
        url="https://news.google.com/rss?topic=w&hl=en-US&gl=US&ceid=US:en",
        pools=["GEOPOLITICAL_FX"],
        currencies=["USD", "EUR", "GBP", "JPY", "CHF", "CAD"],
    ),
    NewsSource(
        id="bbc_world",
        name="BBC World News",
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        pools=["GEOPOLITICAL_FX"],
        currencies=["GBP", "EUR", "USD"],
    ),
    NewsSource(
        id="aljazeera",
        name="Al Jazeera",
        url="https://www.aljazeera.com/xml/rss/all.xml",
        pools=["GEOPOLITICAL_FX"],
        currencies=["USD", "EUR"],
    ),
    NewsSource(
        id="ft_world",
        name="Financial Times",
        url="https://www.ft.com/rss/home",
        pools=["GEOPOLITICAL_FX", "MACRO_ECONOMIC"],
        currencies=["USD", "EUR", "GBP"],
    ),
]

# Currency-to-pair mapping for impact analysis
CURRENCY_TO_PAIRS: dict[str, list[str]] = {
    "USD": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCAD",
        "AUDUSD",
        "NZDUSD",
        "USDCHF",
        "XAUUSD",
        "BTCUSD",
    ],
    "EUR": ["EURUSD", "EURJPY", "EURGBP", "EURCHF", "EURCAD", "EURAUD", "EURNZD"],
    "GBP": ["GBPUSD", "GBPJPY", "EURGBP", "GBPAUD", "GBPCAD", "GBPNZD", "GBPCHF"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"],
    "AUD": ["AUDUSD", "AUDJPY", "EURAUD", "GBPAUD", "AUDCAD", "AUDNZD", "AUDCHF"],
    "NZD": ["NZDUSD", "NZDJPY", "EURNZD", "GBPNZD", "NZDCAD", "AUDNZD"],
    "CAD": ["USDCAD", "CADJPY", "EURAUD", "GBPCAD", "AUDCAD", "NZDCAD"],
    "CHF": ["USDCHF", "EURCHF", "GBPCHF", "AUDCHF", "NZDCHF", "CADCHF"],
}


# ============================================================
# News Fetching
# ============================================================

# Cache for news items (10 minute TTL for fetched news)
_news_cache = TTLCache(default_ttl=600.0, max_size=256)


def _fetch_source(source: NewsSource) -> list[NewsItem]:
    """Fetch and parse a single RSS source."""
    try:
        response = requests.get(
            source.url,
            headers={"User-Agent": "MetaTraderBridge-News/1.0"},
            timeout=source.timeout_seconds,
        )
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        items = []

        for entry in feed.entries:
            # Generate unique ID
            entry_id = entry.get("id", entry.get("link", ""))
            if not entry_id:
                entry_id = (
                    f"{source.id}:{entry.get('title', '')}:{entry.get('published', '')}"
                )

            # Parse date
            pub_date = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_date = datetime(*entry.published_parsed[:6]).isoformat()
            elif hasattr(entry, "published"):
                pub_date = entry.published

            # Extract snippet
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = entry.summary
            elif hasattr(entry, "description"):
                snippet = entry.description
            # Strip HTML tags
            import re

            snippet = re.sub(r"<[^>]+>", "", snippet).strip()

            items.append(
                NewsItem(
                    id=_hash_id(entry_id),
                    title=entry.get("title", "Untitled"),
                    link=entry.get("link", ""),
                    pub_date=pub_date,
                    source_name=source.name,
                    source_id=source.id,
                    pool_id=source.pools[0] if source.pools else "UNKNOWN",
                    content_snippet=snippet[:1000],
                    author=entry.get("author", ""),
                )
            )

        return items
    except Exception as e:
        # Log but don't fail — other sources may still work
        return []


def _hash_id(s: str) -> str:
    """Generate a short hash for an ID string."""
    import hashlib

    return hashlib.md5(s.encode()).hexdigest()[:12]


def _filter_by_time(items: list[NewsItem], hours_back: int) -> list[NewsItem]:
    """Filter items to only those within the specified time window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    filtered = []

    for item in items:
        if not item.pub_date:
            continue
        try:
            pub_dt = _parse_date(item.pub_date)
            if pub_dt is not None and pub_dt >= cutoff:
                filtered.append(item)
        except (ValueError, TypeError):
            continue

    return filtered


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse various date formats to datetime."""
    if not date_str:
        return None

    # Try ISO format first
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, AttributeError):
        pass

    # Try common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d %b %Y %H:%M:%S %z",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue

    return None


def _filter_by_keywords(
    items: list[NewsItem],
    keywords: Optional[list[str]] = None,
    exclude_keywords: Optional[list[str]] = None,
    match_all: bool = False,
) -> list[NewsItem]:
    """Filter items by keyword inclusion/exclusion."""
    if not keywords and not exclude_keywords:
        return items

    excluded = [k.lower() for k in (exclude_keywords or [])]
    included = [k.lower() for k in (keywords or [])]

    filtered = []
    for item in items:
        text = f"{item.title} {item.content_snippet}".lower()

        # Check exclusions first
        if excluded and any(e in text for e in excluded):
            continue

        # Check inclusions
        if included:
            if match_all:
                if not all(k in text for k in included):
                    continue
            else:
                if not any(k in text for k in included):
                    continue

        filtered.append(item)

    return filtered


# ============================================================
# News Enrichment
# ============================================================

# Simple sentiment word list (no heavy NLP dependency)
_POSITIVE_WORDS = {
    "rise",
    "rising",
    "rally",
    "gains",
    "gain",
    "surge",
    "surging",
    "bullish",
    "strong",
    "stronger",
    "beat",
    "beats",
    "upgrade",
    "upgraded",
    "growth",
    "expansion",
    "higher",
    "increased",
    "improving",
    "optimistic",
    "positive",
    "recovery",
    "recovering",
    "boost",
    "outperform",
    "outperformed",
}

_NEGATIVE_WORDS = {
    "fall",
    "falling",
    "drop",
    "dropping",
    "decline",
    "declining",
    "bearish",
    "weak",
    "weaker",
    "miss",
    "misses",
    "downgrade",
    "downgraded",
    "contraction",
    "lower",
    "decreased",
    "worsening",
    "pessimistic",
    "negative",
    "recession",
    "crisis",
    "crash",
    "slump",
    "slumping",
    "loss",
    "losses",
    "worse",
    "deteriorating",
    "fear",
    "risk",
    "concern",
    "sanction",
    "war",
    "conflict",
    "tension",
    "tensions",
    "inflation",
    "unemployment",
    "default",
}


def _simple_sentiment(text: str) -> dict:
    """Lightweight sentiment analysis using word lists."""
    words = set(text.lower().split())
    positive = words & _POSITIVE_WORDS
    negative = words & _NEGATIVE_WORDS

    pos_count = len(positive)
    neg_count = len(negative)
    score = pos_count - neg_count

    if score > 2:
        label = "positive"
    elif score < -2:
        label = "negative"
    elif score > 0:
        label = "slightly_positive"
    elif score < 0:
        label = "slightly_negative"
    else:
        label = "neutral"

    return {
        "score": score,
        "label": label,
        "positive_keywords": sorted(positive),
        "negative_keywords": sorted(negative),
    }


def _extract_topics(text: str, max_topics: int = 8) -> list[str]:
    """Extract key topics from text using simple keyword extraction."""
    # Currency/central bank keywords
    forex_keywords = [
        "interest rate",
        "inflation",
        "gdp",
        "employment",
        "nfp",
        "fomc",
        "ecb",
        "boe",
        "boj",
        "rba",
        "fed",
        "rate cut",
        "rate hike",
        "monetary policy",
        "quantitative easing",
        "quantitative tightening",
        "cpi",
        "ppi",
        "pmi",
        "retail sales",
        "trade balance",
        "forex",
        "currency",
        "dollar",
        "euro",
        "yen",
        "pound",
        "gold",
        "bitcoin",
        "crypto",
        "oil",
        "commodity",
        "recession",
        "growth",
        "stimulus",
        "tariff",
        "sanction",
        "election",
        "geopolitical",
        "central bank",
    ]

    text_lower = text.lower()
    found = []
    for kw in forex_keywords:
        if kw in text_lower:
            found.append(kw)

    return found[:max_topics]


def _extract_entities(text: str) -> list[dict]:
    """Extract named entities (currencies, central banks, countries)."""
    entities = []
    text_upper = text.upper()

    # Currency codes
    for currency in [
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "CAD",
        "AUD",
        "NZD",
        "CNY",
        "INR",
    ]:
        if currency in text_upper:
            entities.append({"name": currency, "type": "Currency"})

    # Central banks
    central_banks = [
        ("Federal Reserve", "Central Bank"),
        ("Fed", "Central Bank"),
        ("ECB", "Central Bank"),
        ("European Central Bank", "Central Bank"),
        ("Bank of England", "Central Bank"),
        ("BoE", "Central Bank"),
        ("Bank of Japan", "Central Bank"),
        ("BoJ", "Central Bank"),
        ("RBA", "Central Bank"),
        ("Reserve Bank of Australia", "Central Bank"),
        ("SNB", "Central Bank"),
        ("PBOC", "Central Bank"),
    ]
    for name, etype in central_banks:
        if name.upper() in text_upper or name in text:
            entities.append({"name": name, "type": etype})

    # Deduplicate
    seen = set()
    unique = []
    for e in entities:
        key = f"{e['type']}:{e['name']}"
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


def _currency_relevance(item: NewsItem) -> dict[str, float]:
    """Determine which currencies are most relevant to this news item."""
    text = f"{item.title} {item.content_snippet}".upper()
    scores = {}

    for currency in ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]:
        score = 0.0
        # Direct mention
        if currency in text:
            score += 0.5
        # Currency pair mention
        for pair in CURRENCY_TO_PAIRS.get(currency, []):
            if pair in text:
                score += 0.3
        # Central bank mention
        if currency == "USD" and ("FED" in text or "FOMC" in text or "POWELL" in text):
            score += 0.3
        if currency == "EUR" and ("ECB" in text or "LAGARDE" in text):
            score += 0.3
        if currency == "GBP" and ("BOE" in text or "BANK OF ENGLAND" in text):
            score += 0.3
        if currency == "JPY" and ("BOJ" in text or "BANK OF JAPAN" in text):
            score += 0.3
        if currency == "AUD" and ("RBA" in text or "RESERVE BANK" in text):
            score += 0.3

        if score > 0:
            scores[currency] = round(min(score, 1.0), 2)

    return scores


# ============================================================
# Public API
# ============================================================


def fetch_news(
    pools: Optional[list[str]] = None,
    currencies: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    exclude_keywords: Optional[list[str]] = None,
    limit: int = 20,
    hours_back: int = 6,
    match_all: bool = False,
    source_ids: Optional[list[str]] = None,
) -> dict:
    """Fetch forex-relevant news from RSS feeds.

    Args:
        pools: Filter by pool IDs (FOREX_MAJOR, CENTRAL_BANKS, etc.)
        currencies: Filter by currency codes (USD, EUR, etc.)
        keywords: Include only items with these keywords
        exclude_keywords: Exclude items with these keywords
        limit: Max items to return
        hours_back: How many hours back to fetch
        match_all: If True, ALL keywords must match (AND vs OR)
        source_ids: Specific source IDs to fetch

    Returns:
        dict with items, count, and meta information.
    """
    # Check cache
    import hashlib

    cache_key = hashlib.md5(
        f"{','.join(sorted(pools or []))}|{','.join(sorted(currencies or []))}|{hours_back}|{limit}".encode()
    ).hexdigest()[:16]

    cached = _news_cache.get(cache_key)
    if cached:
        return cached

    # Resolve sources
    sources = [s for s in NEWS_SOURCES if s.is_active]

    if source_ids:
        sources = [s for s in sources if s.id in source_ids]

    if pools:
        sources = [s for s in sources if any(p in s.pools for p in pools)]

    if currencies:
        currencies_upper = [c.upper() for c in currencies]
        sources = [
            s for s in sources if any(c in s.currencies for c in currencies_upper)
        ]

    # Fetch all sources concurrently
    all_items: list[NewsItem] = []
    sources_queried = len(sources)
    sources_succeeded = 0
    sources_failed = 0

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_source = {
            executor.submit(_fetch_source, source): source for source in sources
        }

        for future in as_completed(future_to_source):
            source = future_to_source[future]
            try:
                items = future.result()
                if items:
                    sources_succeeded += 1
                    all_items.extend(items)
                else:
                    sources_succeeded += 1  # Source responded but no items
            except Exception:
                sources_failed += 1

    # Filter by time
    all_items = _filter_by_time(all_items, hours_back)

    # Deduplicate by title similarity
    seen_titles = set()
    unique_items = []
    for item in all_items:
        title_key = item.title.lower().strip()[:80]
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_items.append(item)

    # Filter by keywords
    filtered = _filter_by_keywords(unique_items, keywords, exclude_keywords, match_all)

    # Sort by date (newest first)
    filtered.sort(key=lambda x: x.pub_date or "", reverse=True)

    # Limit
    result_items = filtered[:limit]

    result = {
        "items": [item.to_dict() for item in result_items],
        "count": len(result_items),
        "meta": {
            "sources_queried": sources_queried,
            "sources_succeeded": sources_succeeded,
            "sources_failed": sources_failed,
            "total_sources_available": len(NEWS_SOURCES),
            "pools_requested": pools or [],
            "currencies_requested": currencies or [],
            "keywords_requested": keywords or [],
            "hours_back": hours_back,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Cache result
    _news_cache.set(cache_key, result)

    return result


def enrich_news(
    items: list[dict],
    extract: Optional[list[str]] = None,
) -> dict:
    """Add sentiment, topics, entities, and currency relevance to news items.

    Args:
        items: List of news item dicts from fetch_news
        extract: Which enrichments to add — ["sentiment", "topics", "entities", "currency_relevance", "summary"]

    Returns:
        dict with enriched items.
    """
    if extract is None:
        extract = ["sentiment", "topics", "entities", "currency_relevance"]

    enriched_items = []

    for item_dict in items:
        text = f"{item_dict.get('title', '')} {item_dict.get('content_snippet', '')}"

        enriched = dict(item_dict)

        if "sentiment" in extract:
            enriched["sentiment"] = _simple_sentiment(text)

        if "topics" in extract:
            enriched["topics"] = _extract_topics(text)

        if "entities" in extract:
            enriched["entities"] = _extract_entities(text)

        if "summary" in extract:
            # First 2 sentences as summary
            sentences = text.split(". ")
            enriched["summary"] = (
                ". ".join(sentences[:2]) + "." if len(sentences) > 1 else text[:200]
            )

        if "currency_relevance" in extract:
            # Reconstruct minimal NewsItem for relevance check
            temp_item = NewsItem(
                id=item_dict.get("id", ""),
                title=item_dict.get("title", ""),
                link=item_dict.get("link", ""),
                pub_date=item_dict.get("pub_date", ""),
                source_name=item_dict.get("source_name", ""),
                source_id=item_dict.get("source_id", ""),
                pool_id=item_dict.get("pool_id", ""),
                content_snippet=item_dict.get("content_snippet", ""),
            )
            enriched["currency_relevance"] = _currency_relevance(temp_item)

        enriched_items.append(enriched)

    return {
        "items": enriched_items,
        "count": len(enriched_items),
        "meta": {
            "items_enriched": len(enriched_items),
            "extract_fields": extract,
        },
    }


def get_available_pools() -> list[dict]:
    """Get all available news pools and their source counts."""
    pool_map: dict[str, dict] = {}
    for source in NEWS_SOURCES:
        for pool in source.pools:
            if pool not in pool_map:
                pool_map[pool] = {"id": pool, "source_count": 0, "currencies": set()}
            pool_map[pool]["source_count"] += 1
            pool_map[pool]["currencies"].update(source.currencies)

    result = []
    for pool in pool_map.values():
        pool["currencies"] = sorted(pool["currencies"])
        result.append(pool)

    return result


def get_available_sources() -> list[dict]:
    """Get all available news sources."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "pools": s.pools,
            "currencies": s.currencies,
            "is_active": s.is_active,
        }
        for s in NEWS_SOURCES
    ]
