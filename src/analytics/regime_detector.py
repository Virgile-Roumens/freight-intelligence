import numpy as np
import pandas as pd

from src.config import REGIME_THRESHOLDS


PHASES = {
    "EXPANSION": {
        "color":       "#3fb950",
        "emoji":       "📈",
        "label":       "Expansion",
        "description": "Rising rates, tightening utilisation. Demand outpacing supply.",
    },
    "PEAK": {
        "color":       "#d29922",
        "emoji":       "🔝",
        "label":       "Peak",
        "description": "Rates at cycle highs. Overshooting risk. New orders increasing.",
    },
    "CONTRACTION": {
        "color":       "#f85149",
        "emoji":       "📉",
        "label":       "Contraction",
        "description": "Falling rates, excess capacity. Demand weakness or supply surge.",
    },
    "TROUGH": {
        "color":       "#8b949e",
        "emoji":       "🔻",
        "label":       "Trough",
        "description": "Rates near lows. Scrapping pressure building. Recovery potential.",
    },
    "NEUTRAL": {
        "color":       "#58a6ff",
        "emoji":       "➡️",
        "label":       "Neutral",
        "description": "No clear directional signal. Monitor for confirmation.",
    },
}


class RegimeDetector:
    """
    Identifies the current freight market cycle phase using rule-based signals.
    Based on the CM Navigator two-layer model framework.
    """

    def detect_phase(self, series: pd.Series) -> dict:
        """
        Classifies the current phase using MA ratio + momentum.
        Returns phase dict with color, emoji, description, and supporting metrics.
        """
        s = series.dropna()
        if len(s) < 50:
            return {**PHASES["NEUTRAL"], "phase": "NEUTRAL", "metrics": {}}

        ma_window  = REGIME_THRESHOLDS["ma_window"]
        mom_window = REGIME_THRESHOLDS["momentum_window"]

        ma_long  = float(s.rolling(min(ma_window, len(s))).mean().iloc[-1])
        ma_short = float(s.rolling(min(50, len(s))).mean().iloc[-1])
        current  = float(s.iloc[-1])
        ma_ratio = current / ma_long if ma_long != 0 else 1.0
        momentum = float(s.pct_change(mom_window).iloc[-1]) if len(s) > mom_window else 0.0

        # Phase classification
        if ma_ratio >= REGIME_THRESHOLDS["peak_ma_ratio"]:
            phase = "PEAK"
        elif ma_ratio >= REGIME_THRESHOLDS["expansion_ma_ratio"] and momentum > 0:
            phase = "EXPANSION"
        elif ma_ratio <= REGIME_THRESHOLDS["trough_ma_ratio"]:
            phase = "TROUGH"
        elif ma_ratio <= REGIME_THRESHOLDS["contraction_ma_ratio"] and momentum < 0:
            phase = "CONTRACTION"
        else:
            phase = "NEUTRAL"

        # Z-score
        std = float(s.rolling(min(ma_window, len(s))).std().iloc[-1])
        zscore = (current - ma_long) / std if std != 0 else 0.0

        # Percentile rank vs 1Y
        rank_1y = float((s.tail(252) < current).sum() / min(len(s), 252) * 100)

        metrics = {
            "current_value":    round(current, 2),
            "ma_long":          round(ma_long, 2),
            "ma_short":         round(ma_short, 2),
            "ma_ratio":         round(ma_ratio, 3),
            "momentum_20d":     round(momentum * 100, 1),
            "zscore_1y":        round(zscore, 2),
            "percentile_1y":    round(rank_1y, 1),
        }

        return {
            **PHASES[phase],
            "phase":   phase,
            "metrics": metrics,
        }

    def classify_history(self, series: pd.Series, min_window: int = 50) -> pd.Series:
        """
        Returns a pd.Series of phase labels for each date.
        Used for chart background shading.
        """
        s = series.dropna()
        phases = pd.Series(index=s.index, dtype=str)

        ma_long_series  = s.rolling(REGIME_THRESHOLDS["ma_window"],  min_periods=min_window).mean()
        ma_short_series = s.rolling(50, min_periods=min_window // 2).mean()
        mom_series      = s.pct_change(REGIME_THRESHOLDS["momentum_window"])
        std_series      = s.rolling(REGIME_THRESHOLDS["ma_window"], min_periods=min_window).std()

        for i in range(len(s)):
            ml  = ma_long_series.iloc[i]
            ms  = ma_short_series.iloc[i]
            cur = s.iloc[i]
            mom = mom_series.iloc[i]
            if pd.isna(ml) or ml == 0:
                phases.iloc[i] = "NEUTRAL"
                continue
            ratio = cur / ml
            if ratio >= REGIME_THRESHOLDS["peak_ma_ratio"]:
                phases.iloc[i] = "PEAK"
            elif ratio >= REGIME_THRESHOLDS["expansion_ma_ratio"] and (pd.isna(mom) or mom > 0):
                phases.iloc[i] = "EXPANSION"
            elif ratio <= REGIME_THRESHOLDS["trough_ma_ratio"]:
                phases.iloc[i] = "TROUGH"
            elif ratio <= REGIME_THRESHOLDS["contraction_ma_ratio"] and (pd.isna(mom) or mom < 0):
                phases.iloc[i] = "CONTRACTION"
            else:
                phases.iloc[i] = "NEUTRAL"
        return phases

    def scorecard(
        self,
        freight_series: pd.Series,
        macro_data: dict | None = None,
        orderbook_pct: float | None = None,
    ) -> pd.DataFrame:
        """
        Returns 6-indicator RAG scorecard as DataFrame.
        Columns: indicator, value, signal, status (green/amber/red), description
        """
        rows = []

        # 1. Price momentum (BDI proxy vs 200D MA)
        s = freight_series.dropna()
        if len(s) >= 50:
            ma200 = float(s.rolling(min(200, len(s))).mean().iloc[-1])
            current = float(s.iloc[-1])
            ratio = current / ma200 if ma200 != 0 else 1.0
            if ratio > 1.10:
                status, signal = "green", "Rates well above MA200"
            elif ratio > 0.95:
                status, signal = "amber", "Rates near MA200"
            else:
                status, signal = "red", "Rates below MA200"
            rows.append({
                "indicator":   "Price vs MA200",
                "value":       f"{ratio:.2f}x",
                "signal":      signal,
                "status":      status,
                "description": "BDI Proxy / 200-day moving average",
            })

        # 2. Rate momentum (4W vs 52W return)
        if len(s) >= 252:
            ret_4w  = float(s.pct_change(20).iloc[-1])
            ret_52w = float(s.pct_change(252).iloc[-1])
            if ret_4w > 0.05 and ret_52w > 0:
                status, signal = "green", f"4W: +{ret_4w*100:.1f}% | 52W: +{ret_52w*100:.1f}%"
            elif ret_4w < -0.05:
                status, signal = "red", f"4W: {ret_4w*100:.1f}% | 52W: {ret_52w*100:.1f}%"
            else:
                status, signal = "amber", f"4W: {ret_4w*100:.1f}% | 52W: {ret_52w*100:.1f}%"
            rows.append({
                "indicator":   "Rate Momentum",
                "value":       f"4W {ret_4w*100:+.1f}%",
                "signal":      signal,
                "status":      status,
                "description": "4-week vs 52-week price momentum",
            })

        # 3. Volatility regime
        if len(s) >= 60:
            vol_30 = float(s.pct_change().rolling(30).std().iloc[-1] * np.sqrt(252))
            vol_1y = float(s.pct_change().rolling(252).std().iloc[-1] * np.sqrt(252)) if len(s) >= 252 else vol_30
            vol_ratio = vol_30 / vol_1y if vol_1y > 0 else 1.0
            if vol_ratio < 0.8:
                status, signal = "green", "Low vol environment"
            elif vol_ratio < 1.3:
                status, signal = "amber", "Normal vol regime"
            else:
                status, signal = "red", "Elevated volatility"
            rows.append({
                "indicator":   "Volatility Regime",
                "value":       f"{vol_30*100:.0f}% ann.",
                "signal":      signal,
                "status":      status,
                "description": "30D realised vol vs 1Y average",
            })

        # 4. Orderbook / fleet ratio
        if orderbook_pct is not None:
            if orderbook_pct < 8:
                status, signal = "green", "Low orderbook — supply disciplined"
            elif orderbook_pct < 15:
                status, signal = "amber", "Moderate orderbook pressure"
            else:
                status, signal = "red", "High orderbook — supply overhang risk"
            rows.append({
                "indicator":   "Orderbook / Fleet",
                "value":       f"{orderbook_pct:.1f}%",
                "signal":      signal,
                "status":      status,
                "description": "Newbuilding orders as % of existing fleet (ESTIMATED)",
            })

        # 5. Commodity demand (iron ore proxy YoY if available)
        if macro_data and "Iron Ore [PROXY]" in macro_data:
            ore = macro_data.get("Iron Ore [PROXY]", {})
            delta_5d = ore.get("delta_5d")
            if delta_5d is not None:
                if delta_5d > 0.02:
                    status, signal = "green", "Iron ore recovering — positive for Capesize"
                elif delta_5d < -0.02:
                    status, signal = "red", "Iron ore declining — Capesize demand risk"
                else:
                    status, signal = "amber", "Iron ore stable"
                rows.append({
                    "indicator":   "Iron Ore Demand",
                    "value":       f"{delta_5d*100:+.1f}% 5D",
                    "signal":      signal,
                    "status":      status,
                    "description": "Iron ore proxy 5-day change",
                })

        # 6. Percentile rank
        if len(s) >= 252:
            rank = float((s.tail(252) < float(s.iloc[-1])).sum() / min(len(s), 252) * 100)
            if rank > 70:
                status, signal = "green", "Above historical average"
            elif rank > 30:
                status, signal = "amber", "Near historical average"
            else:
                status, signal = "red", "Below historical average"
            rows.append({
                "indicator":   "1Y Percentile Rank",
                "value":       f"{rank:.0f}th pct",
                "signal":      signal,
                "status":      status,
                "description": "Current level vs last 12 months",
            })

        return pd.DataFrame(rows)
