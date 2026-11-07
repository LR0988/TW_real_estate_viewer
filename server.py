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
from geo.geocoder import batch_resolve_coordinates, geocode_arcgis, geocode_google_maps, init_geocoder_db

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


_LOCATION_HOTSPOTS_CACHE: Dict[str, Any] = {}
_NUM_MAP = str.maketrans('０１２３４５６７８９巿Ｆｆ～－', '0123456789市FF~-')
_HALF_TO_FULL = str.maketrans('0123456789', '０１２３４５６７８９')
_FULL_TO_HALF = str.maketrans('０１２３４５６７８９', '0123456789')
_DOOR_REGEX = re.compile(r'((?:(?:\d+(?:之\d+)?)巷)?(?:(?:\d+(?:之\d+)?)弄)?(?:\d+(?:[~～\-]\d+)?(?:之\d+)?號))')

_CH_DIGITS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '壹': 1, '貳': 2, '參': 3, '叁': 3, '肆': 4, '伍': 5, '陸': 6, '柒': 7, '捌': 8, '玖': 9,
}
_CH_MULTIPLIERS = {'十': 10, '拾': 10, '百': 100, '佰': 100}
_CH_NUM_CHARS = '零〇一二三四五六七八九十百壹貳參叁肆伍陸柒捌玖拾佰'


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
    """Converts standard & uppercase Chinese numerals (e.g. 七, 十四, 一百六十四, 壹佰陸拾肆, 一六四) to integer."""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not any(m in s for m in _CH_MULTIPLIERS):
        if all(c in _CH_DIGITS for c in s):
            val = 0
            for c in s:
                val = val * 10 + _CH_DIGITS[c]
            return val
        return None
    total = 0
    current = 0
    for idx, c in enumerate(s):
        if c in _CH_DIGITS:
            current = _CH_DIGITS[c]
        elif c in _CH_MULTIPLIERS:
            mult = _CH_MULTIPLIERS[c]
            if current == 0 and idx == 0 and mult == 10:
                current = 1
            total += current * mult
            current = 0
        else:
            return None
    total += current
    return total if total > 0 else None


def normalize_address_numerals(addr: str) -> str:
    """
    Normalizes full-width digits (１６４ -> 164) and Chinese numerals in doors/lanes/floors
    (七樓 -> 7樓, 十四樓之七 -> 14樓之7, 一百六十四號 -> 164號, 七層 -> 7樓)
    while preserving road section numerals (一段, 二段) and road names (三民路, 八德路).
    """
    if not addr:
        return ""
    s = str(addr).translate(_NUM_MAP)

    # 1. Basement floors: 地下一層 -> 地下1樓
    def _repl_b(m):
        n = chinese_to_int(m.group(1))
        return f"地下{n}樓" if n is not None else m.group(0)
    s = re.sub(rf"地下([{_CH_NUM_CHARS}\d]+)[層樓]", _repl_b, s)

    # 2. Compound X之Y before 號/樓/層/巷/弄/室 (e.g. 一百六十四之二號 -> 164之2號)
    def _repl_comp(m):
        n1 = chinese_to_int(m.group(1))
        n2 = chinese_to_int(m.group(2))
        unit = "樓" if m.group(3) == "層" else m.group(3)
        if n1 is not None and n2 is not None:
            return f"{n1}之{n2}{unit}"
        return m.group(0)
    s = re.sub(rf"([{_CH_NUM_CHARS}\d]+)之([{_CH_NUM_CHARS}\d]+)(號|樓|層|巷|弄|室)", _repl_comp, s)

    # 3. Chinese numerals immediately before 號/樓/層/巷/弄/室
    def _repl_unit(m):
        n = chinese_to_int(m.group(1))
        unit = "樓" if m.group(2) == "層" else m.group(2)
        return f"{n}{unit}" if n is not None else m.group(0)
    s = re.sub(rf"([{_CH_NUM_CHARS}]+)(號|樓|層|巷|弄|室)", _repl_unit, s)

    # 4. Arabic digits + 層 -> 樓
    s = re.sub(r"(\d+)層", r"\1樓", s)

    # 5. Sub-unit suffix 之 + Chinese numerals (e.g. 14樓之七 -> 14樓之7)
    def _repl_zhi(m):
        n = chinese_to_int(m.group(1))
        return f"之{n}" if n is not None else m.group(0)
    s = re.sub(rf"之([{_CH_NUM_CHARS}]+)", _repl_zhi, s)
    return s


def normalize_floor_str(fl: str) -> str:
    """Normalizes floor strings like '七層' -> '7樓', '十二層' -> '12樓'."""
    if not fl:
        return ""
    return normalize_address_numerals(str(fl).strip())


def extract_door_str(addr: str) -> str:
    """Extracts normalized door string (e.g. '164號', '28巷79號') from an address or spot_name."""
    clean_addr = normalize_address_numerals(addr or "")
    m = _DOOR_REGEX.search(clean_addr)
    if not m:
        return ""
    return re.sub(r'(\d+)[~～\-]\d+((?:之\d+)?號)', r'\1\2', m.group(1))


def extract_spot_name(addr: str, road: str, proj: str) -> str:
    clean_addr = normalize_address_numerals(addr or "")
    m = _DOOR_REGEX.search(clean_addr)
    door_str = re.sub(r'(\d+)[~～\-]\d+((?:之\d+)?號)', r'\1\2', m.group(1)) if m else ""
    if proj and proj.strip() and proj.strip() not in ('--請選擇--', '?'):
        p = proj.strip()
        if door_str:
            return f"{p} ({door_str})"
        return p
    if door_str:
        return (road or '') + door_str
    return road or clean_addr or '未知'


def extract_spot_type(addr: str, road: str, proj: str) -> str:
    clean_addr = normalize_address_numerals(addr or "")
    m = _DOOR_REGEX.search(clean_addr)
    door_str = m.group(1) if m else ''
    if proj and proj.strip() and proj.strip() not in ('--請選擇--', '?'):
        return 'project'
    if door_str:
        return 'door'
    return 'road'


def enrich_and_normalize_tx_rows(cur: sqlite3.Cursor, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalizes full-width and Chinese numerals in address/floor/total_floors and enriches
    empty project_name from community_master so all transactions show their community name.
    """
    if not rows:
        return rows
    lookup_keys = set()
    for r in rows:
        r["address"] = normalize_address_numerals(r.get("address") or "")
        r["floor"] = normalize_floor_str(r.get("floor") or "")
        r["total_floors"] = normalize_floor_str(r.get("total_floors") or "")
        d_str = extract_door_str(r["address"])
        rd = (r.get("road") or "").strip()
        r_door = rd + d_str if d_str else ""
        r["_road_door"] = r_door
        if not (r.get("project_name") or "").strip() and r.get("full_district") and r_door:
            lookup_keys.add((r["full_district"], r_door))

    if lookup_keys:
        cm_map = {}
        by_dist: Dict[str, List[str]] = {}
        for fdist, rdoor in lookup_keys:
            by_dist.setdefault(fdist, []).append(rdoor)
        for fdist, rdoors in by_dist.items():
            ph = ",".join("?" for _ in rdoors)
            for rdoor_val, cname_val in cur.execute(
                f"SELECT road_door, community_name FROM community_master WHERE full_district = ? AND road_door IN ({ph})",
                [fdist] + rdoors
            ).fetchall():
                if cname_val:
                    cm_map[(fdist, rdoor_val)] = cname_val
        for r in rows:
            if not (r.get("project_name") or "").strip():
                mapped = cm_map.get((r.get("full_district"), r.get("_road_door")))
                if mapped:
                    r["project_name"] = mapped

    for r in rows:
        r.pop("_road_door", None)
    return rows


_SEC_NUM_TO_CH = {
    '1段': '一段', '2段': '二段', '3段': '三段', '4段': '四段', '5段': '五段',
    '6段': '六段', '7段': '七段', '8段': '八段', '9段': '九段'
}


def parse_smart_keyword(kw: str, cur: Optional[sqlite3.Cursor] = None) -> Tuple[str, List[Any]]:
    """
    Intelligently parses Taiwan real estate query strings like:
    '新北市永和區環河東路一段１６４號七樓', '新竹市中山路46號14樓', '綠茵河畔', '金澤21'
    Normalizes Chinese/full-width numerals, decomposes into floors, doors, roads, districts,
    and building names, and produces fast indexed SQL clauses.
    """
    kw_norm = normalize_address_numerals(kw.strip())
    for k_sec, v_sec in _SEC_NUM_TO_CH.items():
        if k_sec in kw_norm:
            kw_norm = kw_norm.replace(k_sec, v_sec)

    kw_work = kw_norm
    clauses = []
    params = []

    # 1. Floor check (after normalize_address_numerals, 七樓/7F/七層 is already 7樓/7F)
    fl_num = None
    m_ar = re.search(r'(\d+)\s*(?:[樓層Ff])(?:之(\d+))?', kw_work)
    if m_ar:
        fl_num = int(m_ar.group(1))
        kw_work = kw_work[:m_ar.start()] + ' ' + kw_work[m_ar.end():]

    if fl_num:
        fl_ch = int_to_chinese_numeral(fl_num)
        fl_ar = str(fl_num)
        fl_ar_full = fl_ar.translate(_HALF_TO_FULL)
        fl_clause = """(
            (floor = ? OR floor = ? OR address LIKE ? OR address LIKE ? OR address LIKE ?)
        """
        fl_params = [f"{fl_ch}層", f"{fl_ar}層", f"%{fl_ch}樓%", f"%{fl_ar}樓%", f"%{fl_ar_full}樓%"]
        if fl_num < 20:
            for ex in ['二', '三', '四', '五', '六', '七', '八', '九', '2', '3', '4', '5', '6', '7', '8', '9']:
                fl_clause += f" AND floor NOT LIKE '%{ex}{fl_ch}層%' AND address NOT LIKE '%{ex}{fl_ch}樓%'"
        if fl_num < 10:
            fl_clause += f" AND floor NOT LIKE '%十{fl_ch}層%' AND address NOT LIKE '%十{fl_ch}樓%' AND address NOT LIKE '%1{fl_ar}樓%' AND address NOT LIKE '%2{fl_ar}樓%'"
        fl_clause += ")"
        clauses.append(fl_clause)
        params.extend(fl_params)

    # 2. Door check
    door_str = None
    m_door = re.search(r'((?:\d+(?:之\d+)?巷)?(?:\d+(?:之\d+)?弄)?\d+(?:之\d+)?號)', kw_work)
    if m_door:
        door_str = m_door.group(1)
        kw_work = kw_work[:m_door.start()] + ' ' + kw_work[m_door.end():]

    # 3. Remaining text (split county, district, road, building name)
    raw_tokens = [t.strip() for t in re.split(r'[\s,，]+', kw_work) if t.strip()]
    tokens = []
    for rt in raw_tokens:
        m = re.match(r'^(.+?[市縣])(.+?[區鄉鎮市])?(.+)?$', rt)
        if m and (m.group(2) or m.group(3)):
            tokens.extend([g for g in m.groups() if g])
        else:
            m2 = re.match(r'^(.+?[區鄉鎮市縣])(.+?)(?:路|街|大道|段)', rt)
            if m2:
                dist_part = m2.group(1)
                rest_part = rt[len(dist_part):]
                tokens.extend([dist_part, rest_part])
            else:
                tokens.append(rt)

    county_tok = None
    dist_tok = None
    road_tok = None
    other_tokens = []

    for t in tokens:
        if len(t) == 3 and t.endswith(('市', '縣')):
            county_tok = t
        elif len(t) >= 5 and any(c in t[:3] for c in ('市', '縣')) and t.endswith(('區', '鄉', '鎮', '市')):
            dist_tok = t
        elif t.endswith(('區', '鄉', '鎮')) or (t.endswith('市') and len(t) <= 4 and county_tok):
            dist_tok = (county_tok + t) if (county_tok and not t.startswith(county_tok)) else t
        elif any(t.endswith(s) for s in ['路', '街', '大道', '段']) or any(k in t for k in ['路一段', '路二段', '路三段', '路四段', '路五段', '街一段', '街二段']):
            road_tok = t
        else:
            other_tokens.append(t)

    # Add indexed county / full_district clause
    if dist_tok:
        if len(dist_tok) >= 5:
            d_alt = dist_tok.replace('台', '臺') if '台' in dist_tok else dist_tok.replace('臺', '台')
            if d_alt != dist_tok:
                clauses.append('(full_district = ? OR full_district = ?)')
                params.extend([dist_tok, d_alt])
            else:
                clauses.append('full_district = ?')
                params.append(dist_tok)
        else:
            clauses.append('district = ?')
            params.append(dist_tok)
    elif county_tok:
        c_alt = county_tok.replace('台', '臺') if '台' in county_tok else county_tok.replace('臺', '台')
        if c_alt != county_tok:
            clauses.append('(county_name = ? OR county_name = ?)')
            params.extend([county_tok, c_alt])
        else:
            clauses.append('county_name = ?')
            params.append(county_tok)

    # Add indexed road clause
    if road_tok:
        clauses.append('(road = ? OR road LIKE ?)')
        params.extend([road_tok, f'{road_tok}%'])

    # Add door clause (using road prefix when available so 164號 never matches 2164號 or XX巷164號)
    if door_str:
        d_h = door_str
        d_f = door_str.translate(_HALF_TO_FULL)
        if road_tok:
            clauses.append('(address LIKE ? OR address LIKE ?)')
            params.extend([f'%{road_tok}{d_h}%', f'%{road_tok}{d_f}%'])
        else:
            clauses.append('(address LIKE ? OR address LIKE ?)')
            params.extend([f'%{d_h}%', f'%{d_f}%'])

    # Remaining tokens (building / community name or lane)
    for t in other_tokens:
        t_f = t.translate(_HALF_TO_FULL)
        cm_doors = []
        if cur is not None:
            cm_doors = cur.execute("""
                SELECT full_district, road, door_no FROM community_master
                WHERE community_name = ? OR community_name LIKE ?
                LIMIT 40
            """, (t, f"%{t}%")).fetchall()
        if cm_doors:
            sub_or = ["project_name LIKE ?", "project_name LIKE ?"]
            sub_p = [f"%{t}%", f"%{t_f}%"]
            for cm_fdist, cm_rd, cm_dno in cm_doors:
                if cm_rd and cm_dno:
                    d_full = cm_dno.translate(_HALF_TO_FULL)
                    sub_or.append("(full_district = ? AND road = ? AND (address LIKE ? OR address LIKE ?))")
                    sub_p.extend([cm_fdist, cm_rd, f"%{cm_rd}{cm_dno}%", f"%{cm_rd}{d_full}%"])
            clauses.append("(" + " OR ".join(sub_or) + ")")
            params.extend(sub_p)
        else:
            clauses.append("""(
                project_name LIKE ? OR project_name LIKE ?
                OR address LIKE ? OR address LIKE ?
                OR road LIKE ? OR road LIKE ?
                OR notes LIKE ? OR notes LIKE ?
            )""")
            params.extend([
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%',
                f'%{t}%', f'%{t_f}%'
            ])

    if not clauses:
        k_half = kw_norm
        k_full = kw_norm.translate(_HALF_TO_FULL)
        return """(
            project_name LIKE ? OR project_name LIKE ?
            OR address LIKE ? OR address LIKE ?
            OR road LIKE ? OR road LIKE ?
        )""", [
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
    exclude_special: bool = Query(False),
    sort_by: str = Query("date_desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """Returns individual transaction records with detailed building, price, age, and address info."""
    conn = get_connection()
    cur = conn.cursor()

    where_clauses = ["trade_year >= 2012"]
    params = []

    if exclude_special:
        where_clauses.append("(is_special_trade = 0 OR is_special_trade IS NULL)")

    if start_period and end_period:
        where_clauses.append("trade_quarter >= ? AND trade_quarter <= ?")
        params.extend([start_period, end_period])
    elif period and period != "all":
        where_clauses.append("trade_quarter = ?")
        params.append(period)

    if region:
        where_clauses.append("(county_name = ? OR full_district = ?)")
        params.extend([region, region])

    if road:
        r_half = normalize_address_numerals(road)
        r_full = r_half.translate(_HALF_TO_FULL)
        where_clauses.append("(road = ? OR road LIKE ? OR road LIKE ?)")
        params.extend([r_half, f"%{r_half}%", f"%{r_full}%"])

    if project:
        p_half = project.translate(_FULL_TO_HALF).strip()
        p_full = p_half.translate(_HALF_TO_FULL)
        cm_doors = cur.execute("""
            SELECT full_district, road, door_no FROM community_master
            WHERE community_name = ? OR community_name LIKE ?
            LIMIT 40
        """, (p_half, f"%{p_half}%")).fetchall()
        if cm_doors:
            sub_or = ["project_name LIKE ?", "project_name LIKE ?"]
            sub_p = [f"%{p_half}%", f"%{p_full}%"]
            for cm_fdist, cm_rd, cm_dno in cm_doors:
                if cm_rd and cm_dno:
                    d_full = cm_dno.translate(_HALF_TO_FULL)
                    sub_or.append("(full_district = ? AND road = ? AND (address LIKE ? OR address LIKE ?))")
                    sub_p.extend([cm_fdist, cm_rd, f"%{cm_rd}{cm_dno}%", f"%{cm_rd}{d_full}%"])
            where_clauses.append("(" + " OR ".join(sub_or) + ")")
            params.extend(sub_p)
        else:
            where_clauses.append("(project_name LIKE ? OR project_name LIKE ?)")
            params.extend([f"%{p_half}%", f"%{p_full}%"])

    if door:
        d_half = normalize_address_numerals(door)
        d_full = d_half.translate(_HALF_TO_FULL)
        where_clauses.append("(address LIKE ? OR address LIKE ?)")
        params.extend([f"%{d_half}%", f"%{d_full}%"])

    if keyword:
        kw_where, kw_params = parse_smart_keyword(keyword, cur=cur)
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
               road, project_name, address, target_type, building_type, floor, total_floors,
               building_area_ping, land_area_ping, total_price_wan, unit_price_wan_ping,
               room_count, hall_count, bath_count, berth_category, berth_area_ping, berth_price, notes,
               is_special_trade, build_year, building_age
        FROM transactions
        WHERE {where_str}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    query_params = list(params) + [limit, offset]
    rows = [dict(r) for r in cur.execute(query, query_params).fetchall()]
    rows = enrich_and_normalize_tx_rows(cur, rows)
    conn.close()

    return {
        "total": total,
        "items": rows,
        "limit": limit,
        "offset": offset
    }


@app.get("/api/search/locate")
def search_and_locate(q: str = Query(..., min_length=1)):
    """
    Intelligent search and geocoding endpoint for full addresses, buildings, and roads.
    Takes freeform queries like '新北市永和區環河東路一段１６４號七樓', '新竹市中山路46號14樓', '綠茵河畔', '金澤21',
    normalizes Chinese/full-width numerals, finds or geocodes real Google Maps GPS coordinates,
    enriches community names (with on-demand 591 discovery), and returns transaction history.
    """
    conn = get_connection()
    cur = conn.cursor()

    q_norm = normalize_address_numerals(q.strip())
    for k_sec, v_sec in _SEC_NUM_TO_CH.items():
        if k_sec in q_norm:
            q_norm = q_norm.replace(k_sec, v_sec)

    has_specific_door_or_floor = bool(re.search(r'\d+\s*[號樓層Ff]', q_norm))
    cm_row = None

    if not has_specific_door_or_floor:
        cm_row = cur.execute("""
            SELECT full_district, road, spot_name, project_name,
                   SUM(tx_count) as total_tx,
                   ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_u,
                   ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_t,
                   MIN(period) as min_p,
                   MAX(period) as max_p
            FROM agg_spot_quarter
            WHERE project_name = ? OR project_name LIKE ? OR spot_name = ?
            GROUP BY full_district, COALESCE(NULLIF(project_name, ''), spot_name)
            ORDER BY CASE WHEN project_name = ? OR spot_name = ? THEN 0 ELSE 1 END, total_tx DESC
            LIMIT 1
        """, (q_norm, f"%{q_norm}%", q_norm, q_norm, q_norm)).fetchone()

    discovered_proj = ""
    spot_only_name = ""

    if cm_row and cm_row[4] and cm_row[4] > 0:
        target_district = cm_row[0]
        target_road = cm_row[1] or ""
        spot_nm = cm_row[2] or q_norm
        proj_nm = cm_row[3] or ""
        discovered_proj = proj_nm
        spot_only_name = spot_nm
        target_name = f"{proj_nm} ({spot_nm})" if (proj_nm and spot_nm and proj_nm != spot_nm) else (proj_nm or spot_nm)
        total_tx = cm_row[4]
        avg_unit_price = cm_row[5] or 0.0
        avg_total_price = cm_row[6] or 0.0
        min_date = cm_row[7]
        max_date = cm_row[8]
        target_addr = f"{target_district}{spot_nm}"
        sample_rows = []
    else:
        kw_where, kw_params = parse_smart_keyword(q_norm, cur=cur)

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
                   road, project_name, address, target_type, building_type, floor, total_floors,
                   building_area_ping, land_area_ping, total_price_wan, unit_price_wan_ping,
                   room_count, hall_count, bath_count, berth_category, berth_area_ping, berth_price, notes,
                   is_special_trade, build_year, building_age
            FROM transactions
            WHERE {kw_where}
            ORDER BY trade_date DESC
            LIMIT 25
        """
        cur.execute(sample_query, kw_params)
        sample_rows = [dict(r) for r in cur.fetchall()]
        sample_rows = enrich_and_normalize_tx_rows(cur, sample_rows)

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
            d_str = extract_door_str(addr)
            spot_only_name = (target_road + d_str) if d_str else target_road

            # On-demand 591 lookup if apartment/mansion door still has no project_name
            b_type = first_tx.get("building_type") or ""
            if not proj and target_district and spot_only_name and ("大樓" in b_type or "華廈" in b_type):
                try:
                    from scripts.backfill_unmapped_buildings import lookup_door_on_591
                    c_nm = first_tx.get("county_name") or target_district[:3]
                    d_nm = first_tx.get("district") or target_district[3:]
                    found_591 = lookup_door_on_591(c_nm, d_nm, spot_only_name)
                    if found_591:
                        proj = found_591
                        cur.execute("""
                            INSERT OR REPLACE INTO community_master
                            (full_district, county_name, district, road, door_no, road_door, community_name, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, '591_ondemand')
                        """, (target_district, c_nm, d_nm, target_road, d_str, spot_only_name, proj))
                        cur.execute("""
                            UPDATE agg_spot_quarter SET project_name = ?
                            WHERE full_district = ? AND spot_name = ?
                        """, (proj, target_district, spot_only_name))
                        conn.commit()
                        for sr in sample_rows:
                            if not sr.get("project_name"):
                                sr["project_name"] = proj
                except Exception:
                    pass

            discovered_proj = proj
            target_name = extract_spot_name(addr, target_road, proj)
            target_addr = addr
        else:
            m_city = re.search(r"(.+?[市縣])(.+?[區鄉鎮市])?", q_norm)
            if m_city:
                target_district = (m_city.group(1) or '') + (m_city.group(2) or '')
            m_rd = re.search(r"([^\s]+?(?:路|街|大道|段))", q_norm)
            if m_rd:
                target_road = m_rd.group(1)
            target_name = q_norm
            spot_only_name = q_norm

    lat = None
    lng = None
    match_score = None
    coord_source = None

    spot_lookup = spot_only_name or (cm_row[2] if (cm_row and cm_row[2]) else target_name)
    if target_district:
        clean_target_name = target_name.split('(')[0].strip() if target_name else ''
        cur.execute("""
            SELECT lat, lng, match_score, source, name
            FROM location_coordinates
            WHERE full_district = ? AND name IN (?, ?, ?)
            ORDER BY match_score DESC LIMIT 1
        """, (target_district, spot_lookup, target_name, clean_target_name))
        c_row = cur.fetchone()
        if c_row:
            lat, lng, match_score, coord_source = c_row[0], c_row[1], c_row[2], c_row[3]

    if (lat is None or lng is None) and target_district:
        d_str = extract_door_str(target_addr or spot_lookup or q_norm)
        res = geocode_google_maps(target_district, spot_lookup or target_name or q_norm, target_road, d_str)
        if not res:
            res = geocode_arcgis(target_district, spot_lookup or target_name or q_norm, target_road, target_addr)
        if res:
            lat, lng, match_score, coord_source = res
            save_name = spot_lookup or target_name
            cur.execute("""
                INSERT OR REPLACE INTO location_coordinates
                (query_key, full_district, name, road, lat, lng, source, match_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (f"{target_district}_{save_name}_{target_road}", target_district, save_name, target_road, lat, lng, coord_source, match_score))
            conn.commit()

    if lat is None or lng is None:
        res = geocode_google_maps(target_district or "", q_norm, target_road, extract_door_str(q_norm))
        if not res:
            res = geocode_arcgis("", q_norm, target_road, target_addr)
        if res:
            lat, lng, match_score, coord_source = res

    conn.close()

    return {
        "query": q_norm,
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
            "name": target_name or q_norm,
            "spot_name": spot_lookup or target_name or q_norm,
            "project_name": discovered_proj,
            "address": target_addr or q_norm,
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


@app.get("/api/locations/hotspots")
def get_location_hotspots(
    period: Optional[str] = Query(None),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    type: str = Query("all", pattern="^(all|road|project)$"),
    min_age: Optional[float] = Query(None),
    max_age: Optional[float] = Query(None),
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lng: Optional[float] = Query(None),
    max_lng: Optional[float] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=5000)
):
    """
    Returns unmerged, granular location-level hotspots (individual door numbers,
    lanes, and building projects) with transaction metrics, market heat score,
    and real GPS coordinates in sub-50ms using pre-aggregated tables.
    """
    conn = get_connection()
    cur = conn.cursor()

    is_county = False
    if region:
        clean_reg = region.replace("臺", "台")
        is_county = any(clean_reg.endswith(x) for x in ["市", "縣"]) and not any(x in clean_reg for x in ["區", "鄉", "鎮", "市區"])

    # Determine if viewport bounding box narrows down visible districts when region is not selected
    viewport_districts = []
    if not region and min_lat is not None and max_lat is not None and min_lng is not None and max_lng is not None:
        # Only apply viewport full-detail expansion when zoomed into a regional/city area (lat span < 1.2 deg)
        if (max_lat - min_lat) < 1.25 and (max_lng - min_lng) < 1.25:
            centroids = get_town_centroids()
            pad = 0.08
            for dist_name, c_xy in centroids.items():
                if c_xy and len(c_xy) >= 2:
                    c_lng, c_lat = c_xy[0], c_xy[1]
                    if (min_lat - pad) <= c_lat <= (max_lat + pad) and (min_lng - pad) <= c_lng <= (max_lng + pad):
                        viewport_districts.append(dist_name)

    use_preagg = (min_age is None and max_age is None)

    if use_preagg:
        where_agg = ["1=1"]
        params_agg = []
        is_range = bool(start_period and end_period and start_period != end_period)

        if is_range:
            where_agg.append("period >= ? AND period <= ?")
            params_agg.extend([start_period, end_period])
        elif period or start_period:
            where_agg.append("period = ?")
            params_agg.append(period or start_period)

        if region:
            if is_county:
                where_agg.append("county_name = ?")
                params_agg.append(region)
            else:
                where_agg.append("full_district = ?")
                params_agg.append(region)
        elif viewport_districts:
            placeholders = ",".join("?" for _ in viewport_districts)
            where_agg.append(f"full_district IN ({placeholders})")
            params_agg.extend(viewport_districts)

        if type == "road":
            where_agg.append("spot_type != 'project'")
        elif type == "project":
            where_agg.append("spot_type = 'project'")

        # When zoomed out to nationwide overview, pre-filter out 1-tx individual doors before window ranking
        if not region and not viewport_districts:
            where_agg.append("(spot_type = 'project' OR tx_count >= 2)")

        if not is_range:
            base_cte = f"""
                SELECT spot_name as spot, spot_type, road, door_addr, project_name,
                       full_district, county_name, district,
                       tx_count, avg_unit_price, avg_total_price,
                       min_unit_price, max_unit_price, building_type,
                       avg_age, build_year, avg_building_ping, avg_land_ping,
                       has_parking_count, berth_type
                FROM agg_spot_quarter
                WHERE {" AND ".join(where_agg)}
            """
        else:
            base_cte = f"""
                SELECT spot_name as spot,
                       MAX(spot_type) as spot_type,
                       MAX(road) as road,
                       MAX(door_addr) as door_addr,
                       MAX(project_name) as project_name,
                       full_district,
                       MAX(county_name) as county_name,
                       MAX(district) as district,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                       ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_total_price,
                       ROUND(MIN(min_unit_price), 2) as min_unit_price,
                       ROUND(MAX(max_unit_price), 2) as max_unit_price,
                       MAX(building_type) as building_type,
                       ROUND(AVG(avg_age), 1) as avg_age,
                       MAX(build_year) as build_year,
                       ROUND(SUM(avg_building_ping * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_building_ping,
                       ROUND(SUM(avg_land_ping * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_land_ping,
                       SUM(has_parking_count) as has_parking_count,
                       MAX(berth_type) as berth_type
                FROM agg_spot_quarter
                WHERE {" AND ".join(where_agg)}
                GROUP BY full_district, spot_name
            """

        if not region and not viewport_districts:
            fetch_limit = limit if limit is not None else 2500
            query = f"""
                WITH AggSpots AS ({base_cte}),
                RankedSpots AS (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY tx_count DESC) as rn
                    FROM AggSpots
                )
                SELECT spot as name, spot_type as type, road, door_addr, project_name,
                       full_district, county_name, district,
                       tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type,
                       avg_age, build_year, avg_building_ping, avg_land_ping, has_parking_count, berth_type
                FROM RankedSpots
                WHERE rn <= 15
                ORDER BY tx_count DESC
                LIMIT ?
            """
            params = params_agg + [fetch_limit]
        else:
            # Region selected (County or District) OR zoomed-in viewport: return ALL unmerged spots without truncation!
            fetch_limit = limit if limit is not None else 4500
            query = f"""
                WITH AggSpots AS ({base_cte})
                SELECT spot as name, spot_type as type, road, door_addr, project_name,
                       full_district, county_name, district,
                       tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type,
                       avg_age, build_year, avg_building_ping, avg_land_ping, has_parking_count, berth_type
                FROM AggSpots
                ORDER BY tx_count DESC
                LIMIT ?
            """
            params = params_agg + [fetch_limit]
    else:
        conn.create_function("get_spot_name", 3, extract_spot_name)
        conn.create_function("get_spot_type", 3, extract_spot_type)
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

        fetch_limit = limit if limit is not None else (4000 if (region and not is_county) else (2000 if is_county else 1500))
        rn_limit = 8 if not region else (30 if is_county else 99999)
        query = f"""
            WITH AggSpots AS (
                SELECT get_spot_name(address, road, project_name) as spot,
                       get_spot_type(address, road, project_name) as spot_type,
                       road, '' as door_addr, project_name, full_district, county_name, district,
                       COUNT(*) as tx_count,
                       ROUND(AVG(unit_price_wan_ping), 2) as avg_unit_price,
                       ROUND(AVG(total_price_wan), 1) as avg_total_price,
                       ROUND(MIN(unit_price_wan_ping), 2) as min_unit_price,
                       ROUND(MAX(unit_price_wan_ping), 2) as max_unit_price,
                       building_type,
                       ROUND(AVG(building_age), 1) as avg_age,
                       MAX(build_year) as build_year,
                       ROUND(AVG(building_area_ping), 2) as avg_building_ping,
                       ROUND(AVG(land_area_ping), 2) as avg_land_ping,
                       SUM(CASE WHEN target_type LIKE '%車位%' OR (berth_category IS NOT NULL AND berth_category != '' AND berth_category != 'nan') OR berth_area_ping > 0 THEN 1 ELSE 0 END) as has_parking_count,
                       MAX(CASE WHEN berth_category IS NOT NULL AND berth_category != '' AND berth_category != 'nan' THEN berth_category ELSE '' END) as berth_type
                FROM transactions
                WHERE {" AND ".join(where_base)}
                GROUP BY full_district, spot
            ),
            RankedSpots AS (
                SELECT *, ROW_NUMBER() OVER(PARTITION BY full_district ORDER BY tx_count DESC) as rn
                FROM AggSpots
            )
            SELECT spot as name, spot_type as type, road, door_addr, project_name,
                   full_district, county_name, district,
                   tx_count, avg_unit_price, avg_total_price, min_unit_price, max_unit_price, building_type,
                   avg_age, build_year, avg_building_ping, avg_land_ping, has_parking_count, berth_type
            FROM RankedSpots
            WHERE rn <= {rn_limit}
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

    # Fast sub-10ms Momentum (QoQ / YoY / MoM) lookup using pre-aggregated tables!
    calc_period = period if period else end_period
    if calc_period and len(calc_period) == 6 and calc_period[4] == 'Q' and hotspots:
        y, q = int(calc_period[:4]), int(calc_period[5])
        prev_q = f"{y if q > 1 else y - 1}Q{q - 1 if q > 1 else 4}"
        prev_y = f"{y - 1}Q{q}"

        reg_cond = ""
        reg_params = [prev_q, prev_y]
        if region:
            if is_county:
                reg_cond = " AND county_name = ?"
            else:
                reg_cond = " AND full_district = ?"
            reg_params.append(region)
        elif viewport_districts:
            vp_ph = ",".join("?" for _ in viewport_districts)
            reg_cond = f" AND full_district IN ({vp_ph})"
            reg_params.extend(viewport_districts)

        road_prev_map = {}
        cur.execute(f"""
            SELECT full_district, road, period, tx_count, avg_unit_price
            FROM agg_road_quarter
            WHERE period IN (?, ?){reg_cond}
        """, reg_params)
        for row in cur.fetchall():
            road_prev_map[(row[0], row[1], row[2])] = {"tx_count": row[3], "avg_price": row[4]}

        proj_prev_map = {}
        cur.execute(f"""
            SELECT full_district, spot_name, period, tx_count, avg_unit_price
            FROM agg_spot_quarter
            WHERE period IN (?, ?){reg_cond} AND spot_type = 'project'
        """, reg_params)
        for row in cur.fetchall():
            proj_prev_map[(row[0], row[1], row[2])] = {"tx_count": row[3], "avg_price": row[4]}

        for h in hotspots:
            if h["type"] == "project":
                pq_data = proj_prev_map.get((h["full_district"], h["name"], prev_q)) or road_prev_map.get((h["full_district"], h.get("road"), prev_q))
                py_data = proj_prev_map.get((h["full_district"], h["name"], prev_y)) or road_prev_map.get((h["full_district"], h.get("road"), prev_y))
            else:
                pq_data = road_prev_map.get((h["full_district"], h.get("road"), prev_q))
                py_data = road_prev_map.get((h["full_district"], h.get("road"), prev_y))

            if pq_data and pq_data["tx_count"] > 0 and pq_data["avg_price"] > 0:
                h["qoq_tx_change"] = round((h["tx_count"] - pq_data["tx_count"]) / pq_data["tx_count"] * 100, 2)
                h["qoq_price_change"] = round((h["avg_unit_price"] - pq_data["avg_price"]) / pq_data["avg_price"] * 100, 2)

            if py_data and py_data["tx_count"] > 0 and py_data["avg_price"] > 0:
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

    if min_age is None and max_age is None:
        where_agg = ["(road LIKE '%路%' OR road LIKE '%街%' OR road LIKE '%大道%' OR road LIKE '%巷%')"]
        params = []
        is_range = bool(start_period and end_period and start_period != end_period)
        if is_range:
            where_agg.append("period >= ? AND period <= ?")
            params.extend([start_period, end_period])
        elif period or start_period:
            where_agg.append("period = ?")
            params.append(period or start_period)

        if region:
            where_agg.append("(county_name = ? OR full_district = ?)")
            params.extend([region, region])

        if not is_range:
            query = f"""
                SELECT road, full_district, county_name, district,
                       tx_count, avg_unit_price, avg_total_price,
                       min_unit_price, max_unit_price, projects
                FROM agg_road_quarter
                WHERE {" AND ".join(where_agg)}
                ORDER BY tx_count DESC
                LIMIT ?
            """
        else:
            if not region:
                where_agg.append("tx_count >= 3")
            query = f"""
                SELECT road, full_district, MAX(county_name) as county_name, MAX(district) as district,
                       SUM(tx_count) as tx_count,
                       ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                       ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_total_price,
                       ROUND(MIN(min_unit_price), 2) as min_unit_price,
                       ROUND(MAX(max_unit_price), 2) as max_unit_price,
                       MAX(projects) as projects
                FROM agg_road_quarter
                WHERE {" AND ".join(where_agg)}
                GROUP BY full_district, road
                ORDER BY tx_count DESC
                LIMIT ?
            """
        params.append(limit)
        rows = cur.execute(query, params).fetchall()
    else:
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
        proj_list = list(dict.fromkeys([p.strip() for p in proj_str.split(",") if p.strip() and p.strip() != "--請選擇--"]))
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

    if min_age is None and max_age is None:
        where_agg = [
            "project_name != '' AND project_name IS NOT NULL",
            "project_name NOT LIKE '%--%'",
            "project_name NOT LIKE '%?%'"
        ]
        params = []
        is_range = bool(start_period and end_period and start_period != end_period)
        if is_range:
            where_agg.append("period >= ? AND period <= ?")
            params.extend([start_period, end_period])
        elif period or start_period:
            where_agg.append("period = ?")
            params.append(period or start_period)

        if region:
            where_agg.append("(county_name = ? OR full_district = ?)")
            params.extend([region, region])

        if is_range and not region:
            where_agg.append("tx_count >= 2")

        query = f"""
            SELECT project_name, full_district,
                   MAX(county_name) as county_name,
                   MAX(district) as district,
                   MAX(road) as road,
                   MAX(door_addr) as door_addr,
                   MAX(spot_name) as spot_name,
                   SUM(tx_count) as tx_count,
                   ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                   ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_total_price,
                   ROUND(MIN(min_unit_price), 2) as min_unit_price,
                   ROUND(MAX(max_unit_price), 2) as max_unit_price,
                   MAX(building_type) as building_type,
                   ROUND(AVG(avg_age), 1) as avg_age,
                   MAX(build_year) as build_year,
                   ROUND(AVG(avg_building_ping), 2) as avg_building_ping,
                   ROUND(AVG(avg_land_ping), 2) as avg_land_ping,
                   SUM(COALESCE(has_parking_count, 0)) as has_parking_count,
                   MAX(berth_type) as berth_type
            FROM agg_spot_quarter
            WHERE {" AND ".join(where_agg)}
            GROUP BY project_name, full_district
            ORDER BY tx_count DESC
            LIMIT ?
        """
        params.append(limit)
        rows = cur.execute(query, params).fetchall()
    else:
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
            SELECT project_name, full_district, county_name, district, road, '' as door_addr,
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
        d["name"] = d.get("spot_name") or d["project_name"]
        results.append(d)

    results = batch_resolve_coordinates(results, centroids)
    for d in results:
        d["name"] = d["project_name"]
    return {"projects": results}


@app.get("/api/community/trend")
def get_community_trend(
    region: Optional[str] = Query(None),
    project_name: Optional[str] = Query(None),
    spot_name: Optional[str] = Query(None),
    road: Optional[str] = Query(None),
    door_addr: Optional[str] = Query(None),
):
    """
    Returns comprehensive historical quarterly price & volume trend, net unit price (excluding parking),
    and same-unit repeat sales profit analysis for a building project / community / door spot.
    """
    conn = get_connection()
    cur = conn.cursor()

    comm_name = (project_name or "").strip()
    s_name = (spot_name or "").strip()
    rd = (road or "").strip()
    d_addr = (door_addr or "").strip()
    f_dist = (region or "").strip()

    # 1. Resolve community_name from community_master if not explicitly given
    if not comm_name and f_dist and (s_name or (rd and d_addr)):
        lookup_door = s_name if s_name else (rd + d_addr)
        row_cm = cur.execute("""
            SELECT community_name, road FROM community_master
            WHERE full_district = ? AND road_door = ?
            LIMIT 1
        """, (f_dist, lookup_door)).fetchone()
        if row_cm:
            comm_name = row_cm[0] or ""
            if not rd and row_cm[1]:
                rd = row_cm[1]

    # Also check agg_spot_quarter if project_name was populated there
    spot_byear = None
    spot_btype = ""
    if not comm_name and f_dist and s_name:
        row_asq = cur.execute("""
            SELECT MAX(project_name), MAX(road), MAX(door_addr), MAX(build_year), MAX(building_type)
            FROM agg_spot_quarter
            WHERE full_district = ? AND spot_name = ?
        """, (f_dist, s_name)).fetchone()
        if row_asq:
            if row_asq[0]:
                comm_name = row_asq[0]
            if not rd and row_asq[1]:
                rd = row_asq[1]
            if not d_addr and row_asq[2]:
                d_addr = row_asq[2]
            spot_byear = row_asq[3]
            spot_btype = row_asq[4] or ""

    # On-demand real-time 591 community lookup if still unmapped apartment/mansion door
    if not comm_name and f_dist and s_name and ("大樓" in spot_btype or "華廈" in spot_btype):
        try:
            from scripts.backfill_unmapped_buildings import lookup_door_on_591
            m_c = re.match(r"^(.+?[市縣])(.+)$", f_dist)
            c_nm = m_c.group(1) if m_c else f_dist[:3]
            d_nm = m_c.group(2) if m_c else ""
            discovered = lookup_door_on_591(c_nm, d_nm, s_name)
            if discovered:
                comm_name = discovered
                # Save for this spot and all sibling doors sharing (full_district, road, build_year)
                if spot_byear and rd:
                    sibs = cur.execute("""
                        SELECT DISTINCT spot_name, door_addr FROM agg_spot_quarter
                        WHERE full_district = ? AND road = ? AND build_year = ? AND spot_type = 'door'
                    """, (f_dist, rd, spot_byear)).fetchall()
                else:
                    sibs = [(s_name, d_addr)]
                for sib_sname, sib_daddr in sibs:
                    cur.execute("""
                        INSERT OR REPLACE INTO community_master
                        (full_district, county_name, district, road, door_no, road_door, community_name, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, '591_ondemand')
                    """, (f_dist, c_nm, d_nm, rd, sib_daddr or "", sib_sname, comm_name))
                if spot_byear and rd:
                    cur.execute("""
                        UPDATE agg_spot_quarter SET project_name = ?
                        WHERE full_district = ? AND road = ? AND build_year = ? AND (project_name IS NULL OR project_name = '')
                    """, (comm_name, f_dist, rd, spot_byear))
                else:
                    cur.execute("""
                        UPDATE agg_spot_quarter SET project_name = ?
                        WHERE full_district = ? AND spot_name = ?
                    """, (comm_name, f_dist, s_name))
                conn.commit()
                _LOCATION_HOTSPOTS_CACHE.clear()
        except Exception:
            pass

    # 2. Collect all doors / spot_names belonging to this community
    door_nos = set()
    road_doors = set()
    roads_set = set()
    if rd:
        roads_set.add(rd)
    if d_addr:
        door_nos.add(d_addr)
    if s_name and s_name.endswith("號"):
        road_doors.add(s_name)

    if comm_name:
        cm_sql = "SELECT full_district, road, door_no, road_door FROM community_master WHERE community_name = ?"
        cm_params = [comm_name]
        if f_dist:
            cm_sql += " AND (full_district = ? OR county_name = ?)"
            cm_params.extend([f_dist, f_dist])
        for r_fdist, r_rd, r_dno, r_rdoor in cur.execute(cm_sql, cm_params).fetchall():
            if not f_dist:
                f_dist = r_fdist
            if r_rd:
                roads_set.add(r_rd)
            if r_dno:
                door_nos.add(r_dno)
            if r_rdoor:
                road_doors.add(r_rdoor)

    # 3. Query quarterly series from agg_spot_quarter
    if comm_name:
        q_sql = """
            SELECT period,
                   SUM(tx_count) as tx_count,
                   ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                   ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_total_price,
                   ROUND(MIN(min_unit_price), 2) as min_unit_price,
                   ROUND(MAX(max_unit_price), 2) as max_unit_price,
                   MAX(building_type) as building_type,
                   ROUND(AVG(avg_age), 1) as avg_age,
                   MAX(build_year) as build_year,
                   ROUND(AVG(avg_building_ping), 2) as avg_building_ping,
                   ROUND(AVG(avg_land_ping), 2) as avg_land_ping,
                   SUM(COALESCE(has_parking_count, 0)) as has_parking_count,
                   MAX(berth_type) as berth_type,
                   GROUP_CONCAT(DISTINCT spot_name) as spots_str,
                   MAX(full_district) as fdist,
                   MAX(road) as mroad
            FROM agg_spot_quarter
            WHERE (project_name = ? OR spot_name = ?)
        """
        q_params = [comm_name, comm_name]
        if f_dist:
            q_sql += " AND (full_district = ? OR county_name = ?)"
            q_params.extend([f_dist, f_dist])
        q_sql += " GROUP BY period ORDER BY period ASC"
        q_rows = cur.execute(q_sql, q_params).fetchall()
    else:
        target_spot = s_name or (rd + d_addr) or rd
        q_sql = """
            SELECT period,
                   SUM(tx_count) as tx_count,
                   ROUND(SUM(avg_unit_price * tx_count) / NULLIF(SUM(tx_count), 0), 2) as avg_unit_price,
                   ROUND(SUM(avg_total_price * tx_count) / NULLIF(SUM(tx_count), 0), 1) as avg_total_price,
                   ROUND(MIN(min_unit_price), 2) as min_unit_price,
                   ROUND(MAX(max_unit_price), 2) as max_unit_price,
                   MAX(building_type) as building_type,
                   ROUND(AVG(avg_age), 1) as avg_age,
                   MAX(build_year) as build_year,
                   ROUND(AVG(avg_building_ping), 2) as avg_building_ping,
                   ROUND(AVG(avg_land_ping), 2) as avg_land_ping,
                   SUM(COALESCE(has_parking_count, 0)) as has_parking_count,
                   MAX(berth_type) as berth_type,
                   GROUP_CONCAT(DISTINCT spot_name) as spots_str,
                   MAX(full_district) as fdist,
                   MAX(road) as mroad
            FROM agg_spot_quarter
            WHERE spot_name = ?
        """
        q_params = [target_spot]
        if f_dist:
            q_sql += " AND (full_district = ? OR county_name = ?)"
            q_params.extend([f_dist, f_dist])
        q_sql += " GROUP BY period ORDER BY period ASC"
        q_rows = cur.execute(q_sql, q_params).fetchall()

    quarters = []
    building_type = ""
    build_year = None
    avg_age = None
    avg_b_ping = None
    avg_l_ping = None
    total_tx_agg = 0
    total_parking_agg = 0
    berth_type_str = ""

    for r in q_rows:
        d = dict(r)
        if not f_dist and d.get("fdist"):
            f_dist = d["fdist"]
        if not rd and d.get("mroad"):
            rd = d["mroad"]
            roads_set.add(rd)
        if d.get("spots_str"):
            for sp in d["spots_str"].split(","):
                if sp.endswith("號"):
                    road_doors.add(sp)
        if d.get("building_type"):
            building_type = d["building_type"]
        if d.get("build_year"):
            build_year = d["build_year"]
        if d.get("avg_age") is not None:
            avg_age = d["avg_age"]
        if d.get("avg_building_ping"):
            avg_b_ping = d["avg_building_ping"]
        if d.get("avg_land_ping"):
            avg_l_ping = d["avg_land_ping"]
        if d.get("berth_type"):
            berth_type_str = d["berth_type"]
        total_tx_agg += d["tx_count"] or 0
        total_parking_agg += d["has_parking_count"] or 0
        quarters.append({
            "period": d["period"],
            "tx_count": d["tx_count"],
            "avg_unit_price": d["avg_unit_price"],
            "net_unit_price": d["avg_unit_price"],
            "min_unit_price": d["min_unit_price"],
            "max_unit_price": d["max_unit_price"],
            "avg_total_price": d["avg_total_price"],
        })

    # 4. Fetch raw transactions for this community/building to compute:
    #    a) Net Unit Price excluding parking (扣除車位後之純建坪單價)
    #    b) Same-Unit Repeat Sales (同戶歷次轉手獲利紀錄)
    #    c) Detailed transaction list
    tx_rows = []
    tx_conditions = []
    tx_params = []
    if comm_name:
        tx_conditions.append("project_name = ?")
        tx_params.append(comm_name)
    if roads_set:
        r_list = list(roads_set)
        ph = ",".join("?" for _ in r_list)
        if f_dist:
            tx_conditions.append(f"(road IN ({ph}) AND (full_district = ? OR county_name = ?))")
            tx_params.extend(r_list + [f_dist, f_dist])
        else:
            tx_conditions.append(f"road IN ({ph})")
            tx_params.extend(r_list)

    if tx_conditions:
        tx_sql = f"""
            SELECT id, trade_date, trade_quarter, full_district, road, project_name, address,
                   target_type, building_type, floor, total_floors,
                   building_area_ping, land_area_ping, total_price_wan, unit_price_wan_ping,
                   room_count, hall_count, bath_count, berth_category, berth_area_ping, berth_price,
                   notes, is_special_trade, build_year, building_age
            FROM transactions
            WHERE ({" OR ".join(tx_conditions)})
              AND target_type != '土地'
              AND unit_price_wan_ping > 0
            ORDER BY trade_date ASC
        """
        cand_rows = cur.execute(tx_sql, tx_params).fetchall()
        for r in cand_rows:
            tr = dict(r)
            tr["address"] = normalize_address_numerals(tr.get("address") or "")
            tr["floor"] = normalize_floor_str(tr.get("floor") or "")
            tr["total_floors"] = normalize_floor_str(tr.get("total_floors") or "")
            tr_proj = (tr.get("project_name") or "").strip()
            clean_addr = tr["address"]
            d_str = extract_door_str(clean_addr)
            r_door = (tr.get("road") or "") + d_str if d_str else ""

            matched = False
            if comm_name and tr_proj == comm_name:
                matched = True
            elif r_door and r_door in road_doors:
                matched = True
            elif d_str and d_str in door_nos:
                matched = True
            elif not door_nos and not road_doors and s_name and s_name in clean_addr:
                matched = True

            if matched:
                if comm_name and not tr_proj:
                    tr["project_name"] = comm_name
                tr["door_str"] = d_str
                tr["road_door"] = r_door
                # Compute net unit price excluding parking
                b_ping = tr.get("building_area_ping") or 0.0
                p_ping = tr.get("berth_area_ping") or 0.0
                tot_wan = tr.get("total_price_wan") or 0.0
                p_wan = round((tr.get("berth_price") or 0) / 10000.0, 2)
                if p_wan > 0 and p_ping > 0 and (b_ping - p_ping) > 3.0 and (tot_wan - p_wan) > 0:
                    net_u = round((tot_wan - p_wan) / (b_ping - p_ping), 2)
                else:
                    net_u = tr.get("unit_price_wan_ping") or 0.0
                tr["net_unit_price"] = net_u
                tr["berth_price_wan"] = p_wan
                tx_rows.append(tr)

    conn.close()

    # Update quarterly net_unit_price from matched transactions
    if tx_rows:
        q_net_map = {}
        for tr in tx_rows:
            if tr.get("is_special_trade") == 1 and len(tx_rows) > 3:
                continue
            qp = tr.get("trade_quarter")
            if qp and tr.get("net_unit_price"):
                q_net_map.setdefault(qp, []).append(tr["net_unit_price"])
        for q_item in quarters:
            vals = q_net_map.get(q_item["period"])
            if vals:
                q_item["net_unit_price"] = round(sum(vals) / len(vals), 2)

    # Detect Repeat Sales (同戶歷次轉手獲利)
    repeat_sales = []
    unit_groups = {}
    for tr in tx_rows:
        d_str = tr.get("door_str") or ""
        fl = (tr.get("floor") or "").strip()
        if not d_str or not fl or fl in ("全", "全棟", "見其他登記事項"):
            continue
        # Preserve sub-unit suffix (e.g. 10樓之2) from normalized address if present
        m_sub = re.search(r'(\d+樓之\d+)', tr.get("address") or "")
        fl_key = m_sub.group(1) if m_sub else fl
        b_bucket = round((tr.get("building_area_ping") or 0) / 3.0)
        u_key = (tr.get("road_door") or d_str, fl_key, b_bucket)
        unit_groups.setdefault(u_key, []).append(tr)

    for (r_door, fl, _), u_txs in unit_groups.items():
        if len(u_txs) < 2:
            continue
        # Deduplicate same-month filings
        dedup = [u_txs[0]]
        for t in u_txs[1:]:
            prev_t = dedup[-1]
            if t["trade_date"][:7] == prev_t["trade_date"][:7] and abs(t["total_price_wan"] - prev_t["total_price_wan"]) < 10:
                continue
            dedup.append(t)
        if len(dedup) < 2:
            continue
        for i in range(len(dedup) - 1):
            t1 = dedup[i]
            t2 = dedup[i + 1]
            if t1["total_price_wan"] <= 0 or t2["total_price_wan"] <= 0:
                continue
            profit_wan = round(t2["total_price_wan"] - t1["total_price_wan"], 1)
            profit_pct = round(profit_wan / t1["total_price_wan"] * 100.0, 1)
            # Calculate holding years
            try:
                y1, m1 = int(t1["trade_date"][:4]), int(t1["trade_date"][5:7])
                y2, m2 = int(t2["trade_date"][:4]), int(t2["trade_date"][5:7])
                hold_years = round(max(0.1, (y2 - y1) + (m2 - m1) / 12.0), 1)
            except Exception:
                hold_years = None
            repeat_sales.append({
                "unit_label": f"{r_door} · {fl}",
                "road_door": r_door,
                "floor": fl,
                "area_ping": t2.get("building_area_ping"),
                "prev_date": t1["trade_date"],
                "prev_quarter": t1["trade_quarter"],
                "prev_total_wan": t1["total_price_wan"],
                "prev_unit_price": t1["unit_price_wan_ping"],
                "curr_date": t2["trade_date"],
                "curr_quarter": t2["trade_quarter"],
                "curr_total_wan": t2["total_price_wan"],
                "curr_unit_price": t2["unit_price_wan_ping"],
                "hold_years": hold_years,
                "profit_wan": profit_wan,
                "profit_pct": profit_pct,
            })

    repeat_sales.sort(key=lambda x: x["curr_date"], reverse=True)

    # Summary KPIs
    first_q = quarters[0] if quarters else None
    latest_q = quarters[-1] if quarters else None
    peak_q = max(quarters, key=lambda x: x["avg_unit_price"]) if quarters else None
    all_time_change_pct = 0.0
    if first_q and latest_q and first_q["avg_unit_price"] and first_q["avg_unit_price"] > 0:
        all_time_change_pct = round(
            (latest_q["avg_unit_price"] - first_q["avg_unit_price"]) / first_q["avg_unit_price"] * 100.0, 1
        )

    total_floors = ""
    for tr in reversed(tx_rows):
        if tr.get("total_floors"):
            total_floors = tr["total_floors"]
            break
    if build_year and avg_age is None:
        avg_age = float(2026 - int(build_year))

    display_name = comm_name or s_name or (rd + d_addr) or rd or "未知建案"
    doors_sorted = sorted(list(road_doors)) if road_doors else ([s_name] if s_name else [])

    return {
        "community_name": comm_name,
        "display_name": display_name,
        "region": f_dist,
        "road": rd,
        "doors": doors_sorted[:30],
        "door_count": len(doors_sorted),
        "building_type": building_type,
        "build_year": build_year,
        "avg_age": avg_age,
        "total_floors": total_floors,
        "summary": {
            "total_tx": max(total_tx_agg, len(tx_rows)),
            "first_period": first_q["period"] if first_q else None,
            "first_avg_price": first_q["avg_unit_price"] if first_q else None,
            "latest_period": latest_q["period"] if latest_q else None,
            "latest_avg_price": latest_q["avg_unit_price"] if latest_q else None,
            "latest_net_price": latest_q["net_unit_price"] if latest_q else None,
            "all_time_change_pct": all_time_change_pct,
            "peak_period": peak_q["period"] if peak_q else None,
            "peak_avg_price": peak_q["avg_unit_price"] if peak_q else None,
            "avg_building_ping": avg_b_ping,
            "avg_land_ping": avg_l_ping,
            "has_parking_count": total_parking_agg,
            "berth_type": berth_type_str,
            "repeat_sales_count": len(repeat_sales),
        },
        "quarters": quarters,
        "repeat_sales": repeat_sales[:50],
        "transactions": list(reversed(tx_rows))[:80],
    }


@app.post("/api/community/tag")
def tag_community_name(
    region: str = Query(...),
    spot_name: str = Query(...),
    community_name: str = Query(...),
    road: Optional[str] = Query(""),
    door_addr: Optional[str] = Query(""),
):
    """
    Allows the user to manually label or update a spot's community/project name,
    persisting it in community_master and agg_spot_quarter immediately.
    """
    cname_clean = community_name.strip()
    if not cname_clean:
        raise HTTPException(status_code=400, detail="建案名稱不可為空")

    conn = get_connection()
    cur = conn.cursor()

    # Extract county_name and district from region
    m = re.match(r"^(.+?[市縣])(.+)$", region.strip())
    county_nm = m.group(1) if m else region
    dist_nm = m.group(2) if m else ""
    d_no = door_addr.strip() or extract_door_str(spot_name)
    rd = road.strip() or spot_name.replace(d_no, "")
    r_door = spot_name.strip()

    cur.execute("""
        INSERT OR REPLACE INTO community_master
        (full_district, county_name, district, road, door_no, road_door, community_name, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
    """, (region.strip(), county_nm, dist_nm, rd, d_no, r_door, cname_clean))

    cur.execute("""
        UPDATE agg_spot_quarter
        SET project_name = ?
        WHERE full_district = ? AND spot_name = ?
    """, (cname_clean, region.strip(), r_door))
    updated_rows = cur.rowcount
    conn.commit()
    conn.close()

    _LOCATION_HOTSPOTS_CACHE.clear()
    return {
        "status": "ok",
        "region": region,
        "spot_name": r_door,
        "community_name": cname_clean,
        "updated_quarters": updated_rows,
    }


_STATUS_CACHE: Dict[str, Any] = {}


@app.get("/api/crawler/status")
def get_crawler_status():
    """Returns database size, record count, completed seasons, and crawler background task status in <1ms."""
    if not _STATUS_CACHE:
        conn = get_connection()
        cur = conn.cursor()
        total_tx = cur.execute("SELECT SUM(tx_count) FROM agg_county_quarter").fetchone()[0] or 5447412
        total_periods = cur.execute("SELECT COUNT(DISTINCT period) FROM agg_county_quarter").fetchone()[0]
        completed_seasons = list_completed_seasons()
        conn.close()
        _STATUS_CACHE["total_tx"] = total_tx
        _STATUS_CACHE["total_periods"] = total_periods
        _STATUS_CACHE["completed_seasons"] = completed_seasons
        _STATUS_CACHE["last_tx"] = "2026-06-28"

    db_size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0.0
    from datetime import datetime
    app_js = STATIC_DIR / "app.js"
    latest_mtime = max(
        Path(__file__).stat().st_mtime,
        app_js.stat().st_mtime if app_js.exists() else 0
    )
    sys_updated = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "total_transactions": _STATUS_CACHE["total_tx"],
        "total_periods": _STATUS_CACHE["total_periods"],
        "completed_seasons_count": len(_STATUS_CACHE["completed_seasons"]),
        "completed_seasons": _STATUS_CACHE["completed_seasons"],
        "latest_transaction_date": _STATUS_CACHE["last_tx"],
        "database_size_mb": db_size_mb,
        "crawler_running": crawler_task_state["is_running"],
        "last_crawl_result": crawler_task_state["last_result"],
        "system_updated_at": sys_updated,
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
