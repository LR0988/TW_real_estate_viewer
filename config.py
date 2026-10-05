import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
GEO_DIR = DATA_DIR / "geo"
DB_PATH = DATA_DIR / "real_estate.db"
STATIC_DIR = BASE_DIR / "web"

# Ensure directories exist
for d in [DATA_DIR, RAW_DIR, GEO_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# MOI Open Data URLs
MOI_BASE_URL = "https://plvr.land.moi.gov.tw"
MOI_LATEST_URL = f"{MOI_BASE_URL}/Download?type=zip&fileName=lvr_landcsv.zip"
MOI_SEASON_URL = f"{MOI_BASE_URL}/DownloadSeason?season={{season}}&type=zip&fileName=lvr_landcsv.zip"

# Taiwan Administrative County Code to Name Mapping
# In MOI Open Data, file prefix letters map to Taiwan's 22 administrative divisions
COUNTY_CODE_MAP = {
    "A": "臺北市",
    "B": "臺中市",
    "C": "基隆市",
    "D": "臺南市",
    "E": "高雄市",
    "F": "新北市",
    "G": "宜蘭縣",
    "H": "桃園市",
    "I": "嘉義市",
    "J": "新竹縣",
    "K": "苗栗縣",
    "M": "南投縣",
    "N": "彰化縣",
    "O": "新竹市",
    "P": "雲林縣",
    "Q": "嘉義縣",
    "T": "屏東縣",
    "U": "花蓮縣",
    "V": "臺東縣",
    "W": "金門縣",
    "X": "澎湖縣",
    "Z": "連江縣",
}

# Reverse mapping for convenience
COUNTY_NAME_MAP = {v: k for k, v in COUNTY_CODE_MAP.items()}

# Name normalization (handling '台' vs '臺' and older county names)
COUNTY_ALIASES = {
    "台北市": "臺北市",
    "台中市": "臺中市",
    "台南市": "臺南市",
    "台東縣": "臺東縣",
    "台北縣": "新北市",
    "桃園縣": "桃園市",
}

# Square meter to Ping factor (Taiwan real estate standard: 1 m² = 0.3025 坪)
PING_FACTOR = 0.3025
SQM_PER_PING = 1 / PING_FACTOR  # ~ 3.305785

# Special transaction keywords in '備註' to filter for fair market pricing
SPECIAL_TRADE_KEYWORDS = [
    "親友", "關係人", "二親等", "三親等", "員工", "借名", "持分", "畸零",
    "共有人", "塔位", "法拍", "毛胚", "急買急賣", "協議價購", "瑕疵", "受債權",
    "地上權", "債務抵償", "政府機關標售", "總價包含", "夾層", "頂樓加蓋", "未登記建物"
]

# Earliest available season in MOI Actual Price Registration
EARLIEST_SEASON = "101S3"  # August 2012
