import re
import zipfile
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np

from config import (
    COUNTY_CODE_MAP,
    PING_FACTOR,
    SQM_PER_PING,
    SPECIAL_TRADE_KEYWORDS,
)
from geo.district_resolver import resolve_district

logger = logging.getLogger(__name__)


def parse_roc_date(val) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """
    Parses Taiwan ROC date (e.g. 1010815 or 991201 or float 1010815.0).
    Returns (iso_date 'YYYY-MM-DD', trade_year, trade_quarter 'YYYYQ#', trade_month 'YYYY-MM').
    """
    if pd.isna(val):
        return None, None, None, None

    try:
        s = str(int(float(val))).strip()
        if len(s) < 6 or len(s) > 7:
            return None, None, None, None

        roc_year = int(s[:-4])
        month = int(s[-4:-2])
        day = int(s[-2:])

        western_year = roc_year + 1911
        current_year = datetime.now().year

        # Handle obvious typing errors in ROC government data where future years were keyed in
        if western_year > current_year:
            if roc_year >= 119:
                roc_year -= 10
                western_year = roc_year + 1911
            elif roc_year in (116, 117):
                roc_year -= 4
                western_year = roc_year + 1911

        if not (1990 <= western_year <= current_year and 1 <= month <= 12 and 1 <= day <= 31):
            return None, None, None, None

        quarter = (month - 1) // 3 + 1
        iso_date = f"{western_year:04d}-{month:02d}-{day:02d}"
        trade_quarter = f"{western_year}Q{quarter}"
        trade_month = f"{western_year}-{month:02d}"

        return iso_date, western_year, trade_quarter, trade_month
    except Exception:
        return None, None, None, None


def check_special_trade(notes: Optional[str]) -> int:
    """Checks if the transaction note indicates non-arms-length/special deal."""
    if not notes or pd.isna(notes):
        return 0
    note_str = str(notes)
    for kw in SPECIAL_TRADE_KEYWORDS:
        if kw in note_str:
            return 1
    return 0


def clean_number(val, default=0.0) -> float:
    """Safely converts string or float value to float."""
    if pd.isna(val):
        return default
    try:
        return float(val)
    except Exception:
        return default


def extract_road(addr: str) -> str:
    """
    Extracts road / street name from Taiwan address.
    e.g. '臺北市大安區忠孝東路四段211~240號' -> '忠孝東路四段'
    e.g. '新北市板橋區文化路二段182巷5號' -> '文化路二段'
    e.g. '高雄市前金區市中一路234巷16號' -> '市中一路'
    """
    if not addr:
        return ""
    # Strip administrative prefixes (e.g. 臺北市大安區, 高雄市前金區, 新竹縣竹東鎮)
    clean = re.sub(r'^(?:臺灣省|福建省)?(?:[^市縣]+?[市縣])?(?:[^區鄉鎮市]+?[區鄉鎮市])?', '', addr)
    m = re.search(r'([^\d段巷弄號里鄰]+?(?:路|街|大道)(?:[一二三四五六七八九十]+段)?)', clean)
    if m:
        return m.group(1).strip()
    m2 = re.search(r'([^\d段巷弄號里區鄰市縣鄉鎮]+?(?:路|街|大道)(?:[一二三四五六七八九十]+段)?)', addr)
    if m2:
        return m2.group(1).strip()
    m_sect = re.search(r'([^號地]+?段)', addr)
    if m_sect:
        c_sect = re.sub(r'^.*?(?:市|縣|區|鄉|鎮)', '', m_sect.group(1)).strip()
        return c_sect if c_sect else m_sect.group(1).strip()
    return ""


def extract_project(row_data: dict, addr: str, notes: str) -> str:
    """
    Extracts building project / development / community name.
    1. Direct MOI '建案名稱' / '社區名稱' column (pre-sales and modern filings)
    2. Notes matching community keywords
    3. Address brackets containing community names
    """
    # 1. Direct column in pre-sales
    for col in ["建案名稱", "社區名稱", "建案", "社區"]:
        val = row_data.get(col)
        if val is not None and pd.notnull(val) and str(val).strip() and str(val) != "nan":
            return str(val).strip()

    # 2. Extract from notes
    if notes:
        m = re.search(r'(?:建案|社區|大樓|建案名稱|社區名稱)[:：]?\s*([^\s,，。;；()（）]+)', notes)
        if m:
            return m.group(1).strip()

    # 3. Extract from address brackets
    if addr:
        m_bkt = re.search(r'[（(【「]([^）)】」]+?(?:社區|大樓|大廈|華廈|特區|莊園|山莊|別墅|名邸|城堡|園區|建案))[）)】」]', addr)
        if m_bkt:
            return m_bkt.group(1).strip()

    return ""


def parse_csv_file(csv_file_obj, county_code: str, season_code: str) -> pd.DataFrame:
    """
    Parses a single county CSV file (from _lvr_land_a.csv or _lvr_land_b.csv).
    """
    county_name = COUNTY_CODE_MAP.get(county_code, f"未知縣市({county_code})")

    try:
        df = pd.read_csv(csv_file_obj, skiprows=[1], encoding="utf-8-sig", low_memory=False)
    except Exception as e:
        logger.warning(f"Error reading CSV for county {county_code}: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Normalize column names: strip spaces and parentheses
    clean_cols = {}
    for c in df.columns:
        clean_name = str(c).strip().replace("（", "(").replace("）", ")")
        clean_cols[c] = clean_name
    df = df.rename(columns=clean_cols)

    # Required columns check
    if "鄉鎮市區" not in df.columns or "總價元" not in df.columns:
        return pd.DataFrame()

    records = []
    col_serial = "編號" if "編號" in df.columns else None
    col_target = "交易標的" if "交易標的" in df.columns else None
    col_district = "鄉鎮市區"
    col_addr = "土地位置建物門牌" if "土地位置建物門牌" in df.columns else None
    col_land_area = "土地移轉總面積平方公尺" if "土地移轉總面積平方公尺" in df.columns else None
    col_zoning = "都市土地使用分區" if "都市土地使用分區" in df.columns else None
    col_date = "交易年月日" if "交易年月日" in df.columns else None
    col_floor = "移轉層次" if "移轉層次" in df.columns else None
    col_total_floors = "總樓層數" if "總樓層數" in df.columns else None
    col_btype = "建物型態" if "建物型態" in df.columns else None
    col_use = "主要用途" if "主要用途" in df.columns else None
    col_b_area = "建物移轉總面積平方公尺" if "建物移轉總面積平方公尺" in df.columns else None
    col_room = "建物現況格局-房" if "建物現況格局-房" in df.columns else None
    col_hall = "建物現況格局-廳" if "建物現況格局-廳" in df.columns else None
    col_bath = "建物現況格局-衛" if "建物現況格局-衛" in df.columns else None
    col_mgmt = "有無管理組織" if "有無管理組織" in df.columns else None
    col_tot_price = "總價元"
    col_u_price = "單價元平方公尺" if "單價元平方公尺" in df.columns else None
    col_berth_cat = "車位類別" if "車位類別" in df.columns else None

    col_berth_area = None
    for cand in ["車位移轉總面積(平方公尺)", "車位移轉總面積平方公尺"]:
        if cand in df.columns:
            col_berth_area = cand
            break

    col_berth_price = "車位總價元" if "車位總價元" in df.columns else None
    col_notes = "備註" if "備註" in df.columns else None
    col_elevator = "電梯" if "電梯" in df.columns else None
    col_build_date = "建築完成年月" if "建築完成年月" in df.columns else None

    for _, row in df.iterrows():
        district = str(row[col_district]).strip() if pd.notnull(row[col_district]) else ""
        if not district:
            continue

        raw_date = row.get(col_date) if col_date else None
        iso_date, trade_year, trade_quarter, trade_month = parse_roc_date(raw_date)
        if not trade_year:
            continue

        tot_price = clean_number(row.get(col_tot_price))
        if tot_price <= 0:
            continue
        tot_price_wan = round(tot_price / 10000.0, 2)

        # Areas in ping
        b_area_sqm = clean_number(row.get(col_b_area)) if col_b_area else 0.0
        b_area_ping = round(b_area_sqm * PING_FACTOR, 2)

        land_area_sqm = clean_number(row.get(col_land_area)) if col_land_area else 0.0
        land_area_ping = round(land_area_sqm * PING_FACTOR, 2)

        berth_area_sqm = clean_number(row.get(col_berth_area)) if col_berth_area else 0.0
        berth_area_ping = round(berth_area_sqm * PING_FACTOR, 2)

        # Unit price calculation
        u_price_sqm = clean_number(row.get(col_u_price)) if col_u_price else 0.0
        if u_price_sqm > 0:
            u_price_ping = round(u_price_sqm * SQM_PER_PING, 1)
        elif b_area_ping > 0:
            u_price_ping = round(tot_price / b_area_ping, 1)
        else:
            u_price_ping = 0.0

        u_price_wan_ping = round(u_price_ping / 10000.0, 2)

        addr_str = str(row.get(col_addr, "")) if col_addr and pd.notnull(row.get(col_addr)) else ""
        notes = str(row.get(col_notes, "")) if pd.notnull(row.get(col_notes)) else ""
        is_special = check_special_trade(notes)

        # Extract road and building project name
        road_name = extract_road(addr_str)
        proj_name = extract_project(row.to_dict(), addr_str, notes)

        # Building completion year and building age calculation
        build_date_raw = row.get(col_build_date) if col_build_date else None
        _, build_year, _, _ = parse_roc_date(build_date_raw)
        building_age = round(max(0.0, float(trade_year - build_year)), 1) if (build_year and trade_year) else None

        # Resolve real administrative district (e.g. 東區/北區/香山區 for 新竹市)
        resolved_dist, full_dist = resolve_district(county_name, district, addr_str, road_name)

        records.append({
            "serial_number": str(row.get(col_serial, "")) if col_serial else "",
            "season_code": season_code,
            "county_code": county_code,
            "county_name": county_name,
            "district": resolved_dist,
            "full_district": full_dist,
            "target_type": str(row.get(col_target, "")) if col_target else "",
            "address": addr_str,
            "land_area_ping": land_area_ping,
            "zoning": str(row.get(col_zoning, "")) if col_zoning else "",
            "trade_date": iso_date,
            "trade_year": trade_year,
            "trade_quarter": trade_quarter,
            "trade_month": trade_month,
            "floor": str(row.get(col_floor, "")) if col_floor else "",
            "total_floors": str(row.get(col_total_floors, "")) if col_total_floors else "",
            "building_type": str(row.get(col_btype, "")) if col_btype else "",
            "main_use": str(row.get(col_use, "")) if col_use else "",
            "building_area_ping": b_area_ping,
            "room_count": int(clean_number(row.get(col_room))),
            "hall_count": int(clean_number(row.get(col_hall))),
            "bath_count": int(clean_number(row.get(col_bath))),
            "has_management": str(row.get(col_mgmt, "")) if col_mgmt else "",
            "total_price": int(tot_price),
            "total_price_wan": tot_price_wan,
            "unit_price_ping": u_price_ping,
            "unit_price_wan_ping": u_price_wan_ping,
            "berth_category": str(row.get(col_berth_cat, "")) if col_berth_cat else "",
            "berth_area_ping": berth_area_ping,
            "berth_price": int(clean_number(row.get(col_berth_price))) if col_berth_price else 0,
            "notes": notes,
            "is_special_trade": is_special,
            "elevator": str(row.get(col_elevator, "")) if col_elevator else "",
            "road": road_name,
            "project_name": proj_name,
            "build_year": build_year,
            "building_age": building_age,
        })

    return pd.DataFrame(records)


def parse_zip_archive(zip_path: Path, season_code: str) -> pd.DataFrame:
    """
    Opens a MOI zip file, iterates both [a-z]_lvr_land_a.csv (買賣)
    and [a-z]_lvr_land_b.csv (預售屋買賣), parses each and concatenates into a single DataFrame.
    """
    if not zip_path.exists():
        logger.error(f"Zip file {zip_path} not found.")
        return pd.DataFrame()

    dfs = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Include both real estate sales (_a.csv) and pre-sale projects (_b.csv)
            file_list = [
                f for f in zf.namelist()
                if f.lower().endswith("_lvr_land_a.csv") or f.lower().endswith("_lvr_land_b.csv")
            ]
            for filename in sorted(file_list):
                county_code = filename[0].upper()
                with zf.open(filename) as f:
                    df_county = parse_csv_file(f, county_code, season_code)
                    if not df_county.empty:
                        dfs.append(df_county)
    except Exception as e:
        logger.error(f"Failed to read zip {zip_path}: {e}")
        return pd.DataFrame()

    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return pd.DataFrame()
