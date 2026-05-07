"""
AIS vessel tracking via aisstream.io WebSocket API.

Connects to wss://stream.aisstream.io/v0/stream, subscribes to bulk carrier
position reports globally, collects for AIS_COLLECT_SECONDS, then returns
a snapshot of vessel positions enriched with Baltic Exchange route assignment,
port zone classification, and congestion metrics.
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger("freightiq.ais")

# ─── AIS Navigational Status codes ────────────────────────────────────────────
_NAV_STATUS = {
    0:  "Underway (engine)",
    1:  "At anchor",
    2:  "Not under command",
    3:  "Restricted manoeuvrability",
    5:  "Moored",
    6:  "Aground",
    8:  "Underway (sailing)",
    15: "Default / not defined",
}

# ─── Static port congestion estimates (manual, updated quarterly) ──────────────
PORT_CONGESTION_ESTIMATES = {
    "Qingdao":       {"region": "China",        "vessels_waiting": 12, "avg_wait_days": 2.1, "status": "Moderate"},
    "Tianjin":       {"region": "China",        "vessels_waiting": 8,  "avg_wait_days": 1.8, "status": "Light"},
    "Port Hedland":  {"region": "Australia",    "vessels_waiting": 6,  "avg_wait_days": 1.5, "status": "Light"},
    "Tubarao":       {"region": "Brazil",       "vessels_waiting": 15, "avg_wait_days": 3.2, "status": "Heavy"},
    "Rotterdam":     {"region": "Europe",       "vessels_waiting": 4,  "avg_wait_days": 0.8, "status": "Light"},
    "Singapore":     {"region": "SE Asia",      "vessels_waiting": 20, "avg_wait_days": 1.2, "status": "Moderate"},
    "Paradip":       {"region": "India",        "vessels_waiting": 18, "avg_wait_days": 4.5, "status": "Heavy"},
    "Richards Bay":  {"region": "South Africa", "vessels_waiting": 9,  "avg_wait_days": 2.8, "status": "Moderate"},
    "Newcastle(AU)": {"region": "Australia",    "vessels_waiting": 10, "avg_wait_days": 2.4, "status": "Moderate"},
    "Dampier":       {"region": "Australia",    "vessels_waiting": 5,  "avg_wait_days": 1.3, "status": "Light"},
}

# ─── Port zone bounding boxes: (lat_min, lon_min, lat_max, lon_max) ───────────
PORT_ZONES = {
    "Qingdao / N.China":   (35.0, 119.0, 38.5, 122.0),
    "Shanghai / Baoshan":  (28.5, 120.5, 32.0, 123.0),
    "Japan":               (30.0, 128.0, 44.0, 145.0),
    "South Korea":         (33.5, 124.0, 38.5, 130.5),
    "Singapore / Malacca": (-2.0,  98.0,  6.0, 105.0),
    "Port Hedland":        (-21.0, 117.0, -18.5, 119.5),
    "Dampier":             (-21.0, 116.0, -19.0, 118.0),
    "Newcastle (AU)":      (-33.5, 151.0, -32.5, 152.5),
    "Tubarao / Vitoria":   (-21.5, -41.5, -17.5, -38.0),
    "Rotterdam / ARA":     ( 51.0,   3.5,  52.5,   7.0),
    "Richards Bay":        (-29.5,  31.5, -28.5,  32.5),
    "Saldanha Bay":        (-33.5,  17.5, -32.5,  19.0),
    "Paradip / India":     ( 19.5,  86.0,  21.5,  87.5),
    "Colombia (Bolivar)":  ( 10.5, -75.5,  11.5, -74.0),
    "Suez Canal":          ( 29.5,  32.0,  31.5,  33.5),
    "Panama Canal":        (  8.5, -80.0,  10.0, -78.5),
    "Lombok Strait":       ( -9.0, 115.0,  -8.0, 116.5),
    "Bab el-Mandeb":       ( 11.5,  42.5,  13.5,  44.5),
    "Cape of Good Hope":   (-35.5,  17.5, -32.5,  21.0),
    "Strait of Hormuz":    ( 25.5,  55.5,  27.5,  57.5),
}

# ─── Baltic Exchange benchmark route definitions ───────────────────────────────
# Each route: corridor bounding boxes for vessel assignment.
# A vessel is assigned to a route if it falls in any segment box.
# COG hints help disambiguate overlapping boxes where needed.
BALTIC_ROUTES = {
    "C2  Tubarao → Rotterdam":      {
        "segment": "Capesize", "cargo": "Iron Ore", "distance_nm": 5150,
        "color": "#58a6ff",
        "description": "Iron ore Brazil → ARA range",
        "path": [(-20.5,-40.3), (-5,-35), (5,-20), (20,-18), (35,-10), (46,-5), (51.9,4.1)],
        "zones": [(-22,-42,-17,-37), (-10,-30,15,0), (38,-15,55,8)],
    },
    "C3  Tubarao → Qingdao":        {
        "segment": "Capesize", "cargo": "Iron Ore", "distance_nm": 15200,
        "color": "#3fb950",
        "description": "Iron ore Brazil → China (via Cape)",
        "path": [(-20.5,-40.3), (-35,-10), (-34.5,18.5), (-20,55), (-5,65), (5,80), (15,100), (25,115), (36,120.4)],
        "zones": [(-22,-42,-17,-37), (-38,-20,-28,30), (-35,30,-15,80), (0,75,25,120), (28,118,38,124)],
    },
    "C5  W.Australia → Qingdao":    {
        "segment": "Capesize", "cargo": "Iron Ore", "distance_nm": 3350,
        "color": "#d29922",
        "description": "Iron ore West Australia → China",
        "path": [(-20.3,118.6), (-15,120), (-8,123), (0,120), (15,118), (25,120), (36,120.4)],
        "zones": [(-24,114,-17,122), (-15,115,5,130), (5,110,38,125)],
    },
    "C4  Richards Bay → Rotterdam":  {
        "segment": "Capesize", "cargo": "Coal", "distance_nm": 5400,
        "color": "#bc8cff",
        "description": "Coal South Africa → ARA range",
        "path": [(-29,31.5), (-33,18), (-20,5), (-5,-5), (20,-18), (46,-5), (51.9,4.1)],
        "zones": [(-31,30,-27,34), (-36,14,-28,25), (-10,-20,20,5), (40,-15,55,8)],
    },
    "C10 Saldanha Bay → Qingdao":   {
        "segment": "Capesize", "cargo": "Iron Ore", "distance_nm": 10900,
        "color": "#db6d28",
        "description": "Iron ore South Africa → China",
        "path": [(-33,18.5), (-34,20), (-30,40), (-20,55), (-5,65), (8,78), (15,100), (25,115), (36,120.4)],
        "zones": [(-35,16,-30,22), (-35,35,-10,80), (5,75,36,125)],
    },
    "P3A Continent → Japan/SK":     {
        "segment": "Panamax", "cargo": "Grain/Coal", "distance_nm": 12100,
        "color": "#56d364",
        "description": "Fronthaul NW Europe → Far East",
        "path": [(52,4), (38,-5), (32,32), (15,50), (5,72), (10,80), (25,100), (32,130), (35,135)],
        "zones": [(48,-5,58,15), (28,30,34,36), (5,60,20,80), (20,95,40,142)],
    },
    "P6  ECSA Round Voyage":        {
        "segment": "Panamax", "cargo": "Grain/Soy", "distance_nm": 10500,
        "color": "#79c0ff",
        "description": "East Coast South America round voyage",
        "path": [(-35,-55), (-28,-50), (-22,-43), (-5,-38), (5,-35), (25,-75), (40,-70)],
        "zones": [(-38,-62,-18,-35), (-18,-45,10,-28)],
    },
    "P2A Transpacific RV":           {
        "segment": "Panamax", "cargo": "Grain/Coal", "distance_nm": 10200,
        "color": "#e3b341",
        "description": "US Gulf → Japan round voyage",
        "path": [(29,-89), (25,-85), (10,-80), (0,-80), (-5,-80), (-30,-85), (-35,-80), (-35,170), (-30,180), (25,140), (35,135)],
        "zones": [(26,-93,30,-87), (5,-85,15,-75), (-38,-90,-25,-70), (-30,-90,-20,180), (20,130,42,148)],
    },
    "P1A Transatlantic RV":          {
        "segment": "Panamax", "cargo": "Coal/Grain", "distance_nm": 9600,
        "color": "#ff7b72",
        "description": "US East Coast / Gulf → Continent round voyage",
        "path": [(29,-89), (30,-85), (35,-74), (40,-70), (45,-55), (50,-30), (52,-5), (52,4)],
        "zones": [(26,-93,30,-87), (25,-80,40,-65), (40,-65,55,-20), (48,-15,56,8)],
    },
}

# Approximate region assignment based on lat/lon
def _assign_region(lat: float, lon: float) -> str:
    if 28.0 <= lat <= 48.0 and -6.0 <= lon <= 42.0:
        return "Mediterranean / Black Sea"
    if 8.0 <= lat <= 30.0 and 32.0 <= lon <= 45.0:
        return "Red Sea"
    if -10.0 <= lat <= 25.0 and 95.0 <= lon <= 135.0:
        return "SE Asia / South China Sea"
    if 48.0 <= lat <= 65.0 and -5.0 <= lon <= 30.0:
        return "NW Europe"
    if -45.0 <= lat <= 30.0 and 20.0 <= lon <= 95.0:
        return "Indian Ocean"
    if -60.0 <= lat <= 70.0 and -80.0 <= lon <= -25.0:
        return "Americas (Atlantic)"
    if -60.0 <= lat <= 10.0 and -90.0 <= lon <= -70.0:
        return "Americas (Pacific)"
    if 25.0 <= lat <= 65.0 and 128.0 <= lon <= 180.0:
        return "N.Pacific / Japan"
    if -45.0 <= lat <= 5.0 and 100.0 <= lon <= 150.0:
        return "Australasia"
    if -40.0 <= lat <= -25.0 and 14.0 <= lon <= 22.0:
        return "Cape of Good Hope"
    return "Atlantic / Other"


def _in_box(lat: float, lon: float, box: tuple) -> bool:
    """True if (lat, lon) falls inside (lat_min, lon_min, lat_max, lon_max)."""
    lat_min, lon_min, lat_max, lon_max = box
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def _assign_port_zone(lat: float, lon: float) -> str | None:
    for zone_name, box in PORT_ZONES.items():
        if _in_box(lat, lon, box):
            return zone_name
    return None


def _assign_route(lat: float, lon: float) -> list[str]:
    """Return list of Baltic route names whose corridor boxes contain this position."""
    assigned = []
    for route_name, meta in BALTIC_ROUTES.items():
        for box in meta["zones"]:
            if _in_box(lat, lon, box):
                assigned.append(route_name)
                break
    return assigned


# ─── Async WebSocket collector ─────────────────────────────────────────────────

async def _collect_ais_messages(
    api_key: str,
    ws_url: str,
    collect_seconds: int,
    bulk_types: list[int],
) -> list[dict]:
    try:
        import websockets
    except ImportError:
        logger.warning("websockets package not installed; AIS disabled")
        return []

    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FiltersShipType": bulk_types,
        "FilterMessageTypes": ["PositionReport"],
    }

    vessels: dict[int, dict] = {}
    deadline = time.monotonic() + collect_seconds

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=12,
            close_timeout=5,
            max_size=2 ** 20,
        ) as ws:
            await ws.send(json.dumps(subscription))
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("MessageType") != "PositionReport":
                    continue

                meta = msg.get("MetaData", {})
                pos  = msg.get("Message", {}).get("PositionReport", {})

                mmsi = meta.get("MMSI") or pos.get("UserID")
                if not mmsi:
                    continue

                lat = meta.get("latitude") or pos.get("Latitude")
                lon = meta.get("longitude") or pos.get("Longitude")
                if lat is None or lon is None:
                    continue
                if abs(lat) > 90 or abs(lon) > 180 or lat == 91.0 or lon == 181.0:
                    continue

                sog        = float(pos.get("Sog", 0.0) or 0.0)
                cog        = float(pos.get("Cog", 0.0) or 0.0)
                nav_status = int(pos.get("NavigationalStatus", 15) or 15)
                ship_type  = int(meta.get("ShipType", 70) or 70)

                # Filter clearly invalid GPS/sensor artefacts
                if sog > 35.0:
                    sog = 0.0

                # Infer segment from ship type sub-code (canonical names used throughout UI)
                if ship_type in (71, 72, 73):
                    segment = "Capesize"
                elif ship_type == 74:
                    segment = "Panamax"
                elif ship_type == 75:
                    segment = "Supramax"
                elif ship_type == 76:
                    segment = "Handysize"
                else:
                    segment = "Bulk Carrier"

                # Real-world AIS: most vessels broadcast nav_status=15 (default/not defined)
                # even when actively steaming. Use SOG as the primary underway indicator;
                # only trust explicit anchor (1), moored (5), aground (6) flags.
                _stopped = nav_status in (1, 5, 6)
                _moving  = sog > 1.5

                vessels[mmsi] = {
                    "mmsi":            mmsi,
                    "name":            (meta.get("ShipName") or "").strip() or f"MMSI-{mmsi}",
                    "lat":             round(float(lat), 4),
                    "lon":             round(float(lon), 4),
                    "sog":             round(sog, 1),
                    "cog":             round(cog, 1),
                    "nav_status_code": nav_status,
                    "nav_status":      _NAV_STATUS.get(nav_status, "Unknown"),
                    "underway":        _moving and not _stopped,
                    "at_anchor":       nav_status == 1 or (not _moving and nav_status == 15),
                    "moored":          nav_status == 5,
                    "destination":     (meta.get("Destination") or "").strip(),
                    "ship_type":       ship_type,
                    "segment":         segment,
                    "timestamp":       meta.get("time_utc", ""),
                    "fetched_at":      datetime.now(timezone.utc).isoformat(),
                }
    except Exception as exc:
        logger.warning("AIS WebSocket error: %s", exc)

    return list(vessels.values())


def _run_in_thread(
    api_key: str,
    ws_url: str,
    collect_seconds: int,
    bulk_types: list[int],
) -> list[dict]:
    result: list[dict] = []

    def _target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result.extend(loop.run_until_complete(
                _collect_ais_messages(api_key, ws_url, collect_seconds, bulk_types)
            ))
        finally:
            loop.close()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=collect_seconds + 18)
    return result


# ─── Main public function ──────────────────────────────────────────────────────

def fetch_live_vessels(
    api_key: str,
    ws_url: str,
    cache_dir: Path,
    collect_seconds: int = 8,
    cache_ttl_minutes: int = 5,
    bulk_types: list[int] | None = None,
) -> pd.DataFrame:
    """
    Return enriched DataFrame of live dry bulk vessel positions.

    Columns: mmsi, name, lat, lon, sog, cog, nav_status, underway, at_anchor,
             moored, destination, ship_type, segment, timestamp, region,
             port_zone, routes (list), cached
    """
    if bulk_types is None:
        bulk_types = list(range(70, 80))

    cache_file = cache_dir / "freight" / "ais_vessels.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # Try disk cache
    if cache_file.exists():
        age_min = (time.time() - cache_file.stat().st_mtime) / 60
        if age_min < cache_ttl_minutes:
            try:
                records = json.loads(cache_file.read_text(encoding="utf-8"))
                if records:
                    df = pd.DataFrame(records)
                    df["cached"] = True
                    # Recompute underway using correct SOG-primary logic in case
                    # the cache was written by an older version using nav_status==0.
                    if "sog" in df.columns:
                        if "nav_status_code" in df.columns:
                            _stopped = df["nav_status_code"].isin([1, 5, 6])
                        elif "nav_status" in df.columns:
                            _stopped = df["nav_status"].isin(["At anchor", "Moored", "Aground"])
                        else:
                            _stopped = pd.Series(False, index=df.index)
                        df["underway"] = (df["sog"] > 1.5) & ~_stopped
                        df["at_anchor"] = (
                            (df.get("nav_status_code", pd.Series(15, index=df.index)) == 1) |
                            ((df["sog"] <= 1.5) & ~_stopped)
                        )
                    return df
            except Exception:
                pass

    if not api_key:
        return pd.DataFrame()

    records = _run_in_thread(api_key, ws_url, collect_seconds, bulk_types)

    for r in records:
        r["region"]    = _assign_region(r["lat"], r["lon"])
        r["port_zone"] = _assign_port_zone(r["lat"], r["lon"])
        r["routes"]    = _assign_route(r["lat"], r["lon"])

    if records:
        tmp = cache_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(records), encoding="utf-8")
            os.replace(tmp, cache_file)
        except Exception as exc:
            logger.warning("AIS cache write error: %s", exc)

    df = pd.DataFrame(records) if records else pd.DataFrame()
    if not df.empty:
        df["cached"] = False
    return df


# ─── Analytics on vessel DataFrame ────────────────────────────────────────────

def get_route_traffic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Count vessels per Baltic Exchange route corridor.
    Returns DataFrame with route metadata and live vessel counts.
    """
    if df.empty or "routes" not in df.columns:
        return pd.DataFrame()

    rows = []
    for route_name, meta in BALTIC_ROUTES.items():
        vessels_on_route = df[df["routes"].apply(lambda rs: route_name in rs if isinstance(rs, list) else False)]
        n_total    = len(vessels_on_route)
        n_underway = int(vessels_on_route["underway"].sum()) if "underway" in vessels_on_route.columns else 0
        n_anchor   = int(vessels_on_route["at_anchor"].sum()) if "at_anchor" in vessels_on_route.columns else 0
        avg_sog    = round(vessels_on_route.loc[vessels_on_route["underway"], "sog"].mean(), 1) \
                     if n_underway > 0 and "sog" in vessels_on_route.columns else 0.0
        rows.append({
            "Route":       route_name,
            "Segment":     meta["segment"],
            "Cargo":       meta["cargo"],
            "Dist (nm)":   meta["distance_nm"],
            "Total":       n_total,
            "Underway":    n_underway,
            "At Anchor":   n_anchor,
            "Avg SOG (kn)": avg_sog,
            "Description": meta["description"],
            "color":       meta["color"],
        })
    return pd.DataFrame(rows)


def get_port_zone_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Count live vessels in each port zone bounding box."""
    if df.empty or "port_zone" not in df.columns:
        return pd.DataFrame()

    rows = []
    for zone in PORT_ZONES:
        zdf = df[df["port_zone"] == zone]
        n_total   = len(zdf)
        n_anchor  = int(zdf["at_anchor"].sum()) if "at_anchor" in zdf.columns else 0
        n_moored  = int(zdf["moored"].sum()) if "moored" in zdf.columns else 0
        n_underway = int(zdf["underway"].sum()) if "underway" in zdf.columns else 0

        # Estimate: anchor + moored = waiting / in port
        est_static = PORT_CONGESTION_ESTIMATES.get(
            next((k for k in PORT_CONGESTION_ESTIMATES if k.split("/")[0].strip().lower() in zone.lower()), ""),
            {}
        )
        _wait = est_static.get("avg_wait_days")
        rows.append({
            "Port Zone":        zone,
            "Live (AIS)":       n_total,
            "Anchor/Moored":    n_anchor + n_moored,
            "Transiting":       n_underway,
            "Est. Wait (days)": f"{_wait:.1f}" if _wait is not None else "—",
            "Est. Status":      est_static.get("status", "—"),
        })

    df_out = pd.DataFrame(rows)
    df_out = df_out[df_out["Live (AIS)"] > 0].sort_values("Live (AIS)", ascending=False)
    return df_out


def get_chokepoint_traffic(df: pd.DataFrame) -> dict:
    """Count vessels currently in/near each major chokepoint zone."""
    chokepoint_boxes = {
        "Suez Canal":           PORT_ZONES["Suez Canal"],
        "Panama Canal":         PORT_ZONES["Panama Canal"],
        "Bab el-Mandeb":        PORT_ZONES["Bab el-Mandeb"],
        "Strait of Hormuz":     PORT_ZONES["Strait of Hormuz"],
        "Lombok Strait":        PORT_ZONES["Lombok Strait"],
        "Singapore / Malacca":  PORT_ZONES["Singapore / Malacca"],
        "Cape of Good Hope":    PORT_ZONES["Cape of Good Hope"],
    }
    result = {}
    for name, box in chokepoint_boxes.items():
        if df.empty:
            result[name] = 0
        else:
            count = df.apply(lambda r: _in_box(r["lat"], r["lon"], box), axis=1).sum()
            result[name] = int(count)
    return result


# ─── Static helpers (unchanged) ───────────────────────────────────────────────

def get_port_congestion_data() -> pd.DataFrame:
    rows = []
    for port, data in PORT_CONGESTION_ESTIMATES.items():
        rows.append({
            "port":            port,
            "region":          data["region"],
            "vessels_waiting": data["vessels_waiting"],
            "avg_wait_days":   data["avg_wait_days"],
            "status":          data["status"],
            "data_source":     "ESTIMATED",
            "last_updated":    "2024-Q4 (manual estimate)",
        })
    return pd.DataFrame(rows)


def get_port_congestion_index() -> dict:
    df = get_port_congestion_data()
    if df.empty:
        return {"index": None, "label": "N/A"}
    weighted = (df["vessels_waiting"] * df["avg_wait_days"]).sum() / df["vessels_waiting"].sum()
    if weighted < 1.5:
        label, color = "Low Congestion", "#3fb950"
    elif weighted < 3.0:
        label, color = "Moderate Congestion", "#d29922"
    else:
        label, color = "High Congestion", "#f85149"
    return {
        "index":  round(float(weighted), 2),
        "label":  label,
        "color":  color,
        "source": "ESTIMATED — manual port data",
    }
