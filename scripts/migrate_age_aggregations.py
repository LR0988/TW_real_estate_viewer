"""
Migrates agg_age_county_quarter and agg_age_town_quarter tables to include
growth rates (QoQ, YoY, MoM) and market heat indicators.
"""
import sqlite3
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np

from config import DB_PATH
from analysis.heat_index import compute_market_heat_score
from db.aggregator import compute_growth_rates

def run_migration(db_path: Path = DB_PATH):
    print(f"Starting age aggregations migration for {db_path}...")
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Recreate table schemas with momentum & heat columns
    cur.execute("DROP TABLE IF EXISTS agg_age_county_quarter_new")
    cur.execute("DROP TABLE IF EXISTS agg_age_town_quarter_new")

    cur.execute("""
    CREATE TABLE agg_age_county_quarter_new (
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
    """)

    cur.execute("""
    CREATE TABLE agg_age_town_quarter_new (
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
    """)

    # 2. Process County Age Aggregations
    print("Processing agg_age_county_quarter...")
    df_c = pd.read_sql_query("SELECT * FROM agg_age_county_quarter", conn)
    df_c["grp"] = df_c["county_name"] + "___" + df_c["age_bracket"]
    df_c_growth = compute_growth_rates(df_c, group_col="grp")

    print("Querying monthly data for county age MoM...")
    df_c_m = pd.read_sql_query("""
        SELECT 
            county_name,
            CASE 
                WHEN building_age >= 0 AND building_age <= 5 THEN '0-5'
                WHEN building_age > 5 AND building_age <= 10 THEN '5-10'
                WHEN building_age > 10 AND building_age <= 20 THEN '10-20'
                WHEN building_age > 20 AND building_age <= 30 THEN '20-30'
                WHEN building_age > 30 THEN '30-999'
                ELSE 'unknown'
            END as age_bracket,
            trade_quarter as period,
            trade_month,
            COUNT(*) as m_tx,
            AVG(CASE WHEN unit_price_wan_ping > 1.0 AND unit_price_wan_ping < 600.0 THEN unit_price_wan_ping ELSE NULL END) as m_price
        FROM transactions
        WHERE is_special_trade = 0 AND trade_year >= 2012 AND trade_quarter IS NOT NULL
        GROUP BY county_name, age_bracket, trade_quarter, trade_month
    """, conn)

    df_c_m["grp"] = df_c_m["county_name"] + "___" + df_c_m["age_bracket"]
    df_c_m = df_c_m.sort_values(by=["grp", "trade_month"]).reset_index(drop=True)
    df_c_m["prev_m_tx"] = df_c_m.groupby("grp")["m_tx"].shift(1)
    df_c_m["prev_m_p"] = df_c_m.groupby("grp")["m_price"].shift(1)

    df_c_m["m_tx_pct"] = np.where(
        df_c_m["prev_m_tx"] > 0,
        ((df_c_m["m_tx"] - df_c_m["prev_m_tx"]) / df_c_m["prev_m_tx"] * 100).round(2),
        0.0
    )
    df_c_m["m_price_pct"] = np.where(
        df_c_m["prev_m_p"] > 0,
        ((df_c_m["m_price"] - df_c_m["prev_m_p"]) / df_c_m["prev_m_p"] * 100).round(2),
        0.0
    )

    c_mom = df_c_m.groupby(["grp", "period"]).agg(
        mom_tx_change=("m_tx_pct", "mean"),
        mom_price_change=("m_price_pct", "mean")
    ).reset_index()

    c_final = pd.merge(df_c_growth, c_mom, on=["grp", "period"], how="left")
    c_final["mom_tx_change"] = c_final["mom_tx_change"].fillna(0.0).round(2)
    c_final["mom_price_change"] = c_final["mom_price_change"].fillna(0.0).round(2)
    c_final = c_final.drop(columns=["grp"])

    c_final.to_sql("agg_age_county_quarter_new", conn, if_exists="append", index=False)
    print(f"✅ Loaded {len(c_final)} rows to agg_age_county_quarter_new.")

    # 3. Process Town Age Aggregations
    print("Processing agg_age_town_quarter...")
    df_t = pd.read_sql_query("SELECT * FROM agg_age_town_quarter", conn)
    df_t["grp"] = df_t["full_district"] + "___" + df_t["age_bracket"]
    df_t_growth = compute_growth_rates(df_t, group_col="grp")

    print("Querying monthly data for town age MoM...")
    df_t_m = pd.read_sql_query("""
        SELECT 
            full_district,
            CASE 
                WHEN building_age >= 0 AND building_age <= 5 THEN '0-5'
                WHEN building_age > 5 AND building_age <= 10 THEN '5-10'
                WHEN building_age > 10 AND building_age <= 20 THEN '10-20'
                WHEN building_age > 20 AND building_age <= 30 THEN '20-30'
                WHEN building_age > 30 THEN '30-999'
                ELSE 'unknown'
            END as age_bracket,
            trade_quarter as period,
            trade_month,
            COUNT(*) as m_tx,
            AVG(CASE WHEN unit_price_wan_ping > 1.0 AND unit_price_wan_ping < 600.0 THEN unit_price_wan_ping ELSE NULL END) as m_price
        FROM transactions
        WHERE is_special_trade = 0 AND trade_year >= 2012 AND trade_quarter IS NOT NULL
        GROUP BY full_district, age_bracket, trade_quarter, trade_month
    """, conn)

    df_t_m["grp"] = df_t_m["full_district"] + "___" + df_t_m["age_bracket"]
    df_t_m = df_t_m.sort_values(by=["grp", "trade_month"]).reset_index(drop=True)
    df_t_m["prev_m_tx"] = df_t_m.groupby("grp")["m_tx"].shift(1)
    df_t_m["prev_m_p"] = df_t_m.groupby("grp")["m_price"].shift(1)

    df_t_m["m_tx_pct"] = np.where(
        df_t_m["prev_m_tx"] > 0,
        ((df_t_m["m_tx"] - df_t_m["prev_m_tx"]) / df_t_m["prev_m_tx"] * 100).round(2),
        0.0
    )
    df_t_m["m_price_pct"] = np.where(
        df_t_m["prev_m_p"] > 0,
        ((df_t_m["m_price"] - df_t_m["prev_m_p"]) / df_t_m["prev_m_p"] * 100).round(2),
        0.0
    )

    t_mom = df_t_m.groupby(["grp", "period"]).agg(
        mom_tx_change=("m_tx_pct", "mean"),
        mom_price_change=("m_price_pct", "mean")
    ).reset_index()

    t_final = pd.merge(df_t_growth, t_mom, on=["grp", "period"], how="left")
    t_final["mom_tx_change"] = t_final["mom_tx_change"].fillna(0.0).round(2)
    t_final["mom_price_change"] = t_final["mom_price_change"].fillna(0.0).round(2)
    t_final = t_final.drop(columns=["grp"])

    t_final.to_sql("agg_age_town_quarter_new", conn, if_exists="append", index=False)
    print(f"✅ Loaded {len(t_final)} rows to agg_age_town_quarter_new.")

    # Swap tables
    print("Swapping tables and building indexes...")
    cur.execute("DROP TABLE agg_age_county_quarter")
    cur.execute("ALTER TABLE agg_age_county_quarter_new RENAME TO agg_age_county_quarter")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_age_county_pq ON agg_age_county_quarter(period, age_bracket);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_age_county_cq ON agg_age_county_quarter(county_name, age_bracket);")

    cur.execute("DROP TABLE agg_age_town_quarter")
    cur.execute("ALTER TABLE agg_age_town_quarter_new RENAME TO agg_age_town_quarter")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_age_town_pq ON agg_age_town_quarter(period, age_bracket);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_age_town_fq ON agg_age_town_quarter(full_district, age_bracket);")

    conn.commit()
    conn.close()
    print(f"🎉 Migration completed in {time.time()-t0:.2f}s!")

if __name__ == "__main__":
    run_migration()
