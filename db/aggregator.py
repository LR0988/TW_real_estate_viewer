import sqlite3
import logging
from pathlib import Path
import pandas as pd
import numpy as np

from config import DB_PATH
from db.database import get_connection
from analysis.heat_index import compute_market_heat_score

logger = logging.getLogger(__name__)


def compute_growth_rates(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """
    Computes QoQ and YoY growth rates for tx_count and avg_unit_price using vectorized joins.
    """
    if df.empty:
        return df

    # q_index = year * 4 + (quarter - 1)
    df["q_index"] = df["year"] * 4 + (df["quarter"] - 1)
    df = df.sort_values(by=[group_col, "q_index"]).reset_index(drop=True)

    # Shift 1 for QoQ
    prev_q = df[[group_col, "q_index", "tx_count", "avg_unit_price"]].copy()
    prev_q["q_index"] = prev_q["q_index"] + 1
    prev_q = prev_q.rename(columns={
        "tx_count": "prev_q_tx",
        "avg_unit_price": "prev_q_price"
    })

    # Shift 4 for YoY
    prev_y = df[[group_col, "q_index", "tx_count", "avg_unit_price"]].copy()
    prev_y["q_index"] = prev_y["q_index"] + 4
    prev_y = prev_y.rename(columns={
        "tx_count": "prev_y_tx",
        "avg_unit_price": "prev_y_price"
    })

    merged = pd.merge(df, prev_q, on=[group_col, "q_index"], how="left")
    merged = pd.merge(merged, prev_y, on=[group_col, "q_index"], how="left")

    merged["qoq_tx_change"] = np.where(
        merged["prev_q_tx"] > 0,
        ((merged["tx_count"] - merged["prev_q_tx"]) / merged["prev_q_tx"] * 100).round(2),
        0.0
    )
    merged["qoq_price_change"] = np.where(
        merged["prev_q_price"] > 0,
        ((merged["avg_unit_price"] - merged["prev_q_price"]) / merged["prev_q_price"] * 100).round(2),
        0.0
    )

    merged["yoy_tx_change"] = np.where(
        merged["prev_y_tx"] > 0,
        ((merged["tx_count"] - merged["prev_y_tx"]) / merged["prev_y_tx"] * 100).round(2),
        0.0
    )
    merged["yoy_price_change"] = np.where(
        merged["prev_y_price"] > 0,
        ((merged["avg_unit_price"] - merged["prev_y_price"]) / merged["prev_y_price"] * 100).round(2),
        0.0
    )

    # Heat scores
    heat_scores = []
    heat_levels = []
    for _, r in merged.iterrows():
        score, level = compute_market_heat_score(
            tx_count=int(r["tx_count"]),
            yoy_tx_change=float(r["yoy_tx_change"]),
            qoq_tx_change=float(r["qoq_tx_change"]),
            yoy_price_change=float(r["yoy_price_change"]),
            qoq_price_change=float(r["qoq_price_change"]),
            avg_unit_price=float(r["avg_unit_price"])
        )
        heat_scores.append(score)
        heat_levels.append(level)

    merged["heat_score"] = heat_scores
    merged["heat_level"] = heat_levels

    return merged.drop(columns=["q_index", "prev_q_tx", "prev_q_price", "prev_y_tx", "prev_y_price"])


def compute_mom_growth_rates(df: pd.DataFrame, group_col: str, valid_price_mask: pd.Series) -> pd.DataFrame:
    """
    Computes Month-over-Month (MoM) growth rates for tx_count and avg_unit_price,
    then aggregates to quarter-level.
    """
    if df.empty or "trade_month" not in df.columns:
        return pd.DataFrame(columns=[group_col, "period", "mom_tx_change", "mom_price_change"])

    # Monthly aggregation
    monthly_tx = df.groupby([group_col, "trade_month", "period"]).size().reset_index(name="m_tx")
    monthly_p = df[valid_price_mask].groupby([group_col, "trade_month"]).agg(
        m_price=("unit_price_wan_ping", "mean")
    ).reset_index()

    monthly = pd.merge(monthly_tx, monthly_p, on=[group_col, "trade_month"], how="left")
    monthly = monthly.sort_values(by=[group_col, "trade_month"]).reset_index(drop=True)

    monthly["prev_m_tx"] = monthly.groupby(group_col)["m_tx"].shift(1)
    monthly["prev_m_p"] = monthly.groupby(group_col)["m_price"].shift(1)

    monthly["m_tx_pct"] = np.where(
        monthly["prev_m_tx"] > 0,
        ((monthly["m_tx"] - monthly["prev_m_tx"]) / monthly["prev_m_tx"] * 100).round(2),
        0.0
    )
    monthly["m_price_pct"] = np.where(
        monthly["prev_m_p"] > 0,
        ((monthly["m_price"] - monthly["prev_m_p"]) / monthly["prev_m_p"] * 100).round(2),
        0.0
    )

    q_mom = monthly.groupby([group_col, "period"]).agg(
        mom_tx_change=("m_tx_pct", "mean"),
        mom_price_change=("m_price_pct", "mean")
    ).reset_index()
    q_mom["mom_tx_change"] = q_mom["mom_tx_change"].round(2)
    q_mom["mom_price_change"] = q_mom["mom_price_change"].round(2)
    return q_mom


def refresh_aggregations(db_path: Path = DB_PATH, exclude_special: bool = True):
    """
    Computes quarter-level aggregations for counties and towns from raw transactions,
    and updates agg_county_quarter and agg_town_quarter tables.
    """
    print(f"Refreshing analytics aggregations (exclude_special={exclude_special})...")
    conn = get_connection(db_path)
    cur = conn.cursor()

    # Determine latest completed published season quarter (e.g. '115S3' -> '2026Q3')
    cur.execute("SELECT season_code FROM checkpoints WHERE season_code NOT LIKE 'latest%' AND status = 'completed' ORDER BY season_code DESC LIMIT 1;")
    chk_row = cur.fetchone()
    max_quarter_cond = ""
    if chk_row and chk_row[0]:
        s = chk_row[0]
        roc_y = int(s[:-2])
        q = s[-1]
        max_q = f"{roc_y + 1911}Q{q}"
        max_quarter_cond = f"AND trade_quarter <= '{max_q}'"
        print(f"Limiting macro aggregation to validated published periods <= {max_q}")

    # Read required fields from transactions (only actual registration era >= 2012)
    query = f"""
    SELECT
        county_name,
        district,
        full_district,
        trade_quarter as period,
        trade_year as year,
        trade_month,
        total_price,
        total_price_wan,
        unit_price_wan_ping,
        building_area_ping
    FROM transactions
    WHERE trade_year >= 2012 {max_quarter_cond} AND trade_quarter IS NOT NULL AND trade_quarter != ''
    """
    if exclude_special:
        query += " AND (is_special_trade = 0 OR is_special_trade IS NULL)"

    df = pd.read_sql_query(query, conn)
    if df.empty:
        print("No transactions found to aggregate.")
        conn.close()
        return

    # Extract quarter number from period (e.g. '2024Q1' -> 1)
    df["quarter"] = df["period"].str.extract(r'Q(\d)').astype(int)

    # Filter reasonable unit prices for housing analysis (e.g. 1 to 500 萬/坪)
    valid_price_mask = (df["unit_price_wan_ping"] > 1.0) & (df["unit_price_wan_ping"] < 600.0)

    # 1. County-level Aggregation
    print("1/2 Calculating County-level metrics...")
    grp_c = df.groupby(["county_name", "period", "year", "quarter"])

    agg_county = grp_c.agg(
        tx_count=("total_price", "count"),
        total_amount_yi=("total_price", lambda s: round(s.sum() / 100_000_000.0, 2)),
        avg_total_price_wan=("total_price_wan", lambda s: round(s.mean(), 1)),
        avg_area_ping=("building_area_ping", lambda s: round(s[s > 0].mean(), 1) if (s > 0).any() else 0.0),
    ).reset_index()

    price_c = df[valid_price_mask].groupby(["county_name", "period"]).agg(
        avg_unit_price=("unit_price_wan_ping", lambda s: round(s.mean(), 2) if len(s) > 0 else 0.0),
        median_unit_price=("unit_price_wan_ping", lambda s: round(s.median(), 2) if len(s) > 0 else 0.0),
    ).reset_index()

    agg_county = pd.merge(agg_county, price_c, on=["county_name", "period"], how="left")
    agg_county["avg_unit_price"] = agg_county["avg_unit_price"].fillna(0.0)
    agg_county["median_unit_price"] = agg_county["median_unit_price"].fillna(0.0)

    agg_county = compute_growth_rates(agg_county, group_col="county_name")

    # Add county MoM
    mom_c = compute_mom_growth_rates(df, group_col="county_name", valid_price_mask=valid_price_mask)
    agg_county = pd.merge(agg_county, mom_c, on=["county_name", "period"], how="left")
    agg_county["mom_tx_change"] = agg_county["mom_tx_change"].fillna(0.0)
    agg_county["mom_price_change"] = agg_county["mom_price_change"].fillna(0.0)

    # 2. Town-level Aggregation
    print("2/2 Calculating Town-level metrics...")
    grp_t = df.groupby(["county_name", "district", "full_district", "period", "year", "quarter"])

    agg_town = grp_t.agg(
        tx_count=("total_price", "count"),
        total_amount_yi=("total_price", lambda s: round(s.sum() / 100_000_000.0, 2)),
        avg_total_price_wan=("total_price_wan", lambda s: round(s.mean(), 1)),
        avg_area_ping=("building_area_ping", lambda s: round(s[s > 0].mean(), 1) if (s > 0).any() else 0.0),
    ).reset_index()

    price_t = df[valid_price_mask].groupby(["full_district", "period"]).agg(
        avg_unit_price=("unit_price_wan_ping", lambda s: round(s.mean(), 2) if len(s) > 0 else 0.0),
        median_unit_price=("unit_price_wan_ping", lambda s: round(s.median(), 2) if len(s) > 0 else 0.0),
    ).reset_index()

    agg_town = pd.merge(agg_town, price_t, on=["full_district", "period"], how="left")
    agg_town["avg_unit_price"] = agg_town["avg_unit_price"].fillna(0.0)
    agg_town["median_unit_price"] = agg_town["median_unit_price"].fillna(0.0)

    agg_town = compute_growth_rates(agg_town, group_col="full_district")

    # Add town MoM
    mom_t = compute_mom_growth_rates(df, group_col="full_district", valid_price_mask=valid_price_mask)
    agg_town = pd.merge(agg_town, mom_t, on=["full_district", "period"], how="left")
    agg_town["mom_tx_change"] = agg_town["mom_tx_change"].fillna(0.0)
    agg_town["mom_price_change"] = agg_town["mom_price_change"].fillna(0.0)

    # Save to SQLite
    cur = conn.cursor()
    cur.execute("DELETE FROM agg_county_quarter")
    cur.execute("DELETE FROM agg_town_quarter")

    agg_county.to_sql("agg_county_quarter", conn, if_exists="append", index=False)
    agg_town.to_sql("agg_town_quarter", conn, if_exists="append", index=False)

    # 3. Refresh building age group aggregations with momentum and market heat
    print("Refreshing building age group aggregations...")
    # Base county age data
    df_c_age = pd.read_sql_query("""
        SELECT 
            county_name,
            trade_quarter as period,
            trade_year as year,
            CAST(SUBSTR(trade_quarter, 6, 1) AS INTEGER) as quarter,
            CASE 
                WHEN building_age >= 0 AND building_age <= 5 THEN '0-5'
                WHEN building_age > 5 AND building_age <= 10 THEN '5-10'
                WHEN building_age > 10 AND building_age <= 20 THEN '10-20'
                WHEN building_age > 20 AND building_age <= 30 THEN '20-30'
                WHEN building_age > 30 THEN '30-999'
                ELSE 'unknown'
            END as age_bracket,
            COUNT(*) as tx_count,
            ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
            ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
            ROUND(AVG(total_price_wan), 1) as avg_total_price_wan
        FROM transactions
        WHERE is_special_trade = 0 AND trade_year >= 2012
        GROUP BY county_name, trade_quarter, trade_year, quarter, age_bracket
    """, conn)
    df_c_age["grp"] = df_c_age["county_name"] + "___" + df_c_age["age_bracket"]
    c_age_growth = compute_growth_rates(df_c_age, group_col="grp")

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
    df_c_m["m_tx_pct"] = np.where(df_c_m["prev_m_tx"] > 0, ((df_c_m["m_tx"] - df_c_m["prev_m_tx"]) / df_c_m["prev_m_tx"] * 100).round(2), 0.0)
    df_c_m["m_price_pct"] = np.where(df_c_m["prev_m_p"] > 0, ((df_c_m["m_price"] - df_c_m["prev_m_p"]) / df_c_m["prev_m_p"] * 100).round(2), 0.0)
    c_age_mom = df_c_m.groupby(["grp", "period"]).agg(
        mom_tx_change=("m_tx_pct", "mean"),
        mom_price_change=("m_price_pct", "mean")
    ).reset_index()

    c_age_final = pd.merge(c_age_growth, c_age_mom, on=["grp", "period"], how="left")
    c_age_final["mom_tx_change"] = c_age_final["mom_tx_change"].fillna(0.0).round(2)
    c_age_final["mom_price_change"] = c_age_final["mom_price_change"].fillna(0.0).round(2)
    c_age_final = c_age_final.drop(columns=["grp"])

    cur.execute("DELETE FROM agg_age_county_quarter")
    c_age_final.to_sql("agg_age_county_quarter", conn, if_exists="append", index=False)

    # Base town age data
    df_t_age = pd.read_sql_query("""
        SELECT 
            county_name,
            district,
            full_district,
            trade_quarter as period,
            trade_year as year,
            CAST(SUBSTR(trade_quarter, 6, 1) AS INTEGER) as quarter,
            CASE 
                WHEN building_age >= 0 AND building_age <= 5 THEN '0-5'
                WHEN building_age > 5 AND building_age <= 10 THEN '5-10'
                WHEN building_age > 10 AND building_age <= 20 THEN '10-20'
                WHEN building_age > 20 AND building_age <= 30 THEN '20-30'
                WHEN building_age > 30 THEN '30-999'
                ELSE 'unknown'
            END as age_bracket,
            COUNT(*) as tx_count,
            ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
            ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
            ROUND(AVG(total_price_wan), 1) as avg_total_price_wan
        FROM transactions
        WHERE is_special_trade = 0 AND trade_year >= 2012
        GROUP BY full_district, trade_quarter, trade_year, quarter, age_bracket
    """, conn)
    df_t_age["grp"] = df_t_age["full_district"] + "___" + df_t_age["age_bracket"]
    t_age_growth = compute_growth_rates(df_t_age, group_col="grp")

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
    df_t_m["m_tx_pct"] = np.where(df_t_m["prev_m_tx"] > 0, ((df_t_m["m_tx"] - df_t_m["prev_m_tx"]) / df_t_m["prev_m_tx"] * 100).round(2), 0.0)
    df_t_m["m_price_pct"] = np.where(df_t_m["prev_m_p"] > 0, ((df_t_m["m_price"] - df_t_m["prev_m_p"]) / df_t_m["prev_m_p"] * 100).round(2), 0.0)
    t_age_mom = df_t_m.groupby(["grp", "period"]).agg(
        mom_tx_change=("m_tx_pct", "mean"),
        mom_price_change=("m_price_pct", "mean")
    ).reset_index()

    t_age_final = pd.merge(t_age_growth, t_age_mom, on=["grp", "period"], how="left")
    t_age_final["mom_tx_change"] = t_age_final["mom_tx_change"].fillna(0.0).round(2)
    t_age_final["mom_price_change"] = t_age_final["mom_price_change"].fillna(0.0).round(2)
    t_age_final = t_age_final.drop(columns=["grp"])

    cur.execute("DELETE FROM agg_age_town_quarter")
    t_age_final.to_sql("agg_age_town_quarter", conn, if_exists="append", index=False)

    conn.commit()
    conn.close()

    print(f"✅ Aggregations refreshed! {len(agg_county):,} county periods, {len(agg_town):,} town periods, plus {len(c_age_final):,} county-age and {len(t_age_final):,} town-age periods.")
