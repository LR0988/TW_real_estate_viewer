"""
Fast Parallel Geocoding Seeder for all Taiwan towns' top roads and projects.
Populates location_coordinates table so map queries have 100% cached coordinates.
"""
import concurrent.futures
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from geo.geocoder import init_geocoder_db, clean_term, normalize_tw_terms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def geocode_one(full_district: str, road: str):
    clean_dist = clean_term(full_district)
    clean_rd = clean_term(road)
    q_norm = normalize_tw_terms(f"{clean_dist}{clean_rd}")
    q_orig = f"{clean_dist}{clean_rd}"

    queries = [q_norm]
    if q_orig != q_norm:
        queries.append(q_orig)

    for q in queries:
        url = (
            f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
            f"?SingleLine={urllib.parse.quote(q)}&countryCode=TWN&outFields=*&f=json&maxLocations=1"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    top = candidates[0]
                    score = float(top.get("score", 0))
                    attr = top.get("attributes", {})
                    addr_type = attr.get("Addr_type", "")
                    match_addr = top.get("address", "") or attr.get("Match_addr", "")

                    if addr_type in ("Locality", "Admin", "Subregion", "District", "City", "Postal"):
                        target_check = clean_rd
                        target_check_alt = normalize_tw_terms(target_check)
                        if target_check and target_check not in match_addr and target_check_alt not in match_addr:
                            continue

                    if score >= 75.0:
                        loc = top["location"]
                        lat = round(float(loc["y"]), 6)
                        lng = round(float(loc["x"]), 6)
                        if 21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6:
                            return lat, lng, score
        except Exception:
            continue
    return None


def run_seed():
    init_geocoder_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1. Fetch existing cached keys
    cur.execute("SELECT query_key FROM location_coordinates")
    existing_keys = set(row[0] for row in cur.fetchall())
    logger.info(f"Existing cached locations: {len(existing_keys)}")

    # 2. Get top 6 roads for EVERY town in Taiwan from recent transactions (>= 2022)
    cur.execute("""
        WITH RankedRoads AS (
            SELECT full_district, road, COUNT(*) as cnt,
                   ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY COUNT(*) DESC) as rn
            FROM transactions
            WHERE trade_year >= 2022 AND road != '' AND road IS NOT NULL
              AND (road LIKE '%路%' OR road LIKE '%街%' OR road LIKE '%大道%' OR road LIKE '%巷%')
              AND target_type != '土地' AND is_special_trade = 0 AND unit_price_wan_ping > 0
            GROUP BY full_district, road
        )
        SELECT full_district, road, cnt
        FROM RankedRoads
        WHERE rn <= 6
        ORDER BY cnt DESC
    """)
    candidates = cur.fetchall()
    conn.close()

    to_fetch = []
    for dist, rd, cnt in candidates:
        c_dist = clean_term(dist)
        c_rd = clean_term(rd)
        key = f"{c_dist}_{c_rd}_{c_rd}"
        key_norm = f"{normalize_tw_terms(c_dist)}_{normalize_tw_terms(c_rd)}_{normalize_tw_terms(c_rd)}"
        if key not in existing_keys and key_norm not in existing_keys:
            to_fetch.append((c_dist, c_rd, key))

    logger.info(f"Total candidates: {len(candidates)}, Need geocoding: {len(to_fetch)}")

    success_items = []
    batch_size = 50

    def worker(item):
        d, r, k = item
        res = geocode_one(d, r)
        if res:
            lat, lng, score = res
            return (k, d, r, r, lat, lng, "arcgis", score)
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker, item): item for item in to_fetch}
        done_cnt = 0
        for fut in concurrent.futures.as_completed(futures):
            done_cnt += 1
            res = fut.result()
            if res:
                success_items.append(res)

            if len(success_items) >= batch_size:
                c2 = sqlite3.connect(DB_PATH)
                c2.executemany("""
                    INSERT OR REPLACE INTO location_coordinates
                    (query_key, full_district, name, road, lat, lng, source, match_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, success_items)
                c2.commit()
                c2.close()
                logger.info(f"Progress: {done_cnt}/{len(to_fetch)} processed, saved {len(success_items)} items...")
                success_items = []

    if success_items:
        c2 = sqlite3.connect(DB_PATH)
        c2.executemany("""
            INSERT OR REPLACE INTO location_coordinates
            (query_key, full_district, name, road, lat, lng, source, match_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, success_items)
        c2.commit()
        c2.close()

    logger.info(f"Seeding completed! Processed {len(to_fetch)} items.")


if __name__ == "__main__":
    run_seed()
