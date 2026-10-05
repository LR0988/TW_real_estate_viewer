import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import sqlite3

from fastapi import FastAPI, Query, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import urllib.request
import urllib.parse
import re
import ssl
import uvicorn

from config import DB_PATH, STATIC_DIR, EARLIEST_SEASON
from db.database import get_connection, get_checkpoint, list_completed_seasons
from db.aggregator import refresh_aggregations
from geo.geo_loader import load_county_geojson, load_town_geojson, ensure_geojson_assets
from crawler.pipeline import crawl_latest, backfill_history
from crawler.downloader import generate_season_list
from analysis.heat_index import compute_market_heat_score
from geo.geocoder import batch_resolve_coordinates, geocode_arcgis, init_geocoder_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Taiwan Real Estate Assessment & Intelligence Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background task state
crawler_task_state = {
    "is_running": False,
    "current_task": None,
    "last_result": None,
}

# Tile cache directory for Taiwan e-Map
EMAP_CACHE_DIR = Path("data/cache/tiles/emap")
EMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
BLANK_TILE_BYTES = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82'
_SSL_CTX = ssl._create_unverified_context()


@app.on_event("startup")
def startup_event():
    ensure_geojson_assets()


@app.get("/api/tiles/emap/{z}/{y}/{x}")
def get_emap_tile(z: int, y: int, x: int):
    """
    Robust high-speed caching tile proxy for Taiwan e-Map (NLSC).
    Eliminates broken tiles and 403/429/0-byte errors from government servers.
    """
    if z > 19:
        return Response(content=BLANK_TILE_BYTES, media_type="image/png")

    tile_file = EMAP_CACHE_DIR / f"{z}_{y}_{x}.jpg"
    if tile_file.exists() and tile_file.stat().st_size > 200:
        return FileResponse(tile_file, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

    url = f"https://wmts.nlsc.gov.tw/wmts/EMAP/default/GoogleMapsCompatible/{z}/{y}/{x}"
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://maps.nlsc.gov.tw/",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                }
            )
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=3.5) as resp:
                data = resp.read()
                if len(data) > 200:
                    try:
                        tile_file.write_bytes(data)
                    except Exception:
                        pass
                    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
        except Exception:
            pass

    # If primary EMAP failed, try EMAP98 fallback
    try:
        url_fb = f"https://wmts.nlsc.gov.tw/wmts/EMAP98/default/GoogleMapsCompatible/{z}/{y}/{x}"
        req_fb = urllib.request.Request(
            url_fb,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://maps.nlsc.gov.tw/"
            }
        )
        with urllib.request.urlopen(req_fb, context=_SSL_CTX, timeout=2.5) as resp:
            data = resp.read()
            if len(data) > 200:
                try:
                    tile_file.write_bytes(data)
                except Exception:
                    pass
                return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        pass

    return Response(content=BLANK_TILE_BYTES, media_type="image/png")


@app.get("/api/periods")
def get_periods():
    """Returns all available periods in chronological order."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT period, year, quarter 
        FROM agg_county_quarter 
        WHERE year >= 2012
        ORDER BY year ASC, quarter ASC
    """)
    rows = cur.fetchall()
    conn.close()
    periods = [r["period"] for r in rows]
    return {"periods": periods}


@app.get("/api/regions")
def get_regions():
    """Returns all 22 counties and their districts in Taiwan."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT county_name, district, full_district
        FROM agg_town_quarter
        WHERE county_name IS NOT NULL AND county_name != ''
        ORDER BY county_name ASC, district ASC
    """)
    tree = {}
    for r in cur.fetchall():
        c, d, fd = r["county_name"], r["district"], r["full_district"]
        if not c:
            continue
        tree.setdefault(c, []).append({"district": d, "full_district": fd})
    conn.close()
    return {"regions": tree}


def get_age_bracket(min_age: Optional[float], max_age: Optional[float]) -> Optional[str]:
    """Helper to match building age filter to pre-aggregated age bracket for sub-millisecond queries."""
    if min_age is None and max_age is None:
        return None
    # match 0-5
    if (min_age is None or min_age == 0) and max_age is not None and abs(max_age - 5.0) < 0.1:
        return "0-5"
    # match 5-10
    if min_age is not None and abs(min_age - 5.0) < 0.1 and max_age is not None and abs(max_age - 10.0) < 0.1:
        return "5-10"
    # match 10-20
    if min_age is not None and abs(min_age - 10.0) < 0.1 and max_age is not None and abs(max_age - 20.0) < 0.1:
        return "10-20"
    # match 20-30
    if min_age is not None and abs(min_age - 20.0) < 0.1 and max_age is not None and abs(max_age - 30.0) < 0.1:
        return "20-30"
    # match 30-999 (30+)
    if min_age is not None and abs(min_age - 30.0) < 0.1 and (max_age is None or max_age >= 900):
        return "30-999"
    return None


@app.get("/api/summary")
def get_national_summary(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None)
):
    """Returns nationwide aggregated statistics for a given period or period range."""
    conn = get_connection()
    cur = conn.cursor()

    bracket = get_age_bracket(min_age, max_age)

    if start_period and end_period:
        p_cond_agg = "period >= ? AND period <= ?"
        p_cond_tx = "trade_quarter >= ? AND trade_quarter <= ?"
        p_params = [start_period, end_period]
        period_label = f"{start_period} ~ {end_period}"
    elif period:
        p_cond_agg = "period = ?"
        p_cond_tx = "trade_quarter = ?"
        p_params = [period]
        period_label = period
    else:
        p_cond_agg = "period = (SELECT period FROM agg_county_quarter ORDER BY year DESC, quarter DESC LIMIT 1)"
        p_cond_tx = "trade_quarter = (SELECT period FROM agg_county_quarter ORDER BY year DESC, quarter DESC LIMIT 1)"
        p_params = []
        period_label = "最新季度"

    if min_age is None and max_age is None:
        cur.execute(f"""
            SELECT 
                SUM(tx_count) as total_tx,
                ROUND(SUM(total_amount_yi), 1) as total_amount,
                ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as national_avg_unit_price,
                ROUND(AVG(mom_tx_change), 2) as avg_mom_tx_change,
                ROUND(AVG(qoq_tx_change), 2) as avg_qoq_tx_change,
                ROUND(AVG(yoy_tx_change), 2) as avg_yoy_tx_change,
                ROUND(AVG(mom_price_change), 2) as avg_mom_price_change,
                ROUND(AVG(qoq_price_change), 2) as avg_qoq_price_change,
                ROUND(AVG(yoy_price_change), 2) as avg_yoy_price_change,
                ROUND(AVG(heat_score), 1) as national_heat_score
            FROM agg_county_quarter
            WHERE {p_cond_agg}
        """, p_params)
        row = cur.fetchone()
    elif bracket is not None:
        cur.execute(f"""
            SELECT 
                SUM(tx_count) as total_tx,
                ROUND(SUM(total_amount_yi), 1) as total_amount,
                ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as national_avg_unit_price,
                ROUND(AVG(mom_tx_change), 2) as avg_mom_tx_change,
                ROUND(AVG(qoq_tx_change), 2) as avg_qoq_tx_change,
                ROUND(AVG(yoy_tx_change), 2) as avg_yoy_tx_change,
                ROUND(AVG(mom_price_change), 2) as avg_mom_price_change,
                ROUND(AVG(qoq_price_change), 2) as avg_qoq_price_change,
                ROUND(AVG(yoy_price_change), 2) as avg_yoy_price_change,
                ROUND(AVG(heat_score), 1) as national_heat_score
            FROM agg_age_county_quarter
            WHERE {p_cond_agg} AND age_bracket = ?
        """, p_params + [bracket])
        row = cur.fetchone()
    else:
        where_clauses = [p_cond_tx, "is_special_trade = 0", "unit_price_wan_ping > 0"]
        params = list(p_params)
        if min_age is not None:
            where_clauses.append("building_age >= ?")
            params.append(min_age)
        if max_age is not None:
            where_clauses.append("building_age <= ?")
            params.append(max_age)
        where_str = " AND ".join(where_clauses)
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_tx,
                ROUND(SUM(total_price) / 100000000.0, 1) as total_amount,
                ROUND(AVG(unit_price_wan_ping), 2) as national_avg_unit_price
            FROM transactions
            WHERE {where_str}
        """, params)
        row = cur.fetchone()

    conn.close()

    if not row or row["total_tx"] is None or row["total_tx"] == 0:
        return {
            "period": period_label,
            "total_tx": 0,
            "total_amount_yi": 0.0,
            "avg_unit_price": 0.0,
            "mom_tx_change": 0.0,
            "qoq_tx_change": 0.0,
            "yoy_tx_change": 0.0,
            "mom_price_change": 0.0,
            "qoq_price_change": 0.0,
            "yoy_price_change": 0.0,
            "heat_score": 0.0,
        }

    c_tx = int(row["total_tx"] or 0)
    c_price = round(float(row["national_avg_unit_price"] or 0.0), 1)
    if "national_heat_score" in row.keys() and row["national_heat_score"] is not None:
        h_score = round(float(row["national_heat_score"]), 1)
    else:
        h_score, _ = compute_market_heat_score(c_tx, 0.0, 0.0, 0.0, 0.0, c_price)

    return {
        "period": period_label,
        "total_tx": c_tx,
        "total_amount_yi": round(float(row["total_amount"] or 0.0), 1),
        "avg_unit_price": c_price,
        "mom_tx_change": round(float(row["avg_mom_tx_change"]), 1) if "avg_mom_tx_change" in row.keys() and row["avg_mom_tx_change"] is not None else 0.0,
        "qoq_tx_change": round(float(row["avg_qoq_tx_change"]), 1) if "avg_qoq_tx_change" in row.keys() and row["avg_qoq_tx_change"] is not None else 0.0,
        "yoy_tx_change": round(float(row["avg_yoy_tx_change"]), 1) if "avg_yoy_tx_change" in row.keys() and row["avg_yoy_tx_change"] is not None else 0.0,
        "mom_price_change": round(float(row["avg_mom_price_change"]), 1) if "avg_mom_price_change" in row.keys() and row["avg_mom_price_change"] is not None else 0.0,
        "qoq_price_change": round(float(row["avg_qoq_price_change"]), 1) if "avg_qoq_price_change" in row.keys() and row["avg_qoq_price_change"] is not None else 0.0,
        "yoy_price_change": round(float(row["avg_yoy_price_change"]), 1) if "avg_yoy_price_change" in row.keys() and row["avg_yoy_price_change"] is not None else 0.0,
        "heat_score": h_score,
    }


@app.get("/api/map/data")
def get_map_data(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    level: str = Query("county", pattern="^(county|town)$"),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None)
):
    """
    Returns GeoJSON enriched with metric properties for choropleth rendering.
    Supports single quarter or multi-quarter range, instant pre-aggregated retrieval or sub-20ms dynamic building-age filtering.
    """
    conn = get_connection()
    cur = conn.cursor()

    is_range = bool(start_period and end_period)
    bracket = get_age_bracket(min_age, max_age)

    if not is_range and not period:
        cur.execute("SELECT period FROM agg_county_quarter ORDER BY year DESC, quarter DESC LIMIT 1")
        row = cur.fetchone()
        period = row["period"] if row else "2024Q3"

    if is_range:
        p_cond_agg = "period >= ? AND period <= ?"
        p_cond_tx = "trade_quarter >= ? AND trade_quarter <= ?"
        p_params = [start_period, end_period]
    else:
        p_cond_agg = "period = ?"
        p_cond_tx = "trade_quarter = ?"
        p_params = [period]

    if level == "county":
        base_geojson = load_county_geojson()
        if min_age is None and max_age is None:
            if not is_range:
                cur.execute(f"SELECT * FROM agg_county_quarter WHERE {p_cond_agg}", p_params)
                stats_rows = cur.fetchall()
                stats_map = {r["county_name"]: dict(r) for r in stats_rows}
            else:
                cur.execute(f"""
                    SELECT county_name,
                           SUM(tx_count) as tx_count,
                           ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                           ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                           ROUND(SUM(median_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as median_unit_price,
                           ROUND(AVG(heat_score), 1) as heat_score
                    FROM agg_county_quarter
                    WHERE {p_cond_agg}
                    GROUP BY county_name
                """, p_params)
                stats_rows = cur.fetchall()
                stats_map = {}
                for r in stats_rows:
                    d = dict(r)
                    h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                    d["heat_score"] = d["heat_score"] or h_score
                    d["heat_level"] = h_lvl
                    stats_map[d["county_name"]] = d

        elif bracket:
            if not is_range:
                cur.execute(f"SELECT * FROM agg_age_county_quarter WHERE {p_cond_agg} AND age_bracket = ?", p_params + [bracket])
                stats_rows = cur.fetchall()
                stats_map = {r["county_name"]: dict(r) for r in stats_rows}
            else:
                cur.execute(f"""
                    SELECT county_name,
                           SUM(tx_count) as tx_count,
                           ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                           ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                           ROUND(SUM(median_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as median_unit_price,
                           ROUND(AVG(heat_score), 1) as heat_score
                    FROM agg_age_county_quarter
                    WHERE {p_cond_agg} AND age_bracket = ?
                    GROUP BY county_name
                """, p_params + [bracket])
                stats_rows = cur.fetchall()
                stats_map = {}
                for r in stats_rows:
                    d = dict(r)
                    h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                    d["heat_score"] = d["heat_score"] or h_score
                    d["heat_level"] = h_lvl
                    stats_map[d["county_name"]] = d
        else:
            where_clauses = [p_cond_tx, "is_special_trade = 0", "unit_price_wan_ping > 0"]
            params = list(p_params)
            if min_age is not None:
                where_clauses.append("building_age >= ?")
                params.append(min_age)
            if max_age is not None:
                where_clauses.append("building_age <= ?")
                params.append(max_age)
            where_str = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT county_name,
                       COUNT(*) as tx_count,
                       ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price
                FROM transactions
                WHERE {where_str}
                GROUP BY county_name
            """, params)
            stats_rows = cur.fetchall()
            stats_map = {}
            for r in stats_rows:
                d = dict(r)
                h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                d["heat_score"] = h_score
                d["heat_level"] = h_lvl
                stats_map[d["county_name"]] = d

        for feat in base_geojson["features"]:
            name = feat["properties"].get("COUNTYNAME", "")
            stats = stats_map.get(name, {})
            c_cnt = stats.get("tx_count", 0)
            feat["properties"]["tx_count"] = c_cnt
            feat["properties"]["total_amount_yi"] = stats.get("total_amount_yi", 0.0)
            feat["properties"]["avg_unit_price"] = stats.get("avg_unit_price", 0.0)
            feat["properties"]["median_unit_price"] = stats.get("median_unit_price", stats.get("avg_unit_price", 0.0))
            feat["properties"]["mom_tx_change"] = stats.get("mom_tx_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["qoq_tx_change"] = stats.get("qoq_tx_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["yoy_tx_change"] = stats.get("yoy_tx_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["mom_price_change"] = stats.get("mom_price_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["qoq_price_change"] = stats.get("qoq_price_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["yoy_price_change"] = stats.get("yoy_price_change") if (c_cnt > 0 and not is_range) else None
            feat["properties"]["heat_score"] = stats.get("heat_score", 0.0)
            feat["properties"]["heat_level"] = stats.get("heat_level", "無資料")

        conn.close()
        return base_geojson

    else:
        # Town level
        base_geojson = load_town_geojson()
        if min_age is None and max_age is None:
            if not is_range:
                cur.execute(f"SELECT * FROM agg_town_quarter WHERE {p_cond_agg}", p_params)
                stats_rows = cur.fetchall()
                stats_map = {r["full_district"]: dict(r) for r in stats_rows}
            else:
                cur.execute(f"""
                    SELECT full_district,
                           SUM(tx_count) as tx_count,
                           ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                           ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                           ROUND(SUM(median_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as median_unit_price,
                           ROUND(AVG(heat_score), 1) as heat_score
                    FROM agg_town_quarter
                    WHERE {p_cond_agg}
                    GROUP BY full_district
                """, p_params)
                stats_rows = cur.fetchall()
                stats_map = {}
                for r in stats_rows:
                    d = dict(r)
                    h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                    d["heat_score"] = d["heat_score"] or h_score
                    d["heat_level"] = h_lvl
                    stats_map[d["full_district"]] = d

        elif bracket:
            if not is_range:
                cur.execute(f"SELECT * FROM agg_age_town_quarter WHERE {p_cond_agg} AND age_bracket = ?", p_params + [bracket])
                stats_rows = cur.fetchall()
                stats_map = {r["full_district"]: dict(r) for r in stats_rows}
            else:
                cur.execute(f"""
                    SELECT full_district,
                           SUM(tx_count) as tx_count,
                           ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                           ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                           ROUND(SUM(median_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as median_unit_price,
                           ROUND(AVG(heat_score), 1) as heat_score
                    FROM agg_age_town_quarter
                    WHERE {p_cond_agg} AND age_bracket = ?
                    GROUP BY full_district
                """, p_params + [bracket])
                stats_rows = cur.fetchall()
                stats_map = {}
                for r in stats_rows:
                    d = dict(r)
                    h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                    d["heat_score"] = d["heat_score"] or h_score
                    d["heat_level"] = h_lvl
                    stats_map[d["full_district"]] = d
        else:
            where_clauses = [p_cond_tx, "is_special_trade = 0", "unit_price_wan_ping > 0"]
            params = list(p_params)
            if min_age is not None:
                where_clauses.append("building_age >= ?")
                params.append(min_age)
            if max_age is not None:
                where_clauses.append("building_age <= ?")
                params.append(max_age)
            where_str = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT full_district,
                       COUNT(*) as tx_count,
                       ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price
                FROM transactions
                WHERE {where_str}
                GROUP BY full_district
            """, params)
            stats_rows = cur.fetchall()
            stats_map = {}
            for r in stats_rows:
                d = dict(r)
                h_score, h_lvl = compute_market_heat_score(d["tx_count"] or 0, 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"] or 0.0)
                d["heat_score"] = h_score
                d["heat_level"] = h_lvl
                stats_map[d["full_district"]] = d

        for feat in base_geojson["features"]:
            full_name = feat["properties"].get("FULLNAME", "")
            stats = stats_map.get(full_name, {})
            t_cnt = stats.get("tx_count", 0)
            feat["properties"]["tx_count"] = t_cnt
            feat["properties"]["total_amount_yi"] = stats.get("total_amount_yi", 0.0)
            feat["properties"]["avg_unit_price"] = stats.get("avg_unit_price", 0.0)
            feat["properties"]["median_unit_price"] = stats.get("median_unit_price", stats.get("avg_unit_price", 0.0))
            feat["properties"]["mom_tx_change"] = stats.get("mom_tx_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["qoq_tx_change"] = stats.get("qoq_tx_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["yoy_tx_change"] = stats.get("yoy_tx_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["mom_price_change"] = stats.get("mom_price_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["qoq_price_change"] = stats.get("qoq_price_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["yoy_price_change"] = stats.get("yoy_price_change") if (t_cnt > 0 and not is_range) else None
            feat["properties"]["heat_score"] = stats.get("heat_score", 0.0)
            feat["properties"]["heat_level"] = stats.get("heat_level", "無資料")

        conn.close()
        return base_geojson


@app.get("/api/trends")
def get_region_trends(
    level: str = Query("county", pattern="^(county|town|national)$"),
    name: str = Query(...),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None)
):
    """
    Returns time series data for a specific county, town, or nationwide across all periods.
    """
    conn = get_connection()
    cur = conn.cursor()

    bracket = get_age_bracket(min_age, max_age)

    if min_age is None and max_age is None:
        if level == "national":
            cur.execute("""
                SELECT period, year, quarter,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                       ROUND(AVG(avg_unit_price), 2) as avg_unit_price,
                       ROUND(AVG(avg_total_price_wan), 1) as avg_total_price_wan,
                       ROUND(AVG(mom_tx_change), 2) as mom_tx_change,
                       ROUND(AVG(qoq_tx_change), 2) as qoq_tx_change,
                       ROUND(AVG(yoy_tx_change), 2) as yoy_tx_change,
                       ROUND(AVG(mom_price_change), 2) as mom_price_change,
                       ROUND(AVG(qoq_price_change), 2) as qoq_price_change,
                       ROUND(AVG(yoy_price_change), 2) as yoy_price_change
                FROM agg_county_quarter 
                WHERE year >= 2012
                GROUP BY period, year, quarter
                ORDER BY year ASC, quarter ASC
            """)
        elif level == "county":
            cur.execute("""
                SELECT * FROM agg_county_quarter 
                WHERE county_name = ? AND year >= 2012
                ORDER BY year ASC, quarter ASC
            """, (name,))
        else:
            cur.execute("""
                SELECT * FROM agg_town_quarter 
                WHERE full_district = ? AND year >= 2012
                ORDER BY year ASC, quarter ASC
            """, (name,))
        rows = cur.fetchall()
        conn.close()
        return {"level": level, "name": name, "series": [dict(r) for r in rows]}

    elif bracket is not None:
        if level == "national":
            cur.execute("""
                SELECT period, year, quarter,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                       ROUND(AVG(avg_unit_price), 2) as avg_unit_price,
                       ROUND(AVG(avg_total_price_wan), 1) as avg_total_price_wan,
                       ROUND(AVG(mom_tx_change), 2) as mom_tx_change,
                       ROUND(AVG(qoq_tx_change), 2) as qoq_tx_change,
                       ROUND(AVG(yoy_tx_change), 2) as yoy_tx_change,
                       ROUND(AVG(mom_price_change), 2) as mom_price_change,
                       ROUND(AVG(qoq_price_change), 2) as qoq_price_change,
                       ROUND(AVG(yoy_price_change), 2) as yoy_price_change,
                       ROUND(AVG(heat_score), 1) as heat_score,
                       '穩健成長' as heat_level
                FROM agg_age_county_quarter 
                WHERE age_bracket = ?
                GROUP BY period, year, quarter
                ORDER BY year ASC, quarter ASC
            """, (bracket,))
        elif level == "county":
            cur.execute("""
                SELECT * FROM agg_age_county_quarter 
                WHERE county_name = ? AND age_bracket = ?
                ORDER BY year ASC, quarter ASC
            """, (name, bracket))
        else:
            cur.execute("""
                SELECT * FROM agg_age_town_quarter 
                WHERE full_district = ? AND age_bracket = ?
                ORDER BY year ASC, quarter ASC
            """, (name, bracket))
        rows = cur.fetchall()
        conn.close()
        return {"level": level, "name": name, "series": [dict(r) for r in rows]}

    else:
        # Dynamic query
        where_clauses = ["trade_year >= 2012", "is_special_trade = 0"]
        params = []
        if min_age is not None:
            where_clauses.append("building_age >= ?")
            params.append(min_age)
        if max_age is not None:
            where_clauses.append("building_age <= ?")
            params.append(max_age)

        if level == "national":
            where_str = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT trade_quarter as period, trade_year as year,
                       CAST(SUBSTR(trade_quarter, 6, 1) AS INTEGER) as quarter,
                       COUNT(*) as tx_count,
                       ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price_wan
                FROM transactions
                WHERE {where_str}
                GROUP BY trade_quarter, trade_year
                ORDER BY trade_year ASC, quarter ASC
            """, params)
        elif level == "county":
            where_clauses.insert(0, "county_name = ?")
            params.insert(0, name)
            where_str = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT trade_quarter as period, trade_year as year,
                       CAST(SUBSTR(trade_quarter, 6, 1) AS INTEGER) as quarter,
                       county_name,
                       COUNT(*) as tx_count,
                       ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price_wan
                FROM transactions
                WHERE {where_str}
                GROUP BY trade_quarter, trade_year
                ORDER BY trade_year ASC, quarter ASC
            """, params)
        else:
            where_clauses.insert(0, "full_district = ?")
            params.insert(0, name)
            where_str = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT trade_quarter as period, trade_year as year,
                       CAST(SUBSTR(trade_quarter, 6, 1) AS INTEGER) as quarter,
                       full_district,
                       COUNT(*) as tx_count,
                       ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price_wan
                FROM transactions
                WHERE {where_str}
                GROUP BY trade_quarter, trade_year
                ORDER BY trade_year ASC, quarter ASC
            """, params)

        rows = cur.fetchall()
        conn.close()

        series = [dict(r) for r in rows]
        # Calculate growth rates dynamically across series
        q_map = {(r["year"], r["quarter"]): r for r in series}
        for r in series:
            y, q = r["year"], r["quarter"]
            prev_q_key = (y, q - 1) if q > 1 else (y - 1, 4)
            prev_y_key = (y - 1, q)
            prev_q = q_map.get(prev_q_key)
            prev_y = q_map.get(prev_y_key)
            if prev_q and prev_q.get("tx_count", 0) > 0:
                r["qoq_tx_change"] = round((r["tx_count"] - prev_q["tx_count"]) / prev_q["tx_count"] * 100, 2)
            else:
                r["qoq_tx_change"] = 0.0
            if prev_q and prev_q.get("avg_unit_price", 0) > 0:
                r["qoq_price_change"] = round((r["avg_unit_price"] - prev_q["avg_unit_price"]) / prev_q["avg_unit_price"] * 100, 2)
            else:
                r["qoq_price_change"] = 0.0
            if prev_y and prev_y.get("tx_count", 0) > 0:
                r["yoy_tx_change"] = round((r["tx_count"] - prev_y["tx_count"]) / prev_y["tx_count"] * 100, 2)
            else:
                r["yoy_tx_change"] = 0.0
            if prev_y and prev_y.get("avg_unit_price", 0) > 0:
                r["yoy_price_change"] = round((r["avg_unit_price"] - prev_y["avg_unit_price"]) / prev_y["avg_unit_price"] * 100, 2)
            else:
                r["yoy_price_change"] = 0.0
            r["mom_tx_change"] = round(r["qoq_tx_change"] / 3.0, 2)
            r["mom_price_change"] = round(r["qoq_price_change"] / 3.0, 2)
            h_score, h_lvl = compute_market_heat_score(
                int(r["tx_count"]), float(r["yoy_tx_change"]), float(r["qoq_tx_change"]),
                float(r["yoy_price_change"]), float(r["qoq_price_change"]), float(r["avg_unit_price"])
            )
            r["heat_score"] = h_score
            r["heat_level"] = h_lvl

        return {"level": level, "name": name, "series": series}


@app.get("/api/ranking")
def get_leaderboard(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    metric: str = Query("heat", pattern="^(heat|volume|price|vol_growth|price_growth)$"),
    level: str = Query("town", pattern="^(county|town)$"),
    limit: int = Query(10, ge=1, le=50),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None)
):
    """
    Returns top ranked districts for the given period (or range), metric, and optional building age bracket.
    """
    conn = get_connection()
    cur = conn.cursor()

    is_range = bool(start_period and end_period)
    if not is_range and not period:
        cur.execute("SELECT period FROM agg_county_quarter ORDER BY year DESC, quarter DESC LIMIT 1")
        row = cur.fetchone()
        period = row["period"] if row else "2024Q3"

    bracket = get_age_bracket(min_age, max_age)
    table = "agg_county_quarter" if level == "county" else "agg_town_quarter"
    name_col = "county_name" if level == "county" else "full_district"

    order_col_map = {
        "heat": "heat_score DESC",
        "volume": "tx_count DESC",
        "price": "avg_unit_price DESC",
        "vol_growth": "tx_count DESC" if is_range else "yoy_tx_change DESC",
        "price_growth": "avg_unit_price DESC" if is_range else "yoy_price_change DESC",
    }
    order_clause = order_col_map.get(metric, "heat_score DESC")

    if min_age is None and max_age is None:
        if not is_range:
            query = f"""
                SELECT {name_col} as region_name, county_name, tx_count, avg_unit_price, 
                       yoy_tx_change, yoy_price_change, heat_score, heat_level, total_amount_yi
                FROM {table}
                WHERE period = ? AND tx_count > 5
                ORDER BY {order_clause}
                LIMIT ?
            """
            cur.execute(query, (period, limit))
            rows = cur.fetchall()
            conn.close()
            return {"ranking": [dict(r) for r in rows]}
        else:
            query = f"""
                SELECT {name_col} as region_name, county_name,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                       ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                       ROUND(AVG(heat_score), 1) as heat_score,
                       0.0 as yoy_tx_change,
                       0.0 as yoy_price_change
                FROM {table}
                WHERE period >= ? AND period <= ?
                GROUP BY {name_col}, county_name
                HAVING tx_count > 5
                ORDER BY {order_clause}
                LIMIT ?
            """
            cur.execute(query, (start_period, end_period, limit))
            rows = cur.fetchall()
            conn.close()
            ranking = []
            for r in rows:
                d = dict(r)
                h_score, h_lvl = compute_market_heat_score(d["tx_count"], 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"])
                d["heat_score"] = d["heat_score"] or h_score
                d["heat_level"] = h_lvl
                ranking.append(d)
            return {"ranking": ranking}

    elif bracket is not None:
        age_table = "agg_age_county_quarter" if level == "county" else "agg_age_town_quarter"
        if not is_range:
            query = f"""
                SELECT {name_col} as region_name, county_name, tx_count, avg_unit_price, 
                       yoy_tx_change, yoy_price_change, heat_score, heat_level, total_amount_yi
                FROM {age_table}
                WHERE period = ? AND age_bracket = ? AND tx_count > 2
                ORDER BY {order_clause}
                LIMIT ?
            """
            cur.execute(query, (period, bracket, limit))
            rows = cur.fetchall()
            conn.close()
            return {"ranking": [dict(r) for r in rows]}
        else:
            query = f"""
                SELECT {name_col} as region_name, county_name,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                       ROUND(SUM(total_amount_yi), 2) as total_amount_yi,
                       ROUND(AVG(heat_score), 1) as heat_score,
                       0.0 as yoy_tx_change,
                       0.0 as yoy_price_change
                FROM {age_table}
                WHERE period >= ? AND period <= ? AND age_bracket = ?
                GROUP BY {name_col}, county_name
                HAVING tx_count > 2
                ORDER BY {order_clause}
                LIMIT ?
            """
            cur.execute(query, (start_period, end_period, bracket, limit))
            rows = cur.fetchall()
            conn.close()
            ranking = []
            for r in rows:
                d = dict(r)
                h_score, h_lvl = compute_market_heat_score(d["tx_count"], 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"])
                d["heat_score"] = d["heat_score"] or h_score
                d["heat_level"] = h_lvl
                ranking.append(d)
            return {"ranking": ranking}

    else:
        # Dynamic query
        where_clauses = ["trade_year >= 2012", "is_special_trade = 0"]
        params = []
        if is_range:
            where_clauses.append("trade_quarter >= ? AND trade_quarter <= ?")
            params.extend([start_period, end_period])
        else:
            where_clauses.append("trade_quarter = ?")
            params.append(period)

        if min_age is not None:
            where_clauses.append("building_age >= ?")
            params.append(min_age)
        if max_age is not None:
            where_clauses.append("building_age <= ?")
            params.append(max_age)

        where_str = " AND ".join(where_clauses)
        name_col = "county_name" if level == "county" else "full_district"

        query = f"""
            SELECT {name_col} as region_name, county_name,
                   COUNT(*) as tx_count,
                   ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                   ROUND(SUM(total_price) / 100000000.0, 2) as total_amount_yi,
                   0.0 as yoy_tx_change,
                   0.0 as yoy_price_change
            FROM transactions
            WHERE {where_str}
            GROUP BY {name_col}
            HAVING tx_count > 2
        """
        rows = cur.execute(query, params).fetchall()
        conn.close()

        enriched = []
        for r in rows:
            d = dict(r)
            cnt = d["tx_count"]
            prc = d["avg_unit_price"]
            h_score, h_lvl = compute_market_heat_score(cnt, 0.0, 0.0, 0.0, 0.0, prc) if cnt > 0 else (0.0, "無資料")
            d["heat_score"] = h_score
            d["heat_level"] = h_lvl
            enriched.append(d)

        if metric in ("volume", "vol_growth"):
            enriched.sort(key=lambda x: x["tx_count"], reverse=True)
        elif metric in ("price", "price_growth"):
            enriched.sort(key=lambda x: x["avg_unit_price"], reverse=True)
        else:
            enriched.sort(key=lambda x: x["heat_score"], reverse=True)

        return {"ranking": enriched[:limit]}


def int_to_chinese_numeral(n: int) -> str:
    """Converts 1-99 to Chinese numerals (e.g. 14 -> 十四, 4 -> 四, 24 -> 二十四)."""
    if n <= 0 or n >= 100:
        return str(n)
    digits = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九']
    tens, units = n // 10, n % 10
    if tens == 0:
        return digits[units]
    elif tens == 1:
        return '十' + digits[units]
    else:
        return digits[tens] + '十' + digits[units]


def chinese_to_int(s: str) -> Optional[int]:
    """Converts Chinese numerals (e.g. 十四, 二十四) to integer."""
    digits = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if not s:
        return None
    if '十' not in s:
        return digits.get(s)
    parts = s.split('十')
    tens = digits.get(parts[0], 1) if parts[0] else 1
    units = digits.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
    return tens * 10 + units


def parse_smart_keyword(kw: str) -> Tuple[str, List[Any]]:
    """
    Intelligently parses Taiwan real estate query strings like:
    '新竹市中山路46號14樓', '中山路46號14F', '新竹市東區關新路248號', '春福承曦'
    Decomposes into floors (Arabic <-> Chinese), doors (half <-> full width),
    roads, counties, and building names.
    """
    HALF_TO_FULL = str.maketrans('0123456789', '０１２３４５６７８９')
    FULL_TO_HALF = str.maketrans('０１２３４５６７８９', '0123456789')
    kw_raw = kw.strip()
    kw_half = kw_raw.translate(FULL_TO_HALF)
    clauses = []
    params = []

    # 1. Floor check
    fl_num = None
    m_ar = re.search(r'(\d+)\s*(?:[樓層Ff])', kw_half)
    if m_ar:
        fl_num = int(m_ar.group(1))
        kw_half = kw_half[:m_ar.start()] + ' ' + kw_half[m_ar.end():]
    else:
        m_ch = re.search(r'([一二三四五六七八九十]+)\s*(?:[樓層])', kw_half)
        if m_ch:
            fl_num = chinese_to_int(m_ch.group(1))
            kw_half = kw_half[:m_ch.start()] + ' ' + kw_half[m_ch.end():]

    if fl_num:
        fl_ch = int_to_chinese_numeral(fl_num)
        fl_ar = str(fl_num)
        fl_clause = """(
            (floor = ? OR floor = ? OR address LIKE ? OR address LIKE ?)
        """
        fl_params = [f"{fl_ch}層", f"{fl_ar}層", f"%{fl_ch}樓%", f"%{fl_ar}樓%"]
        if fl_num < 20:
            for ex in ['二', '三', '四', '五', '六', '七', '八', '九', '2', '3', '4', '5', '6', '7', '8', '9']:
                fl_clause += f" AND floor NOT LIKE '%{ex}{fl_ch}層%' AND address NOT LIKE '%{ex}{fl_ch}樓%'"
        fl_clause += ")"
        clauses.append(fl_clause)
        params.extend(fl_params)

    # 2. Door check
    m_door = re.search(r'((?:\d+巷)?(?:\d+弄)?\d+號)', kw_half)
    if m_door:
        d_str = m_door.group(1)
        d_h = d_str
        d_f = d_str.translate(HALF_TO_FULL)
        clauses.append('(address LIKE ? OR address LIKE ?)')
        params.extend([f'%{d_h}%', f'%{d_f}%'])
        kw_half = kw_half[:m_door.start()] + ' ' + kw_half[m_door.end():]

    # 3. Remaining text (split county, district, road, name)
    raw_tokens = [t.strip() for t in re.split(r'[\s,，]+', kw_half) if t.strip()]
    tokens = []
    for rt in raw_tokens:
        m = re.match(r'^(.+?[市縣])(.+?[區鄉鎮市])?(.+)$', rt)
        if m:
            tokens.extend([g for g in m.groups() if g])
        else:
            m2 = re.match(r'^(.+?[市縣區鄉鎮])(.+)$', rt)
            if m2:
                tokens.extend([g for g in m2.groups() if g])
            else:
                tokens.append(rt)

    for t in tokens:
        if any(t.endswith(s) for s in ['市', '縣', '區', '鄉', '鎮']):
            clauses.append('(county_name LIKE ? OR full_district LIKE ? OR address LIKE ?)')
            params.extend([f'%{t}%', f'%{t}%', f'%{t}%'])
        elif any(t.endswith(s) for s in ['路', '街', '大道', '段']):
            clauses.append('(road LIKE ? OR address LIKE ?)')
            params.extend([f'%{t}%', f'%{t}%'])
        else:
            t_f = t.translate(HALF_TO_FULL)
            clauses.append("""(
                address LIKE ? OR address LIKE ?
                OR road LIKE ? OR road LIKE ?
                OR project_name LIKE ? OR project_name LIKE ?
                OR notes LIKE ? OR notes LIKE ?
            )""")
            params.extend([
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%'
            ])

    if not clauses:
        k_half = kw_raw.translate(FULL_TO_HALF)
        k_full = kw_raw.translate(HALF_TO_FULL)
        return """(
            address LIKE ? OR address LIKE ?
            OR road LIKE ? OR road LIKE ?
            OR project_name LIKE ? OR project_name LIKE ?
            OR notes LIKE ? OR notes LIKE ?
        )""", [
            f'%{k_half}%', f'%{k_full}%',
            f'%{k_half}%', f'%{k_full}%',
            f'%{k_half}%', f'%{k_full}%',
            f'%{k_half}%', f'%{k_full}%'
        ]

    return " AND ".join(clauses), params


@app.get("/api/transactions")
def get_transactions(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    road: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    door: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None),
    min_build_year: Optional[int] = Query(None),
    max_build_year: Optional[int] = Query(None),
    sort_by: str = Query("date_desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """Returns individual transaction records with detailed building, price, age, and address info."""
    conn = get_connection()
    cur = conn.cursor()

    where_clauses = ["trade_year >= 2012"]
    params = []

    if start_period and end_period:
        where_clauses.append("trade_quarter >= ? AND trade_quarter <= ?")
        params.extend([start_period, end_period])
    elif period and period != "all":
        where_clauses.append("trade_quarter = ?")
        params.append(period)

    if region:
        where_clauses.append("(county_name = ? OR full_district = ?)")
        params.extend([region, region])

    # Translation tables for digits
    HALF_TO_FULL = str.maketrans('0123456789', '０１２３４５６７８９')
    FULL_TO_HALF = str.maketrans('０１２３４５６７８９', '0123456789')

    if road:
        r_half = road.translate(FULL_TO_HALF)
        r_full = road.translate(HALF_TO_FULL)
        where_clauses.append("(road LIKE ? OR road LIKE ?)")
        params.extend([f"%{r_half}%", f"%{r_full}%"])

    if project:
        p_half = project.translate(FULL_TO_HALF)
        p_full = project.translate(HALF_TO_FULL)
        where_clauses.append("(project_name LIKE ? OR project_name LIKE ?)")
        params.extend([f"%{p_half}%", f"%{p_full}%"])

    if door:
        d_half = door.translate(FULL_TO_HALF)
        d_full = door.translate(HALF_TO_FULL)
        where_clauses.append("(address LIKE ? OR address LIKE ?)")
        params.extend([f"%{d_half}%", f"%{d_full}%"])

    if keyword:
        kw_where, kw_params = parse_smart_keyword(keyword)
        if kw_where:
            where_clauses.append(f"({kw_where})")
            params.extend(kw_params)

    if min_age is not None:
        where_clauses.append("building_age >= ?")
        params.append(min_age)

    if max_age is not None:
        where_clauses.append("building_age <= ?")
        params.append(max_age)

    if min_build_year is not None:
        where_clauses.append("build_year >= ?")
        params.append(min_build_year)

    if max_build_year is not None:
        where_clauses.append("build_year <= ?")
        params.append(max_build_year)

    where_str = " AND ".join(where_clauses)

    sort_map = {
        "date_desc": "trade_date DESC",
        "date_asc": "trade_date ASC",
        "age_asc": "building_age ASC NULLS LAST",
        "age_desc": "building_age DESC NULLS LAST",
        "build_year_desc": "build_year DESC NULLS LAST",
        "build_year_asc": "build_year ASC NULLS LAST",
        "price_desc": "total_price_wan DESC",
        "price_asc": "total_price_wan ASC",
        "unit_price_desc": "unit_price_wan_ping DESC",
        "unit_price_asc": "unit_price_wan_ping ASC",
        "area_desc": "building_area_ping DESC",
    }
    order_by = sort_map.get(sort_by, "trade_date DESC")

    count_query = f"SELECT COUNT(*) FROM transactions WHERE {where_str}"
    total = cur.execute(count_query, params).fetchone()[0]

    query = f"""
        SELECT id, trade_date, trade_quarter, county_name, district, full_district,
               road, project_name, address, building_type, floor, total_floors,
               building_area_ping, total_price_wan, unit_price_wan_ping,
               room_count, hall_count, bath_count, berth_category, berth_price, notes,
               is_special_trade, build_year, building_age
        FROM transactions
        WHERE {where_str}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    query_params = list(params) + [limit, offset]
    rows = cur.execute(query, query_params).fetchall()
    conn.close()

    return {
        "total": total,
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset
    }


@app.get("/api/search/locate")
def search_and_locate(q: str = Query(..., min_length=1)):
    """
    Intelligent search and geocoding endpoint for full addresses, buildings, and roads.
    Takes freeform queries like '新竹市中山路46號14樓', '關新路248號', '春福承曦',
    parses location components, finds or geocodes real GPS coordinates,
    and returns transaction history and statistics across all history.
    """
    conn = get_connection()
    cur = conn.cursor()

    kw_where, kw_params = parse_smart_keyword(q)

    count_query = f"""
        SELECT COUNT(*),
               ROUND(AVG(unit_price_wan_ping), 2),
               ROUND(AVG(total_price_wan), 1),
               MIN(trade_date),
               MAX(trade_date)
        FROM transactions
        WHERE {kw_where} AND is_special_trade = 0
    """
    cur.execute(count_query, kw_params)
    row = cur.fetchone()
    total_tx = row[0] if row else 0
    avg_unit_price = row[1] if row else 0.0
    avg_total_price = row[2] if row else 0.0
    min_date = row[3] if row else None
    max_date = row[4] if row else None

    sample_query = f"""
        SELECT id, trade_date, trade_quarter, county_name, district, full_district,
               road, project_name, address, building_type, floor, total_floors,
               building_area_ping, total_price_wan, unit_price_wan_ping,
               room_count, hall_count, bath_count, berth_category, berth_price, notes,
               is_special_trade, build_year, building_age
        FROM transactions
        WHERE {kw_where}
        ORDER BY trade_date DESC
        LIMIT 25
    """
    cur.execute(sample_query, kw_params)
    sample_rows = [dict(r) for r in cur.fetchall()]

    target_district = ""
    target_road = ""
    target_name = ""
    target_addr = ""

    if sample_rows:
        first_tx = sample_rows[0]
        target_district = first_tx.get("full_district") or ""
        target_road = first_tx.get("road") or ""
        proj = first_tx.get("project_name") or ""
        addr = first_tx.get("address") or ""
        target_name = extract_spot_name(addr, target_road, proj)
        target_addr = addr
    else:
        m_city = re.search(r"(.+?[市縣])(.+?[區鄉鎮市])?", q)
        if m_city:
            target_district = (m_city.group(1) or '') + (m_city.group(2) or '')
        m_rd = re.search(r"([^\s]+?(?:路|街|大道|段))", q)
        if m_rd:
            target_road = m_rd.group(1)
        target_name = q

    lat = None
    lng = None
    match_score = None
    coord_source = None

    if target_district:
        clean_target_name = target_name.split('(')[0].strip() if target_name else ''
        cur.execute("""
            SELECT lat, lng, match_score, source
            FROM location_coordinates
            WHERE full_district = ? AND (name = ? OR name = ? OR name = ? OR road = ?)
            ORDER BY match_score DESC LIMIT 1
        """, (target_district, target_name, clean_target_name, target_road, target_road))
        c_row = cur.fetchone()
        if c_row:
            lat, lng, match_score, coord_source = c_row[0], c_row[1], c_row[2], c_row[3]

    if (lat is None or lng is None) and target_district:
        res = geocode_arcgis(target_district, target_name or q, target_road)
        if res:
            lat, lng, match_score = res
            coord_source = "arcgis"
            cur.execute("""
                INSERT OR REPLACE INTO location_coordinates
                (query_key, full_district, name, road, lat, lng, source, match_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (f"{target_district}_{target_name}_{target_road}", target_district, target_name, target_road, lat, lng, "arcgis", match_score))
            conn.commit()

    if lat is None or lng is None:
        res = geocode_arcgis("", q, target_road)
        if res:
            lat, lng, match_score = res
            coord_source = "arcgis"

    conn.close()

    return {
        "query": q,
        "found": total_tx > 0 or (lat is not None),
        "total_tx": total_tx,
        "avg_unit_price": avg_unit_price,
        "avg_total_price": avg_total_price,
        "date_range": f"{min_date} ~ {max_date}" if min_date else None,
        "location": {
            "lat": lat,
            "lng": lng,
            "full_district": target_district,
            "road": target_road,
            "name": target_name or q,
            "address": target_addr or q,
            "match_score": match_score,
            "source": coord_source
        },
        "transactions": sample_rows
    }


_TOWN_CENTROIDS_CACHE = None

def get_town_centroids() -> Dict[str, List[float]]:
    global _TOWN_CENTROIDS_CACHE
    if _TOWN_CENTROIDS_CACHE is None:
        try:
            gj = load_town_geojson()
            _TOWN_CENTROIDS_CACHE = {
                f["properties"]["FULLNAME"]: f["properties"]["centroid"]
                for f in gj["features"]
                if f.get("properties") and f["properties"].get("centroid")
            }
        except Exception:
            _TOWN_CENTROIDS_CACHE = {}
    return _TOWN_CENTROIDS_CACHE


_NUM_MAP = str.maketrans('０１２３４５６７８９', '0123456789')
_DOOR_REGEX = re.compile(r'((?:(?:\d+)巷)?(?:(?:\d+)弄)?(?:\d+(?:[~～\-]\d+)?號))')


def extract_spot_name(addr: str, road: str, proj: str) -> str:
    clean_addr = (addr or '').translate(_NUM_MAP)
    m = _DOOR_REGEX.search(clean_addr)
    door_str = m.group(1) if m else ''
    if proj and proj.strip() and proj.strip() not in ('--請選擇--', '?'):
        p = proj.strip()
        if door_str:
            return f"{p} ({door_str})"
        return p
    if door_str:
        return (road or '') + door_str
    return road or clean_addr or '未知'


def extract_spot_type(addr: str, road: str, proj: str) -> str:
    clean_addr = (addr or '').translate(_NUM_MAP)
    m = _DOOR_REGEX.search(clean_addr)
    door_str = m.group(1) if m else ''
    if proj and proj.strip() and proj.strip() not in ('--請選擇--', '?'):
        return 'project'
    if door_str:
        return 'door'
    return 'road'


@app.get("/api/locations/hotspots")
def get_location_hotspots(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    type: str = Query("all", pattern="^(all|road|project)$"),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=5000)
):
    """
    Returns unmerged, granular location-level hotspots (individual door numbers,
    lanes, and building projects) with transaction metrics, market heat score,
    and real GPS coordinates.
    """
    conn = get_connection()
    conn.create_function("get_spot_name", 3, extract_spot_name)
    conn.create_function("get_spot_type", 3, extract_spot_type)
    cur = conn.cursor()

    where_base = [
        "trade_year >= 2012",
        "is_special_trade = 0",
        "unit_price_wan_ping > 0",
        "target_type != '土地'",
        "road NOT LIKE '%--%'",
        "road NOT LIKE '%?%'"
    ]
    params_base = []

    if start_period and end_period:
        where_base.append("trade_quarter >= ? AND trade_quarter <= ?")
        params_base.extend([start_period, end_period])
    elif period:
        where_base.append("trade_quarter = ?")
        params_base.append(period)

    if region:
        where_base.append("(county_name = ? OR full_district = ?)")
        params_base.extend([region, region])

    if min_age is not None:
        where_base.append("building_age >= ?")
        params_base.append(min_age)

    if max_age is not None:
        where_base.append("building_age <= ?")
        params_base.append(max_age)

    if type == "road":
        where_base.append("get_spot_type(address, road, project_name) != 'project'")
    elif type == "project":
        where_base.append("get_spot_type(address, road, project_name) = 'project'")

    # Check whether region is a county (contains 市/縣 but not 區/鄉/鎮)
    is_county = False
    if region:
        clean_reg = region.replace("臺", "台")
        is_county = any(clean_reg.endswith(x) for x in ["市", "縣"]) and not any(x in clean_reg for x in ["區", "鄉", "鎮", "市區"])

    if not region:
        # Nationwide view: Select top 8 unmerged spots per district across all towns
        fetch_limit = limit if limit is not None else 1500
        query = f"""
            WITH AggSpots AS (
                SELECT get_spot_name(address, road, project_name) as spot,
                       get_spot_type(address, road, project_name) as spot_type,
                       road, full_district, county_name, district,
                       COUNT(*) as tx_count,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price,
                       ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
                       ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
                       building_type
                FROM transactions
                WHERE {" AND ".join(where_base)}
                GROUP BY full_district, spot
            ),
            RankedSpots AS (
                SELECT *, ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY tx_count DESC) as rn
                FROM AggSpots
            )
            SELECT spot as name, spot_type as type, road, full_district, county_name, district,
                   tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type
            FROM RankedSpots
            WHERE rn <= 8
            ORDER BY tx_count DESC
            LIMIT ?
        """
        params = list(params_base) + [fetch_limit]
    elif is_county:
        # County view: Select top 30 unmerged spots per district in this county
        fetch_limit = limit if limit is not None else 2000
        query = f"""
            WITH AggSpots AS (
                SELECT get_spot_name(address, road, project_name) as spot,
                       get_spot_type(address, road, project_name) as spot_type,
                       road, full_district, county_name, district,
                       COUNT(*) as tx_count,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price,
                       ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
                       ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
                       building_type
                FROM transactions
                WHERE {" AND ".join(where_base)}
                GROUP BY full_district, spot
            ),
            RankedSpots AS (
                SELECT *, ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY tx_count DESC) as rn
                FROM AggSpots
            )
            SELECT spot as name, spot_type as type, road, full_district, county_name, district,
                   tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type
            FROM RankedSpots
            WHERE rn <= 30
            ORDER BY tx_count DESC
            LIMIT ?
        """
        params = list(params_base) + [fetch_limit]
    else:
        # District view: All unmerged spots in this specific district!
        fetch_limit = limit if limit is not None else 4000
        query = f"""
            WITH AggSpots AS (
                SELECT get_spot_name(address, road, project_name) as spot,
                       get_spot_type(address, road, project_name) as spot_type,
                       road, full_district, county_name, district,
                       COUNT(*) as tx_count,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price,
                       ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
                       ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
                       building_type
                FROM transactions
                WHERE {" AND ".join(where_base)}
                GROUP BY full_district, spot
            )
            SELECT spot as name, spot_type as type, road, full_district, county_name, district,
                   tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type
            FROM AggSpots
            ORDER BY tx_count DESC
            LIMIT ?
        """
        params = list(params_base) + [fetch_limit]

    cur.execute(query, params)
    hotspots = [dict(r) for r in cur.fetchall()]

    for h in hotspots:
        cnt = h["tx_count"]
        prc = h["avg_unit_price"]
        score, level = compute_market_heat_score(cnt, 0.0, 0.0, 0.0, 0.0, prc)
        h["heat_score"] = score
        h["heat_level"] = level
        h["qoq_tx_change"] = 0.0
        h["yoy_tx_change"] = 0.0
        h["mom_tx_change"] = 0.0
        h["qoq_price_change"] = 0.0
        h["yoy_price_change"] = 0.0
        h["mom_price_change"] = 0.0

    # Calculate Momentum (QoQ / YoY / MoM) if single period is present or end_period
    calc_period = period if period else end_period
    if calc_period and len(calc_period) == 6 and calc_period[4] == 'Q':
        y, q = int(calc_period[:4]), int(calc_period[5])
        prev_q = f"{y if q > 1 else y - 1}Q{q - 1 if q > 1 else 4}"
        prev_y = f"{y - 1}Q{q}"

        # 1. Road baseline map (shared across door numbers on the same road)
        road_items = [h for h in hotspots if h.get("road")]
        unique_roads = list({(h["full_district"], h["road"]) for h in road_items if h.get("road")})
        road_prev_map = {}
        for idx in range(0, len(unique_roads), 80):
            chunk = unique_roads[idx:idx + 80]
            road_conds = " OR ".join(["(full_district = ? AND road = ?)" for _ in chunk])
            q_params = [prev_q, prev_y]
            for dist, rd in chunk:
                q_params.extend([dist, rd])
            cur.execute(f"""
                SELECT full_district, road, trade_quarter, COUNT(*) as tx_count, AVG(unit_price_wan_ping) as avg_price
                FROM transactions
                WHERE trade_quarter IN (?, ?) AND ({road_conds}) AND is_special_trade = 0
                GROUP BY full_district, road, trade_quarter
            """, q_params)
            for row in cur.fetchall():
                road_prev_map[(row[0], row[1], row[2])] = {"tx_count": row[3], "avg_price": row[4]}

        # 2. Projects baseline map
        proj_items = [h for h in hotspots if h["type"] == "project"]
        proj_prev_map = {}
        for idx in range(0, len(proj_items), 80):
            chunk = proj_items[idx:idx + 80]
            proj_conds = " OR ".join(["(full_district = ? AND project_name = ?)" for _ in chunk])
            p_params = [prev_q, prev_y]
            for h in chunk:
                p_params.extend([h["full_district"], h["name"]])
            cur.execute(f"""
                SELECT full_district, project_name, trade_quarter, COUNT(*) as tx_count, AVG(unit_price_wan_ping) as avg_price
                FROM transactions
                WHERE trade_quarter IN (?, ?) AND ({proj_conds}) AND is_special_trade = 0
                GROUP BY full_district, project_name, trade_quarter
            """, p_params)
            for row in cur.fetchall():
                proj_prev_map[(row[0], row[1], row[2])] = {"tx_count": row[3], "avg_price": row[4]}

        for h in hotspots:
            if h["type"] == "project":
                pq_data = proj_prev_map.get((h["full_district"], h["name"], prev_q)) or road_prev_map.get((h["full_district"], h.get("road"), prev_q))
                py_data = proj_prev_map.get((h["full_district"], h["name"], prev_y)) or road_prev_map.get((h["full_district"], h.get("road"), prev_y))
            else:
                pq_data = road_prev_map.get((h["full_district"], h.get("road"), prev_q))
                py_data = road_prev_map.get((h["full_district"], h.get("road"), prev_y))

            if pq_data and pq_data["tx_count"] > 0:
                h["qoq_tx_change"] = round((h["tx_count"] - pq_data["tx_count"]) / pq_data["tx_count"] * 100, 2)
                h["qoq_price_change"] = round((h["avg_unit_price"] - pq_data["avg_price"]) / pq_data["avg_price"] * 100, 2)

            if py_data and py_data["tx_count"] > 0:
                h["yoy_tx_change"] = round((h["tx_count"] - py_data["tx_count"]) / py_data["tx_count"] * 100, 2)
                h["yoy_price_change"] = round((h["avg_unit_price"] - py_data["avg_price"]) / py_data["avg_price"] * 100, 2)

            h["mom_tx_change"] = round(h["qoq_tx_change"] / 3.0, 2)
            h["mom_price_change"] = round(h["qoq_price_change"] / 3.0, 2)

            score, level = compute_market_heat_score(
                h["tx_count"], h["yoy_tx_change"], h["qoq_tx_change"],
                h["yoy_price_change"], h["qoq_price_change"], h["avg_unit_price"]
            )
            h["heat_score"] = score
            h["heat_level"] = level

    conn.close()

    hotspots.sort(key=lambda x: (x["tx_count"], x["avg_unit_price"]), reverse=True)
    centroids = get_town_centroids()
    hotspots = batch_resolve_coordinates(hotspots, centroids)
    exact_hotspots = [h for h in hotspots if h.get("lat") is not None and h.get("lng") is not None]

    return {
        "period": f"{start_period} ~ {end_period}" if (start_period and end_period) else period,
        "region": region,
        "type": type,
        "count": len(exact_hotspots),
        "hotspots": exact_hotspots[:fetch_limit]
    }


@app.get("/api/roads")
def get_top_roads(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None),
    limit: int = Query(30, ge=1, le=100)
):
    """Returns road/street level transaction statistics and known project names."""
    conn = get_connection()
    cur = conn.cursor()

    where_clauses = [
        "road != '' AND road IS NOT NULL AND trade_year >= 2012",
        "(road LIKE '%路%' OR road LIKE '%街%' OR road LIKE '%大道%' OR road LIKE '%巷%')",
        "road NOT LIKE '%--%'",
        "road NOT LIKE '%?%'",
        "target_type != '土地'"
    ]
    params = []

    if start_period and end_period:
        where_clauses.append("trade_quarter >= ? AND trade_quarter <= ?")
        params.extend([start_period, end_period])
    elif period:
        where_clauses.append("trade_quarter = ?")
        params.append(period)

    if region:
        where_clauses.append("(county_name = ? OR full_district = ?)")
        params.extend([region, region])

    if min_age is not None:
        where_clauses.append("building_age >= ?")
        params.append(min_age)

    if max_age is not None:
        where_clauses.append("building_age <= ?")
        params.append(max_age)

    where_str = " AND ".join(where_clauses)
    query = f"""
        SELECT road, full_district, county_name, district,
               COUNT(*) as tx_count,
               ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
               ROUND(AVG(total_price_wan), 1) as avg_total_price,
               ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
               ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
               GROUP_CONCAT(DISTINCT project_name) as projects
        FROM transactions
        WHERE {where_str}
        GROUP BY full_district, road
        ORDER BY tx_count DESC
        LIMIT ?
    """
    params.append(limit)
    rows = cur.execute(query, params).fetchall()
    conn.close()

    results = []
    centroids = get_town_centroids()
    for r in rows:
        d = dict(r)
        proj_str = d.get("projects") or ""
        proj_list = [p.strip() for p in proj_str.split(",") if p.strip() and p.strip() != "--請選擇--"]
        d["projects_list"] = proj_list[:5]
        score, lvl = compute_market_heat_score(d["tx_count"], 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"])
        d["heat_score"] = score
        d["heat_level"] = lvl
        d["type"] = "road"
        d["name"] = d["road"]
        results.append(d)

    results = batch_resolve_coordinates(results, centroids)
    return {"roads": results}


@app.get("/api/projects")
def get_top_projects(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None),
    limit: int = Query(30, ge=1, le=100)
):
    """Returns distinct building projects / communities."""
    conn = get_connection()
    cur = conn.cursor()

    where_clauses = [
        "project_name != '' AND project_name IS NOT NULL AND trade_year >= 2012",
        "project_name NOT LIKE '%--%'",
        "project_name NOT LIKE '%?%'",
        "target_type != '土地'"
    ]
    params = []

    if start_period and end_period:
        where_clauses.append("trade_quarter >= ? AND trade_quarter <= ?")
        params.extend([start_period, end_period])
    elif period:
        where_clauses.append("trade_quarter = ?")
        params.append(period)

    if region:
        where_clauses.append("(county_name = ? OR full_district = ?)")
        params.extend([region, region])

    if min_age is not None:
        where_clauses.append("building_age >= ?")
        params.append(min_age)

    if max_age is not None:
        where_clauses.append("building_age <= ?")
        params.append(max_age)

    where_str = " AND ".join(where_clauses)
    query = f"""
        SELECT project_name, full_district, county_name, district, road,
               COUNT(*) as tx_count,
               ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
               ROUND(AVG(total_price_wan), 1) as avg_total_price,
               ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
               ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
               building_type
        FROM transactions
        WHERE {where_str}
        GROUP BY project_name, full_district
        ORDER BY tx_count DESC
        LIMIT ?
    """
    params.append(limit)
    rows = cur.execute(query, params).fetchall()
    conn.close()

    results = []
    centroids = get_town_centroids()
    for r in rows:
        d = dict(r)
        score, lvl = compute_market_heat_score(d["tx_count"], 0.0, 0.0, 0.0, 0.0, d["avg_unit_price"])
        d["heat_score"] = score
        d["heat_level"] = lvl
        d["type"] = "project"
        d["name"] = d["project_name"]
        results.append(d)

    results = batch_resolve_coordinates(results, centroids)
    return {"projects": results}


@app.get("/api/crawler/status")

def get_crawler_status():
    """Returns database size, record count, completed seasons, and crawler background task status."""
    conn = get_connection()
    cur = conn.cursor()
    total_tx = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    total_periods = cur.execute("SELECT COUNT(DISTINCT period) FROM agg_county_quarter").fetchone()[0]
    completed_seasons = list_completed_seasons()
    last_tx = cur.execute("SELECT MAX(trade_date) FROM transactions WHERE trade_date <= date('now')").fetchone()[0]
    conn.close()

    db_size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0.0

    return {
        "total_transactions": total_tx,
        "total_periods": total_periods,
        "completed_seasons_count": len(completed_seasons),
        "completed_seasons": completed_seasons,
        "latest_transaction_date": last_tx,
        "database_size_mb": db_size_mb,
        "crawler_running": crawler_task_state["is_running"],
        "last_crawl_result": crawler_task_state["last_result"],
    }


def _run_latest_crawler_task():
    global crawler_task_state
    crawler_task_state["is_running"] = True
    try:
        res = crawl_latest()
        refresh_aggregations()
        crawler_task_state["last_result"] = res
    except Exception as e:
        crawler_task_state["last_result"] = {"status": "error", "message": str(e)}
    finally:
        crawler_task_state["is_running"] = False


def _run_backfill_crawler_task(start_season: str, end_season: Optional[str]):
    global crawler_task_state
    crawler_task_state["is_running"] = True
    try:
        res = backfill_history(start_season=start_season, end_season=end_season)
        refresh_aggregations()
        crawler_task_state["last_result"] = res
    except Exception as e:
        crawler_task_state["last_result"] = {"status": "error", "message": str(e)}
    finally:
        crawler_task_state["is_running"] = False


@app.post("/api/crawler/crawl_latest")
def trigger_crawl_latest(background_tasks: BackgroundTasks):
    """Triggers crawler to fetch and ingest the latest 10-day batch in background."""
    if crawler_task_state["is_running"]:
        raise HTTPException(status_code=400, detail="Crawler is currently already running.")

    background_tasks.add_task(_run_latest_crawler_task)
    return {"status": "started", "message": "Latest crawl initiated in background."}


@app.post("/api/crawler/backfill")
def trigger_backfill(
    background_tasks: BackgroundTasks,
    start_season: str = Query(EARLIEST_SEASON),
    end_season: Optional[str] = Query(None)
):
    """Triggers historical backfill in background."""
    if crawler_task_state["is_running"]:
        raise HTTPException(status_code=400, detail="Crawler is currently already running.")

    background_tasks.add_task(_run_backfill_crawler_task, start_season, end_season)
    return {
        "status": "started",
        "message": f"Backfill from {start_season} initiated in background."
    }


# Static web frontend
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Web frontend initializing...</h1>")
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
