"""
FreightIQ — Daily Dry Bulk Morning Briefing
Sent every morning at 07:30 Europe/Zurich.

Designed for a freight trader at Louis Dreyfus Geneva — focused on the
metrics that drive a morning desk meeting: BDRY/FFA levels, ag commodity
underlyings, AIS supply tightness, chokepoint status, overnight news.

Run modes:
  - Production: GMAIL_USER + GMAIL_APP_PASSWORD set → email is sent
  - Test:       BRIEFING_TEST=1 → writes briefing_preview.html for inspection
"""
import json
import logging
import os
import re
import smtplib
import sys
import warnings
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import requests
import yfinance as yf

from src.config import (
    AIS_API_KEY, AIS_BULK_CARRIER_TYPES, AIS_WS_URL,
    CACHE_DIR, CHOKEPOINTS, SHIPPING_EQUITIES,
)
from src.utils.cache_manager import CacheManager
from src.data.ais_data import (
    fetch_live_vessels, get_chokepoint_traffic, get_route_traffic,
)
from src.data.freight_data import FreightDataManager
from src.data.macro_data import MacroDataManager
from src.data.news_data import NewsAggregator
from src.analytics.regime_detector import RegimeDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("briefing")

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_mdm   = MacroDataManager(_cache)
_nd    = NewsAggregator(_cache)
_rd    = RegimeDetector()


# ── BDI calibration anchors (from freight.py) ────────────────────────────────
_BDI_FACTOR_MID = 118     # Mid-cycle BDI / BDRY multiplier

# Persisted BDI history file — committed to repo so it accumulates across runs
_BDI_HISTORY_PATH = Path(__file__).parent.parent / "data" / "bdi_history.json"


# ─────────────────────────────────────────────────────────────────────────────
# REAL BDI — scrape from tradingeconomics.com + persist history
# ─────────────────────────────────────────────────────────────────────────────

def scrape_bdi() -> dict | None:
    """
    Scrape current Baltic Dry Index value from tradingeconomics.com.
    Returns {"value": float, "source": str, "url": str} or None on failure.
    """
    url = "https://tradingeconomics.com/commodity/baltic"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            log.warning(f"TE scrape: HTTP {r.status_code}")
            return None
        # The TEChartsMeta JSON block contains "last": <bdi_value>
        m = re.search(r'"last":\s*([\d.]+)', r.text)
        if not m:
            log.warning("TE scrape: BDI value pattern not found")
            return None
        val = float(m.group(1))
        if not (100 < val < 15000):
            log.warning(f"TE scrape: BDI value {val} outside sanity range")
            return None
        return {"value": val, "source": "tradingeconomics.com", "url": url}
    except Exception as e:
        log.warning(f"BDI scrape failed: {e}")
        return None


def load_bdi_history() -> list[dict]:
    if not _BDI_HISTORY_PATH.exists():
        return []
    try:
        with _BDI_HISTORY_PATH.open("r", encoding="utf-8") as f:
            return json.load(f).get("history", [])
    except Exception as e:
        log.warning(f"BDI history load failed: {e}")
        return []


def append_bdi_today(value: float) -> list[dict]:
    """Upsert today's BDI value into the persisted history file."""
    history = load_bdi_history()
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if history and history[-1].get("date") == today:
        history[-1]["value"] = value
    else:
        history.append({"date": today, "value": value})
    # Keep last 730 days (~2y)
    history = history[-730:]
    _BDI_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _BDI_HISTORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            {"history": history,
             "updated_at": datetime.now(timezone.utc).isoformat()},
            f, indent=2,
        )
    return history


def bdi_changes(history: list[dict]) -> dict:
    """Compute 1D / 5D / 30D / 1Y changes from BDI history."""
    if not history:
        return {}
    cur = history[-1]["value"]
    out = {"current": cur, "last_date": history[-1]["date"]}
    for label, lookback in [("d1d", 1), ("d5d", 5), ("d30d", 30), ("d365d", 365)]:
        if len(history) > lookback:
            prev = history[-(lookback + 1)]["value"]
            if prev:
                out[label] = (cur / prev - 1) * 100
    if len(history) >= 2:
        vals = [h["value"] for h in history]
        out["52w_high"] = max(vals[-365:]) if len(vals) >= 365 else max(vals)
        out["52w_low"]  = min(vals[-365:]) if len(vals) >= 365 else min(vals)
        out["pctile"]   = (sum(1 for v in vals if v <= cur) / len(vals)) * 100
    return out


# ─────────────────────────────────────────────────────────────────────────────
# BUNKER PRICES — scrape VLSFO by port from shipandbunker.com
# ─────────────────────────────────────────────────────────────────────────────

def scrape_bunker_prices() -> dict:
    """
    Scrape VLSFO (0.5% sulfur, IMO-2020 compliant) bunker prices for major
    ports from shipandbunker.com. Returns {port: usd_per_mt}.
    """
    url = "https://shipandbunker.com/prices"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0"),
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return {}
        # The page lists each port name followed by its VLSFO price (first numeric value)
        out = {}
        for port in ("Singapore", "Rotterdam", "Houston", "Fujairah", "Hong Kong"):
            pattern = rf'class="[^"]*">({port})<[^>]+>[^<]*<[^>]+>(\d+\.?\d*)'
            m = re.search(pattern, r.text)
            if m:
                price = float(m.group(2))
                if 100 < price < 3000:
                    out[port] = price
        return out
    except Exception as e:
        log.warning(f"Bunker scrape failed: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# ECONOMIC CALENDAR — hardcoded recurring releases relevant to dry bulk
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (kind, day_pattern, weekday, time_cet, name, importance, why_it_matters, url)
#   kind: "monthly" | "weekly" | "biweekly"
#   day_pattern: int (day of month) | "first_friday" | None
#   weekday: 0=Mon..6=Sun | None
_CALENDAR = [
    # Weekly
    ("weekly", None, 2, "16:30 CET",
     "EIA Crude Oil Stocks",        "high",
     "Bunker proxy via Brent — moves voyage TCEs",
     "https://www.eia.gov/petroleum/supply/weekly/"),
    ("weekly", None, 3, "16:00 CET",
     "EIA Natural Gas Storage",     "medium",
     "Gas-coal substitution dynamics affect thermal coal demand",
     "https://ir.eia.gov/ngs/ngs.html"),
    ("weekly", None, 4, "21:30 CET",
     "CFTC Commitment of Traders",  "medium",
     "Speculative positioning in grains & energy",
     "https://www.cftc.gov/MarketReports/CommitmentsofTraders/"),
    # Monthly — China bloc
    ("monthly", 7, None, "03:00 CET",
     "China Trade Balance & Exports/Imports",
     "high",
     "Iron ore + grain import pace — direct dry bulk demand",
     "https://www.tradingeconomics.com/china/balance-of-trade"),
    ("monthly", 10, None, "03:30 CET",
     "China CPI / PPI",             "high",
     "Demand health signal · industrial pricing power",
     "https://www.tradingeconomics.com/china/inflation-cpi"),
    ("monthly", 15, None, "03:00 CET",
     "China Industrial Production / Retail",
     "high",
     "Capesize bellwether — steel mill output ties to iron ore",
     "https://www.tradingeconomics.com/china/industrial-production"),
    # Monthly — USDA & ag
    ("monthly", 12, None, "18:00 CET",
     "USDA WASDE Report",           "high",
     "Global crop S&D — drives grain trade flows & Panamax/Supramax routes",
     "https://www.usda.gov/oce/commodity/wasde"),
    ("monthly", 25, None, "14:30 CET",
     "USDA Cattle on Feed",         "low",
     "Feed grain demand signal (corn → US Gulf exports)",
     "https://usda.library.cornell.edu/concern/publications/m326m174z"),
    # Monthly — US macro
    ("monthly", "first_friday", None, "14:30 CET",
     "US Nonfarm Payrolls",         "high",
     "Risk sentiment & USD direction · headwind/tailwind for commodities",
     "https://www.bls.gov/news.release/empsit.toc.htm"),
    ("monthly", 13, None, "14:30 CET",
     "US CPI Inflation",            "high",
     "Fed path · USD index · commodity flows",
     "https://www.bls.gov/cpi/"),
    ("monthly", 14, None, "14:30 CET",
     "US Retail Sales",             "medium",
     "Consumer demand pulse for finished goods imports",
     "https://www.census.gov/retail/index.html"),
    ("monthly", 1, None, "03:30 CET",
     "China Caixin Manufacturing PMI",
     "high",
     "Leading indicator for steel mills & freight demand",
     "https://www.tradingeconomics.com/china/manufacturing-pmi"),
    # Quarterly-ish
    ("monthly", 28, None, "01:50 CET",
     "Japan Industrial Production", "medium",
     "Capesize iron ore import demand (Pilbara → Japan)",
     "https://www.tradingeconomics.com/japan/industrial-production"),
]


def economic_calendar_today(dt: datetime) -> list[dict]:
    """Return calendar items scheduled for today."""
    weekday = dt.weekday()  # 0=Mon..6=Sun
    day     = dt.day
    is_first_friday = (weekday == 4 and 1 <= day <= 7)

    out = []
    for entry in _CALENDAR:
        kind, day_pat, wd, time_str, name, importance, note, url = entry
        match = False
        if kind == "weekly" and wd == weekday:
            match = True
        elif kind == "monthly":
            if day_pat == "first_friday" and is_first_friday:
                match = True
            elif isinstance(day_pat, int) and day_pat == day:
                match = True
        if match:
            out.append({
                "name": name, "time": time_str, "importance": importance,
                "note": note, "url": url,
            })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LLM NARRATIVE — optional Claude API integration (graceful no-op without key)
# ─────────────────────────────────────────────────────────────────────────────

def generate_llm_narrative(data: dict) -> str | None:
    """
    If ANTHROPIC_API_KEY is set, generate a polished executive summary
    paragraph via Claude. Returns None if no key or on error (caller falls
    back to rule-based narrative).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        log.info("anthropic SDK not installed — LLM narrative disabled")
        return None

    bdi      = data.get("bdi") or {}
    bdi_chg  = data.get("bdi_change") or {}
    macro    = data.get("macro", {})
    snap     = data.get("snapshot", {})
    regime   = data.get("regime", {})
    sigs     = data.get("signals", [])
    spot     = data.get("bdry_spot")
    fwd      = data.get("bdry_fwd", [])
    bunker   = data.get("bunker", {})

    facts = {
        "bdi_now":         bdi.get("value"),
        "bdi_1d_pct":      bdi_chg.get("d1d"),
        "bdi_5d_pct":      bdi_chg.get("d5d"),
        "bdi_30d_pct":     bdi_chg.get("d30d"),
        "bdi_percentile":  bdi_chg.get("pctile"),
        "bdry_spot":       spot,
        "bdry_1d_pct":     (snap.get("BDRY", {}).get("delta_1d") or 0) * 100,
        "bdry_fwd_jun":    fwd[0]["fwd"] if fwd else None,
        "regime":          regime.get("label"),
        "brent_usd_bbl":   (macro.get("Brent") or {}).get("value"),
        "brent_1d_pct":    (macro.get("Brent") or {}).get("d1d"),
        "dxy":             (macro.get("DXY") or {}).get("value"),
        "dxy_1d_pct":      (macro.get("DXY") or {}).get("d1d"),
        "vale_1d_pct":     (macro.get("VALE") or {}).get("d1d"),
        "corn_1d_pct":     (macro.get("Corn") or {}).get("d1d"),
        "wheat_1d_pct":    (macro.get("Wheat") or {}).get("d1d"),
        "soy_1d_pct":      (macro.get("Soybean") or {}).get("d1d"),
        "bunker_singapore": bunker.get("Singapore"),
        "bunker_rotterdam": bunker.get("Rotterdam"),
        "top_signal":      sigs[0].get("text")[:200] if sigs else None,
    }

    prompt = f"""You are a senior dry bulk freight analyst at a global agricultural commodity merchant in Geneva.
Write a 3-4 sentence overnight market commentary for the morning desk meeting. Focus on what changed and the actionable read for the freight book (Capesize/Panamax/Supramax).

Style: institutional, concise, no hedging, no marketing fluff. Reference real numbers. Mention LDC-relevant context (grain trade, ag flows, Brazil/US Gulf/Black Sea) where relevant. End with one specific item to watch today.

Today's facts (JSON):
{json.dumps(facts, indent=2)}

Output ONLY the paragraph in HTML (use <b> for emphasis, no other tags). No preamble."""

    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning(f"LLM narrative failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SVG SPARKLINE
# ─────────────────────────────────────────────────────────────────────────────

def render_bdi_sparkline(history: list[dict],
                         width: int = 220, height: int = 44,
                         color: str = "#5b8cbf") -> str:
    """Inline SVG sparkline of BDI history. Last 90 days; falls back gracefully."""
    if len(history) < 2:
        return ""
    series = history[-90:]
    values = [h["value"] for h in series]
    v_min, v_max = min(values), max(values)
    rng = v_max - v_min if v_max > v_min else 1.0
    n   = len(values)
    pts = " ".join(
        f"{i * (width - 8) / max(n - 1, 1) + 4:.1f},"
        f"{(1 - (v - v_min) / rng) * (height - 8) + 4:.1f}"
        for i, v in enumerate(values)
    )
    last_x = (n - 1) * (width - 8) / max(n - 1, 1) + 4
    last_y = (1 - (values[-1] - v_min) / rng) * (height - 8) + 4
    last_col = "#16803c" if (len(values) > 1 and values[-1] >= values[-2]) else "#c92a2a"
    # Build area path under line
    area_pts = pts + f" {last_x:.1f},{height - 4:.1f} 4,{height - 4:.1f}"
    return f"""
    <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="display:block;">
      <polygon points="{area_pts}" fill="{color}" fill-opacity="0.10"/>
      <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{last_col}"/>
    </svg>
    """


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHERS — each is wrapped to never crash the briefing
# ─────────────────────────────────────────────────────────────────────────────

def _safe(fn, default, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.warning(f"{fn.__name__} failed: {e}")
        return default


def _fetch_macro() -> dict:
    """Cross-asset macro snapshot via yfinance."""
    out = {}
    tickers = [
        ("Brent",       "BZ=F"),
        ("WTI",         "CL=F"),
        ("US10Y",       "^TNX"),
        ("DXY",         "DX-Y.NYB"),
        ("VALE",        "VALE"),
        ("BHP",         "BHP"),
        ("BTU",         "BTU"),
        ("Corn",        "ZC=F"),
        ("Wheat",       "ZW=F"),
        ("Soybean",     "ZS=F"),
        ("SP500",       "^GSPC"),
        ("Hang_Seng",   "^HSI"),
    ]
    for label, ticker in tickers:
        try:
            df = yf.download(ticker, period="10d", auto_adjust=True, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            close = df["Close"].dropna()
            if close.empty:
                continue
            cur  = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) > 1 else cur
            d1   = (cur / prev - 1) * 100 if prev else 0
            d5   = (cur / float(close.iloc[-6]) - 1) * 100 if len(close) > 5 else None
            out[label] = {"value": cur, "d1d": d1, "d5d": d5, "ticker": ticker}
        except Exception:
            pass
    return out


def _fetch_bdry_forward() -> tuple[float | None, list]:
    """BDRY spot + put-call parity forward curve."""
    try:
        spot_df = yf.download("BDRY", period="5d", auto_adjust=True, progress=False)
        if isinstance(spot_df.columns, pd.MultiIndex):
            spot_df.columns = spot_df.columns.droplevel(1)
        spot = float(spot_df["Close"].dropna().iloc[-1])

        tkr  = yf.Ticker("BDRY")
        fwd  = []
        for exp in tkr.options:
            try:
                chain = tkr.option_chain(exp)
                calls, puts = chain.calls.copy(), chain.puts.copy()
                if calls.empty or puts.empty:
                    continue
                calls["dist"] = abs(calls["strike"] - spot)
                k = float(calls.nsmallest(1, "dist")["strike"].iloc[0])
                c_row = calls[calls["strike"] == k]
                p_row = puts[puts["strike"] == k]
                if c_row.empty or p_row.empty:
                    continue
                c_px = float(c_row.iloc[0]["lastPrice"])
                p_px = float(p_row.iloc[0]["lastPrice"])
                if c_px <= 0 or p_px <= 0:
                    continue
                fwd.append({
                    "expiry": exp,
                    "fwd":    k + c_px - p_px,
                    "k":      k,
                })
            except Exception:
                continue
        return spot, fwd
    except Exception:
        return None, []


def _fetch_ais() -> pd.DataFrame:
    """AIS snapshot — short collection window for fast email generation."""
    if not AIS_API_KEY:
        return pd.DataFrame()
    try:
        return fetch_live_vessels(
            api_key=AIS_API_KEY,
            ws_url=AIS_WS_URL,
            cache_dir=CACHE_DIR,
            collect_seconds=15,        # 15s window keeps run-time low
            cache_ttl_minutes=180,     # 3h cache window — fine for a daily briefing
            bulk_types=AIS_BULK_CARRIER_TYPES,
        )
    except Exception as e:
        log.warning(f"AIS fetch failed: {e}")
        return pd.DataFrame()


def fetch_all() -> dict:
    log.info("Fetching market data…")
    snap        = _safe(_fdm.get_freight_snapshot, {})
    composite   = _safe(_fdm.get_weighted_shipping_index, pd.Series(dtype=float), period="3mo")
    bdry_spot, bdry_fwd = _fetch_bdry_forward()
    macro       = _fetch_macro()
    articles    = _safe(_nd.fetch_all_feeds, [], max_per_feed=10)
    signals     = _safe(_nd.detect_signals, [], articles) if articles else []
    ais_df      = _fetch_ais()
    regime      = _rd.detect_phase(composite) if not composite.empty else {}

    # ── Real BDI: scrape + persist + compute changes ────────────────────────
    bdi_scrape = scrape_bdi()
    if bdi_scrape:
        log.info(f"Real BDI scraped: {bdi_scrape['value']:.1f}")
        bdi_history = append_bdi_today(bdi_scrape["value"])
    else:
        log.warning("BDI scrape failed — falling back to last cached history value")
        bdi_history = load_bdi_history()
        if bdi_history:
            bdi_scrape = {
                "value":  bdi_history[-1]["value"],
                "source": "cached",
                "url":    "https://tradingeconomics.com/commodity/baltic",
            }
    bdi_change = bdi_changes(bdi_history) if bdi_history else {}

    # ── YoY context via BDRY 1-year-old close (synthetic BDI proxy) ─────────
    yoy_bdi_proxy = None
    try:
        bdry_1y = yf.download("BDRY", period="2y", auto_adjust=True, progress=False)
        if isinstance(bdry_1y.columns, pd.MultiIndex):
            bdry_1y.columns = bdry_1y.columns.droplevel(1)
        s = bdry_1y["Close"].dropna()
        if len(s) > 252:
            yoy_bdi_proxy = float(s.iloc[-252]) * _BDI_FACTOR_MID  # ~1 trading year ago
    except Exception:
        pass

    # ── Bunker prices by port (live scrape) ──────────────────────────────────
    bunker = scrape_bunker_prices()
    if bunker:
        log.info(f"Bunker prices scraped: {len(bunker)} ports")

    # ── Economic calendar ────────────────────────────────────────────────────
    calendar = economic_calendar_today(datetime.now(ZoneInfo("Europe/Zurich")))

    return {
        "snapshot":      snap,
        "composite":     composite,
        "bdry_spot":     bdry_spot,
        "bdry_fwd":      bdry_fwd,
        "macro":         macro,
        "articles":      articles or [],
        "signals":       signals or [],
        "ais":           ais_df,
        "regime":        regime,
        "bdi":           bdi_scrape,
        "bdi_history":   bdi_history,
        "bdi_change":    bdi_change,
        "yoy_bdi_proxy": yoy_bdi_proxy,
        "bunker":        bunker,
        "calendar":      calendar,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML BUILDERS — modular sections
# ─────────────────────────────────────────────────────────────────────────────

def _arrow(pct: float | None, suffix: str = "%") -> str:
    if pct is None:
        return '<span style="color:#9ba3b4;">—</span>'
    color = "#16803c" if pct >= 0 else "#c92a2a"
    glyph = "▲" if pct >= 0 else "▼"
    return (f'<span style="color:{color}; font-weight:600; '
            f'font-family:\'IBM Plex Mono\',monospace;">{glyph} {pct:+.2f}{suffix}</span>')


def _section_header(label: str, emoji: str = "") -> str:
    e = f"{emoji} " if emoji else ""
    return (f'<div style="font-size:11px; letter-spacing:0.1em; color:#5b8cbf; '
            f'text-transform:uppercase; font-weight:600; margin-bottom:10px;">{e}{label}</div>')


def _sentiment_chip(level: str) -> str:
    """Returns a small inline sentiment chip: 🟢 BULL / 🟡 NEUTRAL / 🔴 BEAR."""
    if   level == "bull":    txt, col, bg = "● BULL",    "#16803c", "#d4f4dd"
    elif level == "bear":    txt, col, bg = "● BEAR",    "#c92a2a", "#fde0e0"
    elif level == "tight":   txt, col, bg = "● TIGHT",   "#c92a2a", "#fde0e0"
    elif level == "slack":   txt, col, bg = "● SLACK",   "#16803c", "#d4f4dd"
    elif level == "neutral": txt, col, bg = "● NEUTRAL", "#a07c00", "#fff5d6"
    else:                    return ""
    return (
        f'<span style="display:inline-block; font-size:9px; font-weight:700; '
        f'letter-spacing:0.08em; color:{col}; background:{bg}; padding:2px 7px; '
        f'border-radius:10px; margin-left:8px; vertical-align:middle; '
        f'font-family:\'IBM Plex Mono\',monospace;">{txt}</span>'
    )


def _section_header_chip(label: str, emoji: str = "", chip: str = "") -> str:
    """Section header with optional sentiment chip on the right."""
    e = f"{emoji} " if emoji else ""
    chip_html = _sentiment_chip(chip) if chip else ""
    return (
        f'<div style="font-size:11px; letter-spacing:0.1em; color:#5b8cbf; '
        f'text-transform:uppercase; font-weight:600; margin-bottom:10px;">'
        f'{e}{label}{chip_html}</div>'
    )


def build_subsegment_section(data: dict) -> str:
    """
    Sub-segment performance using shipping-equity proxies (BCI/BPI/BSI are
    paywalled — equities track them closely enough to be directionally useful).
    """
    macro = data["macro"]
    snap  = data["snapshot"]

    # Map proxies to vessel segment
    segments = [
        ("Capesize",   ["BHP", "VALE"],         "Iron ore charterer mega-fleet · Pilbara/Brazil routes"),
        ("Panamax",    ["NMM"],                 "Diversified MLP · grain/coal flexible book"),
        ("Supramax",   ["EGLE"],                "Pure-play Supramax · minor bulk & grain"),
        ("Diversified",["SBLK"],                "Star Bulk — largest US-listed dry bulk operator"),
    ]

    rows = []
    for seg_name, tickers, note in segments:
        # Average daily move across the proxy tickers
        d1s, d5s, vals = [], [], []
        for tk in tickers:
            m = macro.get(tk) or {}
            v = m.get("value")
            if v is not None:
                vals.append(v)
                if m.get("d1d") is not None: d1s.append(m["d1d"])
                if m.get("d5d") is not None: d5s.append(m["d5d"])
            # Also try snapshot for additional tickers
            s = snap.get(tk) or {}
            sv = s.get("value")
            if sv is not None and v is None:
                vals.append(sv)
                if s.get("delta_1d") is not None: d1s.append(s["delta_1d"] * 100)
                if s.get("delta_5d") is not None: d5s.append(s["delta_5d"] * 100)

        if not vals:
            continue
        avg_d1 = sum(d1s) / len(d1s) if d1s else None
        avg_d5 = sum(d5s) / len(d5s) if d5s else None

        # Sentiment based on combined 1D + 5D move
        score = (avg_d1 or 0) + (avg_d5 or 0) * 0.3
        if   score >  1.5: chip = "bull"
        elif score < -1.5: chip = "bear"
        else:              chip = "neutral"

        chip_html = _sentiment_chip(chip)
        tk_str = " · ".join(tickers)
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px; border-bottom:1px solid #f0f2f5;">
            <div style="font-weight:600; font-size:13px;">{seg_name}{chip_html}</div>
            <div style="font-size:10px; color:#9ba3b4; font-family:'IBM Plex Mono',monospace;">{tk_str}</div>
          </td>
          <td align="right" style="padding:10px 12px; border-bottom:1px solid #f0f2f5;">{_arrow(avg_d1)}</td>
          <td align="right" style="padding:10px 12px; border-bottom:1px solid #f0f2f5;">{_arrow(avg_d5)}</td>
          <td style="padding:10px 12px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">{note}</td>
        </tr>
        """)

    if not rows:
        return ""

    return f"""
    {_section_header_chip("Sub-Segment Performance · Equity Proxies", "📐")}
    <p style="font-size:11px; color:#5a6275; margin:0 0 10px 0;">
      Real Baltic sub-indices (BCI · BPI · BSI · BHSI) require Baltic Exchange subscription. Below: equity proxies
      that historically track each sub-segment, used as a directional proxy. Sentiment chip combines 1D + 5D moves.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:6px;">
      <thead>
        <tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
          <th align="left"  style="padding:8px 12px;">Segment / Proxies</th>
          <th align="right" style="padding:8px 12px;">1D</th>
          <th align="right" style="padding:8px 12px;">5D</th>
          <th align="left"  style="padding:8px 12px;">Context</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_ag_ports_section(data: dict) -> str:
    """
    Promote ag-relevant port congestion (LDC trades grains heavily).
    Highlights Santos, Paranaguá, NOLA, Constanța, Rosario, Tubarão.
    """
    ais_df = data.get("ais")
    if ais_df is None or ais_df.empty:
        return ""

    try:
        from src.data.ais_data import get_port_zone_counts
        port_df = get_port_zone_counts(ais_df)
    except Exception:
        return ""

    if port_df.empty:
        return ""

    AG_PORTS = [
        ("Santos (BR ag)",        "🇧🇷", "Brazil soybean/corn — Panamax"),
        ("Paranaguá (BR ag)",     "🇧🇷", "Brazil soybean/corn — Panamax"),
        ("Tubarao / Vitoria",     "🇧🇷", "Iron ore — Capesize export hub"),
        ("NOLA / US Gulf",        "🇺🇸", "US corn/soy/wheat — Panamax"),
        ("Constanța (Black Sea)", "🇷🇴", "Black Sea grain — Panamax/Supramax"),
        ("Rosario / Up-River (AR)","🇦🇷","Argentine grain/soymeal — Supramax/Panamax"),
        ("Qingdao / N.China",     "🇨🇳", "China iron ore discharge — Capesize"),
        ("Port Hedland",          "🇦🇺", "Iron ore — Capesize export hub"),
    ]

    rows = []
    for port_name, flag, note in AG_PORTS:
        match = port_df[port_df["Port Zone"] == port_name]
        if match.empty:
            continue
        r = match.iloc[0]
        live     = int(r.get("Live (AIS)", 0))
        anchored = int(r.get("Anchor/Moored", 0))
        wait     = str(r.get("Est. Wait (days)", "—"))
        status   = str(r.get("Est. Status", "—"))

        if   status == "Heavy":    chip = "tight"
        elif status == "Light":    chip = "slack"
        else:                      chip = "neutral"

        rows.append(f"""
        <tr>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5;">
            <span style="font-size:14px; margin-right:4px;">{flag}</span>
            <span style="font-weight:600; font-size:12px;">{port_name}</span>
          </td>
          <td align="right" style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600; color:#0066cc;">{live}</td>
          <td align="right" style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; color:#d29922;">{anchored}</td>
          <td align="right" style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-size:11px;">{wait} d</td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5;">{_sentiment_chip(chip)}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-size:10px; color:#5a6275;">{note}</td>
        </tr>
        """)

    if not rows:
        return ""

    return f"""
    {_section_header_chip("Ag & Iron-Ore Port Queue Tracker", "🌾")}
    <p style="font-size:11px; color:#5a6275; margin:0 0 10px 0;">
      Live AIS-detected vessel counts at LDC-relevant export/import zones. Queues at Brazilian / US Gulf / Black Sea ports
      tighten effective Panamax supply for grain trades. Iron-ore ports tie directly to Capesize demand.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:6px;">
      <thead>
        <tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
          <th align="left"  style="padding:8px 12px;">Port</th>
          <th align="right" style="padding:8px 12px;">Live</th>
          <th align="right" style="padding:8px 12px;">Wait</th>
          <th align="right" style="padding:8px 12px;">Est. Days</th>
          <th align="left"  style="padding:8px 12px;">Status</th>
          <th align="left"  style="padding:8px 12px;">Context</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_bunker_section(data: dict) -> str:
    """VLSFO bunker prices by port (scraped from shipandbunker.com)."""
    bunker = data.get("bunker") or {}
    brent  = (data.get("macro") or {}).get("Brent") or {}
    if not bunker:
        return ""

    rows = []
    for port in ["Singapore", "Rotterdam", "Houston", "Fujairah", "Hong Kong"]:
        price = bunker.get(port)
        if price is None:
            continue
        # Implied spread vs Brent (illustrative)
        implied = brent.get("value", 0) * 6.4 if brent.get("value") else None
        spread = (price - implied) if implied else None
        spread_str = ""
        if spread is not None:
            spread_col = "#c92a2a" if spread > 30 else ("#16803c" if spread < -30 else "#5a6275")
            spread_str = (f'<span style="color:{spread_col}; '
                          f'font-family:\'IBM Plex Mono\',monospace; font-size:11px;">'
                          f'{spread:+.0f} vs Brent×6.4</span>')

        # Trader note per port
        note = {
            "Singapore": "Asia bunker hub · key for Pilbara/Brazil → China voyages",
            "Rotterdam": "NW Europe bunker · ARA range · Black Sea grain return legs",
            "Houston":   "US Gulf bunker · NOLA grain export voyages",
            "Fujairah":  "Middle East bunker · Hormuz transit & Indian Ocean routes",
            "Hong Kong": "South China bunker hub",
        }.get(port, "")

        rows.append(f"""
        <tr>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-weight:600; font-size:12px;">{port}</td>
          <td align="right" style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600;">${price:.1f}/mt</td>
          <td align="right" style="padding:8px 12px; border-bottom:1px solid #f0f2f5;">{spread_str}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">{note}</td>
        </tr>
        """)

    if not rows:
        return ""

    return f"""
    {_section_header_chip("VLSFO Bunker Prices · by Port", "⛽")}
    <p style="font-size:11px; color:#5a6275; margin:0 0 10px 0;">
      Live VLSFO (0.5% sulfur) prices scraped from <a href="https://shipandbunker.com/prices" style="color:#5b8cbf;">shipandbunker.com</a>.
      Spread column shows how far each port is from a flat <b>Brent × 6.4</b> approximation — useful when comparing voyage TCEs across bunker hubs.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:6px;">
      <thead>
        <tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
          <th align="left"  style="padding:8px 12px;">Port</th>
          <th align="right" style="padding:8px 12px;">VLSFO</th>
          <th align="right" style="padding:8px 12px;">Spread</th>
          <th align="left"  style="padding:8px 12px;">Trade Relevance</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_calendar_section(data: dict) -> str:
    """Today's economic releases relevant to dry bulk freight."""
    items = data.get("calendar") or []
    if not items:
        return f"""
        {_section_header_chip("Economic Releases Today", "📅")}
        <p style="font-size:12px; color:#5a6275; margin:0;">
          No major commodity-relevant data releases scheduled today.
        </p>
        """

    rows = []
    for it in items:
        imp_col = ("#c92a2a" if it["importance"] == "high" else
                   "#d29922" if it["importance"] == "medium" else "#5a6275")
        rows.append(f"""
        <tr>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-size:11px; font-weight:600;">{it["time"]}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5;">
            <a href="{it["url"]}" style="color:#0066cc; text-decoration:none; font-size:13px; font-weight:500;">{it["name"]}</a>
          </td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-size:11px; color:{imp_col}; font-weight:600; text-transform:uppercase;">{it["importance"]}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">{it["note"]}</td>
        </tr>
        """)

    return f"""
    {_section_header_chip("Economic Releases Today · Freight Sensitivity", "📅")}
    <p style="font-size:11px; color:#5a6275; margin:0 0 10px 0;">
      Data releases that historically move dry bulk freight (directly or via commodity underlyings).
      Times in CET — adjust for the actual time zone of the release. Click each release for the official source.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:6px;">
      <thead>
        <tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
          <th align="left"  style="padding:8px 12px;">Time</th>
          <th align="left"  style="padding:8px 12px;">Release</th>
          <th align="left"  style="padding:8px 12px;">Impact</th>
          <th align="left"  style="padding:8px 12px;">Why it matters</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_hero_tiles(data: dict) -> str:
    """
    Quick-glance tiles at the top of the email: BDI (hero, with sparkline),
    BDRY paper, Brent, USD index. Designed for at-a-glance reading on mobile.
    """
    bdi     = data.get("bdi") or {}
    bdi_chg = data.get("bdi_change") or {}
    history = data.get("bdi_history") or []
    spot    = data.get("bdry_spot")
    snap    = data.get("snapshot", {})
    macro   = data.get("macro", {})

    # ── BDI hero tile (full width, with sparkline) ───────────────────────────
    bdi_val   = bdi.get("value")
    bdi_d1d   = bdi_chg.get("d1d")
    bdi_d5d   = bdi_chg.get("d5d")
    bdi_d30d  = bdi_chg.get("d30d")
    sparkline = render_bdi_sparkline(history, width=240, height=44) if len(history) >= 2 else ""

    if bdi_val:
        d1d_html  = _arrow(bdi_d1d)  if bdi_d1d  is not None else '<span style="color:#9ba3b4;">—</span>'
        d5d_html  = _arrow(bdi_d5d)  if bdi_d5d  is not None else '<span style="color:#9ba3b4;">—</span>'
        d30d_html = _arrow(bdi_d30d) if bdi_d30d is not None else '<span style="color:#9ba3b4;">—</span>'
        src       = bdi.get("source", "tradingeconomics.com")
        bdi_url   = bdi.get("url", "https://tradingeconomics.com/commodity/baltic")
        history_note = f"{len(history)} day{'s' if len(history)!=1 else ''} of history accumulated"

        bdi_tile = f"""
        <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:linear-gradient(135deg,#1e3050 0%,#2d4a6e 100%); border-radius:8px; color:#ffffff; margin-bottom:14px;">
          <tr><td style="padding:20px 24px;">
            <table cellpadding="0" cellspacing="0" border="0" width="100%">
              <tr>
                <td valign="top" style="vertical-align:top;">
                  <div style="font-size:10px; letter-spacing:0.16em; color:#a8c5e6; text-transform:uppercase; font-weight:600; font-family:'IBM Plex Mono',monospace;">
                    🎯 Baltic Dry Index · Real Spot
                  </div>
                  <div style="font-size:36px; font-weight:700; color:#ffffff; margin-top:6px; line-height:1; font-family:'IBM Plex Mono',monospace;">
                    {int(bdi_val):,}
                  </div>
                  <div style="font-size:11px; color:#a8c5e6; margin-top:6px;">
                    1D {d1d_html} &nbsp;·&nbsp; 5D {d5d_html} &nbsp;·&nbsp; 30D {d30d_html}
                  </div>
                </td>
                <td align="right" valign="top" style="vertical-align:top; text-align:right;">
                  {sparkline}
                  <div style="font-size:9px; color:#a8c5e6; margin-top:2px; font-family:'IBM Plex Mono',monospace;">
                    {history_note}
                  </div>
                </td>
              </tr>
            </table>
            <div style="margin-top:12px; padding-top:10px; border-top:1px solid rgba(168,197,230,0.2); font-size:10px; color:#a8c5e6;">
              Source: <a href="{bdi_url}" style="color:#a8c5e6; text-decoration:underline;">{src}</a>
              &nbsp;·&nbsp; Persisted history committed daily to repo
            </div>
          </td></tr>
        </table>
        """
    else:
        bdi_tile = f"""
        <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#fafbfc; border:1px dashed #cbd5e0; border-radius:8px; margin-bottom:14px;">
          <tr><td style="padding:16px 20px; text-align:center; color:#5a6275; font-size:12px;">
            ⚠ BDI scrape unavailable today. Showing BDRY-derived implied BDI in sections below.
          </td></tr>
        </table>
        """

    # ── 3 supporting tiles: BDRY paper, Brent, USD ───────────────────────────
    def _mini(title, value, delta, sub, color="#5b8cbf"):
        delta_html = _arrow(delta) if delta is not None else '<span style="color:#9ba3b4;">—</span>'
        return f"""
        <td valign="top" style="vertical-align:top; padding:0 4px; width:33.3%;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#ffffff; border:1px solid #e2e8f0; border-radius:6px;">
            <tr><td style="padding:12px 14px;">
              <div style="font-size:9px; letter-spacing:0.1em; color:{color}; text-transform:uppercase; font-weight:700; font-family:'IBM Plex Mono',monospace;">
                {title}
              </div>
              <div style="font-size:20px; font-weight:700; color:#1a202c; margin-top:4px; font-family:'IBM Plex Mono',monospace;">
                {value}
              </div>
              <div style="font-size:11px; margin-top:4px;">
                {delta_html} &nbsp;<span style="color:#9ba3b4;">·</span>&nbsp;
                <span style="color:#5a6275; font-size:10px;">{sub}</span>
              </div>
            </td></tr>
          </table>
        </td>
        """

    brent = macro.get("Brent") or {}
    dxy   = macro.get("DXY")   or {}
    bdry_d = (snap.get("BDRY", {}).get("delta_1d") or 0) * 100
    bdry_val_str = f"${spot:.2f}" if spot else "N/A"
    brent_val_str = f"${brent.get('value', 0):.0f}" if brent.get("value") else "N/A"
    dxy_val_str   = f"{dxy.get('value', 0):.1f}"     if dxy.get("value")   else "N/A"

    tiles_row = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:8px;">
      <tr>
        {_mini("BDRY · 5TC FFA",  bdry_val_str,  bdry_d if spot else None,  "5TC Capes paper")}
        {_mini("Brent Crude",     brent_val_str, brent.get("d1d"),          "Bunker → VLSFO")}
        {_mini("USD Index",       dxy_val_str,   dxy.get("d1d"),            "Commodity headwind")}
      </tr>
    </table>
    """

    return bdi_tile + tiles_row


def build_real_bdi_section(data: dict) -> str:
    """
    Dedicated section for the real BDI series with full statistics block.
    Shown after the executive summary for traders who want the deep dive.
    """
    bdi     = data.get("bdi") or {}
    bdi_chg = data.get("bdi_change") or {}
    history = data.get("bdi_history") or []

    bdi_val = bdi.get("value")
    if not bdi_val:
        return ""

    # Header stats
    rows = []

    def _stat_row(label, value, color="#1a202c"):
        return f"""
        <tr>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-size:12px; color:#5a6275;">{label}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600; color:{color}; font-size:13px;">{value}</td>
        </tr>
        """

    rows.append(_stat_row(
        "Current Level",
        f"<b>{int(bdi_val):,}</b>",
        "#0066cc",
    ))
    if bdi_chg.get("d1d") is not None:
        col = "#16803c" if bdi_chg["d1d"] >= 0 else "#c92a2a"
        rows.append(_stat_row("1-Day Change", f"{bdi_chg['d1d']:+.2f}%", col))
    if bdi_chg.get("d5d") is not None:
        col = "#16803c" if bdi_chg["d5d"] >= 0 else "#c92a2a"
        rows.append(_stat_row("5-Day Change", f"{bdi_chg['d5d']:+.2f}%", col))
    if bdi_chg.get("d30d") is not None:
        col = "#16803c" if bdi_chg["d30d"] >= 0 else "#c92a2a"
        rows.append(_stat_row("30-Day Change", f"{bdi_chg['d30d']:+.2f}%", col))
    if bdi_chg.get("d365d") is not None:
        col = "#16803c" if bdi_chg["d365d"] >= 0 else "#c92a2a"
        rows.append(_stat_row("1-Year Change", f"{bdi_chg['d365d']:+.2f}%", col))
    if bdi_chg.get("52w_high") is not None:
        rows.append(_stat_row("52W High",
                              f"{int(bdi_chg['52w_high']):,}"))
    if bdi_chg.get("52w_low") is not None:
        rows.append(_stat_row("52W Low",
                              f"{int(bdi_chg['52w_low']):,}"))
    if bdi_chg.get("pctile") is not None:
        col = ("#c92a2a" if bdi_chg["pctile"] >= 80 else
               "#16803c" if bdi_chg["pctile"] <= 20 else "#1a202c")
        rows.append(_stat_row(
            "Percentile (full history)",
            f"{bdi_chg['pctile']:.0f}th",
            col,
        ))
    rows.append(_stat_row(
        "Days in history",
        f"{len(history)}",
        "#9ba3b4",
    ))

    big_chart = render_bdi_sparkline(history, width=420, height=80, color="#5b8cbf")

    last_date = bdi_chg.get("last_date", "")

    return f"""
    {_section_header("Baltic Dry Index — Real Spot Series", "📉")}
    <p style="font-size:12px; color:#5a6275; margin:0 0 12px 0;">
      Scraped daily from <a href="https://tradingeconomics.com/commodity/baltic" style="color:#5b8cbf; text-decoration:none;">tradingeconomics.com</a>.
      Historical series persists in the repo's <code style="background:#f7f9fc; padding:2px 5px; border-radius:3px; font-size:11px;">data/bdi_history.json</code> — grows by one observation per morning. Last update: <b>{last_date}</b>.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td valign="top" style="vertical-align:top; width:55%; padding-right:14px;">
          <div style="background:#fafbfc; border:1px solid #e2e8f0; border-radius:6px; padding:14px; text-align:center;">
            {big_chart}
            <div style="font-size:10px; color:#9ba3b4; margin-top:6px; font-family:'IBM Plex Mono',monospace;">
              Last {min(len(history), 90)} days
            </div>
          </div>
        </td>
        <td valign="top" style="vertical-align:top; width:45%;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:6px;">
            {''.join(rows)}
          </table>
        </td>
      </tr>
    </table>
    """


def build_overnight_context(data: dict) -> str:
    """
    Narrative prose paragraph describing the overnight dry bulk market state.
    Adapts daily based on BDRY direction, cycle phase, cross-asset moves,
    FFA forward curve shape, and top news driver. ~3-5 sentences.
    """
    snap     = data["snapshot"]
    macro    = data["macro"]
    regime   = data["regime"]
    sigs     = data["signals"]
    articles = data["articles"]
    spot     = data["bdry_spot"]
    fwd      = data["bdry_fwd"]

    sentences: list[str] = []

    # ── Lead: REAL BDI first (if available), then BDRY paper market ──────────
    bdi        = data.get("bdi") or {}
    bdi_chg    = data.get("bdi_change") or {}
    bdi_val    = bdi.get("value")
    bdi_d1d    = bdi_chg.get("d1d")
    bdi_d5d    = bdi_chg.get("d5d")
    pctile     = bdi_chg.get("pctile")

    bdry     = snap.get("BDRY", {})
    bdry_d1d = bdry.get("delta_1d", 0) or 0
    bdry_d5d = bdry.get("delta_5d", 0) or 0

    cycle_phrase = {
        "EXPANSION":   " with the cycle in expansion territory",
        "PEAK":        " though the cycle is showing late-stage peak signals",
        "CONTRACTION": " amid an active cycle contraction",
        "TROUGH":      " near cycle-trough levels",
        "NEUTRAL":     "",
    }.get(regime.get("phase", ""), "")

    # Pick the dominant 1D move to colour the narrative — prefer real BDI
    primary_d1d = bdi_d1d if bdi_d1d is not None else bdry_d1d * 100
    if   primary_d1d >  1.5: action = "extended sharply higher overnight"
    elif primary_d1d >  0.5: action = "edged higher overnight"
    elif primary_d1d < -1.5: action = "sold off overnight"
    elif primary_d1d < -0.5: action = "drifted lower overnight"
    else:                    action = "traded sideways overnight"

    if bdi_val:
        five_day = ""
        if bdi_d5d is not None:
            if   bdi_d5d >  5: five_day = f" (+{bdi_d5d:.1f}% over 5 sessions — momentum building)"
            elif bdi_d5d < -5: five_day = f" ({bdi_d5d:+.1f}% over 5 sessions — sustained pressure)"
            elif abs(bdi_d5d) >= 2: five_day = f" ({bdi_d5d:+.1f}% on the week)"

        pct_phrase = ""
        if pctile is not None:
            if   pctile >= 80: pct_phrase = f" — currently in the {pctile:.0f}th percentile of recorded history (rich)"
            elif pctile <= 20: pct_phrase = f" — currently in the {pctile:.0f}th percentile (cheap)"

        d1d_str = f" ({bdi_d1d:+.2f}% 1D)" if bdi_d1d is not None else ""
        sentences.append(
            f"<b>Baltic Dry Index</b> {action} at <b>{int(bdi_val):,}</b>{d1d_str}{five_day}"
            f"{cycle_phrase}{pct_phrase}."
        )

        # YoY context using BDRY proxy until enough real BDI history accumulates
        yoy_proxy = data.get("yoy_bdi_proxy")
        if yoy_proxy:
            yoy_chg = (bdi_val / yoy_proxy - 1) * 100
            direction = "above" if yoy_chg > 5 else ("below" if yoy_chg < -5 else "broadly in line with")
            sentences.append(
                f"This sits <b>{abs(yoy_chg):.0f}% {direction}</b> the BDRY-implied BDI from the same week one year ago "
                f"(synthetic estimate: ~{int(yoy_proxy):,}) — useful seasonal context until the real BDI history file accumulates 12+ months."
            )

        # Add BDRY paper line as a separate sentence so it doesn't crowd the lead
        if spot:
            bdry_dir = ("firmed" if bdry_d1d > 0.005 else
                        "softened" if bdry_d1d < -0.005 else "held steady")
            sentences.append(
                f"BDRY ETF — the 5TC Capes / 4TC Panamax FFA-backed product, "
                f"a forward-looking proxy — {bdry_dir} to <b>${spot:.2f}</b> "
                f"({bdry_d1d*100:+.2f}%), implying a Day-+30 paper BDI around "
                f"<b>{int(spot * _BDI_FACTOR_MID):,}</b>."
            )
    elif spot:
        sentences.append(
            f"Dry bulk paper {action} — BDRY ETF (closest free 5TC Capes FFA proxy) "
            f"settled at <b>${spot:.2f}</b>, implying a BDI level around "
            f"<b>{int(spot * _BDI_FACTOR_MID):,}</b>{cycle_phrase}."
        )

    # ── Cross-asset context (only mention what actually moved) ───────────────
    cross_bits = []

    brent = macro.get("Brent")
    if brent and abs(brent["d1d"]) > 0.3:
        direction = "firmer" if brent["d1d"] > 0 else "softer"
        cross_bits.append(
            f"Brent {direction} at <b>${brent['value']:.2f}/bbl</b> ({brent['d1d']:+.2f}%, "
            f"VLSFO bunker proxy ~${brent['value']*6.5:.0f}/mt)"
        )

    dxy = macro.get("DXY")
    if dxy and abs(dxy["d1d"]) > 0.25:
        direction  = "firming"  if dxy["d1d"] > 0 else "softening"
        impact     = "headwind" if dxy["d1d"] > 0 else "tailwind"
        cross_bits.append(
            f"DXY {direction} ({dxy['d1d']:+.2f}%) — typically a {impact} for commodity flows"
        )

    vale = macro.get("VALE")
    if vale and abs(vale["d1d"]) > 1.0:
        direction = "rallied" if vale["d1d"] > 0 else "sold off"
        cross_bits.append(
            f"iron ore proxy (VALE) {direction} {vale['d1d']:+.2f}% — Capesize demand bellwether"
        )

    grain_moves = []
    for key, lbl in [("Corn", "corn"), ("Wheat", "wheat"), ("Soybean", "soybeans")]:
        m = macro.get(key)
        if m and abs(m["d1d"]) > 1.0:
            grain_moves.append(f"{lbl} {m['d1d']:+.2f}%")
    if grain_moves:
        cross_bits.append(
            f"on the ag side {', '.join(grain_moves)} — relevant to LDC's Panamax/Supramax exposure"
        )

    if cross_bits:
        sentences.append("Cross-asset: " + "; ".join(cross_bits) + ".")

    # ── FFA forward curve shape ──────────────────────────────────────────────
    if spot and fwd:
        nearest = fwd[0]
        chg = (nearest["fwd"] / spot - 1) * 100
        if abs(chg) > 2:
            if chg < 0:
                shape = "contango (forward at discount)"
                read  = "paper pricing softer prompt physical or seasonal weakness"
            else:
                shape = "backwardation (forward at premium)"
                read  = "paper signalling tight prompt supply"
            sentences.append(
                f"FFA forward curve sits in <b>{shape}</b> "
                f"({datetime.strptime(nearest['expiry'], '%Y-%m-%d').strftime('%b %Y')} "
                f"${nearest['fwd']:.2f}, {chg:+.1f}% vs spot) — {read}."
            )

    # ── Top hook: signal or highest-relevance article ────────────────────────
    hook = ""
    if sigs:
        s_text = (sigs[0].get("text", "") or "").strip()
        if s_text:
            hook = s_text[:170]
    if not hook and articles:
        top = sorted(articles, key=lambda a: -a.get("score", 0))[0]
        if top.get("score", 0) > 0.5:
            hook = (top.get("title", "") or "")[:160]
    if hook:
        sentences.append(f"<b>Watch today:</b> {hook}.")

    if not sentences:
        sentences = [
            "Limited overnight data — markets may be closed or feeds intermittent. "
            "Refer to the sections below for available figures."
        ]

    paragraph = " ".join(sentences)
    return f"""
    {_section_header("Overnight Dry Bulk Context", "🌅")}
    <div style="font-size:14px; line-height:1.7; color:#1a202c; padding:16px 20px; background:#f7f9fc; border-left:4px solid #5b8cbf; border-radius:0 4px 4px 0;">
      {paragraph}
    </div>
    """


def build_exec_summary_llm(data: dict) -> str | None:
    """If ANTHROPIC_API_KEY is set, use Claude for the exec summary."""
    narrative = generate_llm_narrative(data)
    if not narrative:
        return None
    return f"""
    {_section_header_chip("Executive Summary · AI-Generated", "🤖")}
    <div style="font-size:14px; line-height:1.7; color:#1a202c; padding:16px 20px; background:#fafbfc; border-left:4px solid #5b8cbf; border-radius:0 4px 4px 0;">
      {narrative}
    </div>
    <div style="margin-top:6px; font-size:10px; color:#9ba3b4; font-style:italic;">
      Generated by Claude (Anthropic) from today's market facts. Always verify before acting.
    </div>
    """


def build_exec_summary(data: dict) -> str:
    snap   = data["snapshot"]
    macro  = data["macro"]
    regime = data["regime"]
    sigs   = data["signals"]
    spot   = data["bdry_spot"]
    fwd    = data["bdry_fwd"]

    bullets = []

    if spot:
        bdi_imp = int(spot * _BDI_FACTOR_MID)
        bdry_d1d = snap.get("BDRY", {}).get("delta_1d")
        d_str = f" ({bdry_d1d*100:+.2f}%)" if bdry_d1d is not None else ""
        tone = ("📈 firm"  if (bdry_d1d or 0) > 0.005 else
                "📉 soft"  if (bdry_d1d or 0) < -0.005 else
                "➡️ flat")
        bullets.append(
            f"<b>BDRY ETF</b> last <b>${spot:.2f}</b>{d_str} → "
            f"BDI implied ≈ <b>{bdi_imp:,}</b>. Tone: {tone}."
        )

    if fwd:
        nearest = fwd[0]
        chg = (nearest["fwd"] / spot - 1) * 100 if spot else 0
        bullets.append(
            f"<b>FFA forward (put-call parity)</b> "
            f"{datetime.strptime(nearest['expiry'], '%Y-%m-%d').strftime('%b %Y')}: "
            f"<b>${nearest['fwd']:.2f}</b> ({chg:+.1f}% vs spot) — "
            f"market pricing {'a premium' if chg > 1 else 'a discount' if chg < -1 else 'roughly flat'} forward."
        )

    if regime.get("phase"):
        bullets.append(
            f"<b>Cycle phase:</b> {regime.get('emoji','')} "
            f"<b>{regime.get('label','')}</b> — {regime.get('description','')[:120]}"
        )

    brent = macro.get("Brent")
    if brent:
        bullets.append(
            f"<b>Brent</b> ${brent['value']:.2f}/bbl ({brent['d1d']:+.2f}%) → "
            f"VLSFO bunker proxy ~${brent['value']*6.5:.0f}/mt."
        )

    if sigs:
        s = sigs[0]
        bullets.append(f"<b>Top signal:</b> {s.get('text','')[:160]}")

    if not bullets:
        bullets = ["Data unavailable — markets closed or feeds unreachable."]

    li_html = "\n".join(f'<li style="margin-bottom:6px;">{b}</li>' for b in bullets)
    return f"""
    {_section_header("Executive Summary")}
    <ul style="margin:0; padding-left:22px; font-size:14px; color:#1a202c; line-height:1.6;">
      {li_html}
    </ul>
    """


def _row(name, source_link, last, d1, d5, note, highlight=False):
    bg = "background:#f0f7ff;" if highlight else ""
    name_style = "color:#0066cc; font-weight:700;" if highlight else "font-weight:500;"
    src = f'<a href="{source_link}" style="color:#9ba3b4; text-decoration:none; font-size:10px;">↗</a>' if source_link else ""
    return f"""
    <tr style="{bg}">
      <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; {name_style} font-size:13px;">{name} {src}</td>
      <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600;">{last}</td>
      <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5;">{_arrow(d1)}</td>
      <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5;">{_arrow(d5)}</td>
      <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; color:#5a6275; font-size:12px;">{note}</td>
    </tr>
    """


def build_levels_table(data: dict) -> str:
    snap  = data["snapshot"]
    macro = data["macro"]
    spot  = data["bdry_spot"]
    rows  = []

    if "BDRY" in snap:
        b   = snap["BDRY"]
        v   = b.get("value", 0)
        d1  = b["delta_1d"] * 100 if b.get("delta_1d") is not None else None
        d5  = b["delta_5d"] * 100 if b.get("delta_5d") is not None else None
        rows.append(_row(
            "BDRY ETF (5TC Capes FFA proxy)",
            "https://finance.yahoo.com/quote/BDRY",
            f"${v:.2f}", d1, d5,
            f"BDI implied ≈ {int(v*_BDI_FACTOR_MID):,}",
            highlight=True,
        ))

    for tk, lbl in [("SBLK", "Star Bulk Carriers"), ("NMM", "Navios Maritime"),
                    ("EGLE", "Eagle Bulk Shipping"), ("GNK", "Genco Shipping")]:
        if tk in snap:
            d   = snap[tk]
            v   = d.get("value", 0)
            d1  = d["delta_1d"] * 100 if d.get("delta_1d") is not None else None
            d5  = d["delta_5d"] * 100 if d.get("delta_5d") is not None else None
            rows.append(_row(
                f"{lbl} ({tk})",
                f"https://finance.yahoo.com/quote/{tk}",
                f"${v:.2f}", d1, d5, "",
            ))

    macro_rows = [
        ("Brent",    "Brent Crude (BZ=F)",      "https://finance.yahoo.com/quote/BZ%3DF",
         "$/bbl", "Bunker proxy → VLSFO ${val_x65:.0f}/mt"),
        ("WTI",      "WTI Crude (CL=F)",        "https://finance.yahoo.com/quote/CL%3DF",
         "$/bbl", ""),
        ("US10Y",    "US 10Y Treasury",         "https://finance.yahoo.com/quote/%5ETNX",
         "%",     "Risk-free benchmark"),
        ("DXY",      "USD Index (DXY)",         "https://finance.yahoo.com/quote/DX-Y.NYB",
         "",      "Strong USD = headwind for commodities"),
        ("VALE",     "Iron Ore proxy (VALE)",   "https://finance.yahoo.com/quote/VALE",
         "$",     "~30% of dry bulk demand · China-led"),
        ("BHP",      "BHP Group",               "https://finance.yahoo.com/quote/BHP",
         "$",     "Capesize iron ore from Pilbara"),
        ("BTU",      "Coal proxy (BTU)",        "https://finance.yahoo.com/quote/BTU",
         "$",     "~25% of dry bulk demand"),
        ("SP500",    "S&P 500",                 "https://finance.yahoo.com/quote/%5EGSPC",
         "",      "Broad risk sentiment"),
        ("Hang_Seng","Hang Seng",               "https://finance.yahoo.com/quote/%5EHSI",
         "",      "China demand sentiment"),
    ]
    for key, lbl, link, suffix, note_tmpl in macro_rows:
        m = macro.get(key)
        if not m:
            continue
        v = m["value"]
        last_str = (f"${v:,.2f}" if suffix == "$" else
                    f"{v:.2f}{suffix}" if suffix else f"{v:,.2f}")
        if "$/bbl" in suffix:
            last_str = f"${v:.2f}/bbl"
        note = note_tmpl.format(val_x65=v * 6.5) if "{val_x65" in note_tmpl else note_tmpl
        rows.append(_row(lbl, link, last_str, m["d1d"], m.get("d5d"), note))

    return f"""
    {_section_header("Key Levels · Overnight Moves")}
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse; border:1px solid #e2e8f0; border-radius:4px;">
      <thead>
        <tr style="background:#f7f9fc; color:#5a6275; font-size:10px; text-transform:uppercase; letter-spacing:0.06em;">
          <th align="left"  style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">Asset</th>
          <th align="right" style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">Last</th>
          <th align="right" style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">1D</th>
          <th align="right" style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">5D</th>
          <th align="left"  style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">Trader's Note</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_ffa_curve(data: dict) -> str:
    spot = data["bdry_spot"]
    fwd  = data["bdry_fwd"]
    if not spot or not fwd:
        return ""

    rows = [f"""
        <tr style="background:#f0f7ff;">
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-weight:700; color:#0066cc;">SPOT</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:700; color:#0066cc;">${spot:.2f}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; color:#0066cc;">{int(spot*_BDI_FACTOR_MID):,}</td>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">BDRY last close</td>
        </tr>
    """]
    for f in fwd:
        chg = (f["fwd"] / spot - 1) * 100
        chg_str = _arrow(chg)
        exp_str = datetime.strptime(f["expiry"], "%Y-%m-%d").strftime("%b %Y")
        rows.append(f"""
        <tr>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-weight:500;">{exp_str}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600;">${f["fwd"]:.2f}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; color:#d29922;">{int(f["fwd"]*_BDI_FACTOR_MID):,}</td>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-size:11px;">{chg_str} vs spot</td>
        </tr>
        """)

    return f"""
    {_section_header("FFA Forward Curve · BDRY Options (Put-Call Parity)", "🔮")}
    <p style="font-size:12px; color:#5a6275; margin:0 0 10px 0;">
      Indicative only — BDRY options thinly traded. For live FFA quotes use Baltic Exchange or broker screens (Marex, FIS).
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:4px;">
      <thead>
        <tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
          <th align="left"  style="padding:8px 10px;">Expiry</th>
          <th align="right" style="padding:8px 10px;">BDRY Fwd</th>
          <th align="right" style="padding:8px 10px;">BDI Implied</th>
          <th align="left"  style="padding:8px 10px;">vs Spot</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_supply_section(data: dict) -> str:
    ais_df = data["ais"]
    if ais_df.empty:
        chk_block = ""
        ais_block = ""
    else:
        # AIS overall metrics
        n = len(ais_df)
        underway_n = int(ais_df["underway"].sum()) if "underway" in ais_df.columns else 0
        underway_pct = (underway_n / n) * 100 if n else 0
        avg_sog = float(ais_df.loc[ais_df.get("underway", False), "sog"].mean()) \
            if ("sog" in ais_df.columns and underway_n > 0) else 0
        slow_pct = float((ais_df.loc[ais_df.get("underway", False), "sog"] < 11).mean() * 100) \
            if ("sog" in ais_df.columns and underway_n > 0) else 0

        if underway_pct >= 38:
            sig, scol = "TIGHT", "#c92a2a"
        elif underway_pct >= 26:
            sig, scol = "BALANCED", "#d29922"
        else:
            sig, scol = "SLACK", "#16803c"

        ais_block = f"""
        <div style="background:#f7f9fc; border-left:3px solid {scol}; padding:12px 16px; margin-bottom:14px; border-radius:0 4px 4px 0;">
          <div style="font-size:10px; color:#5a6275; text-transform:uppercase; letter-spacing:0.06em;">
            Live AIS · Bulk Carriers Tracked: <b style="color:#1a202c;">{n:,}</b>
          </div>
          <div style="margin-top:6px; font-size:13px;">
            <span style="font-size:15px; font-weight:700; color:{scol};">{sig}</span>
            <span style="color:#1a202c; margin-left:12px;">{underway_pct:.1f}% utilisation</span>
            <span style="color:#9ba3b4; margin:0 6px;">·</span>
            <span style="color:#5a6275;">avg SOG <b>{avg_sog:.1f} kn</b></span>
            <span style="color:#9ba3b4; margin:0 6px;">·</span>
            <span style="color:#5a6275;">slow-steaming <b>{slow_pct:.0f}%</b></span>
          </div>
        </div>
        """

        # Top Baltic routes by traffic
        try:
            route_df = get_route_traffic(ais_df)
            chk_traffic = get_chokepoint_traffic(ais_df)
        except Exception:
            route_df = pd.DataFrame()
            chk_traffic = {}

        route_html = ""
        if not route_df.empty:
            top_routes = route_df.sort_values("Total", ascending=False).head(5)
            route_rows_html = "".join(f"""
            <tr>
              <td style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-size:12px; font-weight:600;">{r['Route']}</td>
              <td style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">{r.get('Cargo','—')}</td>
              <td align="right" style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600;">{int(r['Total'])}</td>
              <td align="right" style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; color:#16803c;">{int(r.get('Underway',0))}</td>
            </tr>
            """ for _, r in top_routes.iterrows())
            route_html = f"""
            <div style="margin-top:14px;">
              <div style="font-size:11px; color:#5a6275; text-transform:uppercase; font-weight:600; margin-bottom:6px;">
                Baltic Route Activity · Top 5 (Live AIS)
              </div>
              <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:4px;">
                <thead><tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
                  <th align="left"  style="padding:6px 10px;">Route</th>
                  <th align="left"  style="padding:6px 10px;">Cargo</th>
                  <th align="right" style="padding:6px 10px;">Vessels</th>
                  <th align="right" style="padding:6px 10px;">Underway</th>
                </tr></thead>
                <tbody>{route_rows_html}</tbody>
              </table>
            </div>
            """

        # Chokepoint live counts
        chk_html = ""
        if chk_traffic:
            chk_rows_html = ""
            for cp_name, cp_meta in CHOKEPOINTS.items():
                # Match by name fragment
                live_count = sum(v for k, v in chk_traffic.items()
                                 if any(w.lower() in k.lower()
                                        for w in cp_name.replace("/", " ").split()[:2]))
                status = cp_meta.get("status", "OPEN")
                color = "#16803c" if status == "OPEN" else ("#d29922" if status == "RESTRICTED" else "#c92a2a")
                chk_rows_html += f"""
                <tr>
                  <td style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-size:12px; font-weight:600;">{cp_name}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #f0f2f5;"><span style="color:{color}; font-weight:600; font-size:11px;">● {status}</span></td>
                  <td align="right" style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace;">{live_count}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #f0f2f5; font-size:10px; color:#9ba3b4;">{cp_meta.get('notes','')[:65]}</td>
                </tr>
                """
            chk_html = f"""
            <div style="margin-top:14px;">
              <div style="font-size:11px; color:#5a6275; text-transform:uppercase; font-weight:600; margin-bottom:6px;">
                Chokepoint Status · Live Vessel Counts
              </div>
              <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:4px;">
                <thead><tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
                  <th align="left"  style="padding:6px 10px;">Chokepoint</th>
                  <th align="left"  style="padding:6px 10px;">Status</th>
                  <th align="right" style="padding:6px 10px;">Live</th>
                  <th align="left"  style="padding:6px 10px;">Notes</th>
                </tr></thead>
                <tbody>{chk_rows_html}</tbody>
              </table>
            </div>
            """

        chk_block = route_html + chk_html

    return f"""
    {_section_header("Supply Drivers · Effective Capacity", "🚢")}
    <p style="font-size:12px; color:#5a6275; margin:0 0 12px 0;">
      Effective supply ≠ headline DWT. Slow steaming, port congestion, chokepoint disruptions all subtract.
      Source: <a href="https://aisstream.io" style="color:#5b8cbf; text-decoration:none;">aisstream.io WebSocket</a>.
    </p>
    {ais_block}
    {chk_block}
    """


def build_demand_section(data: dict) -> str:
    macro = data["macro"]
    rows = []

    items = [
        ("VALE",    "Iron Ore proxy (VALE)",   "https://finance.yahoo.com/quote/VALE",
         "~30% of dry bulk demand · Brazil/Pilbara → China"),
        ("BHP",     "BHP Group",                "https://finance.yahoo.com/quote/BHP",
         "Capesize charterer · Pilbara iron ore"),
        ("BTU",     "Coal proxy (BTU)",         "https://finance.yahoo.com/quote/BTU",
         "~25% of dry bulk demand · gas-substitution risk"),
        ("Corn",    "Corn (ZC=F)",              "https://finance.yahoo.com/quote/ZC%3DF",
         "Panamax · US Gulf, Brazil, Black Sea"),
        ("Wheat",   "Wheat (ZW=F)",             "https://finance.yahoo.com/quote/ZW%3DF",
         "Panamax / Supramax · Black Sea security premium"),
        ("Soybean", "Soybean (ZS=F)",           "https://finance.yahoo.com/quote/ZS%3DF",
         "Panamax · Brazil → China cycle, US new-crop"),
    ]
    for key, lbl, link, note in items:
        m = macro.get(key)
        if not m:
            continue
        v = m["value"]
        last_str = f"${v:,.2f}"
        rows.append(f"""
        <tr>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-size:13px;">
            <b>{lbl}</b> <a href="{link}" style="color:#9ba3b4; text-decoration:none; font-size:10px;">↗</a>
          </td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-family:'IBM Plex Mono',monospace; font-weight:600;">{last_str}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5;">{_arrow(m["d1d"])}</td>
          <td align="right" style="padding:8px 10px; border-bottom:1px solid #f0f2f5;">{_arrow(m.get("d5d"))}</td>
          <td style="padding:8px 10px; border-bottom:1px solid #f0f2f5; font-size:11px; color:#5a6275;">{note}</td>
        </tr>
        """)

    return f"""
    {_section_header("Demand Drivers · Commodity Underlyings", "📦")}
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border:1px solid #e2e8f0; border-radius:4px;">
      <thead><tr style="background:#f7f9fc; font-size:10px; color:#5a6275; text-transform:uppercase;">
        <th align="left"  style="padding:8px 10px;">Commodity</th>
        <th align="right" style="padding:8px 10px;">Last</th>
        <th align="right" style="padding:8px 10px;">1D</th>
        <th align="right" style="padding:8px 10px;">5D</th>
        <th align="left"  style="padding:8px 10px;">Why it matters</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_news_section(data: dict) -> str:
    articles = data["articles"]
    if not articles:
        return ""

    arts = sorted(articles, key=lambda a: -a.get("score", 0))[:7]

    items = []
    for a in arts:
        title  = a.get("title", "")[:160]
        source = a.get("source", "")
        link   = a.get("link", "#")
        score  = int(a.get("score", 0) * 100)
        pub    = (a.get("published") or "")[:16]
        score_color = "#c92a2a" if score >= 70 else ("#d29922" if score >= 40 else "#5b8cbf")
        items.append(f"""
        <li style="margin-bottom:10px; padding:12px 14px; background:#fafbfc; border-radius:4px; border:1px solid #e2e8f0;">
          <a href="{link}" style="color:#0066cc; text-decoration:none; font-weight:500; font-size:13px; line-height:1.4;">{title}</a>
          <div style="font-size:11px; color:#9ba3b4; margin-top:6px; font-family:'IBM Plex Mono',monospace;">
            {source} · {pub}
            <span style="color:{score_color}; margin-left:8px;">●</span>
            <span style="color:{score_color};"> relevance {score}%</span>
          </div>
        </li>
        """)

    return f"""
    {_section_header("Top Dry Bulk & Commodity Stories", "📰")}
    <p style="font-size:12px; color:#5a6275; margin:0 0 10px 0;">
      Curated from RSS feeds: TradeWinds, Splash247, Hellenic Shipping. Click headlines to verify source.
    </p>
    <ul style="list-style:none; padding:0; margin:0;">
      {''.join(items)}
    </ul>
    """


def build_geo_signals(data: dict) -> str:
    sigs = data["signals"]
    if not sigs:
        return ""
    items = []
    for s in sigs[:5]:
        lvl = s.get("level", "amber")
        col = "#c92a2a" if lvl == "red" else ("#d29922" if lvl == "amber" else "#16803c")
        items.append(f"""
        <li style="margin-bottom:8px; padding:10px 14px; border-left:3px solid {col}; background:#fafbfc; border-radius:0 4px 4px 0; list-style:none;">
          <div style="font-size:13px; color:#1a202c; line-height:1.4;">{s.get('text','')[:220]}</div>
        </li>
        """)
    return f"""
    {_section_header("Overnight Geopolitical & Macro Signals", "🌍")}
    <ul style="list-style:none; padding:0; margin:0;">{''.join(items)}</ul>
    """


def build_watch_section(data: dict) -> str:
    items = []
    snap   = data["snapshot"]
    macro  = data["macro"]
    regime = data["regime"]
    fwd    = data["bdry_fwd"]
    spot   = data["bdry_spot"]

    bdry = snap.get("BDRY", {})
    if bdry.get("delta_5d") is not None:
        if bdry["delta_5d"] > 0.05:
            items.append("BDRY 5-day +5%+ — momentum signal; watch for continuation/exhaustion at MA50")
        elif bdry["delta_5d"] < -0.05:
            items.append("BDRY 5-day -5%+ — drawdown signal; watch for support near MA200")

    if regime.get("phase") in ("PEAK", "CONTRACTION"):
        items.append(f"Cycle late-stage ({regime.get('label','')}) — favour short-cover hedges over new long positions")
    elif regime.get("phase") == "TROUGH":
        items.append(f"Cycle ({regime.get('label','')}) — accumulate paper FFA on weakness; physical volume into Q3 favorable")

    if spot and fwd:
        nearest = fwd[0]
        chg = (nearest["fwd"] / spot - 1) * 100
        if chg < -3:
            items.append(f"Forward curve in <b>contango</b>: Spot ${spot:.2f} vs {nearest['expiry'][:7]} fwd ${nearest['fwd']:.2f} ({chg:+.1f}%) — paper market bearish near-term")
        elif chg > 3:
            items.append(f"Forward curve in <b>backwardation</b>: Spot ${spot:.2f} vs {nearest['expiry'][:7]} fwd ${nearest['fwd']:.2f} ({chg:+.1f}%) — tight prompt physical")

    dxy = macro.get("DXY")
    if dxy and abs(dxy["d1d"]) > 0.5:
        direction = "stronger" if dxy["d1d"] > 0 else "weaker"
        items.append(f"DXY {dxy['d1d']:+.2f}% — {direction} USD; watch reaction in commodity-sensitive shipping equities")

    items.append("Today: review FFA broker runs (FIS, Marex, BraemarX) before stand-up · check Baltic Exchange daily fixings if subscribed")
    items.append("LDC desk-relevant: monitor Brazil soybean / corn loadings (Santos, Paranaguá) and US Gulf wheat/corn export inspections")

    list_html = "\n".join(f'<li style="margin-bottom:8px; line-height:1.5;">{i}</li>' for i in items)
    return f"""
    {_section_header("Today's Watch · Trader Action Items", "⚠")}
    <ul style="font-size:13px; color:#1a202c; padding-left:22px; margin:0;">
      {list_html}
    </ul>
    """


def build_sources() -> str:
    sources = [
        ("BDRY ETF & equities",         "https://finance.yahoo.com/quote/BDRY"),
        ("Brent / WTI / 10Y / DXY",     "https://finance.yahoo.com"),
        ("FRED macroeconomic data",     "https://fred.stlouisfed.org"),
        ("AIS live vessel positions",   "https://aisstream.io"),
        ("TradeWinds",                  "https://www.tradewindsnews.com"),
        ("Splash247",                   "https://splash247.com"),
        ("Hellenic Shipping News",      "https://www.hellenicshippingnews.com"),
        ("Baltic Exchange (paywalled)", "https://www.balticexchange.com"),
        ("Clarksons Research",          "https://www.crsl.com"),
    ]
    items = " · ".join(f'<a href="{u}" style="color:#5b8cbf; text-decoration:none;">{n}</a>' for n, u in sources)
    return f"""
    <div style="font-size:10px; letter-spacing:0.08em; color:#9ba3b4; text-transform:uppercase; font-weight:600; margin-bottom:6px;">
      Sources & Verification Links
    </div>
    <div style="font-size:12px; color:#5a6275; line-height:1.7;">
      {items}
    </div>
    <div style="margin-top:10px; font-size:11px; color:#9ba3b4; line-height:1.5;">
      All figures verifiable via the linked sources. Real Baltic Exchange BDI / FFA fixings require subscription.
      BDRY ETF is treated as the closest free-API equivalent of the 5TC Capes FFA — it holds CME freight futures.
    </div>
    """


# ─────────────────────────────────────────────────────────────────────────────
# ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────

def build_email(data: dict) -> tuple[str, str]:
    today = datetime.now(ZoneInfo("Europe/Zurich"))
    date_str = today.strftime("%A, %d %B %Y")

    snap = data["snapshot"]
    bdry = snap.get("BDRY", {})
    bdry_v = bdry.get("value")
    bdry_d = bdry.get("delta_1d")

    # Subject: lead with REAL BDI if available, else BDRY
    bdi_val = (data.get("bdi") or {}).get("value")
    bdi_d1d = (data.get("bdi_change") or {}).get("d1d")
    subject_parts = [f"🚢 Dry Bulk · {today.strftime('%a %d %b')}"]
    if bdi_val is not None:
        bdi_chg_str = f" ({bdi_d1d:+.2f}%)" if bdi_d1d is not None else ""
        subject_parts.append(f"BDI {int(bdi_val):,}{bdi_chg_str}")
    if bdry_v and bdry_d is not None:
        subject_parts.append(f"BDRY ${bdry_v:.2f} ({bdry_d*100:+.2f}%)")
    regime = data.get("regime", {})
    if regime.get("label"):
        subject_parts.append(regime["label"])
    subject = " · ".join(subject_parts)

    # Prefer LLM-generated exec summary when ANTHROPIC_API_KEY is set
    exec_block = build_exec_summary_llm(data) or build_exec_summary(data)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>Dry Bulk Morning Briefing</title>
<style>
  /* Dark mode support — overrides inline light-mode styles */
  @media (prefers-color-scheme: dark) {{
    body, .email-bg {{ background: #0d1117 !important; }}
    .email-card {{ background: #161b22 !important; color: #c9d1d9 !important; }}
    .email-card-alt {{ background: #1c2128 !important; }}
    .email-text {{ color: #c9d1d9 !important; }}
    .email-text-sec {{ color: #8b949e !important; }}
    .email-table-row {{ background: #161b22 !important; border-color: #30363d !important; }}
    .email-table-head {{ background: #1c2128 !important; color: #8b949e !important; }}
    a {{ color: #79c0ff !important; }}
    hr {{ border-color: #30363d !important; }}
  }}
  /* Responsive: stack tile columns on mobile */
  @media (max-width: 540px) {{
    .tile-cell {{ display: block !important; width: 100% !important; padding: 4px 0 !important; }}
    .email-container {{ padding: 12px 6px !important; }}
  }}
</style>
</head>
<body class="email-bg" style="margin:0; padding:0; background:#eef2f7; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; color:#1a202c; line-height:1.5;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" class="email-container" style="background:#eef2f7; padding:24px 12px;">
    <tr><td align="center">
      <table cellpadding="0" cellspacing="0" border="0" width="720" class="email-card" style="max-width:720px; background:#ffffff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.06); overflow:hidden;">

        <tr><td style="padding:28px 32px 18px; background:linear-gradient(180deg,#ffffff 0%,#fafbfc 100%); border-bottom:3px solid #1e3050;" class="email-card">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">
            <tr>
              <td valign="top">
                <div style="font-size:11px; letter-spacing:0.14em; color:#5b8cbf; text-transform:uppercase; font-weight:700; font-family:'IBM Plex Mono',monospace;">
                  FreightIQ · Dry Bulk Morning Briefing
                </div>
                <div class="email-text" style="font-size:24px; color:#1a202c; font-weight:700; margin-top:8px; line-height:1.2;">
                  {date_str}
                </div>
                <div class="email-text-sec" style="font-size:13px; color:#5a6275; margin-top:6px;">
                  Geneva · 07:30 CET · ~10 min read
                </div>
              </td>
            </tr>
          </table>
        </td></tr>

        <tr><td style="padding:20px 32px 8px;" class="email-card">{build_hero_tiles(data)}</td></tr>
        <tr><td style="padding:16px 32px;" class="email-card">{build_overnight_context(data)}</td></tr>
        <tr><td style="padding:0 32px 24px; background:#fafbfc;" class="email-card-alt">{exec_block}</td></tr>
        <tr><td style="padding:24px 32px; background:#ffffff;" class="email-card">{build_real_bdi_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px; background:#ffffff;" class="email-card">{build_subsegment_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_levels_table(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_ffa_curve(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_supply_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_ag_ports_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_bunker_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_demand_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_calendar_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_geo_signals(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_news_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;" class="email-card">{build_watch_section(data)}</td></tr>
        <tr><td style="padding:18px 32px 24px; border-top:1px solid #e2e8f0; background:#fafbfc;" class="email-card-alt">{build_sources()}</td></tr>

        <tr><td style="padding:18px 32px 28px; border-top:1px solid #e2e8f0; font-size:10px; color:#9ba3b4; line-height:1.5;" class="email-text-sec">
          Generated by FreightIQ · automated daily delivery via GitHub Actions cron.
          This briefing is for informational purposes only and does not constitute investment advice.
          Freight indices shown are public-API proxies; institutional Baltic Exchange data requires subscription.
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html


def send_email(subject: str, html: str, to_addr: str, smtp_user: str, smtp_pwd: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(smtp_user, smtp_pwd)
        s.sendmail(smtp_user, [to_addr], msg.as_string())


def main():
    data = fetch_all()

    log.info("Building HTML email…")
    subject, html = build_email(data)

    smtp_user = os.environ.get("GMAIL_USER")
    smtp_pwd  = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr   = os.environ.get("BRIEFING_TO", "virgile.roumens@gmail.com")
    test_mode = os.environ.get("BRIEFING_TEST") == "1" or not (smtp_user and smtp_pwd)

    if test_mode:
        out = Path(__file__).parent / "briefing_preview.html"
        out.write_text(html, encoding="utf-8")
        log.info(f"TEST MODE — preview written to {out}")
        log.info(f"Subject would be: {subject}")
        if not (smtp_user and smtp_pwd):
            log.warning("GMAIL_USER / GMAIL_APP_PASSWORD not configured — set them to enable sending.")
        return

    log.info(f"Sending to {to_addr}…")
    send_email(subject, html, to_addr, smtp_user, smtp_pwd)
    log.info("Email sent successfully.")


if __name__ == "__main__":
    main()
