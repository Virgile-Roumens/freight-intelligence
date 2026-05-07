import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output, State
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd
import json

from src.config import CACHE_DIR, COLORS, VESSEL_SPECS, TRADE_ROUTES
from src.utils.cache_manager import CacheManager
from src.data.commodity_data import CommodityDataManager
from src.analytics.tce_calculator import TCECalculator
from dash_components.cards import page_header, section_header, info_banner, divider

dash.register_page(__name__, path="/tce", name="TCE Calculator", order=8)

_cache = CacheManager(CACHE_DIR)
_cdm   = CommodityDataManager(_cache)
_tce   = TCECalculator()

VESSEL_CLASSES = list(VESSEL_SPECS.keys())
ROUTE_NAMES    = {k: v["display"] for k, v in TRADE_ROUTES.items()}


def _get_default_bunker() -> float:
    try:
        val = _cdm.get_bunker_price_estimate()
        return float(max(200.0, min(1200.0, val))) if val else 650.0
    except Exception:
        return 650.0


def _breakdown_table(result: dict) -> html.Div:
    if not result:
        return html.Div()
    tce_val = result.get("tce_usd_day", 0) or 0
    tce_color = COLORS["accent_green"] if tce_val > 0 else COLORS["accent_red"]

    rows = [
        ("Gross Freight Revenue", f'${result.get("gross_freight_usd", 0):,.0f}'),
        ("Commissions",           f'−${result.get("commission_usd", 0):,.0f}'),
        ("Bunker Cost",           f'−${result.get("bunker_cost_usd", 0):,.0f}'),
        ("  Fuel consumed",       f'  {result.get("bunker_consumed_mt", 0):.0f} mt'),
        ("Port Costs",            f'−${result.get("port_cost_usd", 0):,.0f}'),
        ("Canal Tolls",           f'−${result.get("canal_tolls_usd", 0):,.0f}'),
        ("Demurrage Income",      f'+${result.get("demurrage_revenue", 0):,.0f}'),
        ("Net Voyage Revenue",    f'${result.get("net_voyage_revenue", 0):,.0f}'),
        ("Total Voyage Days",     f'{result.get("total_voyage_days", 0):.1f} days'),
        ("Sea Days (laden)",      f'{result.get("sea_days_laden", 0):.1f}'),
        ("Sea Days (ballast)",    f'{result.get("sea_days_ballast", 0):.1f}'),
        ("Laytime / Port Days",   f'{result.get("port_days", 0):.1f}'),
        ("OPEX ($/day)",          f'${result.get("opex_usd_day", 0):,.0f}'),
        ("Daily P&L vs OPEX",     f'${result.get("daily_margin_usd", 0):,.0f}'),
    ]

    return html.Div([
        html.Div([
            html.Div("TCE Result", className="kpi-label"),
            html.Div(f'${tce_val:,.0f}/day', className="kpi-value",
                     style={"color": tce_color, "font-size": "2rem"}),
            html.Div(f'Time Charter Equivalent — {result.get("vessel_type","")}, '
                     f'{result.get("voyage_days", 0):.0f} voyage days',
                     className="kpi-delta kpi-delta-neu"),
        ], className="kpi-card fiq-card-accent-blue", style={"margin-bottom":"16px"}),
        html.Table([
            html.Tbody([
                html.Tr([
                    html.Td(label, style={"color":"var(--text-secondary)","font-family":"var(--font-mono)","font-size":"0.78rem","padding":"5px 0"}),
                    html.Td(value, style={"font-family":"var(--font-mono)","font-weight":"600",
                                          "text-align":"right","font-size":"0.82rem",
                                          "color":"var(--accent-red)" if value.startswith("−") else "var(--text-primary)"}),
                ]) for label, value in rows
            ]),
        ], className="fiq-table", style={"width":"100%"}),
    ])


def _sensitivity_chart(result: dict, base_params: dict) -> go.Figure:
    fig = go.Figure()
    if not result or not base_params:
        return fig
    try:
        sens_rate  = _tce.sensitivity_analysis(base_params, "freight_rate_usd_ton", range_pct=0.40, steps=20)
        sens_bunker = _tce.sensitivity_analysis(base_params, "bunker_price_usd_mt", range_pct=0.40, steps=20)
        if not sens_rate.empty:
            fig.add_trace(go.Scatter(
                x=sens_rate["value"], y=sens_rate["tce_usd_day"],
                name="vs Freight Rate ($/t)",
                line=dict(color=COLORS["accent_blue"], width=2),
                hovertemplate="Rate $%{x:.2f}/t → TCE $%{y:,.0f}/d<extra></extra>",
            ))
        if not sens_bunker.empty:
            fig.add_trace(go.Scatter(
                x=sens_bunker["value"], y=sens_bunker["tce_usd_day"],
                name="vs Bunker Price ($/mt)",
                line=dict(color=COLORS["accent_orange"], width=2, dash="dash"),
                hovertemplate="Bunker $%{x:.0f}/mt → TCE $%{y:,.0f}/d<extra></extra>",
                xaxis="x2",
            ))
        fig.add_hline(y=0, line_dash="dot", line_color=COLORS["text_secondary"], opacity=0.5)
        tce_val = result.get("tce_usd_day", 0) or 0
        fig.add_hline(y=tce_val, line_dash="dash", line_color=COLORS["accent_green"], opacity=0.8,
                      annotation_text=f"Current TCE: ${tce_val:,.0f}/d",
                      annotation_font=dict(color=COLORS["accent_green"], size=10))
    except Exception:
        pass
    fig.update_layout(height=300, hovermode="x",
                       yaxis_title="TCE ($/day)",
                       legend=dict(x=1.01, y=1, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
                       margin=dict(l=54, r=160, t=12, b=44))
    return fig


def layout(**kwargs):
    default_bunker = _get_default_bunker()
    default_vessel = "Capesize"
    default_route  = "Brazil_to_China_iron_ore"
    route_info     = TRADE_ROUTES[default_route]
    spec           = VESSEL_SPECS[default_vessel]

    return html.Div([
        page_header("🧮 TCE Calculator",
                    "Time Charter Equivalent · voyage economics · sensitivity analysis"),
        info_banner(f"💡 Bunker price auto-estimated from Brent crude × 6.5 ≈ ${default_bunker:,.0f}/mt VLSFO. "
                    "Override below. All calculations use public vessel consumption specs."),

        dbc.Row([
            # ── Input panel ────────────────────────────────────────────────────
            dbc.Col([
                section_header("Voyage Parameters"),
                dbc.Card(dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            dbc.Label("Vessel Class", className="form-label"),
                            dcc.Dropdown(id="tce-vessel",
                                options=[{"label":v,"value":v} for v in VESSEL_CLASSES],
                                value=default_vessel, clearable=False,
                                style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                        ], md=6),
                        dbc.Col([
                            dbc.Label("Trade Route (preset)", className="form-label"),
                            dcc.Dropdown(id="tce-route",
                                options=[{"label":v,"value":k} for k,v in ROUTE_NAMES.items()],
                                value=default_route, clearable=True, placeholder="Custom...",
                                style={"font-family":"var(--font-mono)","font-size":"0.8rem"}),
                        ], md=6),
                    ], className="mb-2"),
                    dbc.Row([
                        dbc.Col([dbc.Label("Cargo (MT)", className="form-label"),
                                  dbc.Input(id="tce-cargo", type="number", value=route_info["typical_cargo_mt"], min=1000, className="form-control")], md=6),
                        dbc.Col([dbc.Label("Freight Rate ($/MT)", className="form-label"),
                                  dbc.Input(id="tce-rate", type="number", value=14.0, min=0, step=0.1, className="form-control")], md=6),
                    ], className="mb-2"),
                    dbc.Row([
                        dbc.Col([dbc.Label("Laden Distance (nm)", className="form-label"),
                                  dbc.Input(id="tce-laden-nm", type="number", value=route_info["laden_nm"], min=100, className="form-control")], md=6),
                        dbc.Col([dbc.Label("Ballast Distance (nm)", className="form-label"),
                                  dbc.Input(id="tce-ballast-nm", type="number", value=route_info["ballast_nm"], min=100, className="form-control")], md=6),
                    ], className="mb-2"),
                    dbc.Row([
                        dbc.Col([dbc.Label("Speed (knots)", className="form-label"),
                                  dbc.Input(id="tce-speed", type="number", value=spec["typical_speed_knots"], min=6, max=20, step=0.1, className="form-control")], md=4),
                        dbc.Col([dbc.Label("Bunker Price ($/mt)", className="form-label"),
                                  dbc.Input(id="tce-bunker", type="number", value=default_bunker, min=200, max=1500, step=10, className="form-control")], md=4),
                        dbc.Col([dbc.Label("Port Days (laytime)", className="form-label"),
                                  dbc.Input(id="tce-laytime", type="number", value=route_info["typical_port_days"], min=0, step=0.5, className="form-control")], md=4),
                    ], className="mb-2"),
                    dbc.Row([
                        dbc.Col([dbc.Label("Port Costs ($)", className="form-label"),
                                  dbc.Input(id="tce-port-costs", type="number", value=80000, min=0, step=1000, className="form-control")], md=4),
                        dbc.Col([dbc.Label("Commission (%)", className="form-label"),
                                  dbc.Input(id="tce-commission", type="number", value=3.75, min=0, max=10, step=0.25, className="form-control")], md=4),
                        dbc.Col([dbc.Label("Canal Tolls ($)", className="form-label"),
                                  dbc.Input(id="tce-tolls", type="number", value=0, min=0, step=1000, className="form-control")], md=4),
                    ], className="mb-2"),
                    dbc.Row([
                        dbc.Col([dbc.Label("Demurrage Days", className="form-label"),
                                  dbc.Input(id="tce-dem-days", type="number", value=0, min=0, step=0.5, className="form-control")], md=6),
                        dbc.Col([dbc.Label("Demurrage Rate ($/day)", className="form-label"),
                                  dbc.Input(id="tce-dem-rate", type="number", value=0, min=0, step=500, className="form-control")], md=6),
                    ], className="mb-3"),
                    dbc.Button("Calculate TCE", id="tce-calc-btn", color="primary", className="btn-primary",
                               style={"width":"100%","font-family":"var(--font-mono)","font-size":"0.8rem"}),
                ])),
            ], md=5),

            # ── Results panel ──────────────────────────────────────────────────
            dbc.Col([
                section_header("Results"),
                dbc.Card(dbc.CardBody(
                    html.Div(
                        info_banner("Enter voyage parameters and click 'Calculate TCE'."),
                        id="tce-breakdown",
                    )
                ), className="mb-3"),
                section_header("Sensitivity Analysis"),
                dbc.Card(dbc.CardBody(
                    dcc.Graph(id="tce-sensitivity-chart", figure=go.Figure(),
                              config={"displayModeBar": False}),
                )),
            ], md=7),
        ], className="g-3"),

        dcc.Store(id="tce-result-store"),
        dcc.Store(id="tce-params-store"),
        dcc.Download(id="tce-download"),

        divider(),

        dbc.Button("⬇ Export Results (CSV)", id="tce-export-btn", color="secondary",
                   className="btn-secondary",
                   style={"font-family":"var(--font-mono)","font-size":"0.78rem","margin-top":"8px"}),
    ])


@callback(
    Output("tce-breakdown",         "children"),
    Output("tce-sensitivity-chart", "figure"),
    Output("tce-result-store",      "data"),
    Output("tce-params-store",      "data"),
    Output("tce-cargo",    "value"),
    Output("tce-laden-nm", "value"),
    Output("tce-ballast-nm","value"),
    Output("tce-laytime",  "value"),
    Output("tce-speed",    "value"),
    Input("tce-calc-btn",  "n_clicks"),
    Input("tce-route",     "value"),
    State("tce-vessel",    "value"),
    State("tce-route",     "value"),
    State("tce-cargo",     "value"),
    State("tce-rate",      "value"),
    State("tce-laden-nm",  "value"),
    State("tce-ballast-nm","value"),
    State("tce-speed",     "value"),
    State("tce-bunker",    "value"),
    State("tce-laytime",   "value"),
    State("tce-port-costs","value"),
    State("tce-commission","value"),
    State("tce-tolls",     "value"),
    State("tce-dem-days",  "value"),
    State("tce-dem-rate",  "value"),
    prevent_initial_call=True,
)
def calculate_tce(n_clicks, route_selected, vessel, route, cargo, rate, laden_nm, ballast_nm,
                  speed, bunker, laytime, port_costs, commission, tolls, dem_days, dem_rate):
    from dash import ctx

    # If route was changed, update preset values
    cargo_out = cargo
    laden_out  = laden_nm
    bal_out    = ballast_nm
    laytime_out = laytime
    speed_out   = speed

    if ctx.triggered_id == "tce-route" and route_selected and route_selected in TRADE_ROUTES:
        ri = TRADE_ROUTES[route_selected]
        cargo_out   = ri["typical_cargo_mt"]
        laden_out   = ri["laden_nm"]
        bal_out     = ri["ballast_nm"]
        laytime_out = ri["typical_port_days"]
        if vessel in VESSEL_SPECS:
            speed_out = VESSEL_SPECS[vessel]["typical_speed_knots"]
        return (
            info_banner("Route preset loaded — click 'Calculate TCE' to compute."),
            go.Figure(),
            None, None,
            cargo_out, laden_out, bal_out, laytime_out, speed_out,
        )

    # Calculate
    try:
        params = dict(
            vessel_type          = vessel or "Capesize",
            cargo_tons           = float(cargo or 180000),
            freight_rate_usd_ton = float(rate or 14.0),
            laden_distance_nm    = float(laden_nm or 11200),
            ballast_distance_nm  = float(ballast_nm or 9500),
            speed_knots          = float(speed or 12.5),
            bunker_price_usd_mt  = float(bunker or 650),
            port_costs_total     = float(port_costs or 80000),
            commissions_pct      = float(commission or 3.75),
            laytime_days         = float(laytime or 4.0),
            demurrage_days       = float(dem_days or 0),
            demurrage_rate       = float(dem_rate or 0),
            canal_tolls          = float(tolls or 0),
        )
        result = _tce.calculate_tce(**params)
        sens_fig = _sensitivity_chart(result, params)
        return (
            _breakdown_table(result),
            sens_fig,
            json.dumps(result),
            json.dumps(params),
            cargo_out, laden_out, bal_out, laytime_out, speed_out,
        )
    except Exception as e:
        from dash_components.cards import info_banner as ib
        return (ib(f"Calculation error: {e}", "error"), go.Figure(), None, None,
                cargo_out, laden_out, bal_out, laytime_out, speed_out)


@callback(
    Output("tce-download", "data"),
    Input("tce-export-btn", "n_clicks"),
    State("tce-result-store", "data"),
    State("tce-params-store", "data"),
    prevent_initial_call=True,
)
def export_csv(n_clicks, result_data, params_data):
    if not result_data:
        return None
    try:
        result = json.loads(result_data)
        params = json.loads(params_data) if params_data else {}
        rows = {**params, **result}
        df = pd.DataFrame([rows])
        return dcc.send_data_frame(df.to_csv, "tce_result.csv", index=False)
    except Exception:
        return None
