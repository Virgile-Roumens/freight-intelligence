import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd

from src.config import CACHE_DIR, COLORS, SHIPPING_EQUITIES
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.data.macro_data import MacroDataManager
from src.data.news_data import NewsAggregator
from src.analytics.regime_detector import RegimeDetector
from dash_components.cards import (
    kpi_card, signal_card, news_card, page_header, section_header, info_banner, divider
)

dash.register_page(__name__, path="/", name="Overview", order=0)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_mdm   = MacroDataManager(_cache)
_nd    = NewsAggregator(_cache)
_rd    = RegimeDetector()


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


def _load_data():
    composite  = _fdm.get_weighted_shipping_index(period="5y")
    snapshot   = _fdm.get_freight_snapshot()
    bdi        = _fdm.get_bdi_history(start="2015-01-01")
    articles   = _nd.fetch_all_feeds(max_per_feed=8)
    comm_df    = _mdm.get_commodity_prices(period="1mo")
    return composite, snapshot, bdi, articles, comm_df


def _build_composite_chart(composite: pd.Series) -> go.Figure:
    fig = go.Figure()
    if composite.empty:
        return fig

    latest_date   = composite.index[-1]
    norm_base_dt  = latest_date - pd.DateOffset(years=1)
    default_start = norm_base_dt

    # Normalise to % return from 1Y ago
    earlier    = composite[composite.index <= norm_base_dt]
    base_price = float(earlier.iloc[-1]) if not earlier.empty else float(composite.iloc[0])
    pct_ret    = (composite / base_price - 1) * 100

    fig.add_trace(go.Scatter(
        x=pct_ret.index, y=pct_ret.round(2).values,
        name="Composite [PROXY]",
        line=dict(color=COLORS["accent_blue"], width=2),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
        hovertemplate="<b>%{x|%d %b %Y}</b><br>Return: <b>%{y:+.1f}%</b><extra></extra>",
    ))

    # MAs on normalised series
    for w, color, dash_sty, lbl in [
        (50,  COLORS["accent_yellow"], "dash", "MA 50"),
        (200, COLORS["accent_orange"], "dot",  "MA 200"),
    ]:
        ma = pct_ret.rolling(w).mean().dropna()
        if not ma.empty:
            fig.add_trace(go.Scatter(
                x=ma.index, y=ma.round(2).values,
                name=lbl,
                line=dict(color=color, width=1.2, dash=dash_sty),
                opacity=0.8,
                hovertemplate=f"{lbl}: %{{y:+.1f}}%<extra></extra>",
            ))

    fig.add_hline(y=0, line_color=COLORS["text_faint"], line_width=0.7, opacity=0.55)

    fig.update_xaxes(
        **_SPIKE_XAXIS,
        rangeselector=dict(
            buttons=[
                dict(count=3,  label="3M",  step="month", stepmode="backward"),
                dict(count=6,  label="6M",  step="month", stepmode="backward"),
                dict(count=1,  label="YTD", step="year",  stepmode="todate"),
                dict(count=1,  label="1Y",  step="year",  stepmode="backward"),
                dict(count=2,  label="2Y",  step="year",  stepmode="backward"),
                dict(count=5,  label="5Y",  step="year",  stepmode="backward"),
                dict(step="all", label="ALL"),
            ],
            bgcolor=COLORS["bg_secondary"], bordercolor=COLORS["border"],
            borderwidth=1, activecolor=COLORS["accent_blue"],
            font=dict(color=COLORS["text_secondary"], size=11,
                      family="'IBM Plex Mono','Courier New',monospace"),
            x=0, y=1.03, xanchor="left", yanchor="bottom",
        ),
        range=[default_start.isoformat(), latest_date.isoformat()],
    )
    fig.update_yaxes(
        title_text="Return from 1Y ago", ticksuffix="%",
        zeroline=True, zerolinecolor=COLORS["text_faint"], zerolinewidth=0.8,
        gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
    )
    fig.update_layout(
        height=320,
        hovermode="x unified",
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=140, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )
    return fig


def _build_regime_gauge(composite: pd.Series, bdi: pd.DataFrame) -> tuple[go.Figure, dict]:
    if not bdi.empty and "value" in bdi.columns:
        regime = _rd.detect_phase(bdi["value"])
    elif not composite.empty:
        regime = _rd.detect_phase(composite)
    else:
        regime = {"phase": "NEUTRAL", "label": "N/A", "color": COLORS["accent_blue"],
                  "emoji": "➡️", "description": "Insufficient data", "metrics": {}}

    ma_ratio = regime.get("metrics", {}).get("ma_ratio", 1.0) or 1.0
    phase_color = regime.get("color", COLORS["accent_blue"])

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=ma_ratio,
        number={"suffix": "x", "font": {"color": COLORS["text_primary"], "size": 26}},
        gauge={
            "axis": {"range": [0.5, 1.5], "tickcolor": COLORS["text_secondary"], "tickfont": {"size": 9}},
            "bar": {"color": phase_color, "thickness": 0.5},
            "steps": [
                {"range": [0.5,  0.80], "color": "rgba(248,81,73,0.18)"},
                {"range": [0.80, 0.95], "color": "rgba(248,81,73,0.09)"},
                {"range": [0.95, 1.05], "color": "rgba(210,153,34,0.09)"},
                {"range": [1.05, 1.20], "color": "rgba(63,185,80,0.09)"},
                {"range": [1.20, 1.5],  "color": "rgba(63,185,80,0.18)"},
            ],
            "threshold": {"line": {"color": COLORS["text_secondary"], "width": 2},
                          "thickness": 0.8, "value": 1.0},
        },
        title={"text": "Price / MA200", "font": {"color": COLORS["text_secondary"], "size": 11}},
    ))
    fig.update_layout(
        height=200,
        margin=dict(l=14, r=14, t=36, b=10),
        paper_bgcolor=COLORS["bg_card"],
        font=dict(color=COLORS["text_primary"]),
    )
    return fig, regime


def layout(**kwargs):
    try:
        composite, snapshot, bdi, articles, comm_df = _load_data()
    except Exception:
        composite = pd.Series(dtype=float)
        snapshot, bdi, articles, comm_df = {}, pd.DataFrame(), [], pd.DataFrame()

    # ── KPI row ────────────────────────────────────────────────────────────────
    kpi_tickers = [("BDRY", "BDRY ETF", True), ("SBLK", "Star Bulk", True),
                   ("GNK",  "Genco",    True), ("EGLE", "Eagle Bulk", True),
                   ("NMM",  "Navios",   True)]
    kpi_cards = []
    for ticker, label, proxy in kpi_tickers:
        d = snapshot.get(ticker, {})
        val = d.get("value")
        d1d = d.get("delta_1d")
        val_str   = f"${val:.2f}" if val else "N/A"
        delta_val = d1d * val if (d1d and val) else None
        delta_pct = d1d * 100 if d1d else None
        color = COLORS.get("chart_palette", ["#58a6ff"])[list(snapshot.keys()).index(ticker) % 10] if ticker in snapshot else COLORS["accent_blue"]
        kpi_cards.append(dbc.Col(kpi_card(label, val_str, delta_val, delta_pct, is_proxy=proxy, accent_color=color), width=12, sm=6, md=True))

    # ── Commodity cross-asset row ───────────────────────────────────────────────
    comm_assets = [("Brent Crude", "#58a6ff"), ("WTI Crude Oil", "#3fb950"),
                   ("Copper", "#bc8cff"), ("Corn", "#d29922"), ("Wheat", "#db6d28")]
    comm_cards = []
    for name, color in comm_assets:
        if not comm_df.empty and name in comm_df.columns:
            s = comm_df[name].dropna()
            val = float(s.iloc[-1]) if not s.empty else None
            d5d = float(s.pct_change(5).iloc[-1]) if len(s) > 5 else None
            val_str  = f"${val:,.2f}" if val else "N/A"
            delta_val = d5d * val if (d5d and val) else None
            comm_cards.append(dbc.Col(kpi_card(name, val_str, delta_val, d5d * 100 if d5d else None, accent_color=color), width=12, sm=6, md=True))
        else:
            comm_cards.append(dbc.Col(kpi_card(name, "N/A", accent_color=color), width=12, sm=6, md=True))

    # ── Charts ─────────────────────────────────────────────────────────────────
    composite_fig  = _build_composite_chart(composite)
    gauge_fig, regime = _build_regime_gauge(composite, bdi)

    phase_color = regime.get("color", COLORS["accent_blue"])
    phase_label = f'{regime.get("emoji","➡️")} {regime.get("label","N/A")}'
    metrics     = regime.get("metrics", {})

    # ── News ───────────────────────────────────────────────────────────────────
    news_items = [news_card(a["title"][:90], a["source"], a.get("published",""),
                            a["link"], a["score"])
                  for a in (articles or [])[:6]]
    if not news_items:
        news_items = [info_banner("News feeds unavailable. Check network connection.")]

    # ── Signals ────────────────────────────────────────────────────────────────
    auto_signals = []
    if not composite.empty and len(composite) > 5:
        r5 = float(composite.pct_change(5).iloc[-1])
        if r5 > 0.05:
            auto_signals.append(signal_card(f"Composite +{r5*100:.1f}% in 5 days — positive momentum", "green"))
        elif r5 < -0.05:
            auto_signals.append(signal_card(f"Composite {r5*100:.1f}% in 5 days — bearish short-term", "red"))
        else:
            auto_signals.append(signal_card(f"Composite {r5*100:+.1f}% 5D — consolidating", "amber"))

    if regime.get("phase") in ("PEAK", "CONTRACTION", "TROUGH"):
        level = "amber" if regime["phase"] == "PEAK" else "red"
        auto_signals.append(signal_card(
            f'Regime: {regime.get("emoji","")} {regime.get("label","")} — {regime.get("description","")[:80]}', level))

    news_signals = []
    if articles:
        sigs = _nd.detect_signals(articles)
        news_signals = [signal_card(s["text"][:110], s["level"]) for s in sigs[:4]]
    if not news_signals:
        news_signals = [
            signal_card("🟡 Red Sea / Bab el-Mandeb: RESTRICTED — Houthi attacks ongoing", "red"),
            signal_card("🟡 Black Sea grain corridor: Monitor for Ukraine war developments", "amber"),
        ]

    return html.Div([
        page_header("🏠 Market Intelligence Overview",
                    "FreightIQ composite index · regime detector · cross-asset snapshot · latest intelligence"),

        # ── KPI row ──────────────────────────────────────────────────────────
        section_header("Shipping Equity Proxies [PROXY]"),
        info_banner("⚠ Freight index values shown are market proxies (BDRY ETF + shipping equities). Baltic Exchange official data requires a paid subscription."),
        dbc.Row(kpi_cards, className="g-2 mb-3"),

        divider(),

        # ── Main 3-column content ─────────────────────────────────────────────
        dbc.Row([
            # Left: composite index chart
            dbc.Col([
                section_header("FreightIQ Composite Index [PROXY]"),
                dbc.Card(dbc.CardBody(
                    dcc.Graph(figure=composite_fig, config={"displayModeBar": False}, style={"height": "300px"})
                ), className="mb-3"),
                html.Div(
                    "Equal-weighted composite: BDRY ETF + dry bulk shipping equities. "
                    "Default view: % return from 1 year ago. Use range buttons to zoom out.",
                    style={"font-size": "0.68rem", "color": "var(--text-faint)", "font-family": "var(--font-mono)"}
                ),
            ], md=7),

            # Right: regime + signals
            dbc.Col([
                section_header("Market Regime"),
                dbc.Card(dbc.CardBody([
                    dcc.Graph(figure=gauge_fig, config={"displayModeBar": False}, style={"height": "200px"}),
                    html.Div(
                        phase_label,
                        style={"text-align": "center", "font-size": "1rem", "font-weight": "600",
                               "color": phase_color, "font-family": "var(--font-mono)", "margin": "6px 0"},
                    ),
                    html.Div(
                        regime.get("description", ""),
                        style={"text-align": "center", "font-size": "0.7rem",
                               "color": "var(--text-secondary)", "font-family": "var(--font-mono)"},
                    ),
                    html.Hr(className="fiq-divider"),
                    html.Div([
                        html.Span(f"Momentum 20D: {metrics.get('momentum_20d', 0):+.1f}%  ",
                                  style={"font-size": "0.7rem", "font-family": "var(--font-mono)", "color": "var(--text-secondary)"}),
                        html.Span(f"  Z-score: {metrics.get('zscore_1y', 0):.2f}σ  ",
                                  style={"font-size": "0.7rem", "font-family": "var(--font-mono)", "color": "var(--text-secondary)"}),
                        html.Span(f"  Pctile: {metrics.get('percentile_1y', 50):.0f}th",
                                  style={"font-size": "0.7rem", "font-family": "var(--font-mono)", "color": "var(--text-secondary)"}),
                    ]) if metrics else None,
                ])),
            ], md=5),
        ], className="g-3 mb-3"),

        divider(),

        # ── Cross-asset snapshot ──────────────────────────────────────────────
        section_header("Cross-Asset Snapshot"),
        dbc.Row(comm_cards, className="g-2 mb-3"),

        divider(),

        # ── Signals + News ────────────────────────────────────────────────────
        dbc.Row([
            dbc.Col([
                section_header("Market Signals"),
                *auto_signals,
                html.Br(),
                *news_signals,
            ], md=5),
            dbc.Col([
                section_header("Latest Intelligence"),
                *news_items,
            ], md=7),
        ], className="g-3"),
    ])
