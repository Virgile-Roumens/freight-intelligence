import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np

from src.config import CACHE_DIR, COLORS, SHIPPING_EQUITIES, HISTORICAL_EVENTS
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.analytics.regime_detector import RegimeDetector, PHASES
from src.analytics.freight_analytics import (
    rolling_volatility, seasonal_index, compute_freight_statistics, seasonality_heatmap,
)
from src.utils.helpers import rolling_zscore
from dash_components.cards import page_header, section_header, info_banner, divider

dash.register_page(__name__, path="/market", name="Market Dashboard", order=1)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_rd    = RegimeDetector()

TICKER_INFO = {
    "BDRY": {"full": "Breakwave Dry Bulk ETF",     "segment": "All segments",               "proxy": "BDI",     "color": "#58a6ff"},
    "SBLK": {"full": "Star Bulk Carriers",          "segment": "Cape · Panamax · Supramax",  "proxy": "BDI",     "color": "#d29922"},
    "GNK":  {"full": "Genco Shipping & Trading",    "segment": "Capesize · Ultramax",        "proxy": "BCI/BSI", "color": "#79c0ff"},
    "DSX":  {"full": "Diana Shipping",              "segment": "Capesize · Panamax",         "proxy": "BCI/BPI", "color": "#56d364"},
    "EGLE": {"full": "Eagle Bulk Shipping",         "segment": "Supramax · Ultramax",        "proxy": "BSI",     "color": "#db6d28"},
    "NMM":  {"full": "Navios Maritime Partners",    "segment": "Cape · Panamax · Supra",     "proxy": "BDI",     "color": "#bc8cff"},
    "SB":   {"full": "Safe Bulkers",                "segment": "Panamax · Post-Panamax",     "proxy": "BPI",     "color": "#ff7b72"},
}

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


def _empty_fig(msg: str = "") -> go.Figure:
    fig = go.Figure()
    if msg:
        fig.add_annotation(
            text=msg,
            x=0.5, y=0.5, xref="paper", yref="paper",
            showarrow=False,
            font=dict(size=13, color=COLORS["text_secondary"],
                      family="'IBM Plex Mono',monospace"),
        )
    return fig


# ── Main performance chart ─────────────────────────────────────────────────────

def _price_perf_chart(
    equities: pd.DataFrame,
    selected: list[str],
    composite: pd.Series,
    show_ma: bool,
    show_vol: bool,
    show_events: bool,
) -> go.Figure:
    # Collect non-empty series
    all_series: dict[str, pd.Series] = {}
    for t in selected:
        if t in equities.columns:
            s = equities[t].dropna()
            if len(s) > 1:
                all_series[t] = s

    if not all_series:
        return _empty_fig("No data — check network or select a ticker above")

    # ── Date anchors ──────────────────────────────────────────────────────────
    latest_date   = max(s.index[-1] for s in all_series.values())
    earliest_date = min(s.index[0]  for s in all_series.values())
    norm_base_dt  = latest_date - pd.DateOffset(years=1)   # % return from 1Y ago = 0%
    default_start = norm_base_dt
    default_end   = latest_date

    rows       = 2 if (show_vol and not composite.empty) else 1
    row_heights = [0.76, 0.24] if rows == 2 else [1.0]

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        row_heights=row_heights, vertical_spacing=0.02,
    )

    palette     = COLORS["chart_palette"]
    eol_annots  = []   # end-of-line Bloomberg-style ticker labels

    for i, (t, s) in enumerate(all_series.items()):
        info  = TICKER_INFO.get(t, {})
        color = info.get("color", palette[i % len(palette)])

        # Normalise: % return from 1Y-ago baseline
        earlier    = s[s.index <= norm_base_dt]
        base_price = float(earlier.iloc[-1]) if not earlier.empty else float(s.iloc[0])
        pct_ret    = (s / base_price - 1) * 100
        latest_ret = float(pct_ret.iloc[-1])

        fig.add_trace(go.Scatter(
            x=pct_ret.index,
            y=pct_ret.round(2).values,
            name=t,
            line=dict(color=color, width=2),
            hovertemplate=(
                f"<b>{t}</b>  {info.get('full','')}<br>"
                "%{x|%d %b %Y}  <b>%{y:+.1f}%</b>"
                "<extra></extra>"
            ),
        ), row=1, col=1)

        # End-of-line label (Bloomberg right-side ticker annotation)
        eol_annots.append(dict(
            x=latest_date, y=latest_ret,
            xref="x", yref="y",
            text=f"  <b>{t}</b>  {latest_ret:+.1f}%",
            font=dict(size=9, color=color, family="'IBM Plex Mono',monospace"),
            showarrow=False, xanchor="left", yanchor="middle",
        ))

    # Zero reference line
    fig.add_hline(y=0, line_color=COLORS["text_faint"], line_width=0.7,
                  opacity=0.55, row=1, col=1)

    # Moving averages on composite
    if show_ma and not composite.empty:
        earlier_c  = composite[composite.index <= norm_base_dt]
        base_c     = float(earlier_c.iloc[-1]) if not earlier_c.empty else float(composite.iloc[0])
        norm_c     = (composite / base_c - 1) * 100
        for w, color, dash_sty, lbl in [
            (50,  COLORS["accent_yellow"], "dash", "MA50"),
            (200, COLORS["accent_orange"], "dot",  "MA200"),
        ]:
            ma = norm_c.rolling(w).mean().dropna()
            if not ma.empty:
                fig.add_trace(go.Scatter(
                    x=ma.index, y=ma.round(2).values,
                    name=f"Comp. {lbl}",
                    line=dict(color=color, width=1.1, dash=dash_sty),
                    opacity=0.7,
                    hovertemplate=f"Comp. {lbl}: %{{y:+.1f}}%<extra></extra>",
                ), row=1, col=1)

    # Historical event markers (only within data range)
    if show_events:
        for ev in HISTORICAL_EVENTS:
            try:
                ev_dt = pd.Timestamp(ev["date"])
                if ev_dt < earliest_date:
                    continue
            except Exception:
                continue
            fig.add_shape(
                type="line", x0=ev["date"], x1=ev["date"],
                y0=0, y1=1, yref="paper",
                line=dict(width=1, dash="dot", color=ev["color"]),
                row=1, col=1,
            )
            fig.add_annotation(
                x=ev["date"], y=0.97, yref="paper", text=ev["label"],
                showarrow=False, textangle=-90, xanchor="right",
                font=dict(size=8, color=ev["color"]), row=1, col=1,
            )

    # Volatility panel
    if show_vol and not composite.empty:
        vol = rolling_volatility(composite, 30) * 100
        fig.add_trace(go.Bar(
            x=vol.index, y=vol.round(2).values,
            name="30D Vol",
            marker_color=COLORS["accent_purple"],
            opacity=0.5,
            hovertemplate="30D Ann. Vol: %{y:.1f}%<extra></extra>",
        ), row=2, col=1)
        fig.update_yaxes(
            title_text="Vol %", row=2, col=1,
            ticksuffix="%", tickfont=dict(size=9),
            gridcolor="rgba(48,54,61,0.35)",
        )

    # X-axis: Bloomberg spike + range selector
    fig.update_xaxes(
        **_SPIKE_XAXIS,
        rangeselector=_RANGESELECTOR_STYLE,
        range=[default_start.isoformat(), default_end.isoformat()],
        row=1, col=1,
    )

    # Y-axis main
    fig.update_yaxes(
        title_text="Return from 1Y ago",
        ticksuffix="%",
        zeroline=True,
        zerolinecolor=COLORS["text_faint"],
        zerolinewidth=0.8,
        gridcolor="rgba(48,54,61,0.35)",
        tickfont=dict(size=10),
        row=1, col=1,
    )

    fig.update_layout(
        height=560,
        hovermode="x unified",
        hoverdistance=30,
        annotations=eol_annots,
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=200, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )
    return fig


# ── Regime chart ───────────────────────────────────────────────────────────────

def _regime_chart(composite: pd.Series, phases_hist: "pd.Series") -> go.Figure:
    if composite.empty:
        return _empty_fig("No composite data")

    latest_date   = composite.index[-1]
    default_start = latest_date - pd.DateOffset(years=2)

    # Normalise to 100 at data start for regime context
    norm_c = composite / composite.iloc[0] * 100

    fig = go.Figure()

    # Regime shading bands
    prev_phase, start_x = None, composite.index[0]
    for dt, phase in phases_hist.items():
        if phase != prev_phase:
            if prev_phase and prev_phase in PHASES:
                fig.add_vrect(
                    x0=start_x, x1=dt,
                    fillcolor=PHASES[prev_phase]["color"],
                    opacity=0.09, layer="below", line_width=0,
                )
            prev_phase, start_x = phase, dt

    # Composite line
    fig.add_trace(go.Scatter(
        x=norm_c.index, y=norm_c.round(2).values,
        name="Composite",
        line=dict(color=COLORS["accent_blue"], width=2),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.05)",
        hovertemplate="%{x|%d %b %Y}  <b>%{y:.1f}</b><extra></extra>",
    ))

    # MA200
    ma200 = norm_c.rolling(200).mean().dropna()
    if not ma200.empty:
        fig.add_trace(go.Scatter(
            x=ma200.index, y=ma200.round(2).values,
            name="MA 200",
            line=dict(color=COLORS["accent_orange"], width=1.4, dash="dot"),
            opacity=0.8,
            hovertemplate="MA200: %{y:.1f}<extra></extra>",
        ))

    # Event markers
    for ev in HISTORICAL_EVENTS:
        try:
            if pd.Timestamp(ev["date"]) < composite.index[0]:
                continue
        except Exception:
            continue
        fig.add_shape(
            type="line", x0=ev["date"], x1=ev["date"],
            y0=0, y1=1, yref="paper",
            line=dict(width=1, dash="dot", color=ev["color"]),
        )
        fig.add_annotation(
            x=ev["date"], y=0.97, yref="paper",
            text=ev["label"], showarrow=False,
            textangle=-90, xanchor="right",
            font=dict(size=8, color=ev["color"]),
        )

    fig.update_xaxes(
        **_SPIKE_XAXIS,
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1Y",  step="year",  stepmode="backward"),
                dict(count=2, label="2Y",  step="year",  stepmode="backward"),
                dict(count=5, label="5Y",  step="year",  stepmode="backward"),
                dict(step="all", label="ALL"),
            ],
            bgcolor=COLORS["bg_secondary"], bordercolor=COLORS["border"],
            borderwidth=1, activecolor=COLORS["accent_blue"],
            font=dict(color=COLORS["text_secondary"], size=11,
                      family="'IBM Plex Mono',monospace"),
            x=0, y=1.03, xanchor="left", yanchor="bottom",
        ),
        range=[default_start.isoformat(), latest_date.isoformat()],
    )
    fig.update_yaxes(
        title_text="Index (100 = data start)",
        gridcolor="rgba(48,54,61,0.35)",
        tickfont=dict(size=10),
    )
    fig.update_layout(
        height=420,
        hovermode="x unified",
        legend=_LEGEND_STYLE,
        margin=dict(l=58, r=160, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )
    return fig


# ── Z-score chart ──────────────────────────────────────────────────────────────

def _zscore_chart(composite: pd.Series) -> go.Figure:
    if composite.empty or len(composite) < 52:
        return _empty_fig("Need at least 52 weeks of data")

    latest_date   = composite.index[-1]
    default_start = latest_date - pd.DateOffset(years=2)
    zs            = rolling_zscore(composite, 52)

    fig = go.Figure()
    fig.add_hrect(y0=1,  y1=2,  fillcolor=COLORS["accent_yellow"], opacity=0.06, line_width=0)
    fig.add_hrect(y0=2,  y1=4,  fillcolor=COLORS["accent_red"],    opacity=0.08, line_width=0)
    fig.add_hrect(y0=-2, y1=-1, fillcolor=COLORS["accent_yellow"], opacity=0.06, line_width=0)
    fig.add_hrect(y0=-4, y1=-2, fillcolor=COLORS["accent_green"],  opacity=0.08, line_width=0)

    fig.add_hline(y=2,  line_dash="dot",  line_color=COLORS["accent_red"],    opacity=0.55,
                  annotation_text="+2σ overbought", annotation_position="top right",
                  annotation_font=dict(color=COLORS["accent_red"], size=9))
    fig.add_hline(y=-2, line_dash="dot",  line_color=COLORS["accent_green"],  opacity=0.55,
                  annotation_text="−2σ oversold", annotation_position="bottom right",
                  annotation_font=dict(color=COLORS["accent_green"], size=9))
    fig.add_hline(y=0,  line_dash="dash", line_color=COLORS["text_faint"], opacity=0.4)

    fig.add_trace(go.Scatter(
        x=zs.index, y=zs.values,
        name="52W Z-Score",
        line=dict(color=COLORS["accent_blue"], width=1.8),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
        hovertemplate="Z-Score: <b>%{y:.2f}σ</b><br>%{x|%d %b %Y}<extra></extra>",
    ))

    fig.update_xaxes(
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
        range=[default_start.isoformat(), latest_date.isoformat()],
    )
    fig.update_yaxes(
        title_text="Z-Score (σ)",
        gridcolor="rgba(48,54,61,0.35)",
        tickfont=dict(size=10),
        zeroline=True, zerolinecolor=COLORS["text_faint"], zerolinewidth=0.7,
    )
    fig.update_layout(
        height=300,
        showlegend=False,
        hovermode="x unified",
        margin=dict(l=58, r=80, t=54, b=42),
        plot_bgcolor=COLORS["bg_primary"],
        paper_bgcolor=COLORS["bg_card"],
    )
    return fig


# ── Page layout ────────────────────────────────────────────────────────────────

def layout(**kwargs):
    try:
        # 5Y history for meaningful default + context
        equities  = _fdm.get_shipping_equities(period="5y")
        composite = _fdm.get_weighted_shipping_index(period="5y")
        bdi       = _fdm.get_bdi_history(start="2015-01-01")
    except Exception:
        equities  = pd.DataFrame()
        composite = pd.Series(dtype=float)
        bdi       = pd.DataFrame()

    default_tickers = ["BDRY", "SBLK", "GNK"]
    valid_tickers   = [t for t in default_tickers if not equities.empty and t in equities.columns]

    # ── Returns table ──────────────────────────────────────────────────────────
    rows_data = []
    for t, info in TICKER_INFO.items():
        if equities.empty or t not in equities.columns:
            continue
        s = equities[t].dropna()
        if s.empty:
            continue
        def ret(n, _s=s): return f"{_s.pct_change(n).iloc[-1]*100:+.1f}%" if len(_s) > n else "—"
        ytd_days = (s.index[-1] - pd.Timestamp(s.index[-1].year, 1, 1)).days
        rows_data.append({
            "Ticker": t, "Company": info["full"], "Segment": info["segment"],
            "Index Proxy": info["proxy"],
            "Price": f"${s.iloc[-1]:.2f}",
            "1D": ret(1), "5D": ret(5), "1M": ret(21), "3M": ret(63),
            "1Y": ret(252), "YTD": ret(min(ytd_days, len(s)-1)),
        })

    returns_rows = []
    for r in rows_data:
        def _col_style(v):
            if v == "—":
                return {"color": "var(--text-faint)", "font-family": "var(--font-mono)"}
            c = "var(--accent-green)" if v.startswith("+") else "var(--accent-red)"
            return {"color": c, "font-weight": "600", "font-family": "var(--font-mono)"}

        returns_rows.append(html.Tr([
            html.Td(
                html.Span(r["Ticker"],
                          style={"color": TICKER_INFO[r["Ticker"]]["color"],
                                 "font-weight": "700", "font-family": "var(--font-mono)"}),
            ),
            html.Td(r["Company"],    style={"color": "var(--text-secondary)", "font-size": "0.78rem"}),
            html.Td(r["Segment"],    style={"color": "var(--text-faint)",     "font-size": "0.68rem"}),
            html.Td(r["Index Proxy"],style={"color": "var(--accent-yellow)",  "font-size": "0.7rem",
                                            "font-family": "var(--font-mono)"}),
            html.Td(r["Price"],      style={"font-weight": "600", "font-family": "var(--font-mono)"}),
            *[html.Td(r[c], style=_col_style(r[c])) for c in ["1D", "5D", "1M", "3M", "1Y", "YTD"]],
        ]))

    # ── Seasonality ────────────────────────────────────────────────────────────
    seas_content = None
    heatmap_fig  = None
    if not composite.empty and len(composite) > 252:
        seas = seasonal_index(composite)
        if not seas.empty:
            colors_bar = [
                COLORS["accent_green"] if v >= 0 else COLORS["accent_red"]
                for v in (seas["seasonal_index"] - 1)
            ]
            seas_vals = (seas["seasonal_index"] - 1) * 100
            seas_fig = go.Figure(go.Bar(
                x=seas["month_name"], y=seas_vals.round(2),
                marker_color=colors_bar,
                text=[f"{v:+.1f}%" for v in seas_vals],
                textposition="outside",
                textfont=dict(size=10, family="'IBM Plex Mono',monospace"),
                hovertemplate="<b>%{x}</b>  vs annual avg: <b>%{y:+.1f}%</b><extra></extra>",
            ))
            seas_fig.add_hline(y=0, line_color=COLORS["text_faint"], line_width=0.8, opacity=0.5)
            seas_fig.update_yaxes(
                title_text="% vs annual mean", ticksuffix="%",
                gridcolor="rgba(48,54,61,0.35)",
            )
            seas_fig.update_layout(
                height=320, showlegend=False,
                margin=dict(l=58, r=20, t=16, b=42),
                plot_bgcolor=COLORS["bg_primary"],
                paper_bgcolor=COLORS["bg_card"],
            )
            seas_content = dcc.Graph(figure=seas_fig, config={"displayModeBar": False})

            pivot = seasonality_heatmap(composite)
            if not pivot.empty:
                month_names = ["Jan","Feb","Mar","Apr","May","Jun",
                                "Jul","Aug","Sep","Oct","Nov","Dec"]
                zmid = float(np.nanmedian(pivot.values))
                heatmap_fig = go.Figure(go.Heatmap(
                    z=pivot.values,
                    x=pivot.columns.astype(str),
                    y=[month_names[m-1] for m in pivot.index],
                    colorscale=[
                        [0,   COLORS["accent_red"]],
                        [0.5, COLORS["bg_card"]],
                        [1,   COLORS["accent_green"]],
                    ],
                    zmid=zmid,
                    text=[[f"{v:.0f}" if not np.isnan(v) else "—" for v in row]
                          for row in pivot.values],
                    texttemplate="%{text}",
                    textfont=dict(size=10, family="'IBM Plex Mono',monospace"),
                    hovertemplate="<b>%{y} %{x}</b><br>Avg index: %{z:.1f}<extra></extra>",
                ))
                heatmap_fig.update_layout(
                    height=360,
                    yaxis_autorange="reversed",
                    margin=dict(l=44, r=60, t=16, b=42),
                    paper_bgcolor=COLORS["bg_card"],
                )

    # ── Statistics ─────────────────────────────────────────────────────────────
    stats_content = html.Div(info_banner("No composite data available."))
    if not composite.empty:
        stats = compute_freight_statistics(composite)
        if stats:
            z_fig = _zscore_chart(composite)
            zscore_val = stats.get("zscore_52w", 0) or 0
            z_interp   = ("Overbought" if zscore_val > 2 else
                           "Oversold"   if zscore_val < -2 else "Normal range")
            stats_content = html.Div([
                dbc.Row([
                    dbc.Col(_stat_card("Current Level",   f"{stats.get('current', 0):.1f}",
                                       f"1Y avg: {stats.get('mean_1y', 0):.1f}"),   width=6, md=3),
                    dbc.Col(_stat_card("52W Z-Score",     f"{zscore_val:+.2f}σ",
                                       z_interp),                                    width=6, md=3),
                    dbc.Col(_stat_card("30D Vol (ann.)",  f"{(stats.get('vol_30d') or 0)*100:.1f}%",
                                       f"1Y pctile: {stats.get('pct_rank_1Y', 50):.0f}th"), width=6, md=3),
                    dbc.Col(_stat_card("Max Drawdown",    f"{(stats.get('drawdown_current', 0))*100:.1f}%",
                                       f"1Y Sharpe: {stats.get('sharpe_1y', 0) or 0:.2f}"), width=6, md=3),
                ], className="g-2 mb-3"),
                dbc.Card(dbc.CardBody(
                    dcc.Graph(figure=z_fig, config={"displayModeBar": True, "displaylogo": False})
                )),
            ])

    # ── Regime history ─────────────────────────────────────────────────────────
    regime_content = html.Div(info_banner("Need at least 50 data points for regime classification."))
    if not composite.empty and len(composite) >= 50:
        phases_hist  = _rd.classify_history(composite)
        reg_fig      = _regime_chart(composite, phases_hist)

        phase_counts = phases_hist.value_counts()
        total        = len(phases_hist)
        phase_badges = dbc.Row([
            dbc.Col(html.Div([
                html.Div(
                    f'{PHASES[ph]["emoji"]}  {PHASES[ph]["label"]}',
                    style={"font-size": "0.68rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "text-transform": "uppercase",
                           "letter-spacing": "0.06em"},
                ),
                html.Div(
                    f'{phase_counts.get(ph, 0) / total * 100:.0f}%',
                    style={"font-size": "1.5rem", "font-weight": "700",
                           "color": PHASES[ph]["color"], "font-family": "var(--font-mono)",
                           "letter-spacing": "-0.02em"},
                ),
                html.Div(
                    f'{phase_counts.get(ph, 0)} trading days',
                    style={"font-size": "0.6rem", "color": "var(--text-faint)",
                           "font-family": "var(--font-mono)"},
                ),
            ], className="fiq-card",
               style={"border-left": f'3px solid {PHASES[ph]["color"]}',
                      "padding": "12px 14px"}),
            width=6, sm=True)
            for ph in PHASES
        ], className="g-2 mb-3")

        # Current regime scorecard
        scorecard = _rd.scorecard(composite)
        sc_rows = []
        if not scorecard.empty:
            for _, row in scorecard.iterrows():
                color = {"green": COLORS["accent_green"],
                         "amber": COLORS["accent_yellow"],
                         "red":   COLORS["accent_red"]}.get(row["status"], COLORS["text_secondary"])
                icon  = {"green": "🟢", "amber": "🟡", "red": "🔴"}.get(row["status"], "⚪")
                sc_rows.append(html.Tr([
                    html.Td(f"{icon}", style={"width": "24px"}),
                    html.Td(row["indicator"],
                            style={"font-family": "var(--font-mono)", "font-size": "0.8rem",
                                   "color": "var(--text-primary)"}),
                    html.Td(row["value"],
                            style={"font-family": "var(--font-mono)", "font-weight": "600",
                                   "color": color, "text-align": "right"}),
                    html.Td(row["signal"],
                            style={"font-size": "0.75rem", "color": "var(--text-secondary)"}),
                ]))

        regime_content = html.Div([
            phase_badges,
            dbc.Card(dbc.CardBody(
                dcc.Graph(figure=reg_fig, config={"displayModeBar": True, "displaylogo": False})
            ), className="mb-3"),
            section_header("Current Regime Scorecard"),
            dbc.Card(dbc.CardBody(
                html.Div(
                    html.Table([
                        html.Thead(html.Tr([html.Th(h) for h in ["", "Indicator", "Value", "Signal"]])),
                        html.Tbody(sc_rows),
                    ], className="fiq-table"),
                    style={"overflow-x": "auto"},
                )
            )) if sc_rows else html.Div(),
        ])

    # ── Build page ─────────────────────────────────────────────────────────────
    perf_fig = _price_perf_chart(equities, valid_tickers, composite, True, True, True)

    return html.Div([
        page_header(
            "📊 Market Dashboard",
            "Shipping equity proxies · seasonality · statistical analysis · regime history",
        ),
        info_banner(
            "⚠ All series are US-listed shipping equity proxies (BDRY, SBLK, GNK…). "
            "Baltic Exchange official indices (BDI/BCI/BPI/BSI/BHSI) require a paid subscription. "
            "Chart default: % return from 1 year ago. Zoom out with 2Y / 5Y / ALL buttons."
        ),

        dcc.Tabs(
            id="market-tabs",
            value="tab-perf",
            className="tabs-bar",
            children=[
                dcc.Tab(label="📈 Price Performance", value="tab-perf",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🌡 Seasonality",       value="tab-seas",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📊 Statistics",        value="tab-stats",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🔄 Regime History",    value="tab-regime",
                        className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # ── Tab: Performance ──────────────────────────────────────────────────
        html.Div([
            dbc.Card(dbc.CardBody([
                dcc.Graph(
                    figure=perf_fig,
                    config={"displayModeBar": True, "displaylogo": False,
                            "modeBarButtonsToRemove": ["autoScale2d", "lasso2d", "select2d"],
                            "toImageButtonOptions": {"format": "png", "width": 1600, "height": 700}},
                ),
            ]), className="mb-3"),
            section_header("Returns Summary"),
            dbc.Card(dbc.CardBody(
                html.Div(
                    html.Table([
                        html.Thead(html.Tr([html.Th(h) for h in
                                            ["Ticker", "Company", "Segment", "Index Proxy",
                                             "Price", "1D", "5D", "1M", "3M", "1Y", "YTD"]])),
                        html.Tbody(returns_rows),
                    ], className="fiq-table"),
                    style={"overflow-x": "auto"},
                )
            )),
        ], id="market-tab-content-perf"),

        # ── Tab: Seasonality ──────────────────────────────────────────────────
        html.Div([
            section_header("Monthly Seasonality — Composite Index [PROXY]"),
            html.P(
                "Premium/discount of each calendar month vs. the full-year mean. "
                "Based on the FreightIQ Composite Index (BDRY ETF + equity basket).",
                style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                       "font-family": "var(--font-mono)", "margin-bottom": "14px"},
            ),
            dbc.Card(dbc.CardBody(
                seas_content or info_banner("Need at least 1 year of data for seasonality analysis.")
            ), className="mb-3"),
            *([section_header("Year × Month Heatmap"),
               html.P("Average index level per month/year. Green = above median; Red = below.",
                      style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                             "font-family": "var(--font-mono)", "margin-bottom": "14px"}),
               dbc.Card(dbc.CardBody(
                   dcc.Graph(figure=heatmap_fig, config={"displayModeBar": False})
               ))]
              if heatmap_fig else []),
        ], id="market-tab-content-seas", style={"display": "none"}),

        # ── Tab: Statistics ───────────────────────────────────────────────────
        html.Div([
            section_header("Composite Index — Statistical Summary [PROXY]"),
            stats_content,
        ], id="market-tab-content-stats", style={"display": "none"}),

        # ── Tab: Regime History ───────────────────────────────────────────────
        html.Div([
            section_header("Historical Regime Classification [PROXY]"),
            html.P(
                "Regime detection using Price/MA200 ratio and 20-day momentum. "
                "Chart defaults to last 2 years — use range buttons to see full history.",
                style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                       "font-family": "var(--font-mono)", "margin-bottom": "14px"},
            ),
            regime_content,
        ], id="market-tab-content-regime", style={"display": "none"}),
    ])


def _stat_card(label: str, value: str, sub: str) -> html.Div:
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div(value, className="kpi-value"),
        html.Div(sub,   className="kpi-delta kpi-delta-neu"),
    ], className="kpi-card")


@callback(
    Output("market-tab-content-perf",   "style"),
    Output("market-tab-content-seas",   "style"),
    Output("market-tab-content-stats",  "style"),
    Output("market-tab-content-regime", "style"),
    Input("market-tabs", "value"),
)
def switch_market_tab(tab: str):
    tabs = ["tab-perf", "tab-seas", "tab-stats", "tab-regime"]
    return [{"display": "block"} if t == tab else {"display": "none"} for t in tabs]
