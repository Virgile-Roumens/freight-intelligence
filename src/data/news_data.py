import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests

from src.config import (
    CACHE_DIR,
    KEYWORDS_FREIGHT,
    KEYWORDS_GEO,
    KEYWORDS_REGULATORY,
    NEWS_CACHE_TTL_HOURS,
    NEWS_FEEDS,
)
from src.utils.cache_manager import CacheManager

logger = logging.getLogger("freightiq.news")

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 FreightIQ/1.0"

_HIGH_KEYWORDS = {"BDI", "capesize", "panamax", "supramax", "handysize", "freight rate",
                   "dry bulk", "FFA", "tonnage", "Baltic Dry"}
_MED_KEYWORDS  = {"iron ore", "coal", "grain", "china", "port congestion", "fleet",
                   "chartering", "scrapping", "orderbook", "newbuilding", "bunker"}
_LOW_KEYWORDS  = {"shipping", "vessel", "cargo", "commodity", "trade", "tanker",
                   "maritime", "charter", "shipowner"}

SIGNAL_PATTERNS = {
    "supply_disruption":   ["strike", "labor action", "port closure", "congestion surge", "blockade"],
    "trade_restriction":   ["sanctions", "embargo", "ban on", "restricted shipping", "seized"],
    "rate_momentum_up":    ["record high", "surging rates", "tight market", "strong demand", "boom"],
    "rate_momentum_down":  ["declining rates", "falling freight", "weak demand", "oversupply", "downturn"],
    "geo_risk":            ["houthi", "attack", "red sea", "black sea conflict", "ukraine grain", "mine"],
    "regulatory":          ["IMO", "CII", "EEXI", "carbon levy", "emissions regulation", "decarbonisation"],
}

SIGNAL_DISPLAY = {
    "supply_disruption":  ("🔴", "red",   "Supply Disruption"),
    "trade_restriction":  ("🔴", "red",   "Trade Restriction"),
    "rate_momentum_up":   ("🟢", "green", "Bullish Rate Signal"),
    "rate_momentum_down": ("🔴", "red",   "Bearish Rate Signal"),
    "geo_risk":           ("🔴", "red",   "Geopolitical Risk"),
    "regulatory":         ("🟡", "amber", "Regulatory Update"),
}


class NewsAggregator:
    """
    Fetches, parses, scores, and caches news from freight/commodity RSS feeds.
    Uses requests + feedparser to bypass User-Agent restrictions.
    """

    def __init__(self, cache_manager: CacheManager | None = None):
        self.cache = cache_manager or CacheManager(CACHE_DIR)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _USER_AGENT

    # ──────────────────────────────────────────────────────────────────────────
    # Fetch
    # ──────────────────────────────────────────────────────────────────────────

    def fetch_all_feeds(self, max_per_feed: int = 20) -> list[dict]:
        """Returns list of article dicts, sorted by relevance score descending."""
        cache_path = self.cache.base_dir / "news" / "articles.json"
        # Check JSON cache
        if cache_path.exists():
            age_h = (time.time() - cache_path.stat().st_mtime) / 3600
            if age_h < NEWS_CACHE_TTL_HOURS:
                try:
                    with open(cache_path, encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        articles = []
        for source, url in NEWS_FEEDS.items():
            try:
                feed = self._parse_feed(url)
                for entry in feed.entries[:max_per_feed]:
                    article = self._normalize_entry(entry, source)
                    article["score"] = self.score_relevance(
                        article["title"] + " " + article["summary"]
                    )
                    articles.append(article)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Feed fetch failed for {source} ({url}): {e}")

        articles.sort(key=lambda x: x["score"], reverse=True)

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(articles, f, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"News cache write failed: {e}")

        return articles

    def _parse_feed(self, url: str) -> feedparser.FeedParserDict:
        """Fetches raw feed text via requests then parses with feedparser."""
        try:
            resp = self._session.get(url, timeout=12)
            resp.raise_for_status()
            return feedparser.parse(resp.text)
        except Exception:
            # Fall back to direct feedparser (handles some edge cases)
            return feedparser.parse(url)

    def _normalize_entry(self, entry, source: str) -> dict:
        published = ""
        if hasattr(entry, "published"):
            published = entry.published
        elif hasattr(entry, "updated"):
            published = entry.updated

        # Try to parse and reformat
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(published)
            published = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        summary = ""
        if hasattr(entry, "summary"):
            summary = entry.summary
        elif hasattr(entry, "description"):
            summary = entry.description

        # Strip HTML tags from summary
        try:
            from bs4 import BeautifulSoup
            summary = BeautifulSoup(summary, "lxml").get_text(separator=" ", strip=True)[:300]
        except Exception:
            summary = summary[:300]

        return {
            "title":     getattr(entry, "title", "No title"),
            "summary":   summary,
            "link":      getattr(entry, "link", "#"),
            "published": published,
            "source":    source,
            "score":     0.0,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────────────────────────

    def score_relevance(self, text: str) -> float:
        """Returns relevance score 0–1 based on keyword frequency."""
        text_lower = text.lower()
        score = 0.0
        for kw in _HIGH_KEYWORDS:
            if kw.lower() in text_lower:
                score += 3.0
        for kw in _MED_KEYWORDS:
            if kw.lower() in text_lower:
                score += 2.0
        for kw in _LOW_KEYWORDS:
            if kw.lower() in text_lower:
                score += 1.0
        # Normalize: max possible ~ 30 (10 high * 3)
        return min(score / 30.0, 1.0)

    # ──────────────────────────────────────────────────────────────────────────
    # Filtering
    # ──────────────────────────────────────────────────────────────────────────

    def filter_by_category(self, articles: list[dict], category: str) -> list[dict]:
        """Filter articles by category using keyword sets."""
        if category == "All":
            return articles
        keyword_map = {
            "Freight":      [kw.lower() for kw in KEYWORDS_FREIGHT],
            "Geopolitical": [kw.lower() for kw in KEYWORDS_GEO],
            "Regulatory":   [kw.lower() for kw in KEYWORDS_REGULATORY],
            "Commodities":  ["iron ore", "coal", "grain", "wheat", "corn", "copper", "oil"],
            "Macro":        ["gdp", "pmi", "inflation", "fed", "interest rate", "usd", "dollar"],
        }
        keywords = keyword_map.get(category, [])
        if not keywords:
            return articles
        result = []
        for a in articles:
            text = (a["title"] + " " + a["summary"]).lower()
            if any(kw in text for kw in keywords):
                result.append(a)
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Signal Detection
    # ──────────────────────────────────────────────────────────────────────────

    def detect_signals(self, articles: list[dict]) -> list[dict]:
        """Returns signal cards from pattern-matching across recent articles."""
        # Count signal type occurrences
        signal_counts = {k: 0 for k in SIGNAL_PATTERNS}
        signal_examples = {k: [] for k in SIGNAL_PATTERNS}

        for article in articles:
            text = (article["title"] + " " + article.get("summary", "")).lower()
            for sig_type, patterns in SIGNAL_PATTERNS.items():
                for pat in patterns:
                    if pat in text:
                        signal_counts[sig_type] += 1
                        if article["title"] not in signal_examples[sig_type]:
                            signal_examples[sig_type].append(article["title"])
                        break

        signals = []
        for sig_type, count in signal_counts.items():
            if count == 0:
                continue
            icon, level, label = SIGNAL_DISPLAY[sig_type]
            example = signal_examples[sig_type][0] if signal_examples[sig_type] else ""
            signals.append({
                "type":    sig_type,
                "label":   label,
                "level":   level,
                "icon":    icon,
                "count":   count,
                "example": example,
                "text":    f'{label}: {count} article{"s" if count > 1 else ""} — "{example[:80]}"',
            })

        signals.sort(key=lambda x: x["count"], reverse=True)
        return signals

    # ──────────────────────────────────────────────────────────────────────────
    # Weekly Briefing
    # ──────────────────────────────────────────────────────────────────────────

    def generate_weekly_briefing(self, articles: list[dict], market_data: dict | None = None) -> str:
        """Generates a formatted markdown weekly briefing."""
        # Top scored articles from last 7 days
        top = sorted(articles, key=lambda x: x["score"], reverse=True)[:8]

        lines = [
            f"# FreightIQ Weekly Intelligence Briefing",
            f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n",
            "---\n",
            "## Top Market News\n",
        ]

        for i, a in enumerate(top[:5], 1):
            lines.append(f"**{i}. {a['title']}**")
            lines.append(f"  *{a['source']} — {a['published']}*")
            if a.get("summary"):
                lines.append(f"  {a['summary'][:200]}...")
            lines.append(f"  [Read more]({a['link']})\n")

        if market_data:
            lines.append("---\n")
            lines.append("## Market Snapshot\n")
            for name, val in list(market_data.items())[:6]:
                v = val.get("value")
                d = val.get("delta_1d")
                if v is None:
                    continue
                sign = "+" if d and d >= 0 else ""
                pct = f" ({sign}{d*100:.1f}%)" if d is not None else ""
                lines.append(f"- **{name}**: {v:.2f}{pct}")

        lines.append("\n---\n*Data sources: Proxy data via yfinance. Baltic Exchange data requires subscription.*")
        return "\n".join(lines)
