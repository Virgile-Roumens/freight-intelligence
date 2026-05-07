import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import dash
from dash import html, dcc, callback, Input, Output, ctx
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pandas as pd

from src.config import (
    CACHE_DIR, COLORS,
    FLEET_ESTIMATES_2024, ORDERBOOK_2024,
    SANCTIONS_DATA, IMO_REGULATIONS,
    CHOKEPOINTS,
    AIS_API_KEY, AIS_WS_URL, AIS_COLLECT_SECONDS,
    AIS_CACHE_TTL_MINUTES, AIS_BULK_CARRIER_TYPES,
)
from src.utils.cache_manager import CacheManager
from src.data.freight_data import FreightDataManager
from src.data.ais_data import (
    fetch_live_vessels, get_route_traffic, get_port_zone_counts,
    get_chokepoint_traffic,
)
from dash_components.cards import (
    page_header, section_header, info_banner, divider, status_badge,
)

dash.register_page(__name__, path="/supply-demand", name="Supply & Demand", order=3)

_cache = CacheManager(CACHE_DIR)

# ── Segment colours — canonical names matching ais_data.py ───────────────────
SEG_COLORS = {
    "Capesize":     "#58a6ff",
    "Panamax":      "#d29922",
    "Supramax":     "#3fb950",
    "Handysize":    "#db6d28",
    "Bulk Carrier": "#bc8cff",
}

SEGMENT_COLORS = {
    "Capesize":  COLORS["accent_blue"],
    "Panamax":   COLORS["accent_yellow"],
    "Supramax":  COLORS["accent_green"],
    "Handysize": COLORS["accent_orange"],
}


# ── Static fleet charts (built once per layout call) ─────────────────────────

def _fleet_chart() -> go.Figure:
    segs = list(FLEET_ESTIMATES_2024)
    fig = go.Figure(go.Bar(
        x=segs,
        y=[FLEET_ESTIMATES_2024[s]["total_dwt_mt"] for s in segs],
        marker_color=[SEGMENT_COLORS[s] for s in segs],
        text=[f"{FLEET_ESTIMATES_2024[s]['total_dwt_mt']:.0f}M dwt" for s in segs],
        textposition="auto",
        hovertemplate="<b>%{x}</b><br>Fleet: <b>%{y:.0f}M DWT</b><extra></extra>",
    ))
    fig.update_layout(
        height=260, yaxis_title="Million DWT", showlegend=False,
        margin=dict(l=44, r=16, t=12, b=36),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )
    return fig


def _orderbook_chart() -> go.Figure:
    segs = list(ORDERBOOK_2024)
    fig = go.Figure()
    for year, color in [
        (2025, COLORS["accent_blue"]),
        (2026, COLORS["accent_yellow"]),
        (2027, COLORS["accent_purple"]),
    ]:
        fig.add_trace(go.Bar(
            name=str(year), x=segs,
            y=[ORDERBOOK_2024[s].get(f"delivery_{year}", 0) for s in segs],
            marker_color=color, opacity=0.85,
        ))
    fig.update_layout(
        height=260, yaxis_title="Vessels on order", barmode="group",
        legend=dict(x=1.01, y=1, font=dict(size=10)),
        margin=dict(l=44, r=120, t=12, b=36),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )
    return fig


# ── AIS content builder — called from the tab-click callback ─────────────────

def _build_ais_content(ais_df: pd.DataFrame) -> list:
    """
    Build the full AIS Live Signal tab from a vessel DataFrame.
    Returns a list of Dash components suitable for html.Div children.
    """
    has_data  = not ais_df.empty
    total     = len(ais_df) if has_data else 0
    is_cached = bool(
        has_data
        and "cached" in ais_df.columns
        and ais_df["cached"].any()
    )

    # ── Fleet utilisation metrics ─────────────────────────────────────────────
    if has_data and "underway" in ais_df.columns:
        underway_n   = int(ais_df["underway"].sum())
        underway_pct = underway_n / total * 100 if total > 0 else 0.0
    else:
        underway_n, underway_pct = 0, 0.0

    if has_data and "at_anchor" in ais_df.columns:
        anchor_n = int(ais_df["at_anchor"].sum())
    else:
        anchor_n = 0

    if has_data and "sog" in ais_df.columns and "underway" in ais_df.columns:
        uw_sog   = ais_df.loc[ais_df["underway"], "sog"]
        avg_sog  = float(uw_sog.mean())  if not uw_sog.empty else 0.0
        slow_pct = float((uw_sog < 11).mean() * 100) if not uw_sog.empty else 0.0
    else:
        avg_sog, slow_pct = 0.0, 0.0

    # ── Signal level ──────────────────────────────────────────────────────────
    if underway_pct >= 38:
        signal, sig_color = "TIGHT",    COLORS["accent_red"]
    elif underway_pct >= 26:
        signal, sig_color = "BALANCED", COLORS["accent_yellow"]
    else:
        signal, sig_color = "SLACK",    COLORS["accent_green"]

    data_tag       = "CACHED" if is_cached else "LIVE"
    data_tag_color = COLORS["accent_yellow"] if is_cached else COLORS["accent_green"]

    # ── No-data early return ──────────────────────────────────────────────────
    if not has_data:
        if not AIS_API_KEY:
            msg = (
                "AIS_API_KEY is not set. Register for a free API key at "
                "aisstream.io, then set the environment variable AIS_API_KEY "
                "and restart the app."
            )
        else:
            msg = (
                "AIS API key is configured but no vessels were returned. "
                "Check your network connection — the WebSocket stream to "
                "aisstream.io may be blocked or rate-limited."
            )
        return [
            html.Div([
                html.Span("📡 AIS Fleet Signal: ",
                          style={"font-size": "0.82rem", "color": "var(--text-secondary)",
                                 "font-family": "var(--font-mono)"}),
                html.Span("NO DATA", style={"font-size": "1.1rem", "font-weight": "700",
                                             "color": COLORS["accent_red"],
                                             "font-family": "var(--font-mono)"}),
            ], className="fiq-card",
               style={"border-left": f"3px solid {COLORS['accent_red']}",
                      "padding": "10px 16px", "margin-bottom": "12px"}),
            info_banner(f"⚠ {msg}"),
        ]

    # ── Signal banner ─────────────────────────────────────────────────────────
    banner = html.Div([
        html.Span("📡 AIS Fleet Signal: ",
                  style={"font-size": "0.82rem", "color": "var(--text-secondary)",
                         "font-family": "var(--font-mono)"}),
        html.Span(signal,
                  style={"font-size": "1.1rem", "font-weight": "700",
                         "color": sig_color, "font-family": "var(--font-mono)"}),
        html.Span(
            f"  —  {underway_n:,} underway  /  {total:,} bulk carriers tracked"
            f"  ({underway_pct:.1f}% utilisation)",
            style={"font-size": "0.78rem", "color": "var(--text-secondary)",
                   "font-family": "var(--font-mono)"},
        ),
        html.Span(f"  [{data_tag}]",
                  style={"font-size": "0.72rem", "font-weight": "700",
                         "color": data_tag_color, "font-family": "var(--font-mono)",
                         "margin-left": "8px"}),
    ], className="fiq-card",
       style={"border-left": f"3px solid {sig_color}",
              "padding": "10px 16px", "margin-bottom": "12px"})

    # ── KPI row ───────────────────────────────────────────────────────────────
    def _k(label, value, sub="", col=None):
        return dbc.Col(html.Div([
            html.Div(label, className="kpi-label"),
            html.Div(value, className="kpi-value",
                     style={"color": col} if col else {}),
            html.Div(sub,   className="kpi-delta kpi-delta-neu"),
        ], className="kpi-card"), width=6, md=True)

    kpi_row = dbc.Row([
        _k("Total Tracked",
           f"{total:,}", "bulk carriers (types 70-79)"),
        _k("Underway",
           f"{underway_n:,}", f"{underway_pct:.1f}% of fleet", sig_color),
        _k("At Anchor / Moored",
           f"{anchor_n:,}", "waiting at load/discharge"),
        _k("Avg SOG (underway)",
           f"{avg_sog:.1f} kn", "speed over ground",
           COLORS["accent_yellow"] if avg_sog < 11 else None),
        _k("Slow Steaming <11kn",
           f"{slow_pct:.0f}%", "% of underway vessels",
           COLORS["accent_red"]    if slow_pct > 25 else
           COLORS["accent_yellow"] if slow_pct > 15 else None),
    ], className="g-2 mb-3")

    # ── Global map — Scattergeo (SVG, no WebGL/CDN required) ─────────────────
    # go.Scattermap (MapLibre GL) requires CDN tile loading and WebGL canvas
    # init, which fails silently in local Dash environments → blank map.
    # go.Scattergeo is pure SVG, fully self-contained, always renders.
    # High-contrast colors ensure the basemap is visible on dark backgrounds.
    map_fig = go.Figure()

    # Pre-compute boolean masks — explicit, no lambdas that can silently fail
    col_uw     = ais_df["underway"].fillna(False).astype(bool)   if "underway"  in ais_df.columns else pd.Series(False, index=ais_df.index)
    col_anchor = ais_df["at_anchor"].fillna(False).astype(bool)  if "at_anchor" in ais_df.columns else pd.Series(False, index=ais_df.index)
    col_moored = ais_df["moored"].fillna(False).astype(bool)     if "moored"    in ais_df.columns else pd.Series(False, index=ais_df.index)

    status_groups = [
        ("Underway",           col_uw,                              COLORS["accent_blue"],   6),
        ("At Anchor / Moored", (~col_uw) & (col_anchor | col_moored), COLORS["accent_yellow"], 7),
        ("Default / Other",    (~col_uw) & ~col_anchor & ~col_moored, "#6e7681",               4),
    ]

    for grp_name, mask, color, mkr_size in status_groups:
        sub = ais_df[mask & ais_df["lat"].notna() & ais_df["lon"].notna()]
        if sub.empty:
            continue
        names    = sub["name"].fillna("—").tolist()         if "name"        in sub.columns else ["—"] * len(sub)
        sogs     = sub["sog"].fillna(0.0).round(1).tolist() if "sog"         in sub.columns else [0.0] * len(sub)
        dests    = sub["destination"].fillna("—").tolist()  if "destination" in sub.columns else ["—"] * len(sub)
        statuses = sub["nav_status"].fillna("—").tolist()   if "nav_status"  in sub.columns else ["—"] * len(sub)
        hover    = [
            f"<b>{n}</b><br>SOG: {s:.1f} kn  |  {st}<br>Dest: {d}"
            for n, s, d, st in zip(names, sogs, dests, statuses)
        ]
        map_fig.add_trace(go.Scattergeo(
            lat=sub["lat"].tolist(),
            lon=sub["lon"].tolist(),
            mode="markers",
            marker=dict(size=mkr_size, color=color, opacity=0.80),
            name=grp_name,
            text=hover,
            hovertemplate="%{text}<extra></extra>",
        ))

    map_fig.update_layout(
        height=500,
        geo=dict(
            showframe=False,
            showcoastlines=True,
            coastlinewidth=1,
            coastlinecolor="#5b8cbf",
            showland=True,
            landcolor="#1e3050",
            showocean=True,
            oceancolor="#0c1a2e",
            showcountries=True,
            countrywidth=0.4,
            countrycolor="#2d4a6e",
            showlakes=False,
            bgcolor="#0c1a2e",
            projection_type="natural earth",
            lataxis_range=[-70, 80],
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor=COLORS["bg_card"],
        legend=dict(
            x=0.01, y=0.99, xanchor="left", yanchor="top",
            font=dict(size=10, color=COLORS["text_primary"],
                      family="'IBM Plex Mono',monospace"),
            bgcolor="rgba(22,27,34,0.88)",
            bordercolor=COLORS["border"], borderwidth=1,
        ),
    )

    # ── Baltic Exchange route traffic ─────────────────────────────────────────
    route_df = get_route_traffic(ais_df)
    route_rows = []
    if not route_df.empty:
        for _, r in route_df.sort_values("Total", ascending=False).iterrows():
            rc       = r.get("color", COLORS["accent_blue"])
            total_v  = int(r.get("Total", 0))
            uway_v   = int(r.get("Underway", 0))
            anch_v   = int(r.get("At Anchor", 0))
            sog_v    = float(r.get("Avg SOG (kn)", 0))
            load_pct = f"{uway_v/total_v*100:.0f}%" if total_v > 0 else "—"
            route_rows.append(html.Tr([
                html.Td(html.Span(str(r.get("Route", "—")),
                                  style={"color": rc, "font-weight": "700",
                                         "font-family": "var(--font-mono)",
                                         "font-size": "0.8rem"})),
                html.Td(str(r.get("Segment", "—")),
                        style={"color": "var(--text-secondary)", "font-size": "0.72rem"}),
                html.Td(str(r.get("Cargo", "—")),
                        style={"color": "var(--text-faint)", "font-size": "0.7rem"}),
                html.Td(str(total_v),
                        style={"font-weight": "700", "font-family": "var(--font-mono)",
                               "color": COLORS["accent_blue"] if total_v > 5 else "var(--text-primary)"}),
                html.Td(f"{uway_v} ({load_pct})",
                        style={"font-family": "var(--font-mono)",
                               "color": COLORS["accent_green"] if uway_v > 0 else "var(--text-faint)"}),
                html.Td(str(anch_v),
                        style={"font-family": "var(--font-mono)",
                               "color": COLORS["accent_yellow"] if anch_v > 3 else "var(--text-faint)"}),
                html.Td(f"{sog_v:.1f} kn" if sog_v > 0 else "—",
                        style={"font-family": "var(--font-mono)", "font-size": "0.78rem",
                               "color": COLORS["accent_orange"] if 0 < sog_v < 11 else "var(--text-secondary)"}),
                html.Td(str(r.get("Description", "")),
                        style={"color": "var(--text-faint)", "font-size": "0.68rem"}),
            ]))

    route_table = html.Div([
        section_header("Baltic Exchange Route Flow — Live Vessel Counts [AIS]"),
        html.P(
            "Counts vessels currently inside each Baltic benchmark corridor. "
            "More vessels on a route + low SOG → tighter effective supply → upward rate pressure.",
            style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                   "font-family": "var(--font-mono)", "margin-bottom": "10px"},
        ),
        dbc.Card(dbc.CardBody(html.Div(
            html.Table([
                html.Thead(html.Tr([html.Th(h) for h in [
                    "Route", "Segment", "Cargo",
                    "Total", "Underway (%)", "Waiting", "Avg SOG", "Description",
                ]])),
                html.Tbody(route_rows or [html.Tr(html.Td(
                    "No vessels detected on Baltic Exchange route corridors.",
                    colSpan=8,
                    style={"text-align": "center", "color": "var(--text-faint)", "padding": "20px"},
                ))]),
            ], className="fiq-table"),
            style={"overflow-x": "auto"},
        ))),
    ])

    # ── Port zone table ───────────────────────────────────────────────────────
    port_counts_df = get_port_zone_counts(ais_df)
    port_rows = []
    if not port_counts_df.empty:
        for _, p in port_counts_df.head(14).iterrows():
            live     = int(p.get("Live (AIS)", 0))
            anchored = int(p.get("Anchor/Moored", 0))
            transit  = int(p.get("Transiting", 0))
            wait     = str(p.get("Est. Wait (days)", "—"))
            status   = str(p.get("Est. Status", "—"))
            sc       = {"Light":    COLORS["accent_green"],
                        "Moderate": COLORS["accent_yellow"],
                        "Heavy":    COLORS["accent_red"]}.get(status, "var(--text-faint)")
            port_rows.append(html.Tr([
                html.Td(str(p.get("Port Zone", "—")),
                        style={"font-weight": "600", "font-family": "var(--font-mono)",
                               "font-size": "0.78rem"}),
                html.Td(str(live),
                        style={"font-family": "var(--font-mono)",
                               "color": COLORS["accent_blue"], "font-weight": "700"}),
                html.Td(str(anchored),
                        style={"font-family": "var(--font-mono)",
                               "color": COLORS["accent_yellow"]}),
                html.Td(str(transit),
                        style={"font-family": "var(--font-mono)"}),
                html.Td(wait,
                        style={"font-family": "var(--font-mono)",
                               "color": COLORS["accent_yellow"]}),
                html.Td(status,
                        style={"font-weight": "600", "color": sc, "font-size": "0.72rem"}),
            ]))

    # ── Chokepoint table ──────────────────────────────────────────────────────
    chk_traffic = get_chokepoint_traffic(ais_df)
    chk_rows = []
    for name, count in sorted(chk_traffic.items(), key=lambda x: -x[1]):
        # Look up status from CHOKEPOINTS config
        cp_meta = next(
            (v for k, v in CHOKEPOINTS.items()
             if any(w.lower() in k.lower() for w in name.replace("/", " ").split()[:2])),
            {}
        )
        cp_status = cp_meta.get("status", "—")
        cp_pct    = cp_meta.get("annual_dry_bulk_pct", "—")
        sc        = COLORS["accent_red"] if cp_status == "RESTRICTED" else COLORS["accent_green"]
        chk_rows.append(html.Tr([
            html.Td(name,
                    style={"font-weight": "600", "font-family": "var(--font-mono)"}),
            html.Td(str(count),
                    style={"font-weight": "700", "font-family": "var(--font-mono)",
                           "color": COLORS["accent_blue"]}),
            html.Td(f"{cp_pct}%" if cp_pct != "—" else "—",
                    style={"font-family": "var(--font-mono)",
                           "color": "var(--text-secondary)"}),
            html.Td(cp_status,
                    style={"font-weight": "700", "color": sc, "font-size": "0.72rem"}),
        ]))

    # ── SOG histogram ─────────────────────────────────────────────────────────
    sog_fig = go.Figure()
    if has_data and "sog" in ais_df.columns and "underway" in ais_df.columns:
        uw_df = ais_df[ais_df["underway"]]
        if not uw_df.empty:
            sv = uw_df["sog"].dropna()
            sog_fig.add_trace(go.Histogram(
                x=sv, nbinsx=25,
                marker_color=COLORS["accent_blue"], opacity=0.75,
                hovertemplate="SOG: %{x:.1f} kn  |  Vessels: %{y}<extra></extra>",
            ))
            sog_fig.add_vline(
                x=11, line_dash="dash", line_color=COLORS["accent_red"],
                annotation_text="11 kn slow-steam",
                annotation_position="top right",
                annotation_font=dict(color=COLORS["accent_red"], size=9),
            )
            if len(sv) > 0:
                sog_fig.add_vline(
                    x=float(sv.mean()), line_dash="dot",
                    line_color=COLORS["accent_yellow"],
                    annotation_text=f"Mean {sv.mean():.1f} kn",
                    annotation_position="top left",
                    annotation_font=dict(color=COLORS["accent_yellow"], size=9),
                )
    sog_fig.update_xaxes(
        title_text="Speed Over Ground (knots)",
        tickfont=dict(size=10, color=COLORS["text_secondary"]),
    )
    sog_fig.update_yaxes(
        title_text="Vessels",
        gridcolor="rgba(48,54,61,0.35)", tickfont=dict(size=10),
    )
    sog_fig.update_layout(
        height=280, showlegend=False, hovermode="x",
        margin=dict(l=48, r=16, t=16, b=44),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Region breakdown bar ──────────────────────────────────────────────────
    # More informative than segment (which is all "Bulk Carrier" from raw AIS).
    # Shows geographic concentration of the fleet at this moment.
    region_fig = go.Figure()
    if has_data and "region" in ais_df.columns:
        region_counts = ais_df["region"].value_counts().head(10)
        palette = COLORS["chart_palette"]
        region_fig.add_trace(go.Bar(
            x=region_counts.values.tolist(),
            y=region_counts.index.tolist(),
            orientation="h",
            marker_color=[palette[i % len(palette)] for i in range(len(region_counts))],
            text=[str(v) for v in region_counts.values],
            textposition="auto",
            hovertemplate="<b>%{y}</b><br>Vessels: %{x}<extra></extra>",
        ))
    region_fig.update_xaxes(
        title_text="Vessels", tickfont=dict(size=9),
        gridcolor="rgba(48,54,61,0.35)",
    )
    region_fig.update_yaxes(tickfont=dict(size=9, color=COLORS["text_secondary"]))
    region_fig.update_layout(
        height=280, showlegend=False,
        margin=dict(l=8, r=16, t=16, b=44),
        plot_bgcolor=COLORS["bg_primary"], paper_bgcolor=COLORS["bg_card"],
    )

    # ── Assemble ──────────────────────────────────────────────────────────────
    return [
        banner,
        info_banner(
            f"📡 Live AIS via aisstream.io WebSocket (free tier, {AIS_COLLECT_SECONDS}s collection). "
            "Underway = SOG > 1.5 kn. Nav-status 15 (default) is treated as underway when SOG > 1.5 kn — "
            "~65% of vessels broadcast this code while actively steaming. "
            "Map uses SVG vector rendering — no CDN or WebGL required."
        ),
        kpi_row,
        divider(),

        section_header("Global Vessel Positions — Bulk Carriers  ·  Blue = Underway  ·  Yellow = Anchor/Moored"),
        dbc.Card(dbc.CardBody(dcc.Graph(
            figure=map_fig,
            config={"displayModeBar": True, "displaylogo": False},
        )), className="mb-3"),

        divider(),
        route_table,
        html.Br(),

        dbc.Row([
            dbc.Col([
                section_header("Port Zone Congestion [AIS + ESTIMATED]"),
                html.P(
                    "Vessels detected in port bounding boxes. "
                    "Wait estimates from quarterly manual data.",
                    style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "margin-bottom": "10px"},
                ),
                dbc.Card(dbc.CardBody(html.Div(
                    html.Table([
                        html.Thead(html.Tr([html.Th(h) for h in [
                            "Port Zone", "Live (AIS)", "Anchor/Moored",
                            "Transiting", "Est. Wait", "Status [EST]",
                        ]])),
                        html.Tbody(port_rows or [html.Tr(html.Td(
                            "No vessels detected in port zones.",
                            colSpan=6,
                            style={"text-align": "center", "color": "var(--text-faint)",
                                   "padding": "16px"},
                        ))]),
                    ], className="fiq-table"),
                    style={"overflow-x": "auto"},
                ))),
            ], md=7),

            dbc.Col([
                section_header("Chokepoint Transit [AIS]"),
                dbc.Card(dbc.CardBody(html.Div(
                    html.Table([
                        html.Thead(html.Tr([
                            html.Th(h) for h in
                            ["Chokepoint", "Vessels", "Dry Bulk %", "Status"]
                        ])),
                        html.Tbody(chk_rows or [html.Tr(html.Td(
                            "No vessels at chokepoints.",
                            colSpan=4,
                            style={"text-align": "center", "color": "var(--text-faint)",
                                   "padding": "16px"},
                        ))]),
                    ], className="fiq-table"),
                    style={"overflow-x": "auto"},
                )), className="mb-3"),
            ], md=5),
        ], className="g-3 mb-3"),

        divider(),

        dbc.Row([
            dbc.Col([
                section_header("Fleet Speed Distribution — Underway Vessels"),
                html.P(
                    "Clustering below 11 kn indicates slow steaming → "
                    "effective supply reduction → upward rate pressure.",
                    style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "margin-bottom": "10px"},
                ),
                dbc.Card(dbc.CardBody(dcc.Graph(
                    figure=sog_fig, config={"displayModeBar": False},
                ))),
            ], md=7),
            dbc.Col([
                section_header("Fleet Geographic Distribution"),
                html.P(
                    "Top 10 regions by vessel count. "
                    "Concentrations in loading regions signal tightening supply.",
                    style={"font-size": "0.72rem", "color": "var(--text-secondary)",
                           "font-family": "var(--font-mono)", "margin-bottom": "10px"},
                ),
                dbc.Card(dbc.CardBody(dcc.Graph(
                    figure=region_fig, config={"displayModeBar": False},
                ))),
            ], md=5),
        ], className="g-3"),
    ]


# ── Page layout ────────────────────────────────────────────────────────────────

def layout(**kwargs):
    # ── Sanctions + regulations (static) ────────────────────────────────────
    sanc_rows = [
        html.Tr([
            html.Td(s["entity"],             style={"font-weight": "500"}),
            html.Td(f'{s["vessels_est"]:,}', style={"font-weight": "600"}),
            html.Td(f'{s["dwt_mt"]:.1f}M',   style={"font-weight": "600"}),
            html.Td(status_badge(s["status"])),
        ])
        for s in SANCTIONS_DATA
    ]

    imo_rows = []
    for r in IMO_REGULATIONS:
        ic = {"High": COLORS["accent_red"], "Medium": COLORS["accent_yellow"],
              "Critical": "#ff00a0"}.get(r["impact"], COLORS["text_secondary"])
        imo_rows.append(html.Tr([
            html.Td(r["date"][:4],
                    style={"font-family": "var(--font-mono)", "font-weight": "600"}),
            html.Td(r["regulation"]),
            html.Td(r["impact"], style={"color": ic, "font-weight": "600"}),
        ]))

    return html.Div([
        page_header(
            "⚖️ Supply & Demand",
            "Fleet composition · orderbook · AIS live utilisation · "
            "route flow · chokepoints · sanctions · regulations",
        ),

        dcc.Tabs(
            id="sd-tabs", value="tab-fleet",
            className="tabs-bar",
            children=[
                dcc.Tab(label="🚢 Fleet & Orderbook", value="tab-fleet",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📡 AIS Live Signal",   value="tab-ais",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="⚠ Sanctions",          value="tab-sanc",
                        className="tab-btn", selected_className="tab-btn tab-active"),
                dcc.Tab(label="📋 Regulations",        value="tab-imo",
                        className="tab-btn", selected_className="tab-btn tab-active"),
            ],
        ),

        # ── Tab: Fleet ────────────────────────────────────────────────────────
        html.Div([
            info_banner(
                "📌 Fleet estimates from UNCTAD/Clarksons public reports (2024). "
                "All figures approximate — labelled ESTIMATED."
            ),
            dbc.Row([
                dbc.Col([
                    section_header("Global Dry Bulk Fleet by Segment [ESTIMATED]"),
                    dbc.Card(dbc.CardBody(dcc.Graph(
                        figure=_fleet_chart(), config={"displayModeBar": False},
                    ))),
                ], md=6),
                dbc.Col([
                    section_header("Orderbook — Scheduled Deliveries [ESTIMATED]"),
                    dbc.Card(dbc.CardBody(dcc.Graph(
                        figure=_orderbook_chart(), config={"displayModeBar": False},
                    ))),
                ], md=6),
            ], className="g-3 mb-3"),
            section_header("Fleet Summary Table [ESTIMATED]"),
            dbc.Card(dbc.CardBody(html.Div(
                html.Table([
                    html.Thead(html.Tr([html.Th(h) for h in [
                        "Segment", "Vessels", "Total DWT", "Avg Age",
                        "OB %", "Del.2025", "Del.2026", "Del.2027",
                    ]])),
                    html.Tbody([
                        html.Tr([
                            html.Td(seg, style={"color": SEGMENT_COLORS.get(seg, "#fff"),
                                                "font-weight": "600"}),
                            html.Td(f'{FLEET_ESTIMATES_2024[seg]["vessels"]:,}'),
                            html.Td(f'{FLEET_ESTIMATES_2024[seg]["total_dwt_mt"]:.0f}M'),
                            html.Td(f'{FLEET_ESTIMATES_2024[seg]["avg_age_years"]:.1f} yrs'),
                            html.Td(f'{ORDERBOOK_2024[seg]["pct_of_fleet"]:.1f}%',
                                    style={"font-weight": "600",
                                           "color": COLORS["accent_yellow"]}),
                            html.Td(ORDERBOOK_2024[seg]["delivery_2025"]),
                            html.Td(ORDERBOOK_2024[seg]["delivery_2026"]),
                            html.Td(ORDERBOOK_2024[seg]["delivery_2027"]),
                        ])
                        for seg in FLEET_ESTIMATES_2024
                    ]),
                ], className="fiq-table"),
                style={"overflow-x": "auto"},
            ))),
        ], id="sd-tab-content-fleet"),

        # ── Tab: AIS Live Signal ──────────────────────────────────────────────
        # NOTE: AIS content is NOT pre-built in layout() — it loads lazily when
        # the user clicks this tab (triggering the update_ais_content callback).
        # This keeps page load fast and avoids a 20-30s WebSocket wait on every
        # navigation to /supply-demand.
        html.Div([
            dbc.Row([
                dbc.Col(
                    dbc.Button(
                        "↺ Refresh AIS Feed",
                        id="ais-refresh-btn",
                        color="primary", size="sm",
                        style={"font-family": "var(--font-mono)", "font-size": "0.8rem"},
                    ),
                    width="auto",
                ),
                dbc.Col(
                    html.Div(
                        id="ais-refresh-status",
                        style={"font-family": "var(--font-mono)", "font-size": "0.72rem",
                               "color": "var(--text-secondary)", "padding-top": "6px"},
                    ),
                    width="auto",
                ),
            ], className="mb-3 align-items-center"),

            dcc.Loading(
                id="ais-loading",
                children=html.Div(
                    id="ais-live-content",
                    children=html.Div(
                        "Click '📡 AIS Live Signal' tab to stream vessel positions.",
                        style={"color": "var(--text-faint)",
                               "font-family": "var(--font-mono)",
                               "font-size": "0.82rem",
                               "padding": "32px 0"},
                    ),
                ),
                type="dot",
                color=COLORS["accent_blue"],
            ),
        ], id="sd-tab-content-ais", style={"display": "none"}),

        # ── Tab: Sanctions ────────────────────────────────────────────────────
        html.Div([
            info_banner(
                "⚠ Sanctions/shadow fleet estimates are based on publicly "
                "available reports. Actual figures are inherently uncertain."
            ),
            section_header("Sanctioned & Shadow Fleet [ESTIMATED]"),
            dbc.Card(dbc.CardBody(html.Div(
                html.Table([
                    html.Thead(html.Tr([html.Th(h) for h in [
                        "Entity", "Est. Vessels", "Est. DWT", "Status",
                    ]])),
                    html.Tbody(sanc_rows),
                ], className="fiq-table"),
                style={"overflow-x": "auto"},
            ))),
        ], id="sd-tab-content-sanc", style={"display": "none"}),

        # ── Tab: Regulations ──────────────────────────────────────────────────
        html.Div([
            section_header("IMO Regulatory Timeline"),
            dbc.Card(dbc.CardBody(html.Div(
                html.Table([
                    html.Thead(html.Tr([html.Th(h) for h in [
                        "Year", "Regulation", "Impact",
                    ]])),
                    html.Tbody(imo_rows),
                ], className="fiq-table"),
                style={"overflow-x": "auto"},
            ))),
        ], id="sd-tab-content-imo", style={"display": "none"}),
    ])


# ── Tab visibility callback ────────────────────────────────────────────────────

@callback(
    Output("sd-tab-content-fleet", "style"),
    Output("sd-tab-content-ais",   "style"),
    Output("sd-tab-content-sanc",  "style"),
    Output("sd-tab-content-imo",   "style"),
    Input("sd-tabs", "value"),
)
def _switch_sd_tab(tab: str):
    order = ["tab-fleet", "tab-ais", "tab-sanc", "tab-imo"]
    return [{"display": "block"} if t == tab else {"display": "none"} for t in order]


# ── AIS live data callback ─────────────────────────────────────────────────────
#
# Triggered by:
#   (a) user clicks the "AIS Live Signal" tab  → sd-tabs value becomes "tab-ais"
#   (b) user clicks "↺ Refresh AIS Feed"       → n_clicks increments
#
# prevent_initial_call=True is critical: in Dash multi-page apps, callbacks with
# prevent_initial_call=False fire ONCE on app init, before the page components
# exist, and are then silently suppressed. The content never appears.
# Instead we fire only on real user interactions (tab click or refresh button).

@callback(
    Output("ais-live-content",   "children"),
    Output("ais-refresh-status", "children"),
    Input("sd-tabs",             "value"),
    Input("ais-refresh-btn",     "n_clicks"),
    prevent_initial_call=True,
)
def update_ais_content(tab: str, n_clicks):
    triggered = ctx.triggered_id

    # Tab switched but NOT to the AIS tab — do nothing
    if triggered == "sd-tabs" and tab != "tab-ais":
        raise dash.exceptions.PreventUpdate

    # Force-bypass cache when refresh button is clicked
    force = triggered == "ais-refresh-btn"
    ttl   = 0 if force else AIS_CACHE_TTL_MINUTES

    try:
        ais_df = fetch_live_vessels(
            api_key=AIS_API_KEY,
            ws_url=AIS_WS_URL,
            cache_dir=CACHE_DIR,
            collect_seconds=AIS_COLLECT_SECONDS,
            cache_ttl_minutes=ttl,
            bulk_types=AIS_BULK_CARRIER_TYPES,
        )
    except Exception:
        ais_df = pd.DataFrame()

    content  = _build_ais_content(ais_df)
    n_str    = f"{len(ais_df):,}" if not ais_df.empty else "0"
    is_live  = not (
        not ais_df.empty
        and "cached" in ais_df.columns
        and ais_df["cached"].any()
    )
    src    = "live stream" if is_live else "cache (< 5 min old)"
    ts     = pd.Timestamp.now().strftime("%H:%M UTC")
    status = f"{ts}  ·  {n_str} vessels  ·  {src}"
    return content, status
