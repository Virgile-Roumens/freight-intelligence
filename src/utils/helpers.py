import logging
import logging.handlers
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "errors.log"
    logger = logging.getLogger("freightiq")
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def format_number(n, decimals: int = 0, prefix: str = "", suffix: str = "") -> str:
    if n is None or (isinstance(n, float) and np.isnan(n)):
        return "N/A"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "N/A"
    if abs(n) >= 1_000_000:
        return f"{prefix}{n/1_000_000:.{decimals}f}M{suffix}"
    if abs(n) >= 1_000:
        return f"{prefix}{n/1_000:.{decimals}f}K{suffix}"
    return f"{prefix}{n:.{decimals}f}{suffix}"


def format_delta(value: float, decimals: int = 1) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}"


def format_pct(value: float, decimals: int = 1) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def get_delta_color(value: float, reverse: bool = False) -> str:
    """Returns hex color for positive/negative/neutral deltas."""
    from src.config import COLORS
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return COLORS["text_secondary"]
    positive_color = COLORS["accent_green"] if not reverse else COLORS["accent_red"]
    negative_color = COLORS["accent_red"] if not reverse else COLORS["accent_green"]
    if value > 0:
        return positive_color
    if value < 0:
        return negative_color
    return COLORS["text_secondary"]


def safe_pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    """Percentage change, handling zeros and NaNs safely."""
    return series.pct_change(periods=periods).replace([np.inf, -np.inf], np.nan)


def normalize_to_100(series: pd.Series) -> pd.Series:
    """Normalize series so first valid value = 100."""
    first = series.dropna().iloc[0] if not series.dropna().empty else None
    if first is None or first == 0:
        return series
    return series / first * 100


def rolling_zscore(series: pd.Series, window: int = 52) -> pd.Series:
    mean = series.rolling(window=window, min_periods=window // 2).mean()
    std = series.rolling(window=window, min_periods=window // 2).std()
    return (series - mean) / std.replace(0, np.nan)


def percentile_rank(series: pd.Series, value: float, lookback: int | None = None) -> float:
    """Return percentile rank of value within series (or last N periods)."""
    if lookback:
        series = series.tail(lookback)
    valid = series.dropna()
    if valid.empty:
        return np.nan
    return float((valid < value).sum() / len(valid) * 100)


def compute_annualized_vol(series: pd.Series, window: int = 30) -> pd.Series:
    """Rolling annualized volatility (log returns)."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(window=window).std() * np.sqrt(252)


def compute_drawdown(series: pd.Series) -> pd.Series:
    peak = series.cummax()
    return (series - peak) / peak


def resample_to_business_daily(series: pd.Series) -> pd.Series:
    """Forward-fill monthly/weekly FRED data to business days."""
    return series.resample("B").ffill()


def last_valid(series: pd.Series):
    """Return last non-NaN value, or None."""
    valid = series.dropna()
    return float(valid.iloc[-1]) if not valid.empty else None


def compute_return(series: pd.Series, periods: int = 1) -> float | None:
    """Return simple return over N periods from last valid value."""
    valid = series.dropna()
    if len(valid) < periods + 1:
        return None
    return float((valid.iloc[-1] / valid.iloc[-1 - periods]) - 1)


def sparkline_svg(data: list[float], width: int = 80, height: int = 28, color: str = "#58a6ff") -> str:
    """Generate an inline SVG sparkline from a list of values."""
    if not data or len(data) < 2:
        return ""
    clean = [v for v in data if v is not None and not np.isnan(v)]
    if len(clean) < 2:
        return ""
    mn, mx = min(clean), max(clean)
    rng = mx - mn if mx != mn else 1
    pad = 3
    pts = []
    for i, v in enumerate(clean):
        x = pad + (i / (len(clean) - 1)) * (width - 2 * pad)
        y = height - pad - ((v - mn) / rng) * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    points_str = " ".join(pts)
    trend_color = color
    if len(clean) >= 2:
        trend_color = "#3fb950" if clean[-1] >= clean[0] else "#f85149"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{points_str}" fill="none" stroke="{trend_color}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )
