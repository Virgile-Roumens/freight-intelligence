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
import os
import sys
import smtplib
import logging
import warnings
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
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

    return {
        "snapshot":   snap,
        "composite":  composite,
        "bdry_spot":  bdry_spot,
        "bdry_fwd":   bdry_fwd,
        "macro":      macro,
        "articles":   articles or [],
        "signals":    signals or [],
        "ais":        ais_df,
        "regime":     regime,
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

    # ── Lead: BDRY direction + cycle phase ───────────────────────────────────
    bdry     = snap.get("BDRY", {})
    bdry_d1d = bdry.get("delta_1d", 0) or 0
    bdry_d5d = bdry.get("delta_5d", 0) or 0

    if spot:
        if   bdry_d1d >  0.015: action = "extended sharply higher overnight"
        elif bdry_d1d >  0.005: action = "edged higher overnight"
        elif bdry_d1d < -0.015: action = "sold off overnight"
        elif bdry_d1d < -0.005: action = "drifted lower overnight"
        else:                   action = "traded sideways overnight"

        cycle_phrase = {
            "EXPANSION":   " with the cycle still in expansion territory",
            "PEAK":        " though the cycle is showing late-stage peak signals",
            "CONTRACTION": " amid an active cycle contraction",
            "TROUGH":      " near cycle-trough levels",
            "NEUTRAL":     "",
        }.get(regime.get("phase", ""), "")

        five_day = ""
        if   bdry_d5d >  0.05: five_day = f" (+{bdry_d5d*100:.1f}% over 5 sessions — momentum building)"
        elif bdry_d5d < -0.05: five_day = f" ({bdry_d5d*100:+.1f}% over 5 sessions — sustained pressure)"
        elif abs(bdry_d5d) >= 0.02: five_day = f" ({bdry_d5d*100:+.1f}% on the week)"

        sentences.append(
            f"Dry bulk paper {action} — BDRY ETF (closest free 5TC Capes FFA proxy) "
            f"settled at <b>${spot:.2f}</b>{five_day}, implying a BDI level around "
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

    subject_parts = [f"🚢 Dry Bulk Briefing · {today.strftime('%a %d %b')}"]
    if bdry_v and bdry_d is not None:
        subject_parts.append(f"BDRY ${bdry_v:.2f} ({bdry_d*100:+.2f}%)")
    regime = data.get("regime", {})
    if regime.get("label"):
        subject_parts.append(regime["label"])
    subject = " · ".join(subject_parts)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dry Bulk Morning Briefing</title>
</head>
<body style="margin:0; padding:0; background:#eef2f7; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; color:#1a202c; line-height:1.5;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#eef2f7; padding:24px 12px;">
    <tr><td align="center">
      <table cellpadding="0" cellspacing="0" border="0" width="720" style="max-width:720px; background:#ffffff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.06); overflow:hidden;">

        <tr><td style="padding:28px 32px 18px; border-bottom:3px solid #1e3050;">
          <div style="font-size:11px; letter-spacing:0.14em; color:#5b8cbf; text-transform:uppercase; font-weight:700; font-family:'IBM Plex Mono',monospace;">
            FreightIQ · Dry Bulk Morning Briefing
          </div>
          <div style="font-size:23px; color:#1a202c; font-weight:700; margin-top:8px; line-height:1.2;">
            {date_str}
          </div>
          <div style="font-size:13px; color:#5a6275; margin-top:6px;">
            Geneva · 07:30 CET · ~10 min read
          </div>
        </td></tr>

        <tr><td style="padding:24px 32px;">{build_overnight_context(data)}</td></tr>
        <tr><td style="padding:0 32px 24px; background:#fafbfc;">{build_exec_summary(data)}</td></tr>
        <tr><td style="padding:24px 32px; background:#ffffff;">{build_levels_table(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_ffa_curve(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_supply_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_demand_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_geo_signals(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_news_section(data)}</td></tr>
        <tr><td style="padding:0 32px 24px;">{build_watch_section(data)}</td></tr>
        <tr><td style="padding:18px 32px 24px; border-top:1px solid #e2e8f0; background:#fafbfc;">{build_sources()}</td></tr>

        <tr><td style="padding:18px 32px 28px; border-top:1px solid #e2e8f0; font-size:10px; color:#9ba3b4; line-height:1.5;">
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
