import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd
import numpy as np

from src.config import CACHE_DIR, COLORS
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.data.commodity_data import CommodityDataManager
from src.analytics.correlation_engine import CorrelationEngine
from dash_components.cards import page_header, section_header, info_banner, divider

dash.register_page(__name__, path="/cross-commodity", name="Cross-Commodity", order=7)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_cdm   = CommodityDataManager(_cache)
_ce    = CorrelationEngine()


def _corr_heatmap(df: pd.DataFrame) -> go.Figure:
    if df.empty or df.shape[1] < 2:
        return go.Figure()
    corr = df.pct_change().dropna().corr().round(2)
    labels = list(corr.columns)
    fig = go.Figure(go.Heatmap(
        z=corr.values,
        x=labels, y=labels,
        colorscale=[[0, COLORS["accent_red"]], [0.5, COLORS["bg_card"]], [1, COLORS["accent_green"]]],
        zmid=0, zmin=-1, zmax=1,
        text=corr.values.round(2),
        texttemplate="%{text:.2f}",
        textfont=dict(size=10),
        colorbar=dict(title="Correlation", tickfont=dict(size=10)),
        hovertemplate="<b>%{y}</b> vs <b>%{x}</b><br>r = <b>%{z:.2f}</b><extra></extra>",
    ))
    fig.update_layout(height=400, margin=dict(l=100, r=40, t=12, b=100),
                       xaxis_tickangle=-45, yaxis_tickangle=0)
    return fig


def _scatter_fig(df: pd.DataFrame, x_col: str, y_col: str) -> go.Figure:
    fig = go.Figure()
    if df.empty or x_col not in df.columns or y_col not in df.columns:
        return fig
    ret = df[[x_col, y_col]].pct_change().dropna()
    x_v, y_v = ret[x_col].values, ret[y_col].values
    fig.add_trace(go.Scatter(
        x=x_v, y=y_v, mode="markers",
        marker=dict(size=5, color=COLORS["accent_blue"], opacity=0.5),
        hovertemplate=f"<b>{x_col}</b>: %{{x:.2f}}%<br><b>{y_col}</b>: %{{y:.2f}}%<extra></extra>",
    ))
    try:
        z = np.polyfit(x_v, y_v, 1)
        p = np.poly1d(z)
        x_line = np.linspace(x_v.min(), x_v.max(), 80)
        corr_val = np.corrcoef(x_v, y_v)[0, 1]
        fig.add_trace(go.Scatter(x=x_line, y=p(x_line), mode="lines",
            line=dict(color=COLORS["accent_orange"], width=1.5, dash="dash"),
            name=f"Trend (r={corr_val:.2f})"))
    except Exception:
        pass
    fig.update_layout(height=300, showlegend=True,
                       xaxis_title=f"{x_col} daily return %", yaxis_title=f"{y_col} daily return %",
                       legend=dict(x=0, y=1, bgcolor="rgba(0,0,0,0)"),
                       margin=dict(l=50, r=16, t=12, b=44))
    return fig


def _lead_lag_fig(s1: pd.Series, s2: pd.Series, n1: str, n2: str) -> go.Figure:
    fig = go.Figure()
    if s1.empty or s2.empty:
        return fig
    try:
        ll = _ce.cross_correlation_leadlag(s1, s2, max_lag=52)
        if ll.empty:
            return fig
        fig.add_trace(go.Bar(
            x=ll["lag"], y=ll["correlation"].round(3),
            marker_color=[COLORS["accent_green"] if v > 0 else COLORS["accent_red"]
                          for v in ll["correlation"]],
            hovertemplate="Lag %{x} weeks<br>r = <b>%{y:.3f}</b><extra></extra>",
        ))
        fig.add_hline(y=0, line_dash="dot", line_color=COLORS["text_secondary"], opacity=0.4)
        best_row = ll.loc[ll["correlation"].abs().idxmax()]
        fig.add_vline(x=float(best_row["lag"]), line_dash="dash", line_color=COLORS["accent_yellow"],
                      annotation_text=f'Best: lag={int(best_row["lag"])}w r={best_row["correlation"]:.2f}',
                      annotation_font=dict(color=COLORS["accent_yellow"], size=10))
    except Exception:
        pass
    fig.update_layout(height=280, xaxis_title="Lag (weeks)", yaxis_title="Pearson r",
                       showlegend=False, margin=dict(l=44, r=16, t=12, b=44))
    return fig


def layout(**kwargs):
    try:
        composite  = _fdm.get_weighted_shipping_index(period="2y")
        all_comm   = _cdm.get_all_commodities(period="2y")
    except Exception:
        composite  = pd.Series(dtype=float)
        all_comm   = pd.DataFrame()

    # Build combined df
    frames = {}
    if not composite.empty:
        frames["Freight [PROXY]"] = composite
    if not all_comm.empty:
        for col in all_comm.columns:
            frames[col] = all_comm[col]
    if not frames:
        return html.Div([page_header("🔗 Cross-Commodity"), info_banner("No data available.")])

    combined = pd.DataFrame(frames).dropna(how="all")

    all_cols = list(combined.columns)
    default_x = "Freight [PROXY]" if "Freight [PROXY]" in all_cols else all_cols[0]
    default_y = "WTI Crude" if "WTI Crude" in all_cols else (all_cols[1] if len(all_cols) > 1 else all_cols[0])

    # Full correlation heatmap
    corr_fig = _corr_heatmap(combined)

    # Default scatter
    scatter_fig = _scatter_fig(combined, default_x, default_y)

    # Default lead-lag
    if default_x in combined.columns and default_y in combined.columns:
        s1 = combined[default_x].resample("W").last().dropna()
        s2 = combined[default_y].resample("W").last().dropna()
        ll_fig = _lead_lag_fig(s1, s2, default_x, default_y)
    else:
        ll_fig = go.Figure()

    return html.Div([
        page_header("🔗 Cross-Commodity Correlations",
                    "Correlation matrix · scatter pairs · lead-lag analysis"),

        dcc.Tabs(id="cc-tabs", value="tab-heatmap",
            className="tabs-bar",
            children=[
                dcc.Tab(label="🔥 Correlation Matrix", value="tab-heatmap", className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="↗ Scatter Pair",        value="tab-scatter", className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="↔ Lead-Lag",             value="tab-leadlag", className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # Tab: Heatmap
        html.Div([
            section_header("Rolling Correlation Matrix — Daily Returns"),
            dbc.Card(dbc.CardBody(dcc.Graph(figure=corr_fig, config={"displayModeBar": False}))),
        ], id="cc-tab-heatmap"),

        # Tab: Scatter
        html.Div([
            info_banner("Select two series to plot daily-return scatter with trend line and Pearson r."),
            dbc.Row([
                dbc.Col([
                    dbc.Label("X Axis", className="form-label"),
                    dcc.Dropdown(id="cc-scatter-x", options=[{"label":c,"value":c} for c in all_cols],
                                 value=default_x, clearable=False,
                                 style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                ], md=5),
                dbc.Col([
                    dbc.Label("Y Axis", className="form-label"),
                    dcc.Dropdown(id="cc-scatter-y", options=[{"label":c,"value":c} for c in all_cols],
                                 value=default_y, clearable=False,
                                 style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                ], md=5),
            ], className="mb-3"),
            dbc.Card(dbc.CardBody(dcc.Graph(id="cc-scatter-chart", figure=scatter_fig,
                                            config={"displayModeBar": False}))),
        ], id="cc-tab-scatter", style={"display":"none"}),

        # Tab: Lead-Lag
        html.Div([
            info_banner("Cross-correlation analysis: positive lag = X leads Y. Peak bar = optimal lead/lag weeks."),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Series A", className="form-label"),
                    dcc.Dropdown(id="cc-lag-x", options=[{"label":c,"value":c} for c in all_cols],
                                 value=default_x, clearable=False,
                                 style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                ], md=5),
                dbc.Col([
                    dbc.Label("Series B", className="form-label"),
                    dcc.Dropdown(id="cc-lag-y", options=[{"label":c,"value":c} for c in all_cols],
                                 value=default_y, clearable=False,
                                 style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                ], md=5),
            ], className="mb-3"),
            dbc.Card(dbc.CardBody(dcc.Graph(id="cc-lag-chart", figure=ll_fig,
                                            config={"displayModeBar": False}))),
        ], id="cc-tab-leadlag", style={"display":"none"}),

        dcc.Store(id="cc-combined-store", data=combined.to_json(date_format="iso")),
    ])


@callback(
    Output("cc-tab-heatmap",  "style"),
    Output("cc-tab-scatter",  "style"),
    Output("cc-tab-leadlag",  "style"),
    Input("cc-tabs", "value"),
)
def switch_cc_tab(tab):
    tabs = ["tab-heatmap", "tab-scatter", "tab-leadlag"]
    return [{"display":"block"} if t == tab else {"display":"none"} for t in tabs]


@callback(
    Output("cc-scatter-chart", "figure"),
    Input("cc-scatter-x", "value"),
    Input("cc-scatter-y", "value"),
    Input("cc-combined-store", "data"),
)
def update_scatter(x_col, y_col, store_data):
    if not store_data or not x_col or not y_col:
        return go.Figure()
    try:
        df = pd.read_json(store_data)
        return _scatter_fig(df, x_col, y_col)
    except Exception:
        return go.Figure()


@callback(
    Output("cc-lag-chart", "figure"),
    Input("cc-lag-x", "value"),
    Input("cc-lag-y", "value"),
    Input("cc-combined-store", "data"),
)
def update_leadlag(x_col, y_col, store_data):
    if not store_data or not x_col or not y_col:
        return go.Figure()
    try:
        df = pd.read_json(store_data)
        if x_col not in df.columns or y_col not in df.columns:
            return go.Figure()
        s1 = df[x_col].dropna()
        s1.index = pd.to_datetime(s1.index)
        s1 = s1.resample("W").last().dropna()
        s2 = df[y_col].dropna()
        s2.index = pd.to_datetime(s2.index)
        s2 = s2.resample("W").last().dropna()
        return _lead_lag_fig(s1, s2, x_col, y_col)
    except Exception:
        return go.Figure()
