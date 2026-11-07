import re
import time
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = "data/real_estate.db"

_NUM_MAP = str.maketrans("０１２３４５６７８９～－巿", "0123456789~-市")
_DOOR_REGEX = re.compile(
    r"((?:(?:\d+(?:之\d+)?)巷)?(?:(?:\d+(?:之\d+)?)弄)?(?:\d+(?:[~～\-]\d+)?(?:之\d+)?號)|(?:\d+(?:之\d+)?)巷(?:\d+(?:之\d+)?)?弄?)"
)
_ROAD_REGEX = re.compile(
    r"([^\d段巷弄號里鄰\(\)（）\s,，]+?(?:路|街|大道)(?:[一二三四五六七八九十0-9東西南北中]+段)?)"
)
_CARD_REGEX = re.compile(
    r'<h3[^>]*>.*?<!--\[-->([^<]+)<!--\]--></em>.*?<p class="address"[^>]*>.*?<em(?:\s+data-v-[a-z0-9]+)?>(?:<!--\[-->)?([^<]+?[路街段巷弄號][^<]*)</em',
    re.S,
)

REGION_CONFIGS = [
    (4, "新竹市", 45),
    (5, "新竹縣", 55),
    (1, "臺北市", 35),
    (3, "新北市", 35),
    (6, "桃園市", 35),
    (8, "臺中市", 35),
    (15, "臺南市", 25),
    (17, "高雄市", 25),
]


def extract_door(addr_str: str) -> str:
    clean = (addr_str or "").translate(_NUM_MAP)
    m = _DOOR_REGEX.search(clean)
    if not m:
        return ""
    door = m.group(1)
    door = re.sub(r"(\d+)[~～\-]\d+((?:之\d+)?號)", r"\1\2", door)
    return door


def extract_road_and_dist(addr_str: str, county_name: str):
    clean = (addr_str or "").strip().translate(_NUM_MAP)
    if clean.startswith(county_name):
        clean = clean[len(county_name):]
    m_dist = re.match(r"^([^區鄉鎮市]{1,3}[區鄉鎮市])", clean)
    dist = m_dist.group(1) if m_dist else ""
    rest = clean[len(dist):] if dist else clean
    m_rd = _ROAD_REGEX.search(rest)
    road = m_rd.group(1) if m_rd else ""
    door = extract_door(rest)
    full_door = (road + door) if (road and door) else ""
    return dist, road, door, full_door


def fetch_591_page(region_id: int, county_name: str, page: int):
    url = f"https://market.591.com.tw/list?regionid={region_id}&page={page}"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            timeout=8,
        )
        if r.status_code != 200:
            return []
        matches = _CARD_REGEX.findall(r.text)
        out = []
        for raw_name, raw_addr in matches:
            name = raw_name.strip()
            addr = raw_addr.strip()
            if not name or not addr:
                continue
            dist, road, door, full_door = extract_road_and_dist(addr, county_name)
            if dist and full_door:
                out.append((county_name + dist, county_name, dist, road, door, full_door, name, "591_market"))
        return out
    except Exception:
        return []


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS community_master (
            full_district TEXT,
            county_name TEXT,
            district TEXT,
            road TEXT,
            door_no TEXT,
            road_door TEXT,
            community_name TEXT,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (full_district, road_door)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_comm ON community_master(full_district, community_name)")
    conn.commit()

    # 1. Crawl 591 community directory
    tasks = []
    for rid, cname, max_p in REGION_CONFIGS:
        for p in range(1, max_p + 1):
            tasks.append((rid, cname, p))

    collected = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch_591_page, rid, cname, p) for rid, cname, p in tasks]
        for f in as_completed(futs):
            res = f.result()
            if res:
                collected.extend(res)

    cur.executemany(
        """
        INSERT OR REPLACE INTO community_master
        (full_district, county_name, district, road, door_no, road_door, community_name, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        collected,
    )
    conn.commit()
    print(f"[Step 1] Crawled {len(collected)} community mappings from 591 in {round(time.time() - t0, 2)}s")

    # 2. Extract internal existing project_name -> door mappings from transactions
    t1 = time.time()
    cur.execute("""
        SELECT full_district, county_name, district, road, address, project_name
        FROM transactions
        WHERE project_name IS NOT NULL AND project_name != ''
          AND road IS NOT NULL AND road != ''
          AND address LIKE '%號%'
    """)
    internal_rows = []
    for fdist, cname, dist, road, addr, pname in cur.fetchall():
        pname_clean = pname.strip()
        if not pname_clean or pname_clean in ("--請選擇--", "?"):
            continue
        door = extract_door(addr)
        if door and door.endswith("號"):
            internal_rows.append((fdist, cname, dist, road, door, road + door, pname_clean, "moi_internal"))

    cur.executemany(
        """
        INSERT OR IGNORE INTO community_master
        (full_district, county_name, district, road, door_no, road_door, community_name, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        internal_rows,
    )
    conn.commit()
    print(f"[Step 2] Added {len(internal_rows)} internal MOI door->project mappings in {round(time.time() - t1, 2)}s")

    cur.execute("SELECT COUNT(*) FROM community_master")
    print("Total unique (full_district, road_door) in community_master:", cur.fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
