import plotly.graph_objects as go
import plotly.io as pio

from src.config import COLORS, PLOTLY_TEMPLATE


def register_plotly_template() -> None:
    pio.templates[PLOTLY_TEMPLATE] = go.layout.Template(
        layout=go.Layout(
            paper_bgcolor=COLORS["bg_secondary"],
            plot_bgcolor=COLORS["bg_primary"],
            font=dict(
                color=COLORS["text_primary"],
                family="'IBM Plex Mono', 'Courier New', monospace",
                size=12,
            ),
            xaxis=dict(
                gridcolor=COLORS["border"],
                zerolinecolor=COLORS["border"],
                linecolor=COLORS["border"],
                tickcolor=COLORS["text_secondary"],
                tickfont=dict(color=COLORS["text_secondary"]),
            ),
            yaxis=dict(
                gridcolor=COLORS["border"],
                zerolinecolor=COLORS["border"],
                linecolor=COLORS["border"],
                tickcolor=COLORS["text_secondary"],
                tickfont=dict(color=COLORS["text_secondary"]),
            ),
            colorway=COLORS["chart_palette"],
            legend=dict(
                bgcolor=COLORS["bg_card"],
                bordercolor=COLORS["border"],
                borderwidth=1,
                font=dict(color=COLORS["text_primary"]),
            ),
            title=dict(font=dict(color=COLORS["text_primary"])),
            hoverlabel=dict(
                bgcolor=COLORS["bg_card"],
                bordercolor=COLORS["border"],
                font=dict(color=COLORS["text_primary"], family="'IBM Plex Mono', monospace"),
            ),
            margin=dict(l=50, r=20, t=40, b=50),
        )
    )
    pio.templates.default = PLOTLY_TEMPLATE
