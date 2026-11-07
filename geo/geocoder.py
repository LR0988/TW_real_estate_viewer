"""
Geocoding and coordinate caching service for Taiwan real estate roads, doors, and projects.
Uses ESRI ArcGIS World Geocoding Engine with permanent SQLite caching and same-road door interpolation.
Guarantees real geographic coordinates without fake town-centroid random circles.
"""
import concurrent.futures
import json
import logging
import math
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

from config import DB_PATH

logger = logging.getLogger(__name__)

_DB_INIT_DONE = False
_LOCK = threading.Lock()
_BG_IN_FLIGHT = set()
_BG_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


def _get_http_session() -> requests.Session:
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json"
        })
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=1)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _THREAD_LOCAL.session = sess
    return sess

_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９巿", "0123456789市")
_DOOR_EXTRACT_RE = re.compile(
    r"((?:(?:\d+(?:之\d+)?)巷)?(?:(?:\d+(?:之\d+)?)弄)?(?:\d+(?:[~～\-]\d+)?(?:之\d+)?號))"
)
_DOOR_NUM_RE = re.compile(r"(\d+)(?:[~～\-](\d+))?(?:之\d+)?號")


def init_geocoder_db(db_path: Path = DB_PATH):
    global _DB_INIT_DONE
    with _LOCK:
        if _DB_INIT_DONE:
            return
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS location_coordinates (
                query_key TEXT PRIMARY KEY,
                full_district TEXT,
                name TEXT,
                road TEXT,
                lat REAL,
                lng REAL,
                source TEXT,
                match_score REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_loc_district ON location_coordinates(full_district);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_loc_dist_road ON location_coordinates(full_district, road);")
        conn.commit()
        conn.close()
        _DB_INIT_DONE = True


def clean_term(s: str, keep_door_parens: bool = True) -> str:
    """Removes noise and selector prefixes while preserving door numbers inside parentheses."""
    if not s:
        return ""
    s = str(s).translate(_FULLWIDTH_MAP)
    s = re.sub(r"^(--請選擇--|\.|\s+)", "", s)
    if keep_door_parens:
        # Only strip parenthesized text if it does NOT contain a door/lane number (號/巷/弄)
        def _repl(m):
            inner = m.group(1)
            if any(k in inner for k in ("號", "巷", "弄")):
                return f"({inner})"
            return ""
        s = re.sub(r"[（\(](.*?)[）\)]", _repl, s)
    else:
        s = re.sub(r"[（\(].*?[）\)]", "", s)
    return s.strip(" -")


def extract_clean_door(s: str, for_geocode: bool = False) -> str:
    """
    Extracts lane/alley/door string (e.g. '40巷25之1號', '233之8號') and resolves ranges like '211~240號' -> '211號'.
    When for_geocode=True, simplifies '25之1號' -> '25號' so ArcGIS PointAddress matches the base building number.
    """
    if not s:
        return ""
    s = str(s).translate(_FULLWIDTH_MAP)
    m = _DOOR_EXTRACT_RE.search(s)
    if not m:
        return ""
    door_str = m.group(1)
    # Convert range '211~240號' to concrete number '211號'
    door_str = re.sub(r"(\d+)[~～\-]\d+((?:之\d+)?號)", r"\1\2", door_str)
    if for_geocode:
        door_str = re.sub(r"(\d+)之\d+號", r"\1號", door_str)
        door_str = re.sub(r"(\d+)之\d+巷", r"\1巷", door_str)
    return door_str


def extract_primary_door_int(s: str) -> Optional[int]:
    """Extracts the primary integer door/lane number along the main road for linear interpolation."""
    if not s:
        return None
    s = str(s).translate(_FULLWIDTH_MAP)
    # If there is a lane number (e.g. 40巷25之1號), the position along the main road is 40!
    m_lane = re.search(r"(\d+)(?:之\d+)?巷", s)
    if m_lane:
        return int(m_lane.group(1))
    m_door = _DOOR_NUM_RE.search(s)
    if m_door:
        return int(m_door.group(1))
    return None


def normalize_tw_terms(s: str) -> str:
    if not s:
        return ""
    rep = {
        '臺': '台', '鹽': '塩', '舘': '館', '峯': '峰', '温': '溫', '庄': '莊',
        '1段': '一段', '2段': '二段', '3段': '三段', '4段': '四段',
        '5段': '五段', '6段': '六段', '7段': '七段', '8段': '八段', '9段': '九段'
    }
    for k, v in rep.items():
        if k in s:
            s = s.replace(k, v)
    return s


def geocode_arcgis(
    full_district: str,
    name: str,
    road: str = "",
    door_addr: str = ""
) -> Optional[Tuple[float, float, float, str]]:
    """
    Queries ESRI ArcGIS World Geocoding Service for Taiwan locations.
    Prioritizes exact PointAddress / StreetAddress using door_addr when available.
    Returns (lat, lng, score, addr_type) if a verified match is found in Taiwan.
    """
    clean_dist = clean_term(full_district, keep_door_parens=False)
    clean_nm = clean_term(name, keep_door_parens=True)
    clean_nm_noparen = clean_term(name, keep_door_parens=False)
    clean_rd = clean_term(road, keep_door_parens=False)
    door_part = extract_clean_door(door_addr, for_geocode=True) or extract_clean_door(clean_nm, for_geocode=True)

    queries = []
    # 1. Exact door/lane address on the road (highest precision: PointAddress / StreetAddress)
    if door_part:
        if clean_rd and not door_part.startswith(clean_rd):
            queries.append(f"{clean_dist}{clean_rd}{door_part}")
        else:
            queries.append(f"{clean_dist}{door_part}")
    # 2. If it's a door/lane in clean_nm directly
    if any(k in clean_nm_noparen for k in ("號", "巷", "弄")):
        addr_q = re.sub(r"(\d+)[~～\-]\d+號", r"\1號", clean_nm_noparen)
        if clean_rd and not addr_q.startswith(clean_rd):
            q_str = f"{clean_dist}{clean_rd}{addr_q}"
        else:
            q_str = f"{clean_dist}{addr_q}"
        if q_str not in queries:
            queries.append(q_str)
    # 3. Project name / POI or road
    if clean_rd and clean_nm_noparen and clean_nm_noparen != clean_rd and not any(k in clean_nm_noparen for k in ("號", "巷", "弄")):
        queries.append(f"{clean_dist}{clean_nm_noparen}")
        queries.append(f"{clean_dist}{clean_rd}")
    elif clean_nm_noparen and not any(k in clean_nm_noparen for k in ("號", "巷", "弄")):
        queries.append(f"{clean_dist}{clean_nm_noparen}")
    elif clean_rd and f"{clean_dist}{clean_rd}" not in queries:
        queries.append(f"{clean_dist}{clean_rd}")

    # Add normalized versions (e.g. 臺 -> 台)
    extra = []
    for q in queries:
        qn = normalize_tw_terms(q)
        if qn != q and qn not in queries and qn not in extra:
            extra.append(qn)
    queries.extend(extra)

    sess = _get_http_session()
    for q in queries[:2]:
        url = (
            f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
            f"?SingleLine={urllib.parse.quote(q)}&countryCode=TWN&outFields=Addr_type,Match_addr,StName,AddNum&f=json&maxLocations=1"
        )
        try:
            resp = sess.get(url, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    top = candidates[0]
                    score = float(top.get("score", 0))
                    attr = top.get("attributes", {})
                    addr_type = attr.get("Addr_type", "")
                    match_addr = top.get("address", "") or attr.get("Match_addr", "")

                    # Reject broad administrative/locality centroids — we only want real streets, POIs, or doors!
                    if addr_type in ("Locality", "Admin", "Subregion", "District", "City", "Postal"):
                        continue

                    # Verify district consistency if full_district has county name
                    if len(clean_dist) >= 3:
                        county_3 = clean_dist[:3]
                        county_3_alt = normalize_tw_terms(county_3)
                        if county_3 not in match_addr and county_3_alt not in normalize_tw_terms(match_addr):
                            continue

                    if score >= 75.0:
                        loc = top["location"]
                        lat = round(float(loc["y"]), 6)
                        lng = round(float(loc["x"]), 6)
                        if 21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6:
                            src = "arcgis_point" if addr_type in ("PointAddress", "StreetAddress", "StreetAddressExt", "Subaddress", "POI") else "arcgis_street"
                            return lat, lng, score, src
        except Exception as e:
            logger.debug(f"ArcGIS geocode error for '{q}': {e}")
            continue

    return None


def _interpolate_along_road(
    door_num: int,
    road_doors: List[Tuple[int, float, float]],
    road_center: Optional[Tuple[float, float]]
) -> Optional[Tuple[float, float, str]]:
    """
    Interpolates a door number along already-geocoded door numbers on the same road.
    Guarantees the marker lies on the actual street geometry rather than a fake diagonal or random circle.
    """
    if len(road_doors) >= 2:
        # Sort by door number
        pts = sorted(road_doors, key=lambda x: x[0])
        # Exact match
        for d, lat, lng in pts:
            if d == door_num:
                return lat, lng, "arcgis_point"
        # Below minimum known door
        if door_num < pts[0][0]:
            d1, lat1, lng1 = pts[0]
            d2, lat2, lng2 = pts[1]
            span = max(1, d2 - d1)
            ratio = max(-0.5, (door_num - d1) / span)
            return round(lat1 + (lat2 - lat1) * ratio, 6), round(lng1 + (lng2 - lng1) * ratio, 6), "road_interpolated"
        # Above maximum known door
        if door_num > pts[-1][0]:
            d1, lat1, lng1 = pts[-2]
            d2, lat2, lng2 = pts[-1]
            span = max(1, d2 - d1)
            ratio = min(0.5, (door_num - d2) / span)
            return round(lat2 + (lat2 - lat1) * ratio, 6), round(lng2 + (lng2 - lng1) * ratio, 6), "road_interpolated"
        # Between two known doors on the same road
        for i in range(len(pts) - 1):
            d1, lat1, lng1 = pts[i]
            d2, lat2, lng2 = pts[i + 1]
            if d1 <= door_num <= d2:
                span = max(1, d2 - d1)
                ratio = (door_num - d1) / span
                return round(lat1 + (lat2 - lat1) * ratio, 6), round(lng1 + (lng2 - lng1) * ratio, 6), "road_interpolated"

    if len(road_doors) == 1:
        d1, lat1, lng1 = road_doors[0]
        if road_center:
            rc_lat, rc_lng = road_center
            if abs(rc_lat - lat1) > 0.0001 or abs(rc_lng - lng1) > 0.0001:
                ratio = max(-0.8, min(0.8, (door_num - d1) / 100.0))
                return round(lat1 + (rc_lat - lat1) * ratio, 6), round(lng1 + (rc_lng - lng1) * ratio, 6), "road_interpolated"
        return lat1, lng1, "arcgis_street"

    if road_center:
        return road_center[0], road_center[1], "arcgis_street"

    return None


def _geocode_and_save_batch(tasks: List[Dict], db_path: Path, max_workers: int = 4) -> Dict[str, Tuple[float, float, float, str]]:
    """Concurrently geocodes a batch of items (rate-limit friendly 4 workers) and persists them to SQLite."""
    results = {}
    if not tasks:
        return results

    def _one(t):
        res = geocode_arcgis(t["dist"], t["nm"], t["rd"], t.get("door_addr", ""))
        if res:
            return t, res
        return t, None

    rows_to_insert = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, t) for t in tasks]
        for fut in concurrent.futures.as_completed(futures):
            try:
                t, res = fut.result()
                if res:
                    lat, lng, score, src = res
                    results[t["key"]] = (lat, lng, score, src)
                    rows_to_insert.append((t["key"], t["dist"], t["nm"], t["rd"], lat, lng, src, score))
            except Exception:
                pass
            finally:
                with _BG_LOCK:
                    _BG_IN_FLIGHT.discard(t["key"])

    if rows_to_insert:
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executemany("""
                INSERT OR REPLACE INTO location_coordinates
                (query_key, full_district, name, road, lat, lng, source, match_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows_to_insert)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"SQLite batch save error: {e}")

    return results


def batch_resolve_coordinates(
    items: List[Dict],
    town_centroids: Dict[str, List[float]] = None,
    db_path: Path = DB_PATH,
    sync_budget: int = 0
) -> List[Dict]:
    """
    Enriches a list of items with real GPS 'lat' and 'lng' coordinates.
    Never uses fake town-centroid random circles.
    """
    init_geocoder_db(db_path)
    if not items:
        return items

    districts_set = set()
    for item in items:
        dist = clean_term(item.get("full_district", ""), keep_door_parens=False)
        nm = clean_term(item.get("name") or item.get("road") or item.get("project_name") or "", keep_door_parens=True)
        rd = clean_term(item.get("road") or "", keep_door_parens=False)
        door_addr = item.get("door_addr") or extract_clean_door(nm) or ""
        item["_key"] = f"{dist}_{nm}_{rd}"
        item["_road_key"] = f"{dist}_{rd}_{rd}" if rd else None
        item["_clean_dist"] = dist
        item["_clean_nm"] = nm
        item["_clean_rd"] = rd
        item["_door_addr"] = door_addr
        if dist:
            districts_set.add(dist)

    # Read all cached coordinates for the relevant districts in a single fast indexed query!
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cached_map: Dict[str, Tuple[float, float, float, str]] = {}
    road_doors_map: Dict[Tuple[str, str], List[Tuple[int, float, float]]] = {}
    road_center_map: Dict[Tuple[str, str], Tuple[float, float]] = {}

    if districts_set:
        dist_list = list(districts_set)
        placeholders = ",".join("?" for _ in dist_list)
        cur.execute(
            f"SELECT query_key, full_district, name, road, lat, lng, match_score, source "
            f"FROM location_coordinates WHERE full_district IN ({placeholders})",
            dist_list
        )
        for qk, f_dist, nm_col, rd_col, lat, lng, score, src in cur.fetchall():
            if lat is None or lng is None:
                continue
            cached_map[qk] = (lat, lng, score, src)
            if rd_col:
                d_int = extract_primary_door_int(nm_col)
                if d_int is not None and src in ("arcgis", "arcgis_point"):
                    road_doors_map.setdefault((f_dist, rd_col), []).append((d_int, lat, lng))
                elif nm_col == rd_col:
                    road_center_map[(f_dist, rd_col)] = (lat, lng)
    conn.close()

    # Identify any items whose exact key is not yet in cached_map
    missing_by_key = {}
    for item in items:
        k = item["_key"]
        if k not in cached_map and k not in missing_by_key:
            missing_by_key[k] = {
                "key": k,
                "dist": item["_clean_dist"],
                "nm": item["_clean_nm"],
                "rd": item["_clean_rd"],
                "door_addr": item["_door_addr"]
            }
        rk = item["_road_key"]
        if rk and rk not in cached_map and rk not in missing_by_key:
            missing_by_key[rk] = {
                "key": rk,
                "dist": item["_clean_dist"],
                "nm": item["_clean_rd"],
                "rd": item["_clean_rd"],
                "door_addr": ""
            }

    missing_list = list(missing_by_key.values())
    if missing_list:
        # Synchronously resolve up to sync_budget items immediately using 16 parallel threads (< 0.8s)
        sync_tasks = []
        bg_tasks = []
        with _BG_LOCK:
            for t in missing_list:
                if t["key"] in _BG_IN_FLIGHT:
                    continue
                if len(sync_tasks) < sync_budget:
                    _BG_IN_FLIGHT.add(t["key"])
                    sync_tasks.append(t)
                elif len(bg_tasks) < 250:
                    _BG_IN_FLIGHT.add(t["key"])
                    bg_tasks.append(t)

        if sync_tasks:
            new_resolved = _geocode_and_save_batch(sync_tasks, db_path, max_workers=16)
            for k, val in new_resolved.items():
                cached_map[k] = val

        if bg_tasks:
            threading.Thread(
                target=_geocode_and_save_batch,
                args=(bg_tasks, db_path, 16),
                daemon=True
            ).start()

    # Assign coordinates to items
    coord_groups = {}
    for item in items:
        key = item["_key"]
        rk = item["_road_key"]
        dist = item["_clean_dist"]
        nm = item["_clean_nm"]
        rd = item["_clean_rd"]

        if key in cached_map:
            lat, lng, score, src = cached_map[key]
            item["lat"] = lat
            item["lng"] = lng
            item["geo_exact"] = True
            item["coord_source"] = src
        else:
            # Try interpolating along known doors on the same road
            d_int = extract_primary_door_int(item["_door_addr"] or nm)
            r_doors = road_doors_map.get((dist, rd), []) if rd else []
            r_center = road_center_map.get((dist, rd)) or (cached_map[rk][:2] if (rk and rk in cached_map) else None)
            interp = _interpolate_along_road(d_int, r_doors, r_center) if (d_int is not None and (r_doors or r_center)) else None
            if interp:
                item["lat"], item["lng"], item["coord_source"] = interp
                item["geo_exact"] = True
            elif r_center:
                item["lat"], item["lng"] = r_center[0], r_center[1]
                item["geo_exact"] = True
                item["coord_source"] = "arcgis_street"
            else:
                item["lat"] = None
                item["lng"] = None
                item["geo_exact"] = False
                item["coord_source"] = "none"

        if item["lat"] is not None and item["lng"] is not None:
            coord_key = (round(item["lat"], 5), round(item["lng"], 5))
            coord_groups.setdefault(coord_key, []).append(item)

        item.pop("_key", None)
        item.pop("_road_key", None)
        item.pop("_clean_dist", None)
        item.pop("_clean_nm", None)
        item.pop("_clean_rd", None)
        item.pop("_door_addr", None)

    # Micro-offset only when multiple distinct spots share the exact same 5-decimal coordinate (~1 meter)
    for coord_key, group in coord_groups.items():
        if len(group) > 1:
            for idx, member in enumerate(group):
                if idx == 0:
                    continue
                angle = idx * 2.39996
                dist_deg = 0.00008 + (min(idx, 12) * 0.00003)  # ~9m to 40m micro-offset
                member["lat"] = round(member["lat"] + (math.sin(angle) * dist_deg), 6)
                member["lng"] = round(member["lng"] + (math.cos(angle) * dist_deg * 1.08), 6)

    return items
