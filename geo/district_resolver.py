"""
District Resolver for Taiwan Real Estate Data.
Resolves municipal-level anomalies in MOI data where provincial cities
(新竹市 and 嘉義市) are not subdivided in raw CSV files.
Includes door-number (門牌) and lane-number (巷) resolution for roads crossing district boundaries.
"""

import json
import os
import re
from typing import Tuple, Dict, Optional

# Numeral normalization map
NUM_MAP = {
    '一': '1', '二': '2', '三': '3', '四': '4', '五': '5',
    '六': '6', '七': '7', '八': '8', '九': '9', '十': '10',
    '１': '1', '２': '2', '３': '3', '４': '4', '５': '5',
    '６': '6', '７': '7', '８': '8', '９': '9', '０': '0'
}


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = text.replace('巿', '市').replace('台', '臺')
    for k, v in NUM_MAP.items():
        s = s.replace(k, v)
    return s


def extract_numbers(addr: str) -> Tuple[Optional[int], Optional[int]]:
    """Extracts (lane_number, door_number) from address."""
    na = normalize_text(addr)
    m_lane = re.search(r'(\d+)巷', na)
    lane = int(m_lane.group(1)) if m_lane else None

    m_range = re.search(r'(\d+)[~～\-](\d+)號', na)
    if m_range:
        door = (int(m_range.group(1)) + int(m_range.group(2))) // 2
    else:
        m_door = re.search(r'(\d+)號', na)
        door = int(m_door.group(1)) if m_door else None
    return lane, door


def resolve_hsinchu_multi_road(road: str, addr: str) -> Optional[str]:
    """Resolves roads crossing administrative districts in Hsinchu City by door numbers."""
    lane, door = extract_numbers(addr)
    combined = road + " " + addr

    # 1. 中山路: No. 1 to 498 & lanes < 500 are 北區 (City Hall, Chenghuang Temple, e.g. 中山路42號).
    #            Lanes >= 500 or door >= 500 are 香山區 (e.g. 中山路640巷, 中山路642號).
    if "中山路" in combined:
        if lane is not None:
            return "香山區" if lane >= 500 else "北區"
        if door is not None:
            return "香山區" if door >= 500 else "北區"
        return "北區"

    # 2. 中正路: <= 107 is 東區 (Station / Dongmen Circle). > 107 is 北區 (City Hall No. 120, Beimen).
    if "中正路" in combined:
        if door is not None:
            return "東區" if door <= 107 else "北區"
        return "北區"

    # 3. 西大路: <= 288 is 東區 (FE21 / Baoshan). > 288 is 北區 (Jingguo / Zhuguang).
    if "西大路" in combined:
        if door is not None:
            return "東區" if door <= 288 else "北區"
        return "北區"

    # 4. 東大路: 1段 is 東區; 3段/4段 are 北區. 2段 <= 200 is 東區; > 200 is 北區.
    if "東大路" in combined:
        if "1段" in combined or "一段" in combined: return "東區"
        if any(k in combined for k in ["3段", "4段", "三段", "四段"]): return "北區"
        if door is not None:
            return "東區" if door <= 200 else "北區"
        return "北區"

    # 5. 南大路: <= 530 is 東區; > 530 is 香山區 (toward Chaiqiao).
    if "南大路" in combined:
        if door is not None:
            return "香山區" if door > 530 else "東區"
        return "東區"

    # 6. 中央路: >= 111 is 東區 (Big City mall at 229); <= 110 is 北區 (City Hall / Dongmen).
    if "中央路" in combined:
        if door is not None:
            return "東區" if door >= 111 else "北區"
        return "東區"

    # 7. 北大路: <= 38 is 東區; > 38 is 北區 (Performing Arts Center, Culture Bureau).
    if "北大路" in combined:
        if door is not None:
            return "東區" if door <= 38 else "北區"
        return "北區"

    # 8. 經國路: 1段 (even <= 300 東區, else 北區); 2段 is 北區; 3段 is 香山區.
    if "經國路3" in combined or "經國路三" in combined: return "香山區"
    if "經國路2" in combined or "經國路二" in combined: return "北區"
    if "經國路1" in combined or "經國路一" in combined:
        if door is not None:
            return "東區" if door % 2 == 0 and door <= 300 else "北區"
        return "北區"

    # 9. 柴橋路: <= 100 東區; > 100 香山區.
    if "柴橋路" in combined:
        if door is not None:
            return "東區" if door <= 100 else "香山區"
        return "香山區"

    # 10. 四維路: <= 70 東區; > 70 北區.
    if "四維路" in combined:
        if door is not None:
            return "東區" if door <= 70 else "北區"
        return "北區"

    # 11. 林森路: <= 100 東區; > 100 北區.
    if "林森路" in combined:
        if door is not None:
            return "東區" if door <= 100 else "北區"
        return "北區"

    # 12. 光華街 / 田美街: 100% 北區
    if "光華" in combined or "田美" in combined:
        return "北區"

    return None


def resolve_chiayi_multi_road(road: str, addr: str) -> Optional[str]:
    """Resolves roads crossing administrative districts in Chiayi City by door numbers."""
    lane, door = extract_numbers(addr)
    combined = road + " " + addr

    if "中山路" in combined:
        if door is not None:
            return "東區" if door <= 300 else "西區"
        return "西區"

    if "中正路" in combined:
        if door is not None:
            return "東區" if door <= 380 else "西區"
        return "西區"

    if "文化路" in combined:
        if door is not None:
            return "東區" if door % 2 == 0 and door <= 200 else "西區"
        return "西區"

    if "民族路" in combined:
        if door is not None:
            return "東區" if door <= 420 else "西區"
        return "西區"

    if "垂楊路" in combined:
        if door is not None:
            return "東區" if door <= 400 else "西區"
        return "西區"

    if "林森西路" in combined:
        if door is not None:
            return "東區" if door <= 250 else "西區"
        return "西區"

    if "民權路" in combined:
        if door is not None:
            return "東區" if door <= 280 else "西區"
        return "西區"

    return None


# Official cadastral sections (地籍地段名)
HSINCHU_SECTIONS = {
    # 東區
    '千甲': '東區', '水源': '東區', '復興': '東區', '光復': '東區', '新莊': '東區',
    '隆恩': '東區', '親仁': '東區', '東門': '東區', '東山': '東區', '柴梳山': '東區',
    '頂竹圍': '東區', '金山': '東區', '關東': '東區', '關西': '東區', '埔頂': '東區',
    '建功': '東區', '科學': '東區', '中華': '東區', '三民': '東區', '研發': '東區',
    '仙宮': '東區', '光明': '東區', '育賢': '東區', '文教': '東區', '竹蓮': '東區',
    '後甲': '東區', '綠水': '東區', '公園': '東區', '東勢': '東區', '東園': '東區',
    '忠孝': '東區', '清大': '東區', '交大': '東區', '赤土崎': '東區',
    # 北區
    '崙子': '北區', '舊社': '北區', '湳雅': '北區', '樹林頭': '北區', '民富': '北區',
    '武陵': '北區', '光華': '北區', '金雅': '北區', '福林': '北區', '苦苓腳': '北區',
    '烏樹林': '北區', '港北': '北區', '西門': '北區', '北門': '北區', '中山': '北區',
    '長和': '北區', '新民': '北區', '水田': '北區', '境福': '北區', '南寮': '北區',
    '浸水': '北區', '吉羊': '北區', '文雅': '北區', '磐石': '北區', '中正': '北區',
    # 香山區
    '柑林': '香山區', '樹下': '香山區', '香山': '香山區', '頂埔': '香山區', '頂寮': '香山區',
    '牛埔': '香山區', '大庄': '香山區', '茄苳': '香山區', '海山': '香山區', '鹽水': '香山區',
    '朝山': '香山區', '美山': '香山區', '內湖': '香山區', '中隘': '香山區', '南隘': '香山區',
    '新港': '香山區', '新興': '香山區', '港南': '香山區', '延平': '香山區', '大湖': '香山區',
    '虎山': '香山區', '香村': '香山區', '海埔': '香山區', '五福': '香山區', '景觀': '香山區'
}

CHIAYI_SECTIONS = {
    # 東區
    '盧厝': '東區', '盧東': '東區', '短竹': '東區', '長竹': '東區', '後庄': '東區',
    '頂庄': '東區', '彌陀': '東區', '崎頂': '東區', '東川': '東區', '林森': '東區',
    '檜村': '東區', '興村': '東區', '宣信': '東區', '新店': '東區', '義教': '東區',
    '芳草': '東區', '下路頭': '東區', '公館': '東區', '山子頂': '東區', '台斗坑': '東區',
    '大同': '東區', '朝陽': '東區', '民生': '東區', '後湖': '東區', '南門': '東區',
    '東門': '東區', '下路': '東區', '中庄': '東區', '太平': '東區', '雲霄': '東區',
    # 西區
    '竹圍子': '西區', '車店': '西區', '港子坪': '西區', '埤子頭': '西區', '北社尾': '西區',
    '同興': '西區', '劉厝': '西區', '荖藤': '西區', '北園': '西區', '白川': '西區',
    '玉川': '西區', '育人': '西區', '遠東': '西區', '大溪': '西區', '磚磘': '西區',
    '番社': '西區', '垂楊': '西區', '慶安': '西區', '永樂': '西區', '榮光': '西區',
    '湖子內': '西區', '下埤': '西區', '鳥岫': '西區', '下埤子': '西區', '竹子腳': '西區',
    '保生': '西區', '新西': '西區', '福民': '西區', '西門': '西區', '文化': '西區',
    '大石': '西區', '仁愛': '西區', '新榮': '西區', '興嘉': '西區', '友愛': '西區'
}


class DistrictResolver:
    _instance = None

    def __init__(self, data_path: str = "data/geo/taiwan_road_districts.json"):
        self.hsinchu_road_map: Dict[str, str] = {}
        self.chiayi_road_map: Dict[str, str] = {}
        self._load_data(data_path)

    def _load_data(self, data_path: str):
        if not os.path.exists(data_path):
            return

        try:
            with open(data_path, "r", encoding="utf-8") as f:
                all_data = json.load(f)

            for item in all_data:
                city = item.get("CityName", "")
                if "新竹市" in city:
                    for area in item.get("AreaList", []):
                        dist = area.get("AreaName")
                        for r in area.get("RoadList", []):
                            norm_r = normalize_text(r.get("RoadName", ""))
                            if norm_r:
                                self.hsinchu_road_map[norm_r] = dist
                elif "嘉義市" in city:
                    for area in item.get("AreaList", []):
                        dist = area.get("AreaName")
                        for r in area.get("RoadList", []):
                            norm_r = normalize_text(r.get("RoadName", ""))
                            if norm_r:
                                self.chiayi_road_map[norm_r] = dist
        except Exception as e:
            print(f"[WARN] Failed to load {data_path}: {e}")

    def resolve_hsinchu(self, road: str, address: str) -> str:
        addr = address or ""
        r = road or ""

        # 1. Direct explicit mention in address
        if "東區" in addr:
            return "東區"
        if "北區" in addr:
            return "北區"
        if "香山" in addr:
            return "香山區"

        # 2. Multi-district road resolution with door / lane number checks
        multi_dist = resolve_hsinchu_multi_road(r, addr)
        if multi_dist:
            return multi_dist

        norm_r = normalize_text(r)
        norm_a = normalize_text(addr)

        # 3. Cadastral section match (地籍地段)
        for sec, dist in HSINCHU_SECTIONS.items():
            if (sec + "段") in norm_r or (sec + "段") in norm_a:
                return dist

        # 4. Exact match in road dictionary
        if norm_r in self.hsinchu_road_map:
            return self.hsinchu_road_map[norm_r]

        # 5. Substring match from road dictionary (longer names first)
        for road_name in sorted(self.hsinchu_road_map.keys(), key=len, reverse=True):
            if len(road_name) >= 3:
                if road_name in norm_r or road_name in norm_a:
                    return self.hsinchu_road_map[road_name]

        # 6. Prominent road / landmark heuristics
        if any(k in norm_a or k in norm_r for k in [
            "關新", "埔頂", "光復路", "清大", "交大", "科學園", "金城", "慈雲",
            "金山", "學府", "南大路", "忠孝路", "大學路", "食品路", "建功", "建中"
        ]):
            return "東區"

        if any(k in norm_a or k in norm_r for k in [
            "武陵", "湳雅", "北大路", "中正路", "西大路", "竹光", "金雅",
            "樹林頭", "天府路", "延平路1", "延平路3", "東大路2", "東大路3", "東大路4",
            "經國路1", "經國路2"
        ]):
            return "北區"

        if any(k in norm_a or k in norm_r for k in [
            "大庄", "牛埔", "中華路4", "中華路5", "中華路6", "延平路2",
            "經國路3", "香山", "元培", "玄奘", "景觀大道", "香村", "五福"
        ]):
            return "香山區"

        # Default fallback: East District (largest population and volume)
        return "東區"

    def resolve_chiayi(self, road: str, address: str) -> str:
        addr = address or ""
        r = road or ""

        # 1. Direct explicit mention
        if "東區" in addr:
            return "東區"
        if "西區" in addr:
            return "西區"

        # 2. Multi-district road resolution with door number checks
        multi_dist = resolve_chiayi_multi_road(r, addr)
        if multi_dist:
            return multi_dist

        norm_r = normalize_text(r).replace("嘉義市", "").replace("嘉義巿", "")
        norm_a = normalize_text(addr).replace("嘉義市", "").replace("嘉義巿", "")

        # 3. Cadastral section match
        for sec, dist in CHIAYI_SECTIONS.items():
            if (sec + "段") in norm_r or (sec + "段") in norm_a:
                return dist

        # 4. Exact match in road dictionary
        if norm_r in self.chiayi_road_map:
            return self.chiayi_road_map[norm_r]

        # 5. Substring match from road dictionary
        for road_name in sorted(self.chiayi_road_map.keys(), key=len, reverse=True):
            if len(road_name) >= 3:
                if road_name in norm_r or road_name in norm_a:
                    return self.chiayi_road_map[road_name]

        # 6. Prominent landmarks / roads
        if any(k in norm_a or k in norm_r for k in [
            "林森東", "彌陀", "小雅", "大雅", "東門", "蘭潭", "忠孝路", "中山路", "吳鳳"
        ]):
            return "東區"

        if any(k in norm_a or k in norm_r for k in [
            "世賢", "博愛", "友愛", "自強", "南京", "玉山", "興嘉", "新民", "重慶", "北港"
        ]):
            return "西區"

        return "西區"

    def resolve(self, county_name: str, raw_district: str, address: str, road: str) -> Tuple[str, str]:
        """
        Resolves (district, full_district).
        For general counties, returns (raw_district, county_name + raw_district).
        For Hsinchu City and Chiayi City, subdivides into real districts.
        """
        if county_name == "新竹市":
            dist = self.resolve_hsinchu(road, address)
            return dist, f"新竹市{dist}"
        elif county_name == "嘉義市":
            dist = self.resolve_chiayi(road, address)
            return dist, f"嘉義市{dist}"
        else:
            clean_dist = raw_district.strip() if raw_district else ""
            return clean_dist, f"{county_name}{clean_dist}"


# Global Singleton
_resolver = None


def get_resolver() -> DistrictResolver:
    global _resolver
    if _resolver is None:
        _resolver = DistrictResolver()
    return _resolver


def resolve_district(county_name: str, raw_district: str, address: str = "", road: str = "") -> Tuple[str, str]:
    return get_resolver().resolve(county_name, raw_district, address, road)
