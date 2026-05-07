import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from datetime import datetime, timedelta
import pandas as pd

from src.config import CACHE_DIR, COLORS, NEWS_FEEDS
from src.utils.cache_manager import CacheManager
from src.data.news_data import NewsAggregator
from dash_components.cards import (page_header, section_header, info_banner,
                                     divider, news_card, signal_card)

dash.register_page(__name__, path="/intelligence", name="Intelligence Feed", order=9)

_cache = CacheManager(CACHE_DIR)
_nd    = NewsAggregator(_cache)

SIGNAL_TYPES = ["All", "DISRUPTION", "REGULATORY", "MARKET_MOVE", "GEOPOLITICAL", "SUPPLY"]
SOURCES = ["All"] + list(NEWS_FEEDS.keys())


def layout(**kwargs):
    try:
        articles = _nd.fetch_all_feeds(max_per_feed=20)
    except Exception:
        articles = []

    if not articles:
        return html.Div([
            page_header("📰 Intelligence Feed",
                        "Freight & shipping news · signal detection · weekly briefing"),
            info_banner("News feeds unavailable. Check network connection or RSS feed URLs in src/config.py.", "error"),
        ])

    # ── Feed health ────────────────────────────────────────────────────────────
    feed_health = {}
    for src, url in NEWS_FEEDS.items():
        count = len([a for a in articles if a.get("source") == src])
        feed_health[src] = count

    health_rows = [
        html.Tr([
            html.Td(src, style={"font-family":"var(--font-mono)","font-weight":"600"}),
            html.Td(f'{count} articles',
                    style={"color": COLORS["accent_green"] if count > 0 else COLORS["accent_red"],
                           "font-family":"var(--font-mono)","font-weight":"600"}),
            html.Td("✓ OK" if count > 0 else "✗ Failed",
                    style={"color": COLORS["accent_green"] if count > 0 else COLORS["accent_red"]}),
        ])
        for src, count in feed_health.items()
    ]

    # ── Signal detection ───────────────────────────────────────────────────────
    signals = _nd.detect_signals(articles)

    # ── Weekly briefing ────────────────────────────────────────────────────────
    briefing = _nd.generate_weekly_briefing(articles)

    # ── Sort & filter articles by relevance ───────────────────────────────────
    sorted_articles = sorted(articles, key=lambda x: x.get("score", 0), reverse=True)

    # Score distribution chart
    score_fig = go.Figure(go.Histogram(
        x=[a.get("score", 0) for a in articles],
        nbinsx=20,
        marker_color=COLORS["accent_blue"], opacity=0.8,
        hovertemplate="Score bin %{x:.2f}–%{x:.2f}: %{y} articles<extra></extra>",
    ))
    score_fig.update_layout(height=180, xaxis_title="Relevance Score", yaxis_title="Count",
                             showlegend=False, margin=dict(l=44, r=16, t=8, b=36))

    # Source distribution
    source_counts = {}
    for a in articles:
        src = a.get("source", "Other")
        source_counts[src] = source_counts.get(src, 0) + 1

    src_fig = go.Figure(go.Bar(
        x=list(source_counts.keys()),
        y=list(source_counts.values()),
        marker_color=COLORS["accent_purple"], opacity=0.8,
        hovertemplate="<b>%{x}</b>: %{y} articles<extra></extra>",
    ))
    src_fig.update_layout(height=180, xaxis_title=None, yaxis_title="Articles",
                           showlegend=False, margin=dict(l=44, r=16, t=8, b=60),
                           xaxis_tickangle=-30)

    # ── Top articles ──────────────────────────────────────────────────────────
    news_cards = [
        news_card(a["title"][:100], a["source"], a.get("published", ""), a["link"], a["score"])
        for a in sorted_articles[:20]
    ]

    # ── Signal cards ─────────────────────────────────────────────────────────
    signal_items = [signal_card(s["text"][:120], s["level"]) for s in signals[:8]]
    if not signal_items:
        signal_items = [info_banner("No significant signals detected in current feed.")]

    return html.Div([
        page_header("📰 Intelligence Feed",
                    "Freight & shipping news · signal detection · weekly briefing"),

        dcc.Tabs(id="intel-tabs", value="tab-feed",
            className="tabs-bar",
            children=[
                dcc.Tab(label="📰 News Feed",        value="tab-feed",     className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="⚡ Signals",           value="tab-signals",  className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📋 Weekly Briefing",  value="tab-briefing", className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📡 Feed Health",      value="tab-health",   className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # ── Tab: Feed ─────────────────────────────────────────────────────
        html.Div([
            dbc.Row([
                dbc.Col([
                    # Source filter
                    dbc.Label("Filter by Source", className="form-label"),
                    dcc.Dropdown(id="intel-source-filter",
                        options=[{"label": s, "value": s} for s in SOURCES],
                        value="All", clearable=False,
                        style={"font-family":"var(--font-mono)","font-size":"0.8rem","margin-bottom":"12px"}),
                    # Score filter
                    dbc.Label("Minimum Relevance Score", className="form-label"),
                    dcc.Slider(id="intel-score-filter", min=0, max=1, step=0.05, value=0,
                               marks={0:"0", 0.25:"25%", 0.5:"50%", 0.75:"75%", 1:"100%"},
                               className="mb-3"),
                    html.Div(id="intel-feed-count", className="kpi-label", style={"margin-bottom":"8px"}),
                    html.Div(id="intel-articles-container",
                             children=news_cards,
                             style={"max-height":"600px","overflow-y":"auto"}),
                ], md=8),
                dbc.Col([
                    section_header("Relevance Distribution"),
                    dbc.Card(dbc.CardBody(dcc.Graph(figure=score_fig, config={"displayModeBar": False})), className="mb-3"),
                    section_header("Articles per Source"),
                    dbc.Card(dbc.CardBody(dcc.Graph(figure=src_fig, config={"displayModeBar": False}))),
                ], md=4),
            ], className="g-3"),
        ], id="intel-tab-content-feed"),

        # ── Tab: Signals ──────────────────────────────────────────────────
        html.Div([
            section_header(f"Automated Signal Detection ({len(signals)} signals)"),
            info_banner("Signals are auto-detected from article titles and summaries using keyword pattern matching."),
            *signal_items,
        ], id="intel-tab-content-signals", style={"display":"none"}),

        # ── Tab: Briefing ─────────────────────────────────────────────────
        html.Div([
            section_header("Weekly Intelligence Briefing"),
            dbc.Card(dbc.CardBody(
                dcc.Markdown(briefing or "_No briefing available. Insufficient recent articles._",
                             style={"font-family":"var(--font-sans)","font-size":"0.86rem",
                                    "color":"var(--text-primary)","line-height":"1.7"}),
            )),
        ], id="intel-tab-content-briefing", style={"display":"none"}),

        # ── Tab: Feed Health ──────────────────────────────────────────────
        html.Div([
            section_header("RSS Feed Status"),
            dbc.Card(dbc.CardBody(
                html.Div(
                    html.Table([
                        html.Thead(html.Tr([html.Th(h) for h in ["Source","Articles","Status"]])),
                        html.Tbody(health_rows),
                    ], className="fiq-table"),
                    style={"overflow-x":"auto"},
                )
            )),
            divider(),
            info_banner(f"Last fetched: {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
                        f"Cache TTL: 2 hours · Total articles: {len(articles)}"),
        ], id="intel-tab-content-health", style={"display":"none"}),

        dcc.Store(id="intel-articles-store",
                  data=[{"title":a["title"][:100],"source":a["source"],
                          "published":a.get("published",""),"link":a["link"],
                          "score":a["score"]} for a in sorted_articles]),
    ])


@callback(
    Output("intel-tab-content-feed",     "style"),
    Output("intel-tab-content-signals",  "style"),
    Output("intel-tab-content-briefing", "style"),
    Output("intel-tab-content-health",   "style"),
    Input("intel-tabs", "value"),
)
def switch_intel_tab(tab):
    tabs = ["tab-feed", "tab-signals", "tab-briefing", "tab-health"]
    return [{"display":"block"} if t == tab else {"display":"none"} for t in tabs]


@callback(
    Output("intel-articles-container", "children"),
    Output("intel-feed-count", "children"),
    Input("intel-source-filter", "value"),
    Input("intel-score-filter",  "value"),
    Input("intel-articles-store","data"),
)
def filter_articles(source, min_score, store_data):
    if not store_data:
        return [], ""
    filtered = [
        a for a in store_data
        if (source == "All" or a.get("source") == source)
        and a.get("score", 0) >= (min_score or 0)
    ]
    cards = [
        news_card(a["title"], a["source"], a.get("published",""), a["link"], a["score"])
        for a in filtered[:25]
    ]
    count_label = f"{len(filtered)} articles"
    return cards, count_label
