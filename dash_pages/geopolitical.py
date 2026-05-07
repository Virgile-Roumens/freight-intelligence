import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd

from src.config import (CACHE_DIR, COLORS, CHOKEPOINTS, HISTORICAL_EVENTS,
                         SANCTIONS_DATA)
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.data.news_data import NewsAggregator
from dash_components.cards import (page_header, section_header, info_banner,
                                     divider, status_badge, news_card)

dash.register_page(__name__, path="/geopolitical", name="Geopolitical Intel", order=4)

_cache = CacheManager(CACHE_DIR)
_fdm   = FreightDataManager(_cache)
_nd    = NewsAggregator(_cache)


def _chokepoint_map() -> go.Figure:
    fig = go.Figure()
    status_colors = {"OPEN": COLORS["accent_green"], "RESTRICTED": COLORS["accent_yellow"],
                     "DISRUPTED": COLORS["accent_red"]}
    for name, info in CHOKEPOINTS.items():
        color = status_colors.get(info["status"], COLORS["text_secondary"])
        size  = 14 if info["status"] != "OPEN" else 10
        fig.add_trace(go.Scattergeo(
            lat=[info["lat"]], lon=[info["lon"]],
            mode="markers+text",
            marker=dict(size=size, color=color, symbol="circle",
                        line=dict(width=1, color="rgba(255,255,255,0.3)")),
            text=[name.split("/")[0].strip()],
            textposition="top center",
            textfont=dict(size=9, color=color),
            name=name,
            hovertemplate=(
                f"<b>{name}</b><br>"
                f"Status: {info['status']}<br>"
                f"Dry bulk: {info['annual_dry_bulk_pct']}% of trade<br>"
                f"Supply impact: {info['effective_supply_impact_pct']}%<br>"
                f"{info['notes']}<extra></extra>"
            ),
        ))

    fig.update_layout(
        height=380,
        geo=dict(
            showframe=False, showcoastlines=True, showland=True, showocean=True,
            landcolor="#1a1f2e", oceancolor="#0d1117",
            coastlinecolor=COLORS["border"], showcountries=True, countrycolor=COLORS["border"],
            bgcolor=COLORS["bg_primary"], projection_type="natural earth",
            center=dict(lat=20, lon=60), projection_scale=1.4,
        ),
        showlegend=False,
        margin=dict(l=0, r=0, t=8, b=8),
        paper_bgcolor=COLORS["bg_card"],
    )
    return fig


def _bdi_events_chart(composite: pd.Series) -> go.Figure:
    fig = go.Figure()
    if not composite.empty:
        norm = composite / composite.iloc[0] * 100
        fig.add_trace(go.Scatter(
            x=norm.index, y=norm.round(2).values,
            name="Composite [PROXY]",
            line=dict(color=COLORS["accent_blue"], width=2),
            hovertemplate="%{x|%d %b %Y}: <b>%{y:.1f}</b><extra></extra>",
        ))
    for ev in HISTORICAL_EVENTS:
        fig.add_shape(type="line", x0=ev["date"], x1=ev["date"],
                      y0=0, y1=1, yref="paper",
                      line=dict(width=1.5, dash="dot", color=ev["color"]))
        fig.add_annotation(x=ev["date"], y=0.97, yref="paper",
                            text=ev["label"], showarrow=False,
                            textangle=-90, xanchor="right",
                            font=dict(size=9, color=ev["color"]))
    fig.update_xaxes(
        rangeselector=dict(
            buttons=[dict(count=1,label="1Y",step="year",stepmode="backward"),
                     dict(count=3,label="3Y",step="year",stepmode="backward"),
                     dict(step="all",label="All")],
            bgcolor=COLORS["bg_card"], activecolor=COLORS["accent_blue"],
            font=dict(color=COLORS["text_primary"], size=11),
        ),
    )
    fig.update_layout(height=320, hovermode="x unified",
                       legend=dict(x=0, y=1, bgcolor="rgba(0,0,0,0)"),
                       margin=dict(l=44, r=16, t=12, b=40))
    return fig


def layout(**kwargs):
    try:
        composite = _fdm.get_weighted_shipping_index(period="5y")
        articles  = _nd.fetch_all_feeds(max_per_feed=10)
    except Exception:
        composite, articles = pd.Series(dtype=float), []

    geo_articles = [a for a in (articles or []) if any(
        kw.lower() in (a.get("title","") + a.get("summary","")).lower()
        for kw in ["red sea","suez","panama","houthi","ukraine","sanctions","war","conflict","blockade"]
    )]

    chk_rows = []
    for name, info in CHOKEPOINTS.items():
        status_class = {"OPEN":"badge-open","RESTRICTED":"badge-restricted","DISRUPTED":"badge-disrupted"}.get(info["status"],"")
        chk_rows.append(html.Tr([
            html.Td(html.B(name), style={"font-weight":"600"}),
            html.Td(html.Span(f'{"🟢" if info["status"]=="OPEN" else "🟡" if info["status"]=="RESTRICTED" else "🔴"} {info["status"]}',
                              className=status_class)),
            html.Td(f'{info["annual_dry_bulk_pct"]}%', style={"font-weight":"600","font-family":"var(--font-mono)"}),
            html.Td(info["rerouting_via"], style={"color":"var(--text-secondary)","font-size":"0.72rem"}),
            html.Td(f'{info.get("extra_distance_nm",0):,} nm', style={"font-family":"var(--font-mono)"}),
            html.Td(f'{info["effective_supply_impact_pct"]:+d}%',
                    style={"color": COLORS["accent_red"] if info["effective_supply_impact_pct"] < 0
                                    else COLORS["accent_green"], "font-weight":"600","font-family":"var(--font-mono)"}),
            html.Td(info["notes"], style={"font-size":"0.7rem","color":"var(--text-secondary)"}),
        ]))

    sanc_rows = []
    for s in SANCTIONS_DATA:
        sanc_rows.append(html.Tr([
            html.Td(s["entity"], style={"font-weight":"500"}),
            html.Td(f'{s["vessels_est"]:,}', style={"font-weight":"600","font-family":"var(--font-mono)"}),
            html.Td(f'{s["dwt_mt"]:.1f}M DWT', style={"font-family":"var(--font-mono)"}),
            html.Td(status_badge(s["status"])),
        ]))

    news_items = [news_card(a["title"][:100], a["source"], a.get("published",""),
                            a["link"], a["score"]) for a in geo_articles[:8]]
    if not news_items:
        news_items = [info_banner("No geopolitical news items detected. Check network connection.")]

    return html.Div([
        page_header("🌍 Geopolitical Intelligence",
                    "Chokepoint status · sanctions · historical events · shipping risk news"),

        dbc.Row([
            dbc.Col([
                section_header("Chokepoint Risk Map"),
                dbc.Card(dbc.CardBody(dcc.Graph(figure=_chokepoint_map(), config={"displayModeBar": True, "displaylogo": False}))),
            ], md=7),
            dbc.Col([
                section_header("Geopolitical News"),
                html.Div(news_items, style={"max-height":"380px","overflow-y":"auto"}),
            ], md=5),
        ], className="g-3 mb-3"),

        divider(),

        section_header("Chokepoint Status — Detailed [LIVE / MANUALLY UPDATED]"),
        dbc.Card(dbc.CardBody(
            html.Div(
                html.Table([
                    html.Thead(html.Tr([html.Th(h) for h in ["Chokepoint","Status","Dry Bulk %","Reroute Via","Extra NM","Supply Impact","Notes"]])),
                    html.Tbody(chk_rows),
                ], className="fiq-table"),
                style={"overflow-x":"auto"},
            )
        )),

        divider(),

        dbc.Row([
            dbc.Col([
                section_header("Historical Events on Composite Index [PROXY]"),
                dbc.Card(dbc.CardBody(dcc.Graph(figure=_bdi_events_chart(composite),
                                                config={"displayModeBar": True, "displaylogo": False}))),
            ], md=8),
            dbc.Col([
                section_header("Sanctioned & Shadow Fleet"),
                dbc.Card(dbc.CardBody(
                    html.Div(
                        html.Table([
                            html.Thead(html.Tr([html.Th(h) for h in ["Entity","Vessels","DWT","Status"]])),
                            html.Tbody(sanc_rows),
                        ], className="fiq-table"),
                        style={"overflow-x":"auto"},
                    )
                )),
                divider(),
                info_banner("⚠ Chokepoint statuses are manually maintained. Update src/config.py → CHOKEPOINTS to reflect current conditions."),
            ], md=4),
        ], className="g-3"),
    ])
