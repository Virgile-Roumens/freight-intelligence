"""Reusable Dash HTML components for FreightIQ."""
from __future__ import annotations

from dash import html


def kpi_card(
    title: str,
    value: str,
    delta: float | None = None,
    delta_pct: float | None = None,
    is_proxy: bool = False,
    accent_color: str | None = None,
    subtitle: str | None = None,
) -> html.Div:
    proxy = html.Span("PROXY", className="proxy-badge") if is_proxy else None
    label_children = [title, proxy] if proxy else [title]

    delta_el = None
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        pct_str = f" ({sign}{delta_pct:.1f}%)" if delta_pct is not None else ""
        cls = "kpi-delta kpi-delta-pos" if delta >= 0 else "kpi-delta kpi-delta-neg"
        delta_el = html.Div(f"{sign}{delta:.2f}{pct_str}", className=cls)

    sub_el = html.Div(subtitle, className="kpi-delta kpi-delta-neu") if subtitle else None

    style = {}
    if accent_color:
        style["borderTop"] = f"2px solid {accent_color}"

    return html.Div(
        [
            html.Div(label_children, className="kpi-label"),
            html.Div(value, className="kpi-value"),
            *(x for x in [delta_el, sub_el] if x is not None),
        ],
        className="kpi-card",
        style=style,
    )


def signal_card(text: str, level: str = "amber") -> html.Div:
    icons = {"red": "🔴", "green": "🟢", "amber": "🟡", "blue": "🔵"}
    icon = icons.get(level, "🟡")
    return html.Div(
        [html.Span(icon + " "), text],
        className=f"signal-card signal-{level}",
    )


def news_card(title: str, source: str, published: str, link: str, score: float) -> html.Div:
    score_pct = int(score * 100)
    return html.Div(
        [
            html.A(title, href=link, target="_blank", className="news-title"),
            html.Div(
                [
                    html.Span(source, className="news-source"),
                    html.Span("·"),
                    html.Span(published[:16] if published else ""),
                    html.Span(f"relevance {score_pct}%", className="score-badge"),
                ],
                className="news-meta",
            ),
        ],
        className="news-card",
    )


def section_header(title: str) -> html.Div:
    return html.Div(title, className="section-header")


def page_header(title: str, subtitle: str = "") -> html.Div:
    children = [html.H1(title, className="page-title")]
    if subtitle:
        children.append(html.P(subtitle, className="page-subtitle"))
    return html.Div(children, className="page-header")


def info_banner(text: str, level: str = "info") -> html.Div:
    cls_map = {
        "info": "info-banner",
        "warning": "warning-banner",
        "error": "error-banner",
        "success": "success-banner",
    }
    return html.Div(text, className=cls_map.get(level, "info-banner"))


def status_badge(status: str) -> html.Span:
    mapping = {
        "OPEN":       ("🟢 Open",       "badge-open"),
        "RESTRICTED": ("🟡 Restricted", "badge-restricted"),
        "DISRUPTED":  ("🔴 Disrupted",  "badge-disrupted"),
        "SANCTIONED": ("🔴 Sanctioned", "badge-sanctioned"),
        "SHADOW":     ("🟡 Shadow",     "badge-shadow"),
    }
    label, cls = mapping.get(status, (status, ""))
    return html.Span(label, className=cls)


def divider() -> html.Hr:
    return html.Hr(className="fiq-divider")
