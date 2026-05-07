import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output, State
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd
import numpy as np

from src.config import CACHE_DIR, COLORS, FFA_TENORS
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.analytics.freight_analytics import rolling_volatility
from dash_components.cards import page_header, section_header, info_banner, divider, kpi_card

dash.register_page(__name__, path="/ffa", name="FFA Derivatives", order=6)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)

# Default manual FFA curve values (BDI equivalent proxies)
DEFAULT_FFA = {t: None for t in FFA_TENORS}

_BDRY_OPTIONS_NOTE = (
    "BDRY options chain: yfinance provides limited options data for ETFs. "
    "For professional FFA analytics, use Baltic Exchange / Clarksons / SSY data feeds."
)


def _vol_chart(composite: pd.Series) -> go.Figure:
    fig = go.Figure()
    if not composite.empty:
        for w, color, lbl in [(20,  COLORS["accent_blue"],  "20D"),
                               (30,  COLORS["accent_yellow"],"30D"),
                               (90,  COLORS["accent_orange"],"90D")]:
            vol = rolling_volatility(composite, w) * 100
            fig.add_trace(go.Scatter(x=vol.index, y=vol.round(2).values,
                name=f"{lbl} Vol", line=dict(color=color, width=1.5),
                hovertemplate=f"{lbl} Vol: <b>%{{y:.1f}}%</b><extra></extra>"))
    fig.update_layout(height=280, hovermode="x unified", yaxis_title="Ann. Vol %", yaxis_ticksuffix="%",
                       legend=dict(x=1.01, y=1, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
                       margin=dict(l=44, r=120, t=12, b=40))
    return fig


def _fwd_curve_fig(spot: float | None, curve_vals: dict) -> go.Figure:
    fig = go.Figure()
    tenors = FFA_TENORS
    values = [curve_vals.get(t) for t in tenors]
    valid  = [(t, v) for t, v in zip(tenors, values) if v is not None]
    if not valid:
        return fig

    vt, vv = zip(*valid)
    if spot:
        vt = ("Spot*",) + vt
        vv = (spot,) + vv

    fig.add_trace(go.Scatter(
        x=list(vt), y=list(vv), mode="lines+markers",
        line=dict(color=COLORS["accent_blue"], width=2),
        marker=dict(size=7, color=COLORS["accent_blue"]),
        name="Forward Curve",
        hovertemplate="<b>%{x}</b>: %{y:,.1f}<extra></extra>",
    ))
    if spot:
        fig.add_hline(y=spot, line_dash="dot", line_color=COLORS["text_secondary"],
                      opacity=0.5,
                      annotation_text=f"Spot* {spot:,.1f}", annotation_position="right")
    fig.update_layout(height=300, yaxis_title="Level (index proxy)",
                       showlegend=False, margin=dict(l=44, r=80, t=12, b=40))
    return fig


def layout(**kwargs):
    try:
        composite = _fdm.get_weighted_shipping_index(period="2y")
        equities  = _fdm.get_shipping_equities(period="1y")
    except Exception:
        composite, equities = pd.Series(dtype=float), pd.DataFrame()

    spot_val = float(composite.iloc[-1]) if not composite.empty else None
    spot_str = f"{spot_val:,.1f}" if spot_val else "N/A"

    # ── BDRY options ─────────────────────────────────────────────────────────
    try:
        from src.data.freight_data import get_bdry_options
        exp, calls_df, puts_df = get_bdry_options()
        have_options = (calls_df is not None and not calls_df.empty)
    except Exception:
        exp, calls_df, puts_df = None, None, None
        have_options = False

    opt_cols = [c for c in ["strike","lastPrice","impliedVolatility","openInterest","volume"] if calls_df is not None and not calls_df.empty and c in calls_df.columns]

    calls_rows, puts_rows = [], []
    if have_options and opt_cols:
        for _, row in calls_df[opt_cols].head(12).iterrows():
            iv = row.get("impliedVolatility", None)
            calls_rows.append(html.Tr([
                html.Td(f'{row.get("strike","—"):.2f}', style={"font-family":"var(--font-mono)","font-weight":"600"}),
                html.Td(f'{row.get("lastPrice","—"):.2f}', style={"font-family":"var(--font-mono)"}),
                html.Td(f'{iv*100:.1f}%' if iv and iv > 0 else "—", style={"color":COLORS["accent_purple"],"font-family":"var(--font-mono)"}),
                html.Td(f'{int(row.get("openInterest",0)):,}', style={"font-family":"var(--font-mono)"}),
            ]))
        for _, row in puts_df[opt_cols].head(12).iterrows():
            iv = row.get("impliedVolatility", None)
            puts_rows.append(html.Tr([
                html.Td(f'{row.get("strike","—"):.2f}', style={"font-family":"var(--font-mono)","font-weight":"600"}),
                html.Td(f'{row.get("lastPrice","—"):.2f}', style={"font-family":"var(--font-mono)"}),
                html.Td(f'{iv*100:.1f}%' if iv and iv > 0 else "—", style={"color":COLORS["accent_purple"],"font-family":"var(--font-mono)"}),
                html.Td(f'{int(row.get("openInterest",0)):,}', style={"font-family":"var(--font-mono)"}),
            ]))

    # ── Seasonal premium table ────────────────────────────────────────────────
    seas_prem = []
    if not composite.empty and len(composite) > 252:
        monthly = composite.groupby(composite.index.month).mean()
        annual_avg = composite.mean()
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        for m in range(1, 13):
            if m in monthly.index:
                prem = (monthly[m] / annual_avg - 1) * 100
                color = COLORS["accent_green"] if prem > 0 else COLORS["accent_red"]
                seas_prem.append(html.Tr([
                    html.Td(months[m-1], style={"font-family":"var(--font-mono)","font-weight":"600"}),
                    html.Td(f"{monthly[m]:.1f}", style={"font-family":"var(--font-mono)"}),
                    html.Td(f"{prem:+.1f}%", style={"color":color,"font-weight":"600","font-family":"var(--font-mono)"}),
                ]))

    # FFA forward curve inputs (manual entry via callbacks)
    ffa_inputs = []
    for tenor in FFA_TENORS:
        ffa_inputs.append(
            dbc.Col([
                dbc.Label(tenor, className="form-label"),
                dbc.Input(id=f"ffa-input-{tenor.replace('+','-').replace(' ','')}",
                          type="number", placeholder="Enter value",
                          className="form-control",
                          style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
            ], width=6, md=True, className="mb-2")
        )

    return html.Div([
        page_header("📉 FFA Derivatives",
                    "BDRY options · realized volatility · manual forward curve · seasonal premium"),
        info_banner("⚠ FFA (Forward Freight Agreement) data from Baltic Exchange requires a paid subscription. "
                    "The forward curve below accepts manual inputs for scenario analysis."),

        dcc.Tabs(id="ffa-tabs", value="tab-vol",
            className="tabs-bar",
            children=[
                dcc.Tab(label="📊 Volatility",      value="tab-vol",     className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📈 Forward Curve",   value="tab-fwd",     className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📋 Options Chain",   value="tab-options", className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="🌡 Seasonal Premium", value="tab-seas",    className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # Tab: Volatility
        html.Div([
            dbc.Row([
                dbc.Col(kpi_card("Composite Spot", spot_str, subtitle="FreightIQ composite index [PROXY]"), width=6, md=3),
                dbc.Col(kpi_card("30D Ann. Vol",
                    f"{rolling_volatility(composite, 30).iloc[-1]*100:.1f}%" if not composite.empty and len(composite) > 30 else "N/A",
                    subtitle="Composite index"), width=6, md=3),
                dbc.Col(kpi_card("90D Ann. Vol",
                    f"{rolling_volatility(composite, 90).iloc[-1]*100:.1f}%" if not composite.empty and len(composite) > 90 else "N/A",
                    subtitle="Composite index"), width=6, md=3),
                dbc.Col(kpi_card("Options",
                    f"Exp: {exp}" if exp else "N/A",
                    subtitle="BDRY options chain"), width=6, md=3),
            ], className="g-2 mb-3"),
            section_header("Rolling Realised Volatility — Composite Index [PROXY]"),
            dbc.Card(dbc.CardBody(dcc.Graph(figure=_vol_chart(composite), config={"displayModeBar": True, "displaylogo": False}))),
        ], id="ffa-tab-content-vol"),

        # Tab: Forward Curve
        html.Div([
            info_banner("Enter FFA forward curve values manually (BDI-equivalent proxy points). Data auto-updates when you type."),
            dbc.Row(ffa_inputs, className="mb-3"),
            html.Div(id="ffa-fwd-chart-container"),
        ], id="ffa-tab-content-fwd", style={"display":"none"}),

        # Tab: Options
        html.Div([
            info_banner(_BDRY_OPTIONS_NOTE),
            *([
                dbc.Row([
                    dbc.Col([
                        section_header(f"Calls (expiry: {exp})"),
                        dbc.Card(dbc.CardBody(
                            html.Div(
                                html.Table([
                                    html.Thead(html.Tr([html.Th(h) for h in ["Strike","Last","IV","OI"]])),
                                    html.Tbody(calls_rows or [html.Tr(html.Td("No data", colSpan=4, style={"text-align":"center","color":"var(--text-faint)"}))]),
                                ], className="fiq-table"),
                                style={"overflow-x":"auto"},
                            )
                        )),
                    ], md=6),
                    dbc.Col([
                        section_header(f"Puts (expiry: {exp})"),
                        dbc.Card(dbc.CardBody(
                            html.Div(
                                html.Table([
                                    html.Thead(html.Tr([html.Th(h) for h in ["Strike","Last","IV","OI"]])),
                                    html.Tbody(puts_rows or [html.Tr(html.Td("No data", colSpan=4, style={"text-align":"center","color":"var(--text-faint)"}))]),
                                ], className="fiq-table"),
                                style={"overflow-x":"auto"},
                            )
                        )),
                    ], md=6),
                ], className="g-3"),
            ] if have_options else [info_banner("BDRY options data unavailable. Market may be closed or yfinance returned no data.")]),
        ], id="ffa-tab-content-options", style={"display":"none"}),

        # Tab: Seasonal Premium
        html.Div([
            dbc.Row([
                dbc.Col([
                    section_header("Monthly Seasonal Premium — Composite [PROXY]"),
                    dbc.Card(dbc.CardBody(
                        html.Div(
                            html.Table([
                                html.Thead(html.Tr([html.Th(h) for h in ["Month","Avg Level","vs Annual Avg"]])),
                                html.Tbody(seas_prem or [html.Tr(html.Td("Insufficient data", colSpan=3))]),
                            ], className="fiq-table"),
                            style={"overflow-x":"auto"},
                        )
                    )),
                ], md=6),
                dbc.Col([
                    info_banner("💡 Seasonal premiums reflect historical average for each month vs. the full-year mean. "
                                "Positive = historically above-average demand for that month. "
                                "Key: Q4 grain exports, Q1/Q2 Brazil iron ore season."),
                ], md=6),
            ], className="g-3"),
        ], id="ffa-tab-content-seas", style={"display":"none"}),
    ])


@callback(
    Output("ffa-tab-content-vol",     "style"),
    Output("ffa-tab-content-fwd",     "style"),
    Output("ffa-tab-content-options", "style"),
    Output("ffa-tab-content-seas",    "style"),
    Input("ffa-tabs", "value"),
)
def switch_ffa_tab(tab):
    tabs = ["tab-vol", "tab-fwd", "tab-options", "tab-seas"]
    return [{"display":"block"} if t == tab else {"display":"none"} for t in tabs]


@callback(
    Output("ffa-fwd-chart-container", "children"),
    [Input(f"ffa-input-{t.replace('+','-').replace(' ','')}", "value") for t in FFA_TENORS],
)
def update_fwd_curve(*args):
    from src.config import COLORS
    try:
        composite = _fdm.get_weighted_shipping_index(period="1mo")
        spot = float(composite.iloc[-1]) if not composite.empty else None
    except Exception:
        spot = None

    curve = {t: float(v) if v is not None else None for t, v in zip(FFA_TENORS, args)}
    fig = _fwd_curve_fig(spot, curve)
    return dbc.Card(dbc.CardBody(dcc.Graph(figure=fig, config={"displayModeBar": False})))
