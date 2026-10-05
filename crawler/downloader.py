import time
import ssl
import logging
from pathlib import Path
from typing import List, Optional
import urllib.request
import urllib.error
from datetime import datetime

from config import RAW_DIR, MOI_LATEST_URL, MOI_SEASON_URL

logger = logging.getLogger(__name__)

# Taiwan Government Certificate Authority (GRCA) fix for Python 3.13 / OpenSSL
SSL_CONTEXT = ssl._create_unverified_context()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


def download_url(url: str, dest_path: Path, max_retries: int = 3, retry_delay: float = 2.0) -> bool:
    """Downloads a file from url with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(f"HTTP {resp.status} on {url} (Attempt {attempt})")
                    time.sleep(retry_delay * attempt)
                    continue
                content = resp.read()
                if len(content) < 100:  # File too small, likely an error response
                    logger.warning(f"File too small ({len(content)} bytes) on {url}")
                    time.sleep(retry_delay * attempt)
                    continue

                with open(dest_path, "wb") as f:
                    f.write(content)
                logger.info(f"Successfully downloaded {dest_path.name} ({len(content):,} bytes)")
                return True
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning(f"Download error on attempt {attempt}/{max_retries} for {url}: {e}")
            time.sleep(retry_delay * attempt)
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            break

    return False


def download_season(season_code: str, force: bool = False) -> Optional[Path]:
    """
    Downloads historical batch zip for a specific season code (e.g. '101S3', '113S1').
    Saves to data/raw/lvr_landcsv_{season_code}.zip.
    """
    dest = RAW_DIR / f"lvr_landcsv_{season_code}.zip"
    if dest.exists() and dest.stat().st_size > 1000 and not force:
        logger.info(f"Using cached file {dest.name}")
        return dest

    url = MOI_SEASON_URL.format(season=season_code)
    logger.info(f"Downloading historical season {season_code} from {url}...")
    success = download_url(url, dest)
    return dest if success else None


def download_latest(force: bool = False) -> Optional[Path]:
    """
    Downloads the latest 10-day incremental batch from MOI Open Data.
    Saves to data/raw/lvr_landcsv_latest.zip.
    """
    dest = RAW_DIR / "lvr_landcsv_latest.zip"
    logger.info(f"Downloading latest published batch from {MOI_LATEST_URL}...")
    success = download_url(MOI_LATEST_URL, dest)
    return dest if success else None


def generate_season_list(start_season: str = "101S3", end_season: Optional[str] = None) -> List[str]:
    """
    Generates a list of season codes from start_season up to end_season or current date.
    Season format: {ROC_YEAR}S{QUARTER}, e.g. '101S3', '101S4', '102S1' ...
    """
    # Parse start
    start_year = int(start_season[:-2])
    start_q = int(start_season[-1])

    # Determine end
    if end_season:
        end_year = int(end_season[:-2])
        end_q = int(end_season[-1])
    else:
        now = datetime.now()
        current_roc_year = now.year - 1911
        current_month = now.month
        current_q = (current_month - 1) // 3 + 1
        end_year = current_roc_year
        end_q = current_q

    seasons = []
    y = start_year
    q = start_q
    while True:
        seasons.append(f"{y}S{q}")
        if y == end_year and q == end_q:
            break
        q += 1
        if q > 4:
            q = 1
            y += 1
        if y > end_year:
            break

    return seasons
