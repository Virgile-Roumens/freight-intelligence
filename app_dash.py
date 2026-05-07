import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from datetime import datetime

import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, Input, Output, callback, clientside_callback

from src.config import APP_NAME, APP_VERSION, FRED_API_KEY
from src.utils.ui_styles import register_plotly_template

register_plotly_template()

app = dash.Dash(
    __name__,
    use_pages=True,
    pages_folder="dash_pages",
    external_stylesheets=[
        dbc.themes.CYBORG,
        "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap",
    ],
    suppress_callback_exceptions=True,
    title="FreightIQ — Dry Bulk Intelligence",
    update_title=None,
    meta_tags=[
        {"name": "viewport", "content": "width=device-width, initial-scale=1"},
        {"name": "description", "content": "Professional dry bulk freight intelligence platform"},
    ],
)
server = app.server

NAV_ITEMS = [
    {"href": "/",                "icon": "🏠", "label": "Overview"},
    {"href": "/market",          "icon": "📊", "label": "Market Dashboard"},
    {"href": "/freight",         "icon": "🚢", "label": "Freight Analysis"},
    {"href": "/supply-demand",   "icon": "⚖️",  "label": "Supply & Demand"},
    {"href": "/geopolitical",    "icon": "🌍", "label": "Geopolitical Intel"},
    {"href": "/macro",           "icon": "📈", "label": "Macro Overlay"},
    {"href": "/ffa",             "icon": "📉", "label": "FFA Derivatives"},
    {"href": "/cross-commodity", "icon": "🔗", "label": "Cross-Commodity"},
    {"href": "/tce",             "icon": "🧮", "label": "TCE Calculator"},
    {"href": "/intelligence",    "icon": "📰", "label": "Intelligence Feed"},
]

_PAGE_LABELS = {item["href"]: item["label"] for item in NAV_ITEMS}


def _nav_id(href: str) -> str:
    return "nav" + href.replace("/", "-").replace("--", "-").rstrip("-") or "nav-home"


def build_sidebar() -> html.Div:
    fred_status = (
        html.Span("FRED ✓", className="status-chip status-chip-green")
        if FRED_API_KEY
        else html.Span("FRED —", className="status-chip")
    )

    nav_links = [
        dcc.Link(
            href=item["href"],
            children=html.Div(
                [
                    html.Span(item["icon"], className="nav-icon"),
                    html.Span(item["label"], className="nav-label"),
                ],
                className="nav-item",
                id=_nav_id(item["href"]),
            ),
            className="nav-link-wrapper",
        )
        for item in NAV_ITEMS
    ]

    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [html.Span("⚓ ", style={"opacity": "0.7"}), html.Span("FreightIQ", className="sidebar-title")],
                        className="sidebar-logo",
                    ),
                    html.Div("Dry Bulk Intelligence", className="sidebar-subtitle"),
                ],
                className="sidebar-header",
            ),
            html.Div(
                [
                    html.Div("Analytics", className="sidebar-section-label"),
                    *nav_links[:6],
                    html.Div("Tools", className="sidebar-section-label"),
                    *nav_links[6:],
                ],
                className="sidebar-nav",
            ),
            html.Div(
                [
                    fred_status,
                    html.Div(f"FreightIQ v{APP_VERSION}", className="sidebar-version"),
                    html.Div(
                        "⚠ Freight indices are market proxies. Baltic Exchange data requires subscription.",
                        className="sidebar-disclaimer",
                    ),
                ],
                className="sidebar-footer",
            ),
        ],
        className="sidebar",
        id="sidebar",
    )


app.layout = html.Div(
    [
        dcc.Location(id="url", refresh=False),
        dcc.Interval(id="clock-interval", interval=60_000, n_intervals=0),
        dcc.Store(id="store-reload-trigger", data=0),
        build_sidebar(),
        html.Div(
            [
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span("FreightIQ", className="topbar-sep", style={"color": "var(--text-faint)"}),
                                html.Span(" / ", className="topbar-sep"),
                                html.Span(id="topbar-page-label", className="topbar-breadcrumb-text"),
                            ],
                            className="topbar-left",
                        ),
                        html.Div(
                            [
                                html.Span(id="topbar-time", className="topbar-time"),
                                html.Button(
                                    "↻ Refresh",
                                    id="btn-global-refresh",
                                    className="topbar-refresh-btn",
                                    n_clicks=0,
                                ),
                            ],
                            className="topbar-right",
                        ),
                    ],
                    className="topbar",
                ),
                html.Div(
                    dash.page_container,
                    className="page-content",
                    id="page-content-wrapper",
                ),
            ],
            className="main-wrapper",
        ),
    ],
    className="app-shell",
)


@callback(Output("topbar-page-label", "children"), Input("url", "pathname"))
def update_breadcrumb(pathname: str) -> str:
    return _PAGE_LABELS.get(pathname, "Overview")


@callback(Output("topbar-time", "children"), Input("clock-interval", "n_intervals"))
def update_clock(_):
    return datetime.now().strftime("%Y-%m-%d  %H:%M")


# Highlight active nav item
for _item in NAV_ITEMS:
    _id = _nav_id(_item["href"])
    _href = _item["href"]

    @callback(
        Output(_id, "className"),
        Input("url", "pathname"),
        prevent_initial_call=False,
    )
    def _mark_active(pathname, _href=_href):
        if pathname == _href or (pathname and pathname.rstrip("/") == _href.rstrip("/")):
            return "nav-item nav-active"
        return "nav-item"


if __name__ == "__main__":
    app.run(port=8503, debug=False, host="127.0.0.1")
