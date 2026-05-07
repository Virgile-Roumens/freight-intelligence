import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from src.config import (
    CACHE_DIR,
    COMMODITY_TICKERS,
    FRED_API_KEY,
    FRED_SERIES,
    MACRO_CACHE_TTL_HOURS,
)
from src.utils.cache_manager import CacheManager

logger = logging.getLogger("freightiq.macro")


class MacroDataManager:
    """
    Fetches macro and commodity data from FRED, World Bank, and yfinance.
    Gracefully degrades when FRED_API_KEY is not set.
    """

    WORLD_BANK_GDP_URL = (
        "https://api.worldbank.org/v2/country/WLD;CHN;USA;DEU;JPN;IND"
        "/indicator/NY.GDP.MKTP.KD.ZG?format=json&per_page=100&mrv=20"
    )

    def __init__(self, cache_manager: CacheManager | None = None, api_key: str = ""):
        self.cache = cache_manager or CacheManager(CACHE_DIR)
        self.api_key = api_key or FRED_API_KEY
        self._fred = None
        if self.api_key:
            try:
                from fredapi import Fred
                self._fred = Fred(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"FRED init failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # FRED
    # ──────────────────────────────────────────────────────────────────────────

    def get_fred_series(
        self,
        series_id: str,
        start: str = "2000-01-01",
        end: str | None = None,
    ) -> pd.Series:
        """Returns a single FRED series as pd.Series, cached to disk."""
        if end is None:
            end = datetime.today().strftime("%Y-%m-%d")

        cache_key = f"fred_{series_id}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None and not cached.empty:
            s = cached.iloc[:, 0]
            s.index = pd.to_datetime(s.index)
            return s[s.index >= start]

        if self._fred is None:
            logger.warning(f"FRED API key not configured — cannot fetch {series_id}")
            return pd.Series(dtype=float, name=series_id)

        try:
            s = self._fred.get_series(series_id, observation_start=start, observation_end=end)
            s.name = series_id
            s.index = pd.to_datetime(s.index)
            s = s.dropna()
            self.cache.save(cache_key, s.to_frame(), "macro")
            time.sleep(0.3)  # respect FRED rate limit
            return s
        except Exception as e:
            logger.error(f"FRED fetch failed for {series_id}: {e}")
            return pd.Series(dtype=float, name=series_id)

    def get_multiple_fred_series(self, series_ids: list[str], start: str = "2010-01-01") -> pd.DataFrame:
        """Fetch multiple FRED series and return as aligned DataFrame."""
        result = {}
        for sid in series_ids:
            s = self.get_fred_series(sid, start=start)
            if not s.empty:
                result[sid] = s
        if not result:
            return pd.DataFrame()
        df = pd.DataFrame(result)
        df.index = pd.to_datetime(df.index)
        return df.sort_index()

    # ──────────────────────────────────────────────────────────────────────────
    # yfinance Commodities
    # ──────────────────────────────────────────────────────────────────────────

    def get_commodity_prices(self, tickers: dict | None = None, period: str = "2y") -> pd.DataFrame:
        """Returns DataFrame of commodity closing prices."""
        if tickers is None:
            tickers = COMMODITY_TICKERS

        cache_key = f"commodities_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            cached.index = pd.to_datetime(cached.index)
            return cached

        frames = {}
        for ticker, name in tickers.items():
            try:
                raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
                if raw.empty:
                    continue
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.droplevel(1)
                close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
                frames[name] = close.dropna()
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Commodity fetch failed for {ticker}: {e}")

        if not frames:
            return pd.DataFrame()

        df = pd.DataFrame(frames)
        df.index.name = "date"
        self.cache.save(cache_key, df, "macro")
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Yield Curve
    # ──────────────────────────────────────────────────────────────────────────

    def get_yield_curve(self, start: str = "2010-01-01") -> pd.DataFrame:
        """Returns yield curve DataFrame with spread."""
        tenors = {"DGS2": "2Y", "DGS5": "5Y", "DGS10": "10Y"}
        data = {}
        for series_id, label in tenors.items():
            s = self.get_fred_series(series_id, start=start)
            if not s.empty:
                data[label] = s
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data).sort_index().ffill()
        if "10Y" in df.columns and "2Y" in df.columns:
            df["spread_2_10"] = df["10Y"] - df["2Y"]
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Snapshot
    # ──────────────────────────────────────────────────────────────────────────

    def get_macro_snapshot(self) -> dict:
        """Returns latest values for key macro indicators."""
        snapshot = {}
        commodity_df = self.get_commodity_prices(period="1mo")
        for name in commodity_df.columns:
            series = commodity_df[name].dropna()
            if series.empty:
                continue
            snapshot[name] = {
                "value":    float(series.iloc[-1]),
                "delta_1d": float(series.pct_change(1).iloc[-1]) if len(series) > 1 else None,
                "delta_5d": float(series.pct_change(5).iloc[-1]) if len(series) > 5 else None,
            }

        # Add FRED series if available
        for series_id in ["DCOILBRENTEU", "DTWEXBGS", "DGS10"]:
            s = self.get_fred_series(series_id, start="2024-01-01")
            if not s.empty:
                snapshot[series_id] = {
                    "value":    float(s.iloc[-1]),
                    "delta_1d": float(s.pct_change(1).iloc[-1]) if len(s) > 1 else None,
                    "delta_5d": float(s.pct_change(5).iloc[-1]) if len(s) > 5 else None,
                }
        return snapshot

    # ──────────────────────────────────────────────────────────────────────────
    # World Bank GDP
    # ──────────────────────────────────────────────────────────────────────────

    def get_world_bank_gdp(self) -> pd.DataFrame:
        """Returns annual GDP growth rates for major economies."""
        cached = self.cache.load("world_bank_gdp", "macro", max_age_hours=168)
        if cached is not None:
            return cached

        try:
            import requests
            resp = requests.get(self.WORLD_BANK_GDP_URL, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data or len(data) < 2:
                return pd.DataFrame()
            records = data[1]
            rows = []
            for r in records:
                if r.get("value") is None:
                    continue
                rows.append({
                    "country": r["country"]["value"],
                    "year":    int(r["date"]),
                    "gdp_growth": float(r["value"]),
                })
            df = pd.DataFrame(rows)
            if df.empty:
                return df
            df = df.pivot(index="year", columns="country", values="gdp_growth").sort_index()
            self.cache.save("world_bank_gdp", df, "macro")
            return df
        except Exception as e:
            logger.warning(f"World Bank GDP fetch failed: {e}")
            return pd.DataFrame()
