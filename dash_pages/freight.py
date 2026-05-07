import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import yfinance as yf

from src.config import CACHE_DIR, COLORS, HISTORICAL_EVENTS, SHIPPING_EQUITIES
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.analytics.regime_detector import RegimeDetector
from src.analytics.freight_analytics import (
    rolling_volatility, seasonal_index, compute_freight_statistics,
)
from src.utils.helpers import rolling_zscore
from dash_components.cards import page_header, section_header, info_banner, divider

dash.register_page(__name__, path="/freight", name="Freight Analysis", order=2)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_rd    = RegimeDetector()

# ── BDI calibration from known anchor points ──────────────────────────────────
# BDRY $3.91 (May 2020) ↔ BDI 393  → factor 100×
# BDRY $41.51 (Oct 2021) ↔ BDI 5650 → factor 136×
_BDI_FACTOR_LOW  = 100   # conservative BDI/BDRY multiplier
_BDI_FACTOR_HIGH = 136   # bull-cycle BDI/BDRY multiplier
_BDI_FACTOR_MID  = 118   # mid-cycle estimate

# Static BDI cycle reference (public domain knowledge)
_BDI_ANCHORS = [
    {"date": "2008-05-20", "bdi": 11793, "label": "2008 Super-Cycle Peak",       "color": "#f85149"},
    {"date": "2016-02-10", "bdi":   290, "label": "2016 All-Time Low",            "color": "#3fb950"},
    {"date": "2020-05-13", "bdi":   393, "label": "2020 COVID Trough",            "color": "#3fb950"},
    {"date": "2021-10-07", "bdi":  5650, "label": "2021 Congestion Boom Peak",    "color": "#f85149"},
    {"date": "2023-06-01", "bdi":  1044, "label": "2023 Post-Boom Low",           "color": "#d29922"},
    {"date": "2024-03-01", "bdi":  2076, "label": "2024 Red Sea Premium",         "color": "#58a6ff"},
]

# ── Shared Bloomberg-style layout helpers ─────────────────────────────────────

_RANGE_BUTTONS = [
    dict(count=3,  label="3M",  step="month", stepmode="backward"),
    dict(count=6,  label="6M",  step="month", stepmode="backward"),
    dict(count=1,  label="YTD", step="year",  stepmode="todate"),
    dict(count=1,  label="1Y",  step="year",  stepmode="backward"),
    dict(count=2,  label="2Y",  step="year",  stepmode="backward"),
    dict(count=5,  label="5Y",  step="year",  stepmode="backward"),
    dict(step="all", label="ALL"),
]

_RANGESELECTOR_STYLE = dict(
    buttons=_RANGE_BUTTONS,
    bgcolor=COLORS["bg_secondary"],
    bordercolor=COLORS["border"],
    borderwidth=1,
    activecolor=COLORS["accent_blue"],
    font=dict(color=COLORS["text_secondary"], size=11,
              family="'IBM Plex Mono','Courier New',monospace"),
    x=0, y=1.03, xanchor="left", yanchor="bottom",
)

_SPIKE_XAXIS = dict(
    showspikes=True,
    spikemode="across",
    spikethickness=1,
    spikecolor=COLORS["text_faint"],
    spikedash="solid",
    spikesnap="cursor",
    tickformat="%b '%y",
    tickfont=dict(size=10, color=COLORS["text_secondary"]),
    rangeslider=dict(visible=False),
)

_LEGEND_STYLE = dict(
    orientation="v",
    x=1.01, y=1.0, yanchor="top",
    font=dict(size=10, family="'IBM Plex Mono',monospace"),
    bgcolor="rgba(22,27,34,0.92)",
    bordercolor=COLORS["border"],
    borderwidth=1,
)

def _load_bdry_ffa() -> tuple[pd.Series, list]:
    """Fetch BDRY history + options-derived forward curve (put-call parity)."""
    try:
        raw = yf.download("BDRY", period="5y", auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)
        price = raw["Close"].dropna()
        spot  = float(price.iloc[-1])
        fwd   = [{"date": price.index[-1], "price": spot, "oi": None, "type": "spot"}]
        tkr   = yf.Ticker("BDRY")
        for exp in tkr.options:
            try:
                chain  = tkr.option_chain(exp)
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
                    "date":   pd.Timestamp(exp),
                    "price":  k + c_px - p_px,   # put-call parity: F = K + C - P
                    "oi":     int(c_row.iloc[0].get("openInterest", 0) or 0)
                              + int(p_row.iloc[0].get("openInterest", 0) or 0),
                    "type":   "forward",
                    "expiry": exp,
                })
            except Exception:
                continue
        return price, fwd
    except Exception:
        return pd.Series(dtype=float), []


def _build_ffa_tab(bdry: pd.Series, fwd_points: list) -> list:
    """Build all components for the FFA & Forward Curve tab."""

    spot = float(bdry.iloc[-1]) if not bdry.empty else None

    # ── Methodology card ──────────────────────────────────────────────────────
    weights_rows = [
        html.Tr([
            html.Th("Ticker"), html.Th("Name"), html.Th("Weight"),
            html.Th("Segment Focus"), html.Th("Role in Composite"),
        ], style={"font-size": "0.72rem"}),
    ]
    roles = {
        "BDRY": ("Capesize + Panamax", "Holds 5TC Capes & 4TC Panamax FFA futures — IS the BDI proxy"),
        "SBLK": ("Diversified",        "Largest US-listed dry bulk operator; largest Capesize exposure"),
        "NMM":  ("Diversified",        "Navios Maritime MLP; Panamax/Supramax diversified"),
        "EGLE": ("Supramax/Ultramax",  "Pure-play Supramax; tracks BSI sub-index"),
        "GNK":  ("Capesize/Ultramax",  "Genco; Capesize + Ultramax mix"),
        "DSX":  ("Panamax",            "Diana; mid-size Panamax operator"),
        "SB":   ("Panamax",            "Safe Bulkers; long-haul Panamax/Kamsarmax"),
    }
    for ticker, info in SHIPPING_EQUITIES.items():
        seg, role = roles.get(ticker, ("—", "—"))
        color = info.get("color", COLORS["accent_blue"])
        weights_rows.append(html.Tr([
            html.Td(html.Span(ticker, style={"color": color, "font-weight": "700",
                                             "font-family": "var(--font-mono)"})),
            html.Td(info["name"],  style={"font-size": "0.75rem"}),
            html.Td(f"{info['weight']*100:.0f}%", style={"font-family": "var(--font-mono)",
                                                           "font-weight": "600"}),
            html.Td(seg,  style={"font-size": "0.72rem", "color": "var(--text-secondary)"}),
            html.Td(role, style={"font-size": "0.68rem", "color": "var(--text-faint)"}),
        ]))

    methodology_card = dbc.Card(dbc.CardBody([
        html.H6("How the FreightIQ Composite is built",
                style={"font-family": "var(--font-mono)", "font-size": "0.82rem",
                       "color": "var(--text-primary)", "margin-bottom": "10px"}),
        html.P(
            "The Baltic Exchange charges ~£30,000/yr for real BDI data. "
            "No free API exists. FreightIQ constructs a proxy composite from "
            "publicly traded instruments that derive their value from the same "
            "underlying dry bulk freight market. Each series is normalised to 100 "
            "at the start date; the composite is a weighted average of those normalised levels.",
            style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                   "font-family": "var(--font-mono)", "margin-bottom": "12px"},
        ),
        html.Div(
            html.Table(weights_rows, className="fiq-table", style={"width": "100%"}),
            style={"overflow-x": "auto"},
        ),
        html.P(
            "⚡ BDRY (42% weight) is not an equity — it is an ETF that holds near-dated "
            "CME 5TC Capesize FFA futures and Panamax Route 4 FFA futures. "
            "It is the closest free equivalent to the 5TC Capes FFA screen used on a trading desk.",
            style={"font-size": "0.70rem", "color": COLORS["accent_yellow"],
                   "font-family": "var(--font-mono)", "margin-top": "10px",
                   "border-left": f"3px solid {COLORS['accent_yellow']}",
                   "padding-left": "10px"},
        ),
    ]), className="mb-3")

    # ── BDRY as 5TC Capes FFA proxy — history chart ───────────────────────────
    bdry_fig = go.Figure()
    if not bdry.empty:
        latest = bdry.index[-1]
        norm_dt = latest - pd.DateOffset(years=1)

        # BDRY price (left axis)
        bdry_fig.add_trace(go.Scatter(
            x=bdry.index, y=bdry.round(3).values,
            name="BDRY ETF (5TC Capes FFA)",
            line=dict(color=COLORS["accent_blue"], width=2),
            fill="tozeroy", fillcolor="rgba(88,166,255,0.06)",
            hovertemplate="<b>%{x|%d %b %Y}</b>  BDRY: <b>$%{y:.2f}</b><extra></extra>",
        ))
        for w, col, dash_sty, lbl in [
            (50,  COLORS["accent_yellow"], "dash", "MA 50"),
            (200, COLORS["accent_orange"], "dot",  "MA 200"),
        ]:
            ma = bdry.rolling(w).mean().dropna()
            if not ma.empty:
                bdry_fig.add_trace(go.Scatter(
                    x=ma.index, y=ma.round(3).values, name=lbl,
                    line=dict(color=col, width=1.1, dash=dash_sty), opacity=0.8,
                    hovertemplate=f"{lbl}: $%{{y:.2f}}<extra></extra>",
                ))

        # Implied BDI band (right axis / secondary)
        bdi_mid  = (bdry * _BDI_FACTOR_MID).round(0)
        bdi_low  = (bdry * _BDI_FACTOR_LOW).round(0)
        bdi_high = (bdry * _BDI_FACTOR_HIGH).round(0)
        bdry_fig.add_trace(go.Scatter(
            x=bdi_high.index, y=bdi_high.values, name="BDI implied (high)",
            line=dict(color="rgba(210,153,34,0.0)"), showlegend=False,
            yaxis="y2", hoverinfo="skip",
        ))
        bdry_fig.add_trace(go.Scatter(
            x=bdi_low.index, y=bdi_low.values, name="BDI implied range",
            fill="tonexty", fillcolor="rgba(210,153,34,0.08)",
            line=dict(color="rgba(210,153,34,0.0)"),
            yaxis="y2",
            hovertemplate="BDI implied: %{y:,.0f}<extra></extra>",
        ))

        # BDI anchor annotations
        for anchor in _BDI_ANCHORS:
            try:
                a_date = pd.Timestamp(anchor["date"])
                if a_date < bdry.index[0] or a_date > bdry.index[-1]:
                    continue
                # Find closest BDRY price on that date
                idx = bdry.index.searchsorted(a_date)
                idx = min(idx, len(bdry) - 1)
                bdry_val = float(bdry.iloc[idx])
                bdry_fig.add_annotation(
                    x=a_date, y=bdry_val, yref="y",
                    text=f"BDI {anchor['bdi']:,}",
                    showarrow=True, arrowhead=2, arrowsize=0.8,
                    arrowcolor=anchor["color"], arrowwidth=1.2,
                    ax=0, ay=-28,
                    font=dict(size=8, color=anchor["color"],
                              family="'IBM Plex Mono',monospace"),
                    bgcolor="rgba(13,17,23,0.85)",
                    bordercolor=anchor["color"], borderwidth=1,
                )
            except Exception:
                pass

        bdry_fig.update_xaxes(
            **_SPIKE_XAXIS,
            rangeselector=_RANGESELECTOR_STYLE,
            range=[norm_dt.isoformat(), latest.isoformat()],
        )
        bdry_fig.update_yaxes(
            title_text="BDRY ETF Price ($)",
            tickprefix="$", gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
        )
        bdry_fig.update_layout(
            height=420,
            hovermode="x unified",
            legend=_LEGEND_STYLE,
            margin=dict(l=58, r=160, t=54, b=42),
            plot_bgcolor=COLORS["bg_primary"],
            paper_bgcolor=COLORS["bg_card"],
            yaxis2=dict(
                title="Implied BDI (est.)",
                overlaying="y", side="right",
                tickfont=dict(size=9, color=COLORS["accent_yellow"]),
                tickformat=",",
                gridcolor="rgba(0,0,0,0)",
                showgrid=False,
            ),
        )

    # ── FFA Forward Curve ─────────────────────────────────────────────────────
    fwd_fig = go.Figure()
    if fwd_points and spot:
        dates  = [p["date"] for p in fwd_points]
        prices = [p["price"] for p in fwd_points]
        types  = [p["type"]  for p in fwd_points]
        ois    = [p.get("oi") for p in fwd_points]

        # Spot point
        spot_pts = [(d, pr) for d, pr, t in zip(dates, prices, types) if t == "spot"]
        if spot_pts:
            fwd_fig.add_trace(go.Scatter(
                x=[spot_pts[0][0]], y=[spot_pts[0][1]],
                mode="markers+text",
                marker=dict(size=12, color=COLORS["accent_blue"], symbol="circle"),
                text=[f"  Spot ${spot_pts[0][1]:.2f}"],
                textposition="middle right",
                textfont=dict(size=9, color=COLORS["accent_blue"],
                              family="'IBM Plex Mono',monospace"),
                name="BDRY Spot",
                hovertemplate="BDRY Spot: <b>$%{y:.2f}</b><br>BDI est: <b>%{customdata:,.0f}</b><extra></extra>",
                customdata=[spot * _BDI_FACTOR_MID],
            ))

        # Forward points
        fwd_pts = [(d, pr, oi) for d, pr, t, oi in zip(dates, prices, types, ois) if t == "forward"]
        if fwd_pts:
            fd, fp, foi = zip(*fwd_pts)
            implied_bdi = [p * _BDI_FACTOR_MID for p in fp]
            hover = [
                f"Expiry: {d.strftime('%b %Y')}<br>Fwd BDRY: <b>${p:.2f}</b>"
                f"<br>Implied BDI: <b>{int(p * _BDI_FACTOR_MID):,}</b>"
                f"<br>BDI range: {int(p * _BDI_FACTOR_LOW):,}–{int(p * _BDI_FACTOR_HIGH):,}"
                f"<br>Open Interest: {oi or 'N/A'}"
                for d, p, oi in zip(fd, fp, foi)
            ]
            fwd_fig.add_trace(go.Scatter(
                x=list(fd), y=list(fp),
                mode="markers+lines",
                marker=dict(size=10, color=COLORS["accent_yellow"], symbol="diamond"),
                line=dict(color=COLORS["accent_yellow"], width=1.5, dash="dash"),
                name="BDRY Forward (put-call parity)",
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            ))
            # BDI implied range band
            fwd_fig.add_trace(go.Scatter(
                x=list(fd), y=[p * _BDI_FACTOR_HIGH for p in fp],
                mode="lines", line=dict(color="rgba(0,0,0,0)"),
                showlegend=False, yaxis="y2", hoverinfo="skip",
            ))
            fwd_fig.add_trace(go.Scatter(
                x=list(fd), y=[p * _BDI_FACTOR_LOW for p in fp],
                fill="tonexty", fillcolor="rgba(210,153,34,0.12)",
                line=dict(color="rgba(0,0,0,0)"),
                name="Implied BDI range", yaxis="y2",
                hovertemplate="BDI range: %{y:,.0f}<extra></extra>",
            ))

        # Extend spot line to first forward point
        if spot_pts and fwd_pts:
            fwd_fig.add_trace(go.Scatter(
                x=[spot_pts[0][0], fwd_pts[0][0]],
                y=[spot_pts[0][1], fwd_pts[0][1]],
                mode="lines",
                line=dict(color=COLORS["accent_blue"], width=1, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))

        fwd_fig.update_xaxes(
            tickformat="%b '%y",
            tickfont=dict(size=10, color=COLORS["text_secondary"]),
            showspikes=True, spikemode="across",
            spikethickness=1, spikecolor=COLORS["text_faint"],
        )
        fwd_fig.update_yaxes(
            title_text="BDRY Price ($)", tickprefix="$",
            gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
        )
        fwd_fig.update_layout(
            height=320,
            hovermode="x unified",
            legend=dict(x=1.01, y=1, font=dict(size=9), bgcolor="rgba(22,27,34,0.9)",
                        bordercolor=COLORS["border"], borderwidth=1),
            margin=dict(l=58, r=160, t=16, b=42),
            plot_bgcolor=COLORS["bg_primary"],
            paper_bgcolor=COLORS["bg_card"],
            yaxis2=dict(
                title="Implied BDI range",
                overlaying="y", side="right",
                tickfont=dict(size=9, color=COLORS["accent_yellow"]),
                tickformat=",", gridcolor="rgba(0,0,0,0)", showgrid=False,
            ),
        )

    # ── Historical BDI reference table ────────────────────────────────────────
    anchor_rows = []
    for a in _BDI_ANCHORS:
        col = a["color"]
        anchor_rows.append(html.Tr([
            html.Td(a["date"][:7], style={"font-family": "var(--font-mono)",
                                          "font-size": "0.75rem"}),
            html.Td(f'{a["bdi"]:,}', style={"font-family": "var(--font-mono)",
                                             "font-weight": "700", "color": col}),
            html.Td(f'${a["bdi"]/_BDI_FACTOR_MID:.1f}',
                    style={"font-family": "var(--font-mono)", "font-size": "0.75rem",
                           "color": "var(--text-secondary)"}),
            html.Td(a["label"], style={"font-size": "0.72rem",
                                       "color": "var(--text-secondary)"}),
        ]))
    if spot:
        anchor_rows.append(html.Tr([
            html.Td("Today", style={"font-family": "var(--font-mono)",
                                    "font-size": "0.75rem",
                                    "color": COLORS["accent_blue"]}),
            html.Td(f'{int(spot * _BDI_FACTOR_MID):,}*',
                    style={"font-family": "var(--font-mono)", "font-weight": "700",
                           "color": COLORS["accent_blue"]}),
            html.Td(f'${spot:.2f}',
                    style={"font-family": "var(--font-mono)", "font-size": "0.75rem",
                           "color": COLORS["accent_blue"]}),
            html.Td("BDRY-implied estimate",
                    style={"font-size": "0.72rem", "color": COLORS["accent_blue"]}),
        ], style={"border-top": f"1px solid {COLORS['accent_blue']}"}))

    return [
        info_banner(
            "⚡ BDRY (Breakwave Dry Bulk Shipping ETF) holds near-dated CME 5TC Capesize FFA futures "
            "and Panamax Route 4 FFA futures — it IS a traded FFA product, not an equity. "
            "Real FFA screens (Baltic Exchange / broker platforms) require subscription. "
            "Forward curve below derived from BDRY options via put-call parity (indicative only — low liquidity)."
        ),

        dbc.Row([
            dbc.Col([
                section_header("FreightIQ Composite — Methodology & Basket"),
                methodology_card,
            ], md=12),
        ], className="mb-3"),

        divider(),

        dbc.Row([
            dbc.Col([
                section_header("BDRY ETF — 5TC Capes FFA Proxy  ·  Price History with Implied BDI"),
                html.P(
                    "BDRY tracks near-dated 5TC Capesize FFA (75%) + Panamax T/C Route 4 FFA (25%). "
                    "Right axis shows estimated BDI level derived from BDRY price "
                    f"(calibration: $3.91→BDI 393 in May-2020; $41.51→BDI 5,650 in Oct-2021; "
                    f"mid factor {_BDI_FACTOR_MID}×).",
                    style={"font-size": "0.70rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "margin-bottom": "10px"},
                ),
                dbc.Card(dbc.CardBody(dcc.Graph(
                    figure=bdry_fig,
                    config={"displayModeBar": True, "displaylogo": False,
                            "toImageButtonOptions": {"format": "png", "width": 1600, "height": 600}},
                ))),
            ], md=8),

            dbc.Col([
                section_header("Historical BDI Cycle Reference"),
                html.P(
                    "* Today's BDI estimate derived from BDRY × 118 (mid-cycle factor). "
                    "Range: BDRY × 100 (bear) to × 136 (bull).",
                    style={"font-size": "0.68rem", "color": "var(--text-faint)",
                           "font-family": "var(--font-mono)", "margin-bottom": "8px"},
                ),
                dbc.Card(dbc.CardBody(html.Div(
                    html.Table([
                        html.Thead(html.Tr([
                            html.Th("Date"), html.Th("BDI"),
                            html.Th("BDRY equiv"), html.Th("Context"),
                        ])),
                        html.Tbody(anchor_rows),
                    ], className="fiq-table"),
                    style={"overflow-x": "auto"},
                ))),
            ], md=4),
        ], className="g-3 mb-3"),

        divider(),

        dbc.Row([
            dbc.Col([
                section_header("FFA Forward Curve — BDRY Options Term Structure (indicative)"),
                html.P(
                    "Forward BDRY price = K + C − P (put-call parity on ATM options). "
                    "Right axis shows BDI-implied range (×100 / ×136). "
                    "⚠ BDRY options are thinly traded — treat as directional indication only. "
                    "For live FFA quotes, use Baltic Exchange screens or broker platforms (Freight Investor Services, Marex).",
                    style={"font-size": "0.70rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "margin-bottom": "10px"},
                ),
                dbc.Card(dbc.CardBody(dcc.Graph(
                    figure=fwd_fig,
                    config={"displayModeBar": False},
                ))),
            ], md=12),
        ], className="g-3"),
    ]


_TICKER_COLORS = {
    "BDRY": "#58a6ff", "SBLK": "#d29922", "GNK": "#79c0ff",
    "DSX":  "#56d364", "EGLE": "#db6d28", "NMM": "#bc8cff", "SB": "#ff7b72",
}
_TICKER_FULL = {
    "BDRY": "Breakwave Dry Bulk ETF",  "SBLK": "Star Bulk Carriers",
    "GNK":  "Genco Shipping & Trading","DSX":  "Diana Shipping",
    "EGLE": "Eagle Bulk Shipping",     "NMM":  "Navios Maritime Partners",
    "SB":   "Safe Bulkers",
}


def layout(**kwargs):
    try:
        equities  = _fdm.get_shipping_equities(period="5y")
        composite = _fdm.get_weighted_shipping_index(period="5y")
        bdi       = _fdm.get_bdi_history(start="2015-01-01")
        bdry, fwd_points = _load_bdry_ffa()
    except Exception:
        equities = pd.DataFrame()
        composite = pd.Series(dtype=float)
        bdi = pd.DataFrame()
        bdry, fwd_points = pd.Series(dtype=float), []

    # ── Primary series: BDI history if available, else composite ──────────────
    primary = bdi["value"] if (not bdi.empty and "value" in bdi.columns) else composite
    source_label = (
        "DataHub BDI (historical)"
        if (not bdi.empty and "value" in bdi.columns
            and "source" in bdi.columns
            and (bdi["source"] == "datahub").any())
        else "FreightIQ Composite [PROXY]"
    )

    # ── Date anchors ──────────────────────────────────────────────────────────
    if not primary.empty:
        latest_date   = primary.index[-1]
        default_start = latest_date - pd.DateOffset(years=1)
    else:
        latest_date   = pd.Timestamp.now()
        default_start = latest_date - pd.DateOffset(years=1)

    # ── 1Y statistics ─────────────────────────────────────────────────────────
    _1y_ago = pd.Timestamp.now() - pd.DateOffset(years=1)
    bdi_1y  = primary.loc[primary.index >= _1y_ago] if not primary.empty else pd.Series(dtype=float)
    hi1y   = float(bdi_1y.max())  if not bdi_1y.empty else None
    lo1y   = float(bdi_1y.min())  if not bdi_1y.empty else None
    avg1y  = float(bdi_1y.mean()) if not bdi_1y.empty else None
    cur    = float(primary.iloc[-1]) if not primary.empty else None
    pctile = float((primary <= cur).mean() * 100) if (cur is not None and not primary.empty) else None

    regime      = _rd.detect_phase(primary) if not primary.empty else {
        "phase": "NEUTRAL", "label": "N/A", "color": COLORS["accent_blue"],
        "emoji": "➡️", "description": "N/A", "metrics": {},
    }
    phase_color = regime.get("color", COLORS["accent_blue"])

    # ── Main time series chart ─────────────────────────────────────────────────
    main_fig = go.Figure()
    if not primary.empty:
        main_fig.add_trace(go.Scatter(
            x=primary.index, y=primary.round(2).values,
            name=source_label,
            line=dict(color=COLORS["accent_blue"], width=2),
            fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
            hovertemplate="<b>%{x|%d %b %Y}</b><br>Value: <b>%{y:,.1f}</b><extra></extra>",
        ))
        for w, color, dash_style, lbl in [
            (50,  COLORS["accent_yellow"], "dash", "MA 50"),
            (200, COLORS["accent_orange"], "dot",  "MA 200"),
        ]:
            ma = primary.rolling(w).mean().dropna()
            if not ma.empty:
                main_fig.add_trace(go.Scatter(
                    x=ma.index, y=ma.round(2).values,
                    name=lbl, line=dict(color=color, width=1.2, dash=dash_style),
                    opacity=0.8,
                    hovertemplate=f"{lbl}: %{{y:,.0f}}<extra></extra>",
                ))

    for ev in HISTORICAL_EVENTS:
        try:
            if not primary.empty and pd.Timestamp(ev["date"]) < primary.index[0]:
                continue
        except Exception:
            pass
        main_fig.add_shape(
            type="line", x0=ev["date"], x1=ev["date"],
            y0=0, y1=1, yref="paper",
            line=dict(width=1, dash="dot", color=ev["color"]),
        )
        main_fig.add_annotation(
            x=ev["date"], y=0.97, yref="paper", text=ev["label"],
            showarrow=False, textangle=-90, xanchor="right",
            font=dict(size=8, color=ev["color"]),
        )

    main_fig.update_xaxes(
        **_SPIKE_XAXIS,
        rangeselector=_RANGESELECTOR_STYLE,
        range=[default_start.isoformat(), latest_date.isoformat()],
    )
    main_fig.update_yaxes(
        title_text=source_label,
        gridcolor="rgba(48,54,61,0.35)",
        tickfont=dict(size=10),
    )
    main_fig.update_layout(
        height=440,
        hovermode="x unified",
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=160, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )

    # ── KPI cards ─────────────────────────────────────────────────────────────
    def _kpi(label, val, sub=""):
        return dbc.Col(html.Div([
            html.Div(label, className="kpi-label"),
            html.Div(val,   className="kpi-value"),
            html.Div(sub,   className="kpi-delta kpi-delta-neu"),
        ], className="kpi-card"), width=6, md=True)

    kpi_row = dbc.Row([
        _kpi("Current Level",       f"{cur:,.1f}"    if cur      else "N/A", source_label),
        _kpi("1Y High",             f"{hi1y:,.0f}"   if hi1y     else "N/A"),
        _kpi("1Y Low",              f"{lo1y:,.0f}"   if lo1y     else "N/A"),
        _kpi("1Y Avg",              f"{avg1y:,.0f}"  if avg1y    else "N/A"),
        _kpi("Pctile (full hist.)", f"{pctile:.0f}th" if pctile is not None else "N/A",
             f'{regime.get("emoji","")} {regime.get("label","")}'),
    ], className="g-2 mb-3")

    # ── Volatility chart ───────────────────────────────────────────────────────
    vol_fig = go.Figure()
    if not primary.empty:
        vol30 = rolling_volatility(primary, 30) * 100
        vol90 = rolling_volatility(primary, 90) * 100
        v_end   = primary.index[-1]
        v_start = v_end - pd.DateOffset(years=2)
        vol_fig.add_trace(go.Scatter(
            x=vol30.index, y=vol30.round(2).values,
            name="30D Vol",
            line=dict(color=COLORS["accent_blue"], width=1.5),
            hovertemplate="30D Ann. Vol: %{y:.1f}%<extra></extra>",
        ))
        vol_fig.add_trace(go.Scatter(
            x=vol90.index, y=vol90.round(2).values,
            name="90D Vol",
            line=dict(color=COLORS["accent_orange"], width=1.5, dash="dash"),
            hovertemplate="90D Ann. Vol: %{y:.1f}%<extra></extra>",
        ))
        vol_fig.update_xaxes(
            **_SPIKE_XAXIS,
            rangeselector=dict(
                buttons=[
                    dict(count=1, label="1Y", step="year", stepmode="backward"),
                    dict(count=2, label="2Y", step="year", stepmode="backward"),
                    dict(step="all", label="ALL"),
                ],
                bgcolor=COLORS["bg_secondary"], bordercolor=COLORS["border"],
                borderwidth=1, activecolor=COLORS["accent_blue"],
                font=dict(color=COLORS["text_secondary"], size=11,
                          family="'IBM Plex Mono',monospace"),
                x=0, y=1.03, xanchor="left", yanchor="bottom",
            ),
            range=[v_start.isoformat(), v_end.isoformat()],
        )
    vol_fig.update_yaxes(
        title_text="Ann. Vol %", ticksuffix="%",
        gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
    )
    vol_fig.update_layout(
        height=280, hovermode="x unified",
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=120, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Z-score chart ──────────────────────────────────────────────────────────
    z_fig = go.Figure()
    if not primary.empty and len(primary) > 52:
        zs      = rolling_zscore(primary, 52)
        z_end   = primary.index[-1]
        z_start = z_end - pd.DateOffset(years=2)
        z_fig.add_hrect(y0=1,  y1=2,  fillcolor=COLORS["accent_yellow"], opacity=0.06, line_width=0)
        z_fig.add_hrect(y0=2,  y1=4,  fillcolor=COLORS["accent_red"],    opacity=0.08, line_width=0)
        z_fig.add_hrect(y0=-2, y1=-1, fillcolor=COLORS["accent_yellow"], opacity=0.06, line_width=0)
        z_fig.add_hrect(y0=-4, y1=-2, fillcolor=COLORS["accent_green"],  opacity=0.08, line_width=0)
        z_fig.add_hline(y=2,  line_dash="dot",  line_color=COLORS["accent_red"],   opacity=0.5,
                        annotation_text="+2σ overbought", annotation_position="top right",
                        annotation_font=dict(color=COLORS["accent_red"], size=9))
        z_fig.add_hline(y=-2, line_dash="dot",  line_color=COLORS["accent_green"], opacity=0.5,
                        annotation_text="−2σ oversold",  annotation_position="bottom right",
                        annotation_font=dict(color=COLORS["accent_green"], size=9))
        z_fig.add_hline(y=0,  line_dash="dash", line_color=COLORS["text_faint"],   opacity=0.4)
        z_fig.add_trace(go.Scatter(
            x=zs.index, y=zs.values,
            name="52W Z-Score",
            line=dict(color=COLORS["accent_blue"], width=1.6),
            fill="tozeroy", fillcolor="rgba(88,166,255,0.08)",
            hovertemplate="Z-Score: <b>%{y:.2f}σ</b><br>%{x|%d %b %Y}<extra></extra>",
        ))
        z_fig.update_xaxes(
            **_SPIKE_XAXIS,
            rangeselector=dict(
                buttons=[
                    dict(count=1, label="1Y", step="year", stepmode="backward"),
                    dict(count=2, label="2Y", step="year", stepmode="backward"),
                    dict(step="all", label="ALL"),
                ],
                bgcolor=COLORS["bg_secondary"], bordercolor=COLORS["border"],
                borderwidth=1, activecolor=COLORS["accent_blue"],
                font=dict(color=COLORS["text_secondary"], size=11,
                          family="'IBM Plex Mono',monospace"),
                x=0, y=1.03, xanchor="left", yanchor="bottom",
            ),
            range=[z_start.isoformat(), z_end.isoformat()],
        )
    z_fig.update_yaxes(
        title_text="Z-Score (σ)",
        zeroline=True, zerolinecolor=COLORS["text_faint"], zerolinewidth=0.7,
        gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
    )
    z_fig.update_layout(
        height=280, showlegend=False, hovermode="x unified",
        margin=dict(l=58, r=80, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Seasonality ───────────────────────────────────────────────────────────
    seas_fig = None
    if not composite.empty and len(composite) > 252:
        seas = seasonal_index(composite)
        if not seas.empty:
            seas_vals  = ((seas["seasonal_index"] - 1) * 100).round(2)
            colors_bar = [
                COLORS["accent_green"] if v >= 0 else COLORS["accent_red"]
                for v in seas_vals
            ]
            seas_fig = go.Figure(go.Bar(
                x=seas["month_name"], y=seas_vals,
                marker_color=colors_bar,
                text=[f"{v:+.1f}%" for v in seas_vals],
                textposition="outside",
                textfont=dict(size=10, family="'IBM Plex Mono',monospace"),
                hovertemplate="<b>%{x}</b><br>Seasonal: <b>%{y:+.1f}%</b><extra></extra>",
            ))
            seas_fig.add_hline(y=0, line_color=COLORS["text_faint"], line_width=0.8, opacity=0.5)
            seas_fig.update_yaxes(
                title_text="% vs annual mean", ticksuffix="%",
                gridcolor="rgba(48,54,61,0.35)",
            )
            seas_fig.update_layout(
                height=300, showlegend=False,
                margin=dict(l=58, r=20, t=16, b=42),
                plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
            )

    # ── Equity overlay — Bloomberg % return from 1Y ago ───────────────────────
    eq_fig    = go.Figure()
    eq_annots = []
    if not equities.empty:
        eq_tickers = [t for t in list(SHIPPING_EQUITIES.keys())[:7] if t in equities.columns]
        valid_eq   = [(t, equities[t].dropna()) for t in eq_tickers if not equities[t].dropna().empty]
        if valid_eq:
            eq_latest    = max(s.index[-1] for _, s in valid_eq)
            eq_norm_base = eq_latest - pd.DateOffset(years=1)

            for t, s in valid_eq:
                color      = _TICKER_COLORS.get(t, COLORS["accent_blue"])
                earlier    = s[s.index <= eq_norm_base]
                base_price = float(earlier.iloc[-1]) if not earlier.empty else float(s.iloc[0])
                pct_ret    = (s / base_price - 1) * 100
                latest_ret = float(pct_ret.iloc[-1])

                eq_fig.add_trace(go.Scatter(
                    x=pct_ret.index, y=pct_ret.round(2).values,
                    name=t,
                    line=dict(color=color, width=1.8),
                    hovertemplate=(
                        f"<b>{t}</b>  {_TICKER_FULL.get(t,'')}<br>"
                        "%{x|%d %b %Y}  <b>%{y:+.1f}%</b><extra></extra>"
                    ),
                ))
                eq_annots.append(dict(
                    x=eq_latest, y=latest_ret,
                    xref="x", yref="y",
                    text=f"  <b>{t}</b>  {latest_ret:+.1f}%",
                    font=dict(size=9, color=color, family="'IBM Plex Mono',monospace"),
                    showarrow=False, xanchor="left", yanchor="middle",
                ))

            eq_fig.add_hline(y=0, line_color=COLORS["text_faint"], line_width=0.7, opacity=0.55)
            eq_fig.update_xaxes(
                **_SPIKE_XAXIS,
                rangeselector=_RANGESELECTOR_STYLE,
                range=[eq_norm_base.isoformat(), eq_latest.isoformat()],
            )
            eq_fig.update_yaxes(
                title_text="Return from 1Y ago", ticksuffix="%",
                zeroline=True, zerolinecolor=COLORS["text_faint"], zerolinewidth=0.8,
                gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
            )

    eq_fig.update_layout(
        height=440,
        hovermode="x unified",
        hoverdistance=30,
        annotations=eq_annots,
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=200, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )

    # ── Build page ─────────────────────────────────────────────────────────────
    return html.Div([
        page_header("🚢 Freight Analysis",
                    "BDI proxy time series · volatility · z-score · seasonality · equity overlay"),

        dcc.Tabs(
            id="freight-tabs", value="tab-ts",
            className="tabs-bar",
            children=[
                dcc.Tab(label="📈 Time Series",    value="tab-ts",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🔮 FFA & Forward",  value="tab-ffa",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📊 Volatility",     value="tab-vol",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🌡 Seasonality",    value="tab-seas",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🚢 Equity Overlay", value="tab-eq",
                        className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # ── Tab: Time Series ──────────────────────────────────────────────────
        html.Div([
            info_banner(
                "📌 What is the FreightIQ Composite? "
                "An equal-weighted basket of BDRY ETF (42% — holds 5TC Capes FFA futures) + "
                "dry bulk shipping equities (SBLK, NMM, EGLE, GNK, DSX, SB). "
                "Each constituent is normalised to 100 at the start date; the composite is a "
                "weighted average of those levels. Real BDI data (Baltic Exchange) requires a "
                "£30,000/yr subscription — no free API exists. "
                "See the 🔮 FFA & Forward tab for full methodology and implied BDI levels."
            ),
            kpi_row,
            html.Div([
                html.Span(
                    f'{regime.get("emoji","")} Market Regime: ',
                    style={"font-family": "var(--font-mono)", "font-size": "0.8rem",
                           "color": "var(--text-secondary)"},
                ),
                html.Span(
                    regime.get("label", "N/A"),
                    style={"font-family": "var(--font-mono)", "font-size": "0.88rem",
                           "font-weight": "700", "color": phase_color},
                ),
                html.Span(
                    f' — {regime.get("description","")[:80]}',
                    style={"font-family": "var(--font-mono)", "font-size": "0.75rem",
                           "color": "var(--text-secondary)"},
                ),
            ], className="fiq-card",
               style={"border-left": f"3px solid {phase_color}",
                      "padding": "10px 16px", "margin-bottom": "14px"}),
            dbc.Card(dbc.CardBody(dcc.Graph(
                figure=main_fig,
                config={"displayModeBar": True, "displaylogo": False,
                        "modeBarButtonsToRemove": ["autoScale2d", "lasso2d", "select2d"],
                        "toImageButtonOptions": {"format": "png", "width": 1600, "height": 700}},
            ))),
        ], id="freight-tab-content-ts"),

        # ── Tab: FFA & Forward Curve ──────────────────────────────────────────
        html.Div(
            _build_ffa_tab(bdry, fwd_points),
            id="freight-tab-content-ffa",
            style={"display": "none"},
        ),

        # ── Tab: Volatility ───────────────────────────────────────────────────
        html.Div([
            dbc.Row([
                dbc.Col([
                    section_header("Rolling Annualised Volatility"),
                    dbc.Card(dbc.CardBody(dcc.Graph(
                        figure=vol_fig,
                        config={"displayModeBar": True, "displaylogo": False},
                    ))),
                ], md=6),
                dbc.Col([
                    section_header("52-Week Z-Score"),
                    dbc.Card(dbc.CardBody(dcc.Graph(
                        figure=z_fig,
                        config={"displayModeBar": True, "displaylogo": False},
                    ))),
                ], md=6),
            ], className="g-3"),
        ], id="freight-tab-content-vol", style={"display": "none"}),

        # ── Tab: Seasonality ──────────────────────────────────────────────────
        html.Div([
            section_header("Monthly Seasonality — Composite Index [PROXY]"),
            html.P(
                "Premium/discount of each calendar month vs. the full-year mean.",
                style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                       "font-family": "var(--font-mono)", "margin-bottom": "14px"},
            ),
            dbc.Card(dbc.CardBody(
                dcc.Graph(figure=seas_fig, config={"displayModeBar": False})
                if seas_fig
                else info_banner("Need at least 1 year of data for seasonality analysis.")
            )),
        ], id="freight-tab-content-seas", style={"display": "none"}),

        # ── Tab: Equity Overlay ───────────────────────────────────────────────
        html.Div([
            section_header("Shipping Equity Basket — % Return from 1Y Ago [PROXY]"),
            html.P(
                "Normalised to % return from 1 year ago. Default view: last 12 months. "
                "Use 2Y / 5Y / ALL to expand.",
                style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                       "font-family": "var(--font-mono)", "margin-bottom": "14px"},
            ),
            dbc.Card(dbc.CardBody(dcc.Graph(
                figure=eq_fig,
                config={"displayModeBar": True, "displaylogo": False,
                        "toImageButtonOptions": {"format": "png", "width": 1600, "height": 560}},
            ))),
        ], id="freight-tab-content-eq", style={"display": "none"}),
    ])


@callback(
    Output("freight-tab-content-ts",   "style"),
    Output("freight-tab-content-ffa",  "style"),
    Output("freight-tab-content-vol",  "style"),
    Output("freight-tab-content-seas", "style"),
    Output("freight-tab-content-eq",   "style"),
    Input("freight-tabs", "value"),
)
def switch_freight_tab(tab: str):
    tabs = ["tab-ts", "tab-ffa", "tab-vol", "tab-seas", "tab-eq"]
    return [{"display": "block"} if t == tab else {"display": "none"} for t in tabs]
