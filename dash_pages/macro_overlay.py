import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import dash
from dash import html, dcc
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import yfinance as yf

from src.config import CACHE_DIR, COLORS
from src.utils.cache_manager import CacheManager
from src.data.macro_data import MacroDataManager
from src.data.freight_data import FreightDataManager
from src.data.commodity_data import CommodityDataManager
from src.analytics.regime_detector import RegimeDetector
from dash_components.cards import page_header, section_header, info_banner, divider, kpi_card

dash.register_page(__name__, path="/macro", name="Macro Overlay", order=5)

_cache = CacheManager(CACHE_DIR)
_mdm   = MacroDataManager(_cache)
_fdm   = FreightDataManager(_cache)
_cdm   = CommodityDataManager(_cache)
_rd    = RegimeDetector()

# yfinance fallbacks for each FRED series — used when FRED key is absent
_YF_FALLBACK = {
    "DCOILBRENTEU": "BZ=F",      # Brent crude futures
    "DGS10":        "^TNX",       # 10-year Treasury yield (already in %)
    "DGS2":         "^IRX",       # 13-week T-bill as short-rate proxy
    "DTWEXBGS":     "DX-Y.NYB",   # DXY spot as USD broad index proxy
}


def _load_series(series_id: str, start: str = "2020-01-01") -> pd.Series:
    """Load a macro series: FRED first, yfinance fallback where available."""
    s = _mdm.get_fred_series(series_id, start=start)
    if not s.empty:
        return s
    yf_ticker = _YF_FALLBACK.get(series_id)
    if not yf_ticker:
        return pd.Series(dtype=float, name=series_id)
    try:
        raw = yf.download(yf_ticker, period="3y", auto_adjust=True, progress=False)
        if raw.empty:
            return pd.Series(dtype=float, name=series_id)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)
        close = raw["Close"].dropna()
        close.name = series_id
        return close[close.index >= pd.Timestamp(start)]
    except Exception:
        return pd.Series(dtype=float, name=series_id)


def layout(**kwargs):
    # ── Load all series individually (FRED → yfinance fallback) ───────────────
    try:
        s_brent  = _load_series("DCOILBRENTEU", start="2018-01-01")
        s_dgs10  = _load_series("DGS10",        start="2018-01-01")
        s_dgs2   = _load_series("DGS2",         start="2018-01-01")
        s_usd    = _load_series("DTWEXBGS",     start="2018-01-01")
        s_indpro = _load_series("INDPRO",        start="2010-01-01")
        s_cpi    = _load_series("CPIAUCSL",      start="2010-01-01")
        comm_df  = _mdm.get_commodity_prices(period="2y")
        composite = _fdm.get_weighted_shipping_index(period="2y")
        yc_df    = _mdm.get_yield_curve()
    except Exception:
        s_brent = s_dgs10 = s_dgs2 = s_usd = s_indpro = s_cpi = pd.Series(dtype=float)
        comm_df, composite, yc_df = pd.DataFrame(), pd.Series(dtype=float), pd.DataFrame()

    fred_active = bool(_mdm._fred)

    # ── Macro KPIs ────────────────────────────────────────────────────────────
    def _kpi_from_series(s: pd.Series, label: str, fmt: str, unit: str, source: str = ""):
        s = s.dropna()
        if s.empty:
            sub = "FRED key required" if not fred_active else "Data unavailable"
            return dbc.Col(kpi_card(label, "N/A", subtitle=sub), width=6, md=True)
        val = float(s.iloc[-1])
        d1  = float(s.pct_change(1).iloc[-1]) if len(s) > 1 else None
        val_str = (fmt % val) + (" " + unit if unit else "")
        delta_v = d1 * val if d1 is not None else None
        delta_p = d1 * 100 if d1 is not None else None
        sub     = source if source else ""
        return dbc.Col(kpi_card(label, val_str.strip(), delta_v, delta_p, subtitle=sub), width=6, md=True)

    usd_src  = "" if fred_active else "proxy: DXY"
    rate_src = "" if fred_active else "proxy: ^TNX / ^IRX"

    kpi_cards = [
        _kpi_from_series(s_brent,  "Brent Crude",             "%.2f", "$/bbl",
                         "" if fred_active else "proxy: BZ=F"),
        _kpi_from_series(s_dgs10,  "10Y Treasury Yield",      "%.2f", "%",   rate_src),
        _kpi_from_series(s_dgs2,   "2Y Treasury Yield",       "%.2f", "%",   rate_src),
        _kpi_from_series(s_usd,    "USD Broad Index",         "%.2f", "",    usd_src),
        _kpi_from_series(s_indpro, "US Industrial Production","%.1f", "idx"),
        _kpi_from_series(s_cpi,    "US CPI",                  "%.1f", "idx"),
    ]

    # ── Multi-series overlay chart ────────────────────────────────────────────
    overlay_series = [
        (s_brent,  "Brent Crude",            COLORS["accent_orange"]),
        (s_dgs10,  "10Y Treasury",           COLORS["accent_blue"]),
        (s_usd,    "USD Index",              COLORS["accent_green"]),
        (s_indpro, "Industrial Production",  COLORS["accent_yellow"]),
        (s_cpi,    "US CPI",                 COLORS["accent_purple"]),
    ]
    over_fig = go.Figure()
    for s, label, color in overlay_series:
        s = s.dropna()
        if s.empty:
            continue
        norm = s / s.iloc[0] * 100
        over_fig.add_trace(go.Scatter(
            x=norm.index, y=norm.round(2).values,
            name=label, line=dict(color=color, width=1.8),
            hovertemplate=f"<b>{label}</b> %{{x|%b %Y}}: %{{y:.1f}}<extra></extra>",
        ))
    over_fig.update_xaxes(
        rangeselector=dict(
            buttons=[dict(count=1, label="1Y", step="year", stepmode="backward"),
                     dict(count=3, label="3Y", step="year", stepmode="backward"),
                     dict(count=5, label="5Y", step="year", stepmode="backward"),
                     dict(step="all", label="All")],
            bgcolor=COLORS["bg_card"], activecolor=COLORS["accent_blue"],
            font=dict(color=COLORS["text_primary"], size=11),
        ),
    )
    over_fig.update_layout(
        height=320, hovermode="x unified",
        yaxis_title="Normalised (100 = start)",
        legend=dict(x=1.01, y=1, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        margin=dict(l=44, r=160, t=12, b=40),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Yield curve ──────────────────────────────────────────────────────────
    # get_yield_curve() returns columns "2Y", "5Y", "10Y", "spread_2_10"
    yc_fig = go.Figure()
    if not yc_df.empty:
        tenor_map = [("2Y", COLORS["accent_blue"]), ("5Y", COLORS["accent_green"]),
                     ("10Y", COLORS["accent_orange"])]
        for tenor, color in tenor_map:
            if tenor not in yc_df.columns:
                continue
            s = yc_df[tenor].dropna()
            yc_fig.add_trace(go.Scatter(
                x=s.index, y=s.round(3).values, name=f"{tenor} Yield",
                line=dict(color=color, width=1.6),
                hovertemplate=f"<b>{tenor}</b> %{{x|%d %b %Y}}: %{{y:.2f}}%<extra></extra>",
            ))
        if "spread_2_10" in yc_df.columns:
            spread = yc_df["spread_2_10"].dropna()
            yc_fig.add_trace(go.Scatter(
                x=spread.index, y=spread.round(3).values, name="2/10 Spread",
                line=dict(color=COLORS["accent_red"], width=1.2, dash="dash"),
                hovertemplate="2/10 Spread: <b>%{y:.2f}%</b><extra></extra>",
            ))
            yc_fig.add_hline(y=0, line_dash="dot", line_color=COLORS["text_secondary"], opacity=0.4)
    yc_fig.update_layout(
        height=280, hovermode="x unified", yaxis_title="Yield %", yaxis_ticksuffix="%",
        legend=dict(x=1.01, y=1, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        margin=dict(l=44, r=120, t=12, b=40),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Bunker price section ──────────────────────────────────────────────────
    bunker_est = _cdm.get_bunker_price_estimate()
    bunker_card = kpi_card(
        "VLSFO Bunker (est.)",
        f"${bunker_est:,.0f}/mt" if bunker_est else "N/A",
        subtitle="Brent × 6.5 approximation",
    )

    # ── Freight vs Brent scatter ──────────────────────────────────────────────
    scatter_fig = go.Figure()
    if not s_brent.empty and not composite.empty:
        idx_common = s_brent.index.intersection(composite.index)
        if len(idx_common) > 10:
            x_vals = s_brent.loc[idx_common].values
            y_vals = composite.loc[idx_common].values
            scatter_fig.add_trace(go.Scatter(
                x=x_vals, y=y_vals, mode="markers",
                marker=dict(size=5, color=COLORS["accent_blue"], opacity=0.5),
                hovertemplate="Brent: $%{x:.1f}<br>Composite: %{y:.1f}<extra></extra>",
            ))
            z = np.polyfit(x_vals, y_vals, 1)
            p = np.poly1d(z)
            x_line = np.linspace(x_vals.min(), x_vals.max(), 80)
            scatter_fig.add_trace(go.Scatter(
                x=x_line, y=p(x_line), mode="lines",
                line=dict(color=COLORS["accent_orange"], width=1.5, dash="dash"),
                name="Trend", showlegend=False,
            ))
    scatter_fig.update_layout(
        height=260, showlegend=False,
        xaxis_title="Brent Crude ($/bbl)",
        yaxis_title="FreightIQ Composite [PROXY]",
        margin=dict(l=48, r=16, t=12, b=44),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    return html.Div([
        page_header("📈 Macro Overlay",
                    "FRED macro series · yield curve · USD vs freight · bunker costs"),
        info_banner("📊 Macro data sourced from FRED (St. Louis Fed) with yfinance fallbacks for rates & Brent. "
                    + ("FRED API: Connected ✓" if fred_active else
                       "FRED API: Not configured — Brent/10Y/2Y/USD use yfinance proxies. "
                       "Set FRED_API_KEY for INDPRO & CPI.")),

        section_header("Key Macro Indicators"),
        dbc.Row(kpi_cards, className="g-2 mb-3"),

        divider(),

        dbc.Row([
            dbc.Col([
                section_header("Macro Series — Normalised to 100 at Start"),
                dbc.Card(dbc.CardBody(dcc.Graph(figure=over_fig, config={"displayModeBar": True, "displaylogo": False}))),
            ], md=8),
            dbc.Col([
                section_header("Bunker Cost Estimate"),
                bunker_card,
                html.Br(),
                section_header("Freight vs Industrial Production"),
                dbc.Card(dbc.CardBody(dcc.Graph(figure=scatter_fig, config={"displayModeBar": False}))),
            ], md=4),
        ], className="g-3 mb-3"),

        divider(),

        section_header("US Treasury Yield Curve"),
        dbc.Card(dbc.CardBody(dcc.Graph(figure=yc_fig, config={"displayModeBar": True, "displaylogo": False}))),
    ])
