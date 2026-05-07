import io
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from src.config import (
    BDI_DATAHUB_URL,
    BDI_STALE_DAYS,
    CACHE_DIR,
    FREIGHT_CACHE_TTL_HOURS,
    SHIPPING_EQUITIES,
)
from src.utils.cache_manager import CacheManager

logger = logging.getLogger("freightiq.freight")

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class FreightDataManager:
    """
    Manages all freight market data using a three-layer cascade:
    1. Disk cache  →  2. datahub.io BDI CSV  →  3. BDRY ETF proxy
    All proxied data is clearly flagged with source="BDRY_proxy".
    """

    def __init__(self, cache_manager: CacheManager | None = None):
        self.cache = cache_manager or CacheManager(CACHE_DIR)

    # ──────────────────────────────────────────────────────────────────────────
    # BDI History
    # ──────────────────────────────────────────────────────────────────────────

    def get_bdi_history(self, start: str = "2000-01-01") -> pd.DataFrame:
        """
        Returns DataFrame with columns: [value, source].
        Index is DatetimeIndex.
        source values: 'datahub', 'BDRY_proxy', 'cached'
        """
        # Layer 1 — disk cache
        cached = self.cache.load("bdi_history", "freight", max_age_hours=FREIGHT_CACHE_TTL_HOURS)
        if cached is not None:
            cached.index = pd.to_datetime(cached.index)
            return cached

        # Layer 2 — datahub.io
        df = self._fetch_datahub_bdi(start)
        if df is not None and not df.empty:
            self.cache.save("bdi_history", df, "freight")
            return df

        # Layer 3 — BDRY ETF proxy
        df = self._fetch_bdry_proxy(start)
        if df is not None and not df.empty:
            self.cache.save("bdi_history", df, "freight")
            return df

        logger.error("All BDI data sources failed")
        return pd.DataFrame(columns=["value", "source"])

    def _fetch_datahub_bdi(self, start: str) -> pd.DataFrame | None:
        try:
            session = requests.Session()
            session.headers["User-Agent"] = _USER_AGENT
            resp = session.get(BDI_DATAHUB_URL, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            # Detect date column
            date_col = next((c for c in df.columns if "date" in c.lower()), df.columns[0])
            val_col = next((c for c in df.columns if c.upper() in ("BDI", "VALUE", "CLOSE")), df.columns[-1])
            df = df.rename(columns={date_col: "date", val_col: "value"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date").sort_index()
            df = df[["value"]].copy()
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna()
            df = df[df.index >= start]
            # Check freshness
            days_old = (pd.Timestamp.now() - df.index.max()).days
            if days_old > BDI_STALE_DAYS:
                logger.warning(f"datahub.io BDI data is {days_old} days old — falling through to proxy")
                return None
            df["source"] = "datahub"
            return df
        except Exception as e:
            logger.warning(f"datahub.io BDI fetch failed: {e}")
            return None

    def _fetch_bdry_proxy(self, start: str) -> pd.DataFrame | None:
        try:
            raw = yf.download("BDRY", start=start, auto_adjust=True, progress=False)
            if raw.empty:
                return None
            # Handle MultiIndex columns from newer yfinance
            if isinstance(raw.columns, pd.MultiIndex):
                raw = raw.xs("BDRY", axis=1, level=1) if "BDRY" in raw.columns.get_level_values(1) else raw
                raw.columns = raw.columns.droplevel(1) if isinstance(raw.columns, pd.MultiIndex) else raw.columns
            close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
            df = pd.DataFrame({"value": close, "source": "BDRY_proxy"})
            df.index.name = "date"
            df = df.dropna(subset=["value"])
            return df
        except Exception as e:
            logger.warning(f"BDRY ETF proxy fetch failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Shipping Equities
    # ──────────────────────────────────────────────────────────────────────────

    def get_shipping_equities(self, tickers: list | None = None, period: str = "2y") -> pd.DataFrame:
        """Returns DataFrame of closing prices, one column per ticker."""
        if tickers is None:
            tickers = list(SHIPPING_EQUITIES.keys())

        cache_key = f"shipping_equities_{period}"
        cached = self.cache.load(cache_key, "freight", max_age_hours=FREIGHT_CACHE_TTL_HOURS)
        if cached is not None:
            cached.index = pd.to_datetime(cached.index)
            return cached

        frames = {}
        for ticker in tickers:
            try:
                raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
                if raw.empty:
                    continue
                # Flatten MultiIndex if present
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.droplevel(1)
                close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
                frames[ticker] = close.dropna()
                time.sleep(0.15)
            except Exception as e:
                logger.warning(f"Equity fetch failed for {ticker}: {e}")

        if not frames:
            return pd.DataFrame()

        df = pd.DataFrame(frames)
        df.index.name = "date"
        self.cache.save(cache_key, df, "freight")
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Weighted Composite Index
    # ──────────────────────────────────────────────────────────────────────────

    def get_weighted_shipping_index(self, period: str = "2y") -> pd.Series:
        """
        Returns a composite shipping index normalized to 100 at start.
        Labeled 'FreightIQ Composite Index [PROXY]'.
        """
        equities = self.get_shipping_equities(period=period)
        if equities.empty:
            return pd.Series(dtype=float, name="FreightIQ Composite Index")

        normalized = pd.DataFrame()
        total_weight = 0.0
        composite = None

        for ticker, meta in SHIPPING_EQUITIES.items():
            if ticker not in equities.columns:
                continue
            series = equities[ticker].dropna()
            if series.empty:
                continue
            weight = meta.get("weight", 0.1)
            norm = series / series.iloc[0] * 100
            total_weight += weight
            if composite is None:
                composite = norm * weight
            else:
                composite = composite.add(norm * weight, fill_value=0)

        if composite is None or total_weight == 0:
            return pd.Series(dtype=float, name="FreightIQ Composite Index")

        composite = composite / total_weight
        composite.name = "FreightIQ Composite Index"
        return composite

    # ──────────────────────────────────────────────────────────────────────────
    # Snapshot (latest values)
    # ──────────────────────────────────────────────────────────────────────────

    def get_freight_snapshot(self) -> dict:
        """Returns dict with latest values, 1D change, 5D change for key metrics."""
        equities = self.get_shipping_equities(period="1mo")
        snapshot = {}

        for ticker in SHIPPING_EQUITIES:
            if equities.empty or ticker not in equities.columns:
                snapshot[ticker] = {"value": None, "delta_1d": None, "delta_5d": None, "is_proxy": True}
                continue
            series = equities[ticker].dropna()
            if series.empty:
                snapshot[ticker] = {"value": None, "delta_1d": None, "delta_5d": None, "is_proxy": True}
                continue
            latest = float(series.iloc[-1])
            delta_1d = float(series.pct_change(1).iloc[-1]) if len(series) > 1 else None
            delta_5d = float(series.pct_change(5).iloc[-1]) if len(series) > 5 else None
            snapshot[ticker] = {
                "value":    latest,
                "delta_1d": delta_1d,
                "delta_5d": delta_5d,
                "is_proxy": True,
            }

        return snapshot

    # ──────────────────────────────────────────────────────────────────────────
    # Placeholder for official Baltic Exchange data
    # ──────────────────────────────────────────────────────────────────────────

    def fetch_baltic_official(self, api_key: str) -> pd.DataFrame:
        """
        Placeholder for Baltic Exchange official API integration.
        Requires a Baltic Exchange data license.
        Contact: data@balticexchange.com
        """
        raise NotImplementedError(
            "Baltic Exchange official data requires a paid subscription. "
            "Contact data@balticexchange.com"
        )
