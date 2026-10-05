import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Returns a SQLite connection with row_factory configured."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")  # High concurrency & speed
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Path = DB_PATH):
    """Initializes tables and indexes."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        serial_number TEXT,
        season_code TEXT,
        county_code TEXT,
        county_name TEXT,
        district TEXT,
        full_district TEXT,
        target_type TEXT,
        address TEXT,
        land_area_ping REAL,
        zoning TEXT,
        trade_date TEXT,
        trade_year INTEGER,
        trade_quarter TEXT,
        trade_month TEXT,
        floor TEXT,
        total_floors TEXT,
        building_type TEXT,
        main_use TEXT,
        building_area_ping REAL,
        room_count INTEGER,
        hall_count INTEGER,
        bath_count INTEGER,
        has_management TEXT,
        total_price INTEGER,
        total_price_wan REAL,
        unit_price_ping REAL,
        unit_price_wan_ping REAL,
        berth_category TEXT,
        berth_area_ping REAL,
        berth_price INTEGER,
        notes TEXT,
        is_special_trade INTEGER DEFAULT 0,
        elevator TEXT,
        road TEXT,
        project_name TEXT,
        build_year INTEGER,
        building_age REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_tx_quarter ON transactions(trade_quarter);
    CREATE INDEX IF NOT EXISTS idx_tx_county_quarter ON transactions(county_name, trade_quarter);
    CREATE INDEX IF NOT EXISTS idx_tx_district_quarter ON transactions(full_district, trade_quarter);
    CREATE INDEX IF NOT EXISTS idx_tx_road ON transactions(road);
    CREATE INDEX IF NOT EXISTS idx_tx_project ON transactions(project_name);
    CREATE INDEX IF NOT EXISTS idx_tx_serial ON transactions(serial_number);
    CREATE INDEX IF NOT EXISTS idx_tx_special ON transactions(is_special_trade);
    CREATE INDEX IF NOT EXISTS idx_tx_season ON transactions(season_code);
    CREATE INDEX IF NOT EXISTS idx_tx_build_year ON transactions(build_year);
    CREATE INDEX IF NOT EXISTS idx_tx_building_age ON transactions(building_age);

    CREATE TABLE IF NOT EXISTS checkpoints (
        season_code TEXT PRIMARY KEY,
        status TEXT,
        record_count INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS agg_county_quarter (
        county_name TEXT,
        period TEXT,
        year INTEGER,
        quarter INTEGER,
        tx_count INTEGER,
        total_amount_yi REAL,
        avg_unit_price REAL,
        median_unit_price REAL,
        avg_total_price_wan REAL,
        avg_area_ping REAL,
        qoq_tx_change REAL,
        yoy_tx_change REAL,
        mom_tx_change REAL DEFAULT 0.0,
        qoq_price_change REAL,
        yoy_price_change REAL,
        mom_price_change REAL DEFAULT 0.0,
        heat_score REAL,
        heat_level TEXT,
        PRIMARY KEY (county_name, period)
    );

    CREATE TABLE IF NOT EXISTS agg_town_quarter (
        county_name TEXT,
        district TEXT,
        full_district TEXT,
        period TEXT,
        year INTEGER,
        quarter INTEGER,
        tx_count INTEGER,
        total_amount_yi REAL,
        avg_unit_price REAL,
        median_unit_price REAL,
        avg_total_price_wan REAL,
        avg_area_ping REAL,
        qoq_tx_change REAL,
        yoy_tx_change REAL,
        mom_tx_change REAL DEFAULT 0.0,
        qoq_price_change REAL,
        yoy_price_change REAL,
        mom_price_change REAL DEFAULT 0.0,
        heat_score REAL,
        heat_level TEXT,
        PRIMARY KEY (full_district, period)
    );

    CREATE INDEX IF NOT EXISTS idx_agg_county_period ON agg_county_quarter(period);
    CREATE INDEX IF NOT EXISTS idx_agg_town_period ON agg_town_quarter(period);

    CREATE TABLE IF NOT EXISTS agg_age_county_quarter (
        county_name TEXT,
        period TEXT,
        year INTEGER,
        quarter INTEGER,
        age_bracket TEXT,
        tx_count INTEGER,
        total_amount_yi REAL,
        avg_unit_price REAL,
        avg_total_price_wan REAL,
        qoq_tx_change REAL DEFAULT 0.0,
        yoy_tx_change REAL DEFAULT 0.0,
        mom_tx_change REAL DEFAULT 0.0,
        qoq_price_change REAL DEFAULT 0.0,
        yoy_price_change REAL DEFAULT 0.0,
        mom_price_change REAL DEFAULT 0.0,
        heat_score REAL DEFAULT 0.0,
        heat_level TEXT DEFAULT '無資料',
        PRIMARY KEY (county_name, period, age_bracket)
    );
    CREATE INDEX IF NOT EXISTS idx_age_county_pq ON agg_age_county_quarter(period, age_bracket);
    CREATE INDEX IF NOT EXISTS idx_age_county_cq ON agg_age_county_quarter(county_name, age_bracket);

    CREATE TABLE IF NOT EXISTS agg_age_town_quarter (
        county_name TEXT,
        district TEXT,
        full_district TEXT,
        period TEXT,
        year INTEGER,
        quarter INTEGER,
        age_bracket TEXT,
        tx_count INTEGER,
        total_amount_yi REAL,
        avg_unit_price REAL,
        avg_total_price_wan REAL,
        qoq_tx_change REAL DEFAULT 0.0,
        yoy_tx_change REAL DEFAULT 0.0,
        mom_tx_change REAL DEFAULT 0.0,
        qoq_price_change REAL DEFAULT 0.0,
        yoy_price_change REAL DEFAULT 0.0,
        mom_price_change REAL DEFAULT 0.0,
        heat_score REAL DEFAULT 0.0,
        heat_level TEXT DEFAULT '無資料',
        PRIMARY KEY (full_district, period, age_bracket)
    );
    CREATE INDEX IF NOT EXISTS idx_age_town_pq ON agg_age_town_quarter(period, age_bracket);
    CREATE INDEX IF NOT EXISTS idx_age_town_fq ON agg_age_town_quarter(full_district, age_bracket);
    """)

    conn.commit()
    conn.close()
    logger.info("Database schema initialized.")


def insert_transactions_batch(df: pd.DataFrame, season_code: str, db_path: Path = DB_PATH) -> int:
    """Inserts a batch of transactions parsed from a DataFrame."""
    if df.empty:
        return 0

    conn = get_connection(db_path)
    cur = conn.cursor()

    # Columns expected in database
    cols = [
        "serial_number", "season_code", "county_code", "county_name",
        "district", "full_district", "target_type", "address",
        "land_area_ping", "zoning", "trade_date", "trade_year",
        "trade_quarter", "trade_month", "floor", "total_floors",
        "building_type", "main_use", "building_area_ping", "room_count",
        "hall_count", "bath_count", "has_management", "total_price",
        "total_price_wan", "unit_price_ping", "unit_price_wan_ping",
        "berth_category", "berth_area_ping", "berth_price", "notes",
        "is_special_trade", "elevator", "road", "project_name",
        "build_year", "building_age"
    ]

    # Ensure all columns exist in df
    for col in cols:
        if col not in df.columns:
            df[col] = None

    # Replace NaN with None
    clean_df = df[cols].where(pd.notnull(df[cols]), None)
    records = clean_df.to_dict(orient="records")

    placeholders = ", ".join([f":{c}" for c in cols])
    sql = f"""
    INSERT INTO transactions ({", ".join(cols)})
    VALUES ({placeholders})
    """

    try:
        cur.executemany(sql, records)
        conn.commit()
        count = len(records)
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to insert records for {season_code}: {e}")
        raise e
    finally:
        conn.close()

    return count


def record_checkpoint(season_code: str, status: str, count: int, db_path: Path = DB_PATH):
    """Updates the checkpoint table for a season."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO checkpoints (season_code, status, record_count, updated_at)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(season_code) DO UPDATE SET
        status = excluded.status,
        record_count = excluded.record_count,
        updated_at = CURRENT_TIMESTAMP
    """, (season_code, status, count))
    conn.commit()
    conn.close()


def get_checkpoint(season_code: str, db_path: Path = DB_PATH) -> Optional[dict]:
    """Retrieves checkpoint for a specific season."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM checkpoints WHERE season_code = ?", (season_code,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def list_completed_seasons(db_path: Path = DB_PATH) -> List[str]:
    """Returns list of successfully completed seasons."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT season_code FROM checkpoints WHERE status = 'completed'")
    seasons = [r["season_code"] for r in cur.fetchall()]
    conn.close()
    return seasons
