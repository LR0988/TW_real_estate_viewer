"""
High-precision geocoding and coordinate caching service for Taiwan real estate roads, doors, and projects.
Primary Engine: Google Maps Verification (maps.google.com/maps?q=...&hl=zh-TW&output=embed)
Secondary Engine: ESRI ArcGIS World Geocoding Service (PointAddress / StreetAddress)
Guarantees real house-number and building coordinates without fake road-center clusters or random circles.
"""
import concurrent.futures
import logging
import math
import re
import sqlite3
import threading
import urllib.parse
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

_VALID_RESIDENTIAL_GCIDS = {
    "condominium_complex", "apartment_complex", "housing_complex",
    "housing_development", "housing_society", "apartment_building",
    "gated_community", "subpremise", "premise", "townhouse_complex",
    "housing_cooperative", "residential_building"
}

_GMAP_EXACT_RE = re.compile(
    r'\["0x[0-9a-f]+:0x[0-9a-f]+","([^"]+)",\[(2[1-6]\.\d+),(12[0-2]\.\d+)\]'
)
_GMAP_CAT_RE = re.compile(
    r'\[(2[1-6]\d{7}),(12[0-2]\d{7})\][^\[\]]*?"gcid:([a-z_]+)"'
)

_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９巿", "0123456789市")
_DOOR_EXTRACT_RE = re.compile(
    r"((?:(?:\d+(?:之\d+)?)巷)?(?:(?:\d+(?:之\d+)?)弄)?(?:\d+(?:[~～\-]\d+)?(?:之\d+)?號)|(?:\d+(?:之\d+)?)巷(?:\d+(?:之\d+)?)?弄?)"
)
_DOOR_NUM_RE = re.compile(r"(\d+)(?:[~～\-](\d+))?(?:之\d+)?號")


def _get_http_session() -> requests.Session:
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-TW,zh;q=0.9"
        })
        adapter = requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=24, max_retries=1)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _THREAD_LOCAL.session = sess
    return sess


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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_loc_lat_lng ON location_coordinates(lat, lng, full_district, name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_loc_dist_name ON location_coordinates(full_district, name);")
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
    Extracts lane/alley/door string (e.g. '40巷25之1號', '476巷80號', '476巷') and resolves ranges.
    When for_geocode=True (for ArcGIS fallback), simplifies '25之1號' -> '25號'.
    """
    if not s:
        return ""
    s = str(s).translate(_FULLWIDTH_MAP)
    m = _DOOR_EXTRACT_RE.search(s)
    if not m:
        return ""
    door_str = m.group(1)
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


def geocode_google_maps(
    full_district: str,
    name: str,
    road: str = "",
    door_addr: str = "",
    road_center: Optional[Tuple[float, float]] = None
) -> Optional[Tuple[float, float, float, str]]:
    """
    Verifies and resolves exact coordinates from Google Maps (maps.google.com/maps?q=...&hl=zh-TW&output=embed).
    Validates county/district consistency, rejects sales centers when a site address exists,
    and ensures door queries match actual house/lane numbers rather than bare road centers.
    """
    clean_dist = normalize_tw_terms(clean_term(full_district, keep_door_parens=False))
    county = clean_dist[:3] if len(clean_dist) >= 3 else ""
    clean_nm = clean_term(name, keep_door_parens=True)
    clean_nm_noparen = normalize_tw_terms(clean_term(name, keep_door_parens=False)).replace("?", " ").replace("/", " ").strip()
    raw_rd = clean_term(road, keep_door_parens=False)
    # Ignore land-section-only names (e.g. 新竹市東光段) as street names
    is_land_sec = bool(raw_rd and raw_rd.endswith("段") and not any(k in raw_rd for k in ("路", "街", "道", "巷", "弄")))
    clean_rd = "" if is_land_sec else normalize_tw_terms(raw_rd)

    door_full = extract_clean_door(door_addr, for_geocode=False) or extract_clean_door(clean_nm, for_geocode=False)
    is_bare_road = bool(clean_rd and clean_nm_noparen == clean_rd and not door_full)
    is_door_spot = any(k in clean_nm_noparen for k in ("號", "巷", "弄")) and not is_bare_road

    queries: List[Tuple[str, str]] = []
    if is_bare_road:
        queries.append((f"{clean_dist}{clean_rd}", "road"))
    elif is_door_spot:
        target_door = door_full or clean_nm_noparen
        if clean_rd and not target_door.startswith(clean_rd):
            queries.append((f"{clean_dist}{clean_rd}{target_door}", "door"))
        else:
            queries.append((f"{clean_dist}{target_door}", "door"))
        # Also add simplified door if '之' is present
        door_simple = extract_clean_door(target_door, for_geocode=True)
        if door_simple and door_simple != target_door:
            q_s = f"{clean_dist}{clean_rd}{door_simple}" if (clean_rd and not door_simple.startswith(clean_rd)) else f"{clean_dist}{door_simple}"
            queries.append((q_s, "door"))
    else:
        # Project / Building spot
        if clean_rd:
            queries.append((f"{clean_dist} {clean_rd} {clean_nm_noparen}", "project"))
        else:
            queries.append((f"{clean_dist} {clean_nm_noparen}", "project"))
        if door_full:
            if clean_rd and not door_full.startswith(clean_rd):
                queries.append((f"{clean_dist}{clean_rd}{door_full}", "door"))
            else:
                queries.append((f"{clean_dist}{door_full}", "door"))

    sess = _get_http_session()
    for q, qtype in queries:
        url = f"https://maps.google.com/maps?q={urllib.parse.quote(q)}&hl=zh-TW&output=embed"
        try:
            resp = sess.get(url, timeout=3.5)
            if resp.status_code != 200:
                continue
            text = resp.text

            # 1. Check Spotlit Exact Entity Match: ["0x...:0x...","<matched_title>",[lat,lng]]
            m = _GMAP_EXACT_RE.search(text)
            if m:
                title = normalize_tw_terms(m.group(1))
                lat = round(float(m.group(2)), 6)
                lng = round(float(m.group(3)), 6)
                if not (21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6):
                    continue
                if county and county not in title:
                    continue
                # Reject sales centers if we have an actual site door/lane
                if qtype == "project" and door_full and any(k in title for k in ("接待中心", "銷售中心", "接待會館")):
                    continue
                if qtype == "door":
                    # Must match an actual door/lane, not a bare road center
                    if not any(k in title for k in ("號", "巷", "弄")):
                        continue
                    if clean_rd and clean_rd not in title:
                        continue
                    return lat, lng, 99.5, "gmap_point"
                elif qtype == "project":
                    # Must match a specific place/door/building, not just the bare district
                    if any(k in title for k in ("號", "巷", "弄", "社區", "基地", "大樓", "家園", "莊園")) or clean_nm_noparen[:2] in title:
                        return lat, lng, 99.0, "gmap_point"
                elif qtype == "road":
                    return lat, lng, 95.0, "gmap_street"

            # 2. For Project queries, check Categorical Residential Complex Match
            if qtype == "project":
                for mc in _GMAP_CAT_RE.finditer(text):
                    gcid = mc.group(3)
                    if gcid in _VALID_RESIDENTIAL_GCIDS:
                        lat = round(int(mc.group(1)) / 1e7, 6)
                        lng = round(int(mc.group(2)) / 1e7, 6)
                        if 21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6:
                            # If road_center is known, verify within ~4.5km (0.042 deg) so we don't pick another city
                            if road_center:
                                if abs(lat - road_center[0]) > 0.045 or abs(lng - road_center[1]) > 0.045:
                                    continue
                            return lat, lng, 96.0, "gmap_point"
        except Exception as e:
            logger.debug(f"Google Maps geocode error for '{q}': {e}")
            continue

    return None


def geocode_arcgis(
    full_district: str,
    name: str,
    road: str = "",
    door_addr: str = ""
) -> Optional[Tuple[float, float, float, str]]:
    """
    Secondary fallback using ESRI ArcGIS World Geocoding Service.
    For door addresses, strictly requires PointAddress / StreetAddress (never accepts bare StreetName center).
    """
    clean_dist = clean_term(full_district, keep_door_parens=False)
    clean_nm = clean_term(name, keep_door_parens=True)
    clean_nm_noparen = clean_term(name, keep_door_parens=False)
    clean_rd = clean_term(road, keep_door_parens=False)
    door_part = extract_clean_door(door_addr, for_geocode=True) or extract_clean_door(clean_nm, for_geocode=True)
    is_bare_road = bool(clean_rd and clean_nm_noparen == clean_rd and not door_part)
    is_door_spot = any(k in clean_nm_noparen for k in ("號", "巷", "弄")) and not is_bare_road

    queries = []
    if door_part:
        if clean_rd and not door_part.startswith(clean_rd):
            queries.append(f"{clean_dist}{clean_rd}{door_part}")
        else:
            queries.append(f"{clean_dist}{door_part}")
    if is_door_spot:
        addr_q = re.sub(r"(\d+)[~～\-]\d+號", r"\1號", clean_nm_noparen)
        q_str = f"{clean_dist}{clean_rd}{addr_q}" if (clean_rd and not addr_q.startswith(clean_rd)) else f"{clean_dist}{addr_q}"
        if q_str not in queries:
            queries.append(q_str)
    elif is_bare_road:
        queries.append(f"{clean_dist}{clean_rd}")
    elif clean_nm_noparen:
        queries.append(f"{clean_dist}{clean_nm_noparen}")

    sess = _get_http_session()
    for q in queries[:2]:
        url = (
            f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
            f"?SingleLine={urllib.parse.quote(q)}&countryCode=TWN&outFields=Addr_type,Match_addr,StName,AddNum&f=json&maxLocations=1"
        )
        try:
            resp = sess.get(url, timeout=3.0)
            if resp.status_code == 200:
                candidates = resp.json().get("candidates", [])
                if candidates:
                    top = candidates[0]
                    score = float(top.get("score", 0))
                    attr = top.get("attributes", {})
                    addr_type = attr.get("Addr_type", "")
                    match_addr = top.get("address", "") or attr.get("Match_addr", "")

                    if addr_type in ("Locality", "Admin", "Subregion", "District", "City", "Postal"):
                        continue
                    # CRITICAL: Never accept bare StreetName center when querying a specific door address!
                    if is_door_spot and addr_type not in ("PointAddress", "StreetAddress", "StreetAddressExt", "Subaddress"):
                        continue

                    if len(clean_dist) >= 3:
                        county_3 = clean_dist[:3]
                        county_3_alt = normalize_tw_terms(county_3)
                        if county_3 not in match_addr and county_3_alt not in normalize_tw_terms(match_addr):
                            continue

                    if score >= 78.0:
                        loc = top["location"]
                        lat = round(float(loc["y"]), 6)
                        lng = round(float(loc["x"]), 6)
                        if 21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6:
                            src = "arcgis_point" if addr_type in ("PointAddress", "StreetAddress", "StreetAddressExt", "Subaddress", "POI") else "arcgis_street"
                            return lat, lng, score, src
        except Exception:
            continue

    return None


def geocode_best(
    full_district: str,
    name: str,
    road: str = "",
    door_addr: str = "",
    road_center: Optional[Tuple[float, float]] = None
) -> Optional[Tuple[float, float, float, str]]:
    """
    Queries Google Maps first (highest accuracy for Taiwan house numbers, lanes, and communities),
    then falls back to ESRI ArcGIS PointAddress.
    """
    res = geocode_google_maps(full_district, name, road, door_addr, road_center=road_center)
    if res:
        return res
    return geocode_arcgis(full_district, name, road, door_addr)


def _interpolate_along_road(
    door_num: int,
    road_doors: List[Tuple[int, float, float]]
) -> Optional[Tuple[float, float, str]]:
    """
    Interpolates a door number ONLY when >= 2 verified door points already exist on the same road.
    Never dumps doors onto a single road-center point (which would create fake clusters).
    """
    if len(road_doors) >= 2:
        pts = sorted(road_doors, key=lambda x: x[0])
        for d, lat, lng in pts:
            if d == door_num:
                return lat, lng, "gmap_point"
        if door_num < pts[0][0]:
            d1, lat1, lng1 = pts[0]
            d2, lat2, lng2 = pts[1]
            span = max(1, d2 - d1)
            ratio = max(-0.35, (door_num - d1) / span)
            return round(lat1 + (lat2 - lat1) * ratio, 6), round(lng1 + (lng2 - lng1) * ratio, 6), "road_interpolated"
        if door_num > pts[-1][0]:
            d1, lat1, lng1 = pts[-2]
            d2, lat2, lng2 = pts[-1]
            span = max(1, d2 - d1)
            ratio = min(0.35, (door_num - d2) / span)
            return round(lat2 + (lat2 - lat1) * ratio, 6), round(lng2 + (lng2 - lng1) * ratio, 6), "road_interpolated"
        for i in range(len(pts) - 1):
            d1, lat1, lng1 = pts[i]
            d2, lat2, lng2 = pts[i + 1]
            if d1 <= door_num <= d2:
                span = max(1, d2 - d1)
                ratio = (door_num - d1) / span
                return round(lat1 + (lat2 - lat1) * ratio, 6), round(lng1 + (lng2 - lng1) * ratio, 6), "road_interpolated"
    return None


def _geocode_and_save_batch(tasks: List[Dict], db_path: Path, max_workers: int = 16) -> Dict[str, Tuple[float, float, float, str]]:
    """Concurrently geocodes a batch of items via Google Maps + ArcGIS and persists them to SQLite."""
    results = {}
    if not tasks:
        return results

    def _one(t):
        res = geocode_best(t["dist"], t["nm"], t["rd"], t.get("door_addr", ""), road_center=t.get("road_center"))
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
            conn = sqlite3.connect(db_path, timeout=15.0)
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
    Enriches a list of items with verified GPS 'lat' and 'lng' coordinates from Google Maps / ArcGIS.
    Never creates fake road-center spiral balls.
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
                if d_int is not None and src in ("gmap_point", "arcgis_point", "arcgis"):
                    road_doors_map.setdefault((f_dist, rd_col), []).append((d_int, lat, lng))
                elif nm_col == rd_col:
                    road_center_map[(f_dist, rd_col)] = (lat, lng)
    conn.close()

    missing_by_key = {}
    for item in items:
        k = item["_key"]
        if k not in cached_map and k not in missing_by_key:
            dist = item["_clean_dist"]
            rd = item["_clean_rd"]
            missing_by_key[k] = {
                "key": k,
                "dist": dist,
                "nm": item["_clean_nm"],
                "rd": rd,
                "door_addr": item["_door_addr"],
                "road_center": road_center_map.get((dist, rd))
            }

    missing_list = list(missing_by_key.values())
    if missing_list:
        sync_tasks = []
        bg_tasks = []
        with _BG_LOCK:
            for t in missing_list:
                if t["key"] in _BG_IN_FLIGHT:
                    continue
                if len(sync_tasks) < sync_budget:
                    _BG_IN_FLIGHT.add(t["key"])
                    sync_tasks.append(t)
                elif len(bg_tasks) < 500:
                    _BG_IN_FLIGHT.add(t["key"])
                    bg_tasks.append(t)

        if sync_tasks:
            new_resolved = _geocode_and_save_batch(sync_tasks, db_path, max_workers=16)
            for k, val in new_resolved.items():
                cached_map[k] = val
                # Also update road_doors_map for newly resolved points
                t_info = missing_by_key.get(k)
                if t_info and t_info["rd"]:
                    d_int = extract_primary_door_int(t_info["door_addr"] or t_info["nm"])
                    if d_int is not None and val[3] in ("gmap_point", "arcgis_point"):
                        road_doors_map.setdefault((t_info["dist"], t_info["rd"]), []).append((d_int, val[0], val[1]))

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
            d_int = extract_primary_door_int(item["_door_addr"] or nm)
            r_doors = road_doors_map.get((dist, rd), []) if rd else []
            interp = _interpolate_along_road(d_int, r_doors) if (d_int is not None and len(r_doors) >= 2) else None
            if interp:
                item["lat"], item["lng"], item["coord_source"] = interp
                item["geo_exact"] = True
            elif nm == rd and (dist, rd) in road_center_map:
                item["lat"], item["lng"] = road_center_map[(dist, rd)]
                item["geo_exact"] = True
                item["coord_source"] = "gmap_street"
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

    # Tiny side-by-side separation (~7m) when multiple doors in an alley share the same geocoded point
    for coord_key, group in coord_groups.items():
        if 1 < len(group) <= 12:
            n_others = len(group) - 1
            for idx, member in enumerate(group):
                if idx == 0:
                    continue
                angle = (2.0 * math.pi * (idx - 1)) / max(1, n_others)
                dist_deg = 0.000065 if n_others <= 5 else 0.000085
                member["lat"] = round(member["lat"] + (math.sin(angle) * dist_deg), 6)
                member["lng"] = round(member["lng"] + (math.cos(angle) * dist_deg * 1.08), 6)

    return items
