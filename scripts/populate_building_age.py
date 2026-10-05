"""
Populate build_year and building_age in transactions table from raw zip archives.
"""

import os
import sys
import glob
import zipfile
import csv
import io
import time
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from crawler.parser import parse_roc_date

csv.field_size_limit(sys.maxsize)

DB_PATH = Path("data/real_estate.db")
RAW_DIR = Path("data/raw")


def populate():
    t_start = time.time()
    zip_files = sorted(glob.glob(str(RAW_DIR / "*.zip")))
    print(f"Found {len(zip_files)} raw zip archives in {RAW_DIR}.")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA cache_size = -200000;")  # 200MB cache
    conn.execute("PRAGMA temp_store = MEMORY;")
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS temp_build_years;")
    cur.execute("""
        CREATE TABLE temp_build_years (
            serial_number TEXT PRIMARY KEY,
            build_year INTEGER,
            building_age REAL
        ) WITHOUT ROWID;
    """)
    conn.commit()

    total_extracted = 0
    batch = []
    batch_size = 50000

    print("Phase 1: Extracting building dates from raw archives into temp table...")
    for idx, zip_path in enumerate(zip_files, 1):
        zname = Path(zip_path).name
        t0 = time.time()
        count = 0
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                for name in z.namelist():
                    if name.endswith(".csv") and not name.startswith("schema") and not name.startswith("manifest"):
                        with z.open(name) as f:
                            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
                            try:
                                header = next(reader)
                            except StopIteration:
                                continue
                            if header and "The main use" in header[0]:
                                try:
                                    header = next(reader)
                                except StopIteration:
                                    continue

                            col_serial = None
                            col_date = None
                            col_build = None
                            for c_idx, c_val in enumerate(header):
                                col_name = c_val.strip()
                                if col_name == "編號":
                                    col_serial = c_idx
                                elif col_name == "交易年月日":
                                    col_date = c_idx
                                elif col_name == "建築完成年月":
                                    col_build = c_idx

                            if col_serial is None or col_build is None:
                                continue

                            for row in reader:
                                if len(row) > max(col_serial, col_build):
                                    serial = row[col_serial].strip()
                                    raw_build = row[col_build].strip()
                                    if serial and raw_build:
                                        _, byear, _, _ = parse_roc_date(raw_build)
                                        if byear:
                                            trade_year = None
                                            if col_date is not None and len(row) > col_date:
                                                _, trade_year, _, _ = parse_roc_date(row[col_date].strip())
                                            b_age = round(max(0.0, float(trade_year - byear)), 1) if (trade_year and byear) else None
                                            batch.append((serial, byear, b_age))
                                            count += 1
                                            if len(batch) >= batch_size:
                                                cur.executemany("INSERT OR REPLACE INTO temp_build_years VALUES (?, ?, ?);", batch)
                                                conn.commit()
                                                batch.clear()
        except Exception as e:
            print(f"  [ERROR] Failed to parse {zname}: {e}")

        total_extracted += count
        if idx % 10 == 0 or idx == len(zip_files):
            print(f"  [{idx}/{len(zip_files)}] Processed {zname}: +{count} rows (Cumulative extracted: {total_extracted:,})")

    if batch:
        cur.executemany("INSERT OR REPLACE INTO temp_build_years VALUES (?, ?, ?);", batch)
        conn.commit()
        batch.clear()

    cur.execute("SELECT COUNT(*) FROM temp_build_years;")
    temp_count = cur.fetchone()[0]
    print(f"\nPhase 1 Complete! Stored {temp_count:,} unique building records in temp table ({time.time() - t_start:.1f}s total).")

    print("\nPhase 2: Updating transactions table via UPDATE ... FROM join...")
    t_update = time.time()
    cur.execute("""
        UPDATE transactions
        SET build_year = t.build_year,
            building_age = t.building_age
        FROM temp_build_years t
        WHERE transactions.serial_number = t.serial_number;
    """)
    conn.commit()
    print(f"Phase 2 Complete! Updated transactions in {time.time() - t_update:.1f}s.")

    print("\nPhase 3: Cleanup and verification...")
    cur.execute("DROP TABLE temp_build_years;")
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM transactions WHERE build_year IS NOT NULL;")
    filled_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM transactions;")
    total_count = cur.fetchone()[0]

    pct = (filled_count / total_count * 100) if total_count else 0
    print(f"🎉 Verification: {filled_count:,} / {total_count:,} ({pct:.1f}%) transactions have build_year and building_age!")
    print(f"Total time elapsed: {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    populate()
