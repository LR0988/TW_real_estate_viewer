"""
Fast Parallel Geocoding Seeder for all Taiwan districts' top spots, doors, projects, and roads.
Uses 4 persistent HTTP Keep-Alive workers against ArcGIS to populate location_coordinates.
"""
import concurrent.futures
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from geo.geocoder import init_geocoder_db, clean_term, extract_clean_door, geocode_arcgis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_seed():
    init_geocoder_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT query_key FROM location_coordinates")
    existing_keys = {row[0] for row in cur.fetchall()}
    logger.info(f"Existing cached locations: {len(existing_keys)}")

    cur.execute("""
        WITH Ranked AS (
            SELECT full_district, spot_name, road, MAX(door_addr) as door_addr, SUM(tx_count) as total_tx,
                   ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY SUM(tx_count) DESC) as rn
            FROM agg_spot_quarter
            WHERE period >= '2023Q1'
            GROUP BY full_district, spot_name
        )
        SELECT full_district, spot_name, road, door_addr
        FROM Ranked
        WHERE rn <= 15
        ORDER BY total_tx DESC
    """)
    candidates = cur.fetchall()
    conn.close()

    road_tasks = []
    spot_tasks = []
    for f_dist, s_name, rd, d_addr in candidates:
        dist = clean_term(f_dist, keep_door_parens=False)
        nm = clean_term(s_name, keep_door_parens=True)
        clean_rd = clean_term(rd or "", keep_door_parens=False)
        door = d_addr or extract_clean_door(nm) or ""
        if clean_rd:
            rk = f"{dist}_{clean_rd}_{clean_rd}"
            if rk not in existing_keys:
                existing_keys.add(rk)
                road_tasks.append((rk, dist, clean_rd, clean_rd, ""))
        k = f"{dist}_{nm}_{clean_rd}"
        if k not in existing_keys:
            existing_keys.add(k)
            spot_tasks.append((k, dist, nm, clean_rd, door))

    all_tasks = road_tasks + spot_tasks
    logger.info(f"Seeding {len(road_tasks)} roads and {len(spot_tasks)} specific spots/doors...")

    def _worker(item):
        k, dist, nm, rd, door = item
        res = geocode_arcgis(dist, nm, rd, door)
        if res:
            lat, lng, score, src = res
            return (k, dist, nm, rd, lat, lng, src, score)
        return None

    t0 = time.time()
    saved = 0
    batch = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for idx, out in enumerate(pool.map(_worker, all_tasks), 1):
            if out:
                batch.append(out)
                saved += 1
            if len(batch) >= 25:
                c = sqlite3.connect(DB_PATH, timeout=15)
                c.execute("PRAGMA journal_mode = WAL;")
                c.executemany("""
                    INSERT OR REPLACE INTO location_coordinates
                    (query_key, full_district, name, road, lat, lng, source, match_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
                c.commit()
                c.close()
                batch.clear()
            if idx % 100 == 0:
                logger.info(f"Progress: {idx}/{len(all_tasks)} processed, {saved} geocoded ({round(time.time() - t0, 1)}s)")

    if batch:
        c = sqlite3.connect(DB_PATH, timeout=15)
        c.execute("PRAGMA journal_mode = WAL;")
        c.executemany("""
            INSERT OR REPLACE INTO location_coordinates
            (query_key, full_district, name, road, lat, lng, source, match_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        c.commit()
        c.close()

    logger.info(f"Completed! Geocoded {saved}/{len(all_tasks)} locations in {round(time.time() - t0, 1)}s.")


if __name__ == "__main__":
    run_seed()
