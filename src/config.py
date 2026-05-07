import os
from pathlib import Path

# Load .env from project root if present (local dev). On GitHub Actions, secrets
# are injected as real env vars so this is a no-op. Never commit .env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ─── PATHS ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data_cache"
EXPORTS_DIR = BASE_DIR / "exports" / "reports"
LOGS_DIR = BASE_DIR / "logs"

# ─── API KEYS — env-only, never commit values here ────────────────────────────
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")  # Optional: newsapi.org
AIS_API_KEY  = os.getenv("AIS_API_KEY",  "")
AIS_WS_URL   = "wss://stream.aisstream.io/v0/stream"

# AIS dry bulk ship type codes (IMO classification)
AIS_BULK_CARRIER_TYPES = list(range(70, 80))  # 70-79 = bulk carriers
AIS_COLLECT_SECONDS    = 20   # seconds to stream before closing connection
AIS_CACHE_TTL_MINUTES  = 5   # minutes to cache vessel snapshot

# ─── CACHE TTLs ───────────────────────────────────────────────────────────────
FREIGHT_CACHE_TTL_HOURS = 4
MACRO_CACHE_TTL_HOURS = 24
NEWS_CACHE_TTL_HOURS = 2
SUPPLY_CACHE_TTL_HOURS = 168  # 1 week

# ─── APP METADATA ─────────────────────────────────────────────────────────────
APP_NAME = "FreightIQ"
APP_SUBTITLE = "Dry Bulk Freight Strategic Intelligence"
APP_VERSION = "1.0.0"

# ─── DESIGN SYSTEM ────────────────────────────────────────────────────────────
COLORS = {
    "bg_primary":     "#0d1117",
    "bg_secondary":   "#161b22",
    "bg_card":        "#21262d",
    "bg_hover":       "#30363d",
    "border":         "#30363d",
    "text_primary":   "#e6edf3",
    "text_secondary": "#8b949e",
    "text_faint":     "#484f58",
    "accent_green":   "#3fb950",
    "accent_red":     "#f85149",
    "accent_blue":    "#58a6ff",
    "accent_yellow":  "#d29922",
    "accent_orange":  "#db6d28",
    "accent_purple":  "#bc8cff",
    "chart_palette": [
        "#58a6ff", "#3fb950", "#f85149", "#d29922", "#bc8cff",
        "#79c0ff", "#56d364", "#ff7b72", "#e3b341", "#d2a8ff",
    ],
}

PLOTLY_TEMPLATE = "freightiq_dark"

# ─── BALTIC INDICES ───────────────────────────────────────────────────────────
BALTIC_INDICES = {
    "BDI":  {"name": "Baltic Dry Index",       "description": "Overall dry bulk benchmark",  "color": "#58a6ff"},
    "BCI":  {"name": "Baltic Capesize Index",   "vessel": "Capesize 180K DWT",               "color": "#3fb950"},
    "BPI":  {"name": "Baltic Panamax Index",    "vessel": "Panamax 75K DWT",                 "color": "#d29922"},
    "BSI":  {"name": "Baltic Supramax Index",   "vessel": "Supramax 56K DWT",                "color": "#bc8cff"},
    "BHSI": {"name": "Baltic Handysize Index",  "vessel": "Handysize 35K DWT",               "color": "#db6d28"},
}

# ─── SHIPPING EQUITY PROXIES ──────────────────────────────────────────────────
SHIPPING_EQUITIES = {
    "BDRY": {"name": "Breakwave Dry Bulk ETF",   "weight": 0.42, "color": "#58a6ff"},
    "SBLK": {"name": "Star Bulk Carriers",        "weight": 0.23, "color": "#d29922"},
    "NMM":  {"name": "Navios Maritime Partners",  "weight": 0.10, "color": "#bc8cff"},
    "EGLE": {"name": "Eagle Bulk Shipping",       "weight": 0.10, "color": "#db6d28"},
    "GNK":  {"name": "Genco Shipping & Trading",  "weight": 0.08, "color": "#79c0ff"},
    "DSX":  {"name": "Diana Shipping",            "weight": 0.04, "color": "#56d364"},
    "SB":   {"name": "Safe Bulkers",              "weight": 0.03, "color": "#ff7b72"},
}

# ─── COMMODITY FUTURES (yfinance) ─────────────────────────────────────────────
COMMODITY_TICKERS = {
    "CL=F":  "WTI Crude Oil",
    "BZ=F":  "Brent Crude",
    "NG=F":  "Natural Gas",
    "HO=F":  "Heating Oil",
    "HG=F":  "Copper",
    "ZC=F":  "Corn",
    "ZS=F":  "Soybeans",
    "ZW=F":  "Wheat",
    "GC=F":  "Gold",
    "SI=F":  "Silver",
}

# Coal / iron ore equity proxies (no direct futures on free platforms)
COMMODITY_PROXIES = {
    "BTU":  "Peabody Energy (thermal coal proxy)",
    "ARLP": "Alliance Resource Partners (coal proxy)",
    "VALE": "Vale S.A. (iron ore proxy)",
    "BHP":  "BHP Group (iron ore / coal proxy)",
    "RIO":  "Rio Tinto (iron ore proxy)",
}

# ─── FRED SERIES ──────────────────────────────────────────────────────────────
FRED_SERIES = {
    "INDPRO":       "US Industrial Production Index",
    "IPMAN":        "US Manufacturing Production",
    "DCOILWTICO":   "WTI Crude Oil Price",
    "DCOILBRENTEU": "Brent Crude Price",
    "PIORECRUSD":   "Iron Ore Price (World Bank)",
    "PCOALAUUSDM":  "Coal Price Australia (monthly)",
    "PWHEAMTUSDM":  "Wheat Price (monthly)",
    "PMAIZMTUSDM":  "Corn/Maize Price (monthly)",
    "PSOYBUSDM":    "Soybean Price (monthly)",
    "PCOPPUSDM":    "Copper Price (monthly)",
    "CPIAUCSL":     "US CPI",
    "UNRATE":       "US Unemployment Rate",
    "DGS10":        "10Y US Treasury Yield",
    "DGS2":         "2Y US Treasury Yield",
    "DTWEXBGS":     "USD Broad Index",
    "PAYEMS":       "Nonfarm Payrolls",
}

# ─── VESSEL SPECS ─────────────────────────────────────────────────────────────
VESSEL_SPECS = {
    "Capesize": {
        "typical_dwt":              180000,
        "consumption_laden_mt_day": 55.0,   # VLSFO at ~12.5 knots
        "consumption_ballast_mt_day": 48.0,
        "consumption_port_mt_day":   5.0,
        "typical_opex_usd_day":     8500,
        "typical_speed_knots":      12.5,
    },
    "Panamax": {
        "typical_dwt":              75000,
        "consumption_laden_mt_day": 32.0,
        "consumption_ballast_mt_day": 28.0,
        "consumption_port_mt_day":   3.5,
        "typical_opex_usd_day":     7000,
        "typical_speed_knots":      13.0,
    },
    "Supramax": {
        "typical_dwt":              56000,
        "consumption_laden_mt_day": 26.0,
        "consumption_ballast_mt_day": 22.0,
        "consumption_port_mt_day":   3.0,
        "typical_opex_usd_day":     6500,
        "typical_speed_knots":      13.5,
    },
    "Handysize": {
        "typical_dwt":              35000,
        "consumption_laden_mt_day": 20.0,
        "consumption_ballast_mt_day": 17.0,
        "consumption_port_mt_day":   2.5,
        "typical_opex_usd_day":     6000,
        "typical_speed_knots":      13.0,
    },
}

# ─── TRADE ROUTES ─────────────────────────────────────────────────────────────
TRADE_ROUTES = {
    "Brazil_to_China_iron_ore": {
        "display": "Brazil → China (Iron Ore)",
        "segment": "Capesize",
        "laden_nm": 11200,
        "ballast_nm": 9500,
        "typical_cargo_mt": 180000,
        "typical_port_days": 4.0,
    },
    "Australia_to_China_iron_ore": {
        "display": "W.Australia → China (Iron Ore)",
        "segment": "Capesize",
        "laden_nm": 4500,
        "ballast_nm": 4500,
        "typical_cargo_mt": 170000,
        "typical_port_days": 3.5,
    },
    "Australia_to_Japan_coal": {
        "display": "Australia → Japan (Coal)",
        "segment": "Panamax",
        "laden_nm": 4300,
        "ballast_nm": 4000,
        "typical_cargo_mt": 70000,
        "typical_port_days": 3.0,
    },
    "USA_Gulf_to_Japan_grain": {
        "display": "US Gulf → Japan (Grain)",
        "segment": "Panamax",
        "laden_nm": 9200,
        "ballast_nm": 8000,
        "typical_cargo_mt": 55000,
        "typical_port_days": 3.5,
    },
    "Black_Sea_to_Europe_grain": {
        "display": "Black Sea → Europe (Grain)",
        "segment": "Supramax",
        "laden_nm": 1500,
        "ballast_nm": 1500,
        "typical_cargo_mt": 35000,
        "typical_port_days": 3.0,
    },
    "Indonesia_to_India_coal": {
        "display": "Indonesia → India (Coal)",
        "segment": "Panamax",
        "laden_nm": 3200,
        "ballast_nm": 3000,
        "typical_cargo_mt": 75000,
        "typical_port_days": 3.0,
    },
    "South_Africa_to_EU_coal": {
        "display": "South Africa → Europe (Coal)",
        "segment": "Panamax",
        "laden_nm": 6200,
        "ballast_nm": 5800,
        "typical_cargo_mt": 60000,
        "typical_port_days": 3.5,
    },
    "USA_Gulf_to_Europe_grain": {
        "display": "US Gulf → Europe (Grain)",
        "segment": "Supramax",
        "laden_nm": 5000,
        "ballast_nm": 4500,
        "typical_cargo_mt": 40000,
        "typical_port_days": 3.0,
    },
    "Colombia_to_Europe_coal": {
        "display": "Colombia → Europe (Coal)",
        "segment": "Panamax",
        "laden_nm": 4500,
        "ballast_nm": 4000,
        "typical_cargo_mt": 65000,
        "typical_port_days": 3.0,
    },
}

# ─── GEOPOLITICAL CHOKEPOINTS ─────────────────────────────────────────────────
CHOKEPOINTS = {
    "Suez Canal": {
        "status": "OPEN",
        "annual_dry_bulk_pct": 12,
        "rerouting_via": "Cape of Good Hope",
        "extra_distance_nm": 3500,
        "effective_supply_impact_pct": -4,
        "notes": "Key for Asia-Europe routes",
        "lat": 30.42,
        "lon": 32.35,
    },
    "Panama Canal": {
        "status": "OPEN",
        "annual_dry_bulk_pct": 6,
        "rerouting_via": "Cape Horn",
        "extra_distance_nm": 8000,
        "effective_supply_impact_pct": -2,
        "notes": "Drought restrictions in 2023-24; water levels recovering",
        "lat": 9.08,
        "lon": -79.68,
    },
    "Red Sea / Bab el-Mandeb": {
        "status": "RESTRICTED",
        "annual_dry_bulk_pct": 10,
        "rerouting_via": "Cape of Good Hope",
        "extra_distance_nm": 4500,
        "effective_supply_impact_pct": -6,
        "notes": "Houthi attacks ongoing since Dec 2023",
        "lat": 12.60,
        "lon": 43.45,
    },
    "Black Sea": {
        "status": "RESTRICTED",
        "annual_dry_bulk_pct": 4,
        "rerouting_via": "N/A",
        "extra_distance_nm": 0,
        "effective_supply_impact_pct": -2,
        "notes": "Ukraine conflict; grain export corridor at risk",
        "lat": 43.00,
        "lon": 34.00,
    },
    "Strait of Malacca": {
        "status": "OPEN",
        "annual_dry_bulk_pct": 35,
        "rerouting_via": "Lombok Strait",
        "extra_distance_nm": 900,
        "effective_supply_impact_pct": -15,
        "notes": "Busiest shipping lane globally",
        "lat": 2.50,
        "lon": 101.50,
    },
    "Strait of Hormuz": {
        "status": "OPEN",
        "annual_dry_bulk_pct": 3,
        "rerouting_via": "N/A — no viable alternative",
        "extra_distance_nm": 0,
        "effective_supply_impact_pct": -8,
        "notes": "Handles ~20% of global oil; Iran tensions; closure would spike energy costs → freight rates",
        "lat": 26.57,
        "lon": 56.25,
    },
}

# ─── NEWS RSS FEEDS ───────────────────────────────────────────────────────────
NEWS_FEEDS = {
    "TradeWinds":         "https://www.tradewindsnews.com/rss",
    "Splash247":          "https://splash247.com/feed/",
    "Hellenic Shipping":  "https://www.hellenicshippingnews.com/feed/",
    "Reuters Business":   "https://feeds.reuters.com/reuters/businessNews",
    "Bloomberg Markets":  "https://feeds.bloomberg.com/markets/news.rss",
    "UNCTAD":             "https://unctad.org/news/feed",
}

KEYWORDS_FREIGHT = [
    "Baltic Dry", "BDI", "Capesize", "Panamax", "Supramax", "Handysize",
    "dry bulk", "freight rate", "FFA", "chartering", "vessel", "shipping",
    "Suez Canal", "Panama Canal", "iron ore", "coal trade", "grain trade",
    "port congestion", "fleet utilisation", "ton-mile", "bunker",
    "scrapping", "orderbook", "newbuilding", "Red Sea", "Black Sea",
    "Houthi", "Ukraine grain", "sanctions shipping", "IMO", "CII", "EEXI",
    "Strait of Hormuz", "Hormuz", "Iran", "Persian Gulf",
]

KEYWORDS_GEO = [
    "Red Sea", "Suez", "Panama", "Malacca", "Black Sea", "Houthi",
    "sanctions", "embargo", "conflict", "war", "blockade", "disruption",
    "rerouting", "Ukraine", "Iran", "Russia",
]

KEYWORDS_REGULATORY = [
    "IMO", "CII", "EEXI", "carbon", "emissions", "decarbonisation",
    "methanol", "ammonia", "LNG", "scrubber", "2030", "2050",
]

# ─── HISTORICAL EVENTS FOR CHART ANNOTATIONS ──────────────────────────────────
HISTORICAL_EVENTS = [
    {"date": "2008-09-15", "label": "Lehman Collapse", "color": "#f85149"},
    {"date": "2016-02-10", "label": "BDI All-Time Low", "color": "#f85149"},
    {"date": "2020-03-11", "label": "COVID Pandemic", "color": "#f85149"},
    {"date": "2021-03-23", "label": "Ever Given Suez", "color": "#d29922"},
    {"date": "2021-10-01", "label": "Freight Boom Peak", "color": "#3fb950"},
    {"date": "2022-02-24", "label": "Ukraine Invasion", "color": "#f85149"},
    {"date": "2023-12-01", "label": "Red Sea Attacks", "color": "#f85149"},
]

# ─── REGIME DETECTION THRESHOLDS ─────────────────────────────────────────────
REGIME_THRESHOLDS = {
    "expansion_ma_ratio": 1.05,
    "peak_ma_ratio":      1.20,
    "contraction_ma_ratio": 0.95,
    "trough_ma_ratio":    0.80,
    "ma_window":          200,
    "momentum_window":    20,
}

# ─── FLEET ESTIMATES (2024, from public sources) ──────────────────────────────
# Source: UNCTAD / Clarksons public reports — approximate
FLEET_ESTIMATES_2024 = {
    "Capesize":  {"vessels": 1750, "total_dwt_mt": 385.0, "avg_age_years": 11.2},
    "Panamax":   {"vessels": 2450, "total_dwt_mt": 185.0, "avg_age_years": 10.8},
    "Supramax":  {"vessels": 3800, "total_dwt_mt": 225.0, "avg_age_years": 9.5},
    "Handysize": {"vessels": 3200, "total_dwt_mt": 120.0, "avg_age_years": 12.1},
}

# ─── ORDERBOOK ESTIMATES (2024) ───────────────────────────────────────────────
ORDERBOOK_2024 = {
    "Capesize":  {"pct_of_fleet": 7.5,  "delivery_2025": 45, "delivery_2026": 38, "delivery_2027": 22},
    "Panamax":   {"pct_of_fleet": 10.2, "delivery_2025": 85, "delivery_2026": 72, "delivery_2027": 30},
    "Supramax":  {"pct_of_fleet": 8.8,  "delivery_2025": 95, "delivery_2026": 80, "delivery_2027": 35},
    "Handysize": {"pct_of_fleet": 6.1,  "delivery_2025": 55, "delivery_2026": 42, "delivery_2027": 18},
}

# ─── SANCTIONS / SHADOW FLEET ────────────────────────────────────────────────
SANCTIONS_DATA = [
    {"entity": "Russian Fleet (sanctioned)",  "vessels_est": 180, "dwt_mt": 14.0,  "status": "SANCTIONED"},
    {"entity": "Iranian Fleet (sanctioned)",   "vessels_est": 95,  "dwt_mt": 6.5,   "status": "SANCTIONED"},
    {"entity": "Dark/Shadow Fleet (est.)",     "vessels_est": 600, "dwt_mt": 45.0,  "status": "SHADOW"},
]

# ─── REGULATORY TIMELINE ─────────────────────────────────────────────────────
IMO_REGULATIONS = [
    {"date": "2020-01-01", "regulation": "IMO 2020 Sulphur Cap (0.5%)",          "impact": "Medium"},
    {"date": "2023-01-01", "regulation": "CII Rating Scheme (Carbon Intensity)",  "impact": "Medium"},
    {"date": "2023-05-01", "regulation": "EEXI Enforcement (Energy Efficiency)",  "impact": "High"},
    {"date": "2024-01-01", "regulation": "EU ETS Shipping Inclusion",             "impact": "High"},
    {"date": "2025-01-01", "regulation": "FuelEU Maritime (EU Green Fuel)",       "impact": "Medium"},
    {"date": "2030-01-01", "regulation": "IMO 2030 GHG 40% Reduction Target",    "impact": "High"},
    {"date": "2050-01-01", "regulation": "IMO 2050 Net-Zero Target",              "impact": "Critical"},
]

# ─── SESSION STATE KEYS ───────────────────────────────────────────────────────
SS_DATE_RANGE = "global_date_range"
SS_VESSEL_CLASS = "selected_vessel_class"
SS_REFRESH_TIME = "last_data_refresh"
SS_FRED_KEY = "fred_api_key_validated"

# ─── BDI DATA SOURCE ─────────────────────────────────────────────────────────
BDI_DATAHUB_URL = "https://datahub.io/core/bdi/r/bdi.csv"
BDI_STALE_DAYS = 30  # Fall through to proxy if datahub data older than this

# ─── FFA TENORS ──────────────────────────────────────────────────────────────
FFA_TENORS = ["Spot", "Q+1", "Q+2", "Q+3", "Cal+1", "Cal+2", "Cal+3"]
