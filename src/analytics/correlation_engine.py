import logging

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger("freightiq.correlation")


class CorrelationEngine:
    """Cross-asset correlation analysis for freight intelligence."""

    def correlation_matrix(self, df: pd.DataFrame, window: int | None = None) -> pd.DataFrame:
        """
        Returns Pearson correlation matrix.
        If window is set, computes over the last N rows; else uses full history.
        """
        aligned = df.dropna(how="all").ffill().dropna(how="any")
        if aligned.shape[0] < 5:
            return pd.DataFrame()
        if window:
            aligned = aligned.tail(window)
        return aligned.corr(method="pearson")

    def rolling_pairwise_correlation(
        self,
        s1: pd.Series,
        s2: pd.Series,
        window: int = 60,
    ) -> pd.Series:
        """Rolling Pearson correlation between two series."""
        combined = pd.concat([s1, s2], axis=1).dropna()
        if combined.shape[0] < window:
            return pd.Series(dtype=float)
        return combined.iloc[:, 0].rolling(window).corr(combined.iloc[:, 1])

    def cross_correlation_leadlag(
        self,
        s1: pd.Series,
        s2: pd.Series,
        max_lag: int = 52,
        freq: str = "W",
    ) -> pd.DataFrame:
        """
        Computes cross-correlation of s1 vs s2 for lags -max_lag to +max_lag.
        Positive lag = s2 leads s1 by that many periods.
        Returns DataFrame with columns: lag, correlation, p_value.
        """
        # Resample to weekly to reduce noise
        a = s1.resample(freq).last().dropna() if freq else s1.dropna()
        b = s2.resample(freq).last().dropna() if freq else s2.dropna()

        combined = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
        if len(combined) < 10:
            return pd.DataFrame(columns=["lag", "correlation", "p_value"])

        results = []
        for lag in range(-max_lag, max_lag + 1):
            if lag == 0:
                x = combined["a"]
                y = combined["b"]
            elif lag > 0:
                x = combined["a"].iloc[lag:]
                y = combined["b"].iloc[:-lag]
            else:
                x = combined["a"].iloc[:lag]
                y = combined["b"].iloc[-lag:]

            if len(x) < 5 or len(y) < 5:
                continue
            try:
                r, p = stats.pearsonr(x.values, y.values)
                results.append({"lag": lag, "correlation": r, "p_value": p})
            except Exception:
                pass

        return pd.DataFrame(results)

    def optimal_lead_lag(self, s1: pd.Series, s2: pd.Series, max_lag: int = 52) -> dict:
        """
        Returns the lag at which |correlation| is maximized.
        Positive lag means s2 leads s1.
        """
        df = self.cross_correlation_leadlag(s1, s2, max_lag=max_lag)
        if df.empty:
            return {"lag": None, "correlation": None, "p_value": None, "interpretation": "N/A"}
        idx = df["correlation"].abs().idxmax()
        row = df.loc[idx]
        lag = int(row["lag"])
        if lag > 0:
            interp = f"s2 leads s1 by {lag} weeks"
        elif lag < 0:
            interp = f"s1 leads s2 by {abs(lag)} weeks"
        else:
            interp = "No lead-lag (concurrent)"
        return {
            "lag":             lag,
            "correlation":     round(float(row["correlation"]), 3),
            "p_value":         round(float(row["p_value"]), 4),
            "significant":     bool(row["p_value"] < 0.05),
            "interpretation":  interp,
        }

    def compute_r_squared(
        self,
        s1: pd.Series,
        s2: pd.Series,
        window: int = 90,
    ) -> pd.Series:
        """Rolling R² between two series."""
        return self.rolling_pairwise_correlation(s1, s2, window=window) ** 2

    def build_correlation_dataset(
        self,
        named_series: dict[str, pd.Series],
        window: int | None = None,
    ) -> pd.DataFrame:
        """
        Takes a dict of {name: pd.Series} and returns a correlation matrix.
        Handles different frequencies by forward-filling to daily.
        """
        frames = {}
        for name, series in named_series.items():
            if series.empty:
                continue
            s = series.copy()
            s.index = pd.to_datetime(s.index)
            frames[name] = s.resample("D").ffill()

        if not frames:
            return pd.DataFrame()

        df = pd.DataFrame(frames).sort_index()
        return self.correlation_matrix(df, window=window)

    def regime_conditional_correlation(
        self,
        df: pd.DataFrame,
        regime_series: pd.Series,
    ) -> dict[str, pd.DataFrame]:
        """
        Returns separate correlation matrices for each regime phase.
        regime_series: pd.Series with string labels (e.g., 'EXPANSION', 'CONTRACTION').
        """
        regimes = regime_series.dropna().unique()
        result = {}
        for regime in regimes:
            dates = regime_series[regime_series == regime].index
            sub = df.loc[df.index.intersection(dates)]
            if len(sub) >= 5:
                result[regime] = sub.corr(method="pearson")
        return result
