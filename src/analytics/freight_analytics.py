import numpy as np
import pandas as pd
from scipy import stats


def seasonal_index(series: pd.Series, min_years: int = 2) -> pd.DataFrame:
    """
    Computes monthly seasonal index (average monthly value / overall mean).
    Returns DataFrame with columns: month, month_name, avg_value, seasonal_index, observations.
    """
    if series.empty:
        return pd.DataFrame()
    df = series.to_frame("value").copy()
    df.index = pd.to_datetime(df.index)
    df["month"] = df.index.month
    df["year"]  = df.index.year

    monthly = df.groupby("month")["value"].agg(["mean", "count"]).reset_index()
    monthly.columns = ["month", "avg_value", "observations"]

    overall_mean = df["value"].mean()
    monthly["seasonal_index"] = monthly["avg_value"] / overall_mean if overall_mean != 0 else 1.0
    month_names = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                   7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    monthly["month_name"] = monthly["month"].map(month_names)
    return monthly.sort_values("month")


def seasonality_heatmap(series: pd.Series) -> pd.DataFrame:
    """
    Returns month × year pivot table of average values.
    Useful for heatmap visualisation.
    """
    if series.empty:
        return pd.DataFrame()
    df = series.to_frame("value").copy()
    df.index = pd.to_datetime(df.index)
    df["month"] = df.index.month
    df["year"]  = df.index.year
    pivot = df.pivot_table(values="value", index="month", columns="year", aggfunc="mean")
    return pivot


def dual_ma_signals(series: pd.Series, short: int = 20, long: int = 200) -> pd.DataFrame:
    """
    Computes dual moving average crossover signals.
    Returns DataFrame with: value, ma_short, ma_long, signal (1=bull, -1=bear, 0=neutral).
    """
    df = series.to_frame("value").copy()
    df["ma_short"] = series.rolling(short, min_periods=short // 2).mean()
    df["ma_long"]  = series.rolling(long,  min_periods=long  // 2).mean()
    df["signal"]   = 0
    df.loc[df["ma_short"] > df["ma_long"], "signal"]  =  1
    df.loc[df["ma_short"] < df["ma_long"], "signal"]  = -1
    return df


def rolling_volatility(series: pd.Series, window: int = 30) -> pd.Series:
    """Annualized rolling volatility from log returns."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(window=window, min_periods=window // 2).std() * np.sqrt(252)


def compute_drawdown(series: pd.Series) -> pd.Series:
    """Rolling drawdown from rolling peak (0 to -1 scale)."""
    peak = series.cummax()
    return (series - peak) / peak.replace(0, np.nan)


def percentile_rank(series: pd.Series, current_value: float) -> dict:
    """Returns percentile rank of current value vs 1Y, 3Y, 5Y, 10Y windows."""
    result = {}
    for label, lookback_days in [("1Y", 252), ("3Y", 756), ("5Y", 1260), ("10Y", 2520)]:
        window = series.dropna().tail(lookback_days)
        if window.empty:
            result[label] = None
            continue
        rank = float((window < current_value).sum() / len(window) * 100)
        result[label] = round(rank, 1)
    return result


def rolling_zscore(series: pd.Series, window: int = 52) -> pd.Series:
    """Rolling z-score vs a rolling mean and std."""
    mean = series.rolling(window=window, min_periods=window // 2).mean()
    std  = series.rolling(window=window, min_periods=window // 2).std()
    return (series - mean) / std.replace(0, np.nan)


def compute_momentum(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple momentum: current / N-period-ago - 1."""
    return series.pct_change(period)


def sharpe_like_ratio(series: pd.Series, risk_free: float = 0.0) -> float | None:
    """Annualized Sharpe-like ratio of daily returns (no risk-free asset scaling)."""
    daily_ret = series.pct_change().dropna()
    if daily_ret.empty or daily_ret.std() == 0:
        return None
    return float((daily_ret.mean() * 252 - risk_free) / (daily_ret.std() * np.sqrt(252)))


def ar1_mean_reversion_halflife(series: pd.Series) -> float | None:
    """
    Estimates Ornstein-Uhlenbeck half-life via AR(1) regression.
    half_life = -log(2) / log(beta), where beta is the AR(1) coefficient.
    """
    s = series.dropna()
    if len(s) < 20:
        return None
    lag = s.shift(1).dropna()
    delta = s.diff().dropna()
    aligned = pd.concat([lag, delta], axis=1).dropna()
    if aligned.empty:
        return None
    beta = np.polyfit(aligned.iloc[:, 0], aligned.iloc[:, 1], 1)[0]
    if beta >= 0:
        return None  # not mean-reverting
    half_life = -np.log(2) / np.log(1 + beta)
    return round(float(half_life), 1) if half_life > 0 else None


def compute_freight_statistics(series: pd.Series, current_val: float | None = None) -> dict:
    """
    Returns comprehensive statistics for a freight series.
    Used in the statistical panel on the Freight Analysis page.
    """
    s = series.dropna()
    if s.empty:
        return {}
    if current_val is None:
        current_val = float(s.iloc[-1])

    vol_30d = float(rolling_volatility(s, 30).iloc[-1]) if len(s) > 30 else None

    result = {
        "current":          current_val,
        "mean_1y":          float(s.tail(252).mean()),
        "mean_5y":          float(s.tail(1260).mean()),
        "min_1y":           float(s.tail(252).min()),
        "max_1y":           float(s.tail(252).max()),
        "vol_30d":          vol_30d,
        "zscore_52w":       float(rolling_zscore(s, 52).iloc[-1]) if len(s) > 52 else None,
        "sharpe_1y":        sharpe_like_ratio(s.tail(252)),
        "drawdown_current": float(compute_drawdown(s).iloc[-1]),
        "half_life_days":   ar1_mean_reversion_halflife(s),
        **{f"pct_rank_{k}": v for k, v in percentile_rank(s, current_val).items()},
    }
    return result
