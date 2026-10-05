"""
Streamlined ArcGIS Geocoding Seeder for Top Taiwan Real Estate Roads and Projects.
"""
import concurrent.futures
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from config import DB_PATH
from geo.geocoder import init_geocoder_db, clean_term

logger = logging.getLogger(__name__)

def geocode_one(query_str: str, timeout: float = 6.0):
    url = (
        f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
        f"?SingleLine={urllib.parse.quote(query_str)}&countryCode=TWN&f=json&maxLocations=1"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                top = candidates[0]
                score = float(top.get("score", 0))
                if score >= 75.0:
                    loc = top["location"]
                    lat = round(float(loc["y"]), 6)
                    lng = round(float(loc["x"]), 6)
                    if 21.8 <= lat <= 26.5 and 119.3 <= lng <= 122.6:
                        return lat, lng, score, top.get("address", "")
    except Exception:
        pass
    return None


def run_seed():
    init_geocoder_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Get top 250 roads with real street indicators in recent transactions
    cur.execute("""
        SELECT full_district, road, COUNT(*) as cnt
        FROM transactions
        WHERE road != '' AND road IS NOT NULL
          AND (road LIKE '%路%' OR road LIKE '%街%' OR road LIKE '%大道%' OR road LIKE '%巷%')
          AND road NOT LIKE '%--%' AND road NOT LIKE '%?%' AND target_type != '土地'
        GROUP BY full_district, road
        ORDER BY cnt DESC
        LIMIT 250
    """)
    roads = cur.fetchall()

    # Get top 150 projects
    cur.execute("""
        SELECT full_district, project_name, road, COUNT(*) as cnt
        FROM transactions
        WHERE project_name != '' AND project_name IS NOT NULL
          AND project_name NOT LIKE '%--%' AND project_name NOT LIKE '%?%' AND target_type != '土地'
        GROUP BY full_district, project_name
        ORDER BY cnt DESC
        LIMIT 150
    """)
    projects = cur.fetchall()
    conn.close()

    items = []
    for r in roads:
        dist, rd = clean_term(r[0]), clean_term(r[1])
        key = f"{dist}_{rd}_{rd}"
        q = f"{dist}{rd}"
        items.append((key, dist, rd, rd, q))

    for p in projects:
        dist, proj, rd = clean_term(p[0]), clean_term(p[1]), clean_term(p[2])
        key = f"{dist}_{proj}_{rd}"
        q = f"{dist}{rd} {proj}" if rd else f"{dist}{proj}"
        items.append((key, dist, proj, rd, q))

    print(f"Total items to geocode: {len(items)}")

    def worker(it):
        key, dist, nm, rd, q = it
        res = geocode_one(q)
        if not res and rd and nm != rd:
            res = geocode_one(f"{dist}{rd}")
        if not res and len(dist) >= 6:
            # Try county + name
            res = geocode_one(f"{dist[:3]}{nm}")
        if res:
            lat, lng, score, addr = res
            return (key, dist, nm, rd, lat, lng, "arcgis", score)
        return None

    success_rows = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(worker, it): it for it in items}
        for future in concurrent.futures.as_completed(future_map):
            try:
                r = future.result()
                if r:
                    success_rows.append(r)
            except Exception:
                pass

    # Insert into SQLite
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR REPLACE INTO location_coordinates
        (query_key, full_district, name, road, lat, lng, source, match_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, success_rows)
    conn.commit()
    conn.close()

    print(f"Successfully geocoded {len(success_rows)}/{len(items)} items in {round(time.time() - t0, 2)}s.")

if __name__ == "__main__":
    run_seed()
