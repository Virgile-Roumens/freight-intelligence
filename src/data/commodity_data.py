import logging
import time

import pandas as pd
import yfinance as yf

from src.config import CACHE_DIR, COMMODITY_PROXIES, MACRO_CACHE_TTL_HOURS
from src.utils.cache_manager import CacheManager

logger = logging.getLogger("freightiq.commodity")


class CommodityDataManager:
    """
    Provides commodity price data using yfinance equity proxies
    where no direct futures data is available (coal, iron ore).
    All proxy data is clearly labelled.
    """

    IRON_ORE_PROXIES = ["VALE", "BHP", "RIO"]
    COAL_PROXIES = ["BTU", "ARLP"]
    GRAIN_TICKERS = {"ZC=F": "Corn", "ZW=F": "Wheat", "ZS=F": "Soybeans"}
    ENERGY_TICKERS = {"CL=F": "WTI Crude", "BZ=F": "Brent Crude", "NG=F": "Natural Gas"}
    METALS_TICKERS = {"HG=F": "Copper", "GC=F": "Gold"}

    def __init__(self, cache_manager: CacheManager | None = None):
        self.cache = cache_manager or CacheManager(CACHE_DIR)

    def _fetch_single(self, ticker: str, period: str = "2y") -> pd.Series | None:
        try:
            raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
            if raw.empty:
                return None
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.droplevel(1)
            close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
            return close.dropna()
        except Exception as e:
            logger.warning(f"Fetch failed for {ticker}: {e}")
            return None

    def get_iron_ore_proxy(self, period: str = "2y") -> pd.DataFrame:
        """Returns iron ore proxy DataFrame (VALE, BHP, RIO). Labelled [PROXY]."""
        cache_key = f"iron_ore_proxy_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            return cached
        frames = {}
        for ticker in self.IRON_ORE_PROXIES:
            s = self._fetch_single(ticker, period)
            if s is not None:
                frames[f"{ticker} [PROXY]"] = s
            time.sleep(0.1)
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        self.cache.save(cache_key, df, "macro")
        return df

    def get_coal_proxy(self, period: str = "2y") -> pd.DataFrame:
        """Returns thermal coal proxy DataFrame (BTU, ARLP). Labelled [PROXY]."""
        cache_key = f"coal_proxy_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            return cached
        frames = {}
        for ticker in self.COAL_PROXIES:
            s = self._fetch_single(ticker, period)
            if s is not None:
                frames[f"{ticker} [PROXY]"] = s
            time.sleep(0.1)
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        self.cache.save(cache_key, df, "macro")
        return df

    def get_grain_basket(self, period: str = "2y") -> pd.DataFrame:
        """Returns grain futures prices (Corn, Wheat, Soybeans)."""
        cache_key = f"grains_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            return cached
        frames = {}
        for ticker, name in self.GRAIN_TICKERS.items():
            s = self._fetch_single(ticker, period)
            if s is not None:
                frames[name] = s
            time.sleep(0.1)
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        self.cache.save(cache_key, df, "macro")
        return df

    def get_energy_basket(self, period: str = "2y") -> pd.DataFrame:
        """Returns energy prices (WTI, Brent, Natural Gas)."""
        cache_key = f"energy_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            return cached
        frames = {}
        for ticker, name in self.ENERGY_TICKERS.items():
            s = self._fetch_single(ticker, period)
            if s is not None:
                frames[name] = s
            time.sleep(0.1)
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        self.cache.save(cache_key, df, "macro")
        return df

    def get_metals_basket(self, period: str = "2y") -> pd.DataFrame:
        """Returns metals prices (Copper, Gold)."""
        cache_key = f"metals_{period}"
        cached = self.cache.load(cache_key, "macro", max_age_hours=MACRO_CACHE_TTL_HOURS)
        if cached is not None:
            return cached
        frames = {}
        for ticker, name in self.METALS_TICKERS.items():
            s = self._fetch_single(ticker, period)
            if s is not None:
                frames[name] = s
            time.sleep(0.1)
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        self.cache.save(cache_key, df, "macro")
        return df

    def get_all_commodities(self, period: str = "2y") -> pd.DataFrame:
        """Returns a unified DataFrame with all commodity series."""
        frames = [
            self.get_energy_basket(period),
            self.get_metals_basket(period),
            self.get_grain_basket(period),
            self.get_iron_ore_proxy(period),
        ]
        dfs = [f for f in frames if not f.empty]
        if not dfs:
            return pd.DataFrame()
        return pd.concat(dfs, axis=1).sort_index()

    def get_bunker_price_estimate(self) -> float | None:
        """
        Estimates VLSFO bunker price from Brent crude (USD/barrel → USD/mt).
        Conversion: Brent $/bbl × 6.5 ≈ VLSFO $/mt
        (1 tonne VLSFO ≈ 6.35 barrels + ~$60-80/mt refinery/quality premium)
        """
        try:
            raw = yf.download("BZ=F", period="5d", auto_adjust=True, progress=False)
            if raw.empty:
                return None
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.droplevel(1)
            close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
            brent = float(close.dropna().iloc[-1])
            # Brent is in USD/barrel; VLSFO ≈ Brent × 6.5 in USD/mt
            vlsfo_est = round(brent * 6.5, 0)
            # Sanity clamp: VLSFO realistic range $300–$1000/mt
            return float(max(300.0, min(1000.0, vlsfo_est)))
        except Exception:
            return None
