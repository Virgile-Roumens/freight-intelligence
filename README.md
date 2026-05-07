# FreightIQ — Dry Bulk Freight Intelligence Platform

A professional-grade market intelligence platform for dry bulk freight, built with Plotly Dash.
Designed for commodity traders and analysts at firms trading iron ore, coal, grain, and minor bulks.

---

## Features

| Page | Description |
|---|---|
| 🏠 Overview | Composite index · regime gauge · cross-asset snapshot · news |
| 📊 Market Dashboard | Freight proxy performance · moving averages · seasonality · regime |
| 🚢 Freight Analysis | BDI proxy deep dive · sub-index overlay · vol · z-score · cycle phase |
| ⚖️ Supply & Demand | Fleet & orderbook · **live AIS vessel tracking** · chokepoints · sanctions |
| 🌍 Geopolitical Intel | Chokepoint monitor · rerouting impact · IMO timeline · disruption events |
| 📈 Macro Overlay | FRED data · yield curve · USD index · industrial production · bunker cost |
| 📉 FFA & Derivatives | Manual forward curve builder · BDRY vol proxy · seasonal basis |
| 🔗 Cross-Commodity | Rolling correlation matrix · lead-lag analysis · scatter regression |
| 🧮 TCE Calculator | Full voyage economics · sensitivity tornado · scenario comparison · CSV export |
| 📰 Intelligence Feed | RSS aggregation · relevance scoring · signal detection · weekly briefing |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API keys

**AIS Live Tracking (free tier — required for the AIS Live Signal feature):**

Register at [aisstream.io](https://aisstream.io) and set:
```bash
# Windows
set AIS_API_KEY=your_key_here

# or add to a .env file
AIS_API_KEY=your_key_here
```

**FRED Macro Data (free):**
```bash
set FRED_API_KEY=your_key_here
```
Register at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html).
Without this key, FRED-sourced series (industrial production, yield curve, USD index) show as unavailable. All other features work without it.

### 3. Launch

```bash
# Windows — double-click launch.bat, or:
set PYTHONPATH=%CD%
python app_dash.py
```

Open: **http://localhost:8503**

---

## Data Sources

| Data | Source | Notes |
|---|---|---|
| BDI Historical | datahub.io public CSV | Falls back to BDRY ETF if stale |
| BDI Live Proxy | BDRY ETF via yfinance | **Not Baltic Exchange data — proxy** |
| Shipping Equities | yfinance (GOGL, SBLK, NMM, GNK, EGLE) | Composite index basket |
| Commodity Futures | yfinance (CL=F, ZC=F, HG=F, ZW=F...) | WTI, Brent, grains, metals |
| Macro Series | FRED API | INDPRO, yield curve, USD index |
| Live Vessel Positions | aisstream.io WebSocket (free tier) | 20s collection window, bulk carriers |
| News & Intelligence | RSS (TradeWinds, Splash247, Hellenic) | 2h cache |
| Fleet / Orderbook | 2024 estimates — UNCTAD / Clarksons public | Manual update required |

> **Proxy disclaimer:** FreightIQ uses market proxies for Baltic Exchange data. Real BDI, BCI, BPI, BSI, BHSI daily fixing requires a Baltic Exchange subscription. All proxied values are labelled **[PROXY]** in the UI.

---

## Architecture

```
freight_intelligence/
├── app_dash.py               # Entry point — Dash app + navbar
├── launch.bat                # Windows launcher
├── requirements.txt
├── dash_pages/               # One file per page (Dash multi-page routing)
│   ├── overview.py
│   ├── market.py
│   ├── freight.py
│   ├── supply_demand.py      # AIS live tracking
│   ├── geopolitical.py
│   ├── macro_overlay.py
│   ├── ffa.py
│   ├── cross_commodity.py
│   ├── tce_calculator.py
│   └── intelligence.py
├── dash_components/
│   └── cards.py              # Shared UI components (KPI cards, banners, tables)
├── src/
│   ├── config.py             # All constants, tickers, AIS config, thresholds
│   ├── data/
│   │   ├── freight_data.py   # FreightDataManager — yfinance + datahub BDI
│   │   ├── macro_data.py     # MacroDataManager — FRED + commodity futures
│   │   ├── news_data.py      # NewsAggregator — RSS feeds + signal detection
│   │   └── ais_data.py       # AIS WebSocket client — live vessel positions
│   ├── analytics/
│   │   ├── tce_calculator.py      # TCE + sensitivity + breakeven
│   │   ├── freight_analytics.py   # Seasonality, MA signals, drawdown
│   │   ├── correlation_engine.py  # Rolling correlation, lead-lag
│   │   └── regime_detector.py     # MA-ratio cycle phase classifier
│   └── utils/
│       ├── ui_styles.py      # Plotly dark theme template
│       ├── helpers.py        # Number formatting, z-score, delta colour
│       └── cache_manager.py  # Disk CSV cache (atomic writes)
├── data_cache/               # Persistent cross-session cache
├── scripts/
│   └── morning_briefing.py   # Daily HTML email briefing generator
└── .github/workflows/
    └── morning_briefing.yml  # GitHub Actions cron (07:30 Europe/Zurich)
```

---

## Daily Morning Briefing (Email Bot)

A scheduled GitHub Actions workflow sends an HTML morning briefing every day at **07:30 Europe/Zurich**, summarising overnight dry bulk market activity.

**Sections included:**
- Executive summary (cycle phase, BDRY level, FFA forward, key signals)
- Key levels table (BDRY, shipping equities, Brent, USD, treasuries, iron ore proxies)
- FFA forward curve (BDRY options put-call parity → implied BDI)
- Supply drivers (live AIS utilisation, Baltic route activity, chokepoint status)
- Demand drivers (iron ore, coal, grains — with route relevance notes)
- Geopolitical & macro signals (auto-detected from RSS)
- Top stories (TradeWinds, Splash247, Hellenic — relevance-scored)
- Today's watch (auto-generated trader action items)
- All sources cited with clickable verification links

### Setup

1. **Push the project to GitHub** (a private repo works fine).

2. **Generate a Gmail App Password** at https://myaccount.google.com/apppasswords (requires 2-Step Verification enabled). Copy the 16-character password.

3. **Add repository secrets** at *Settings → Secrets and variables → Actions*:
   - `GMAIL_USER` — your Gmail address
   - `GMAIL_APP_PASSWORD` — the 16-char app password from step 2
   - `AIS_API_KEY` — aisstream.io key (recommended, for live AIS section)
   - `FRED_API_KEY` — FRED key (optional, for macro fallbacks)

4. **Test manually**: in the GitHub Actions tab, run the *Morning Briefing — Dry Bulk* workflow once via the *Run workflow* button. Verify the email lands in your inbox.

5. **Done.** The cron runs automatically every morning — independent of your laptop being on or off, since the job runs in GitHub's cloud.

### Local preview

```bash
BRIEFING_TEST=1 python scripts/morning_briefing.py
```

Writes `scripts/briefing_preview.html` for inspection in a browser without sending an email.

---

## Conceptual Framework

FreightIQ is built around the two-layer model of freight price formation:

**Structural layer:** `Spot Rate = f(Ton-Mile Demand, Effective Supply)`

Effective supply ≠ headline fleet DWT. It is reduced by slow steaming, port congestion, canal disruptions, and ballast repositioning — all monitored in the Supply & Demand page.

**Shipping cycle:**
```
Demand Shock → Utilisation Tightens → Spot Spikes →
Ordering Wave (18-24M lag) → Fleet Over-Expansion →
Rates Collapse → Scrapping → Recovery
```

**TCE as the universal unit:** Voyage rates (USD/tonne) and TC rates (USD/day) are incomparable without TCE normalisation. The TCE Calculator converts any route into a daily hire equivalent.

---

## Known Limitations

- Baltic Exchange real-time data requires a paid subscription — all BDI/BCI/BPI/BSI/BHSI values are proxies
- FFA live quotes require a Baltic Exchange or broker platform — forward curves are manually entered
- Fleet and orderbook data are 2024 estimates — live data requires Clarksons Research or VesselsValue
- Some RSS feeds (Bloomberg, Reuters) may require authentication

---

*FreightIQ — built for commodity traders, not investment advice.*
