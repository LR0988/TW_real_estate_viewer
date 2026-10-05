"""
Continuous Background Backfill Worker for Taiwan Real Estate Data.
Downloads historical quarterly batches from MOI and refreshes aggregations periodically.
"""

import time
import sys
import logging
from pathlib import Path
from datetime import datetime

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import EARLIEST_SEASON
from crawler.pipeline import backfill_history
from crawler.downloader import generate_season_list, download_season
from crawler.parser import parse_zip_archive
from db.database import init_db, insert_transactions_batch, record_checkpoint, list_completed_seasons
from db.aggregator import refresh_aggregations

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_worker")


def run_continuous_backfill(start_season: str = "104S1", end_season: str = "115S1"):
    init_db()
    seasons = generate_season_list(start_season, end_season)
    completed = set(list_completed_seasons())

    to_process = [s for s in seasons if s not in completed]
    print(f"=== 啟動連續歷史資料補齊任務 ===")
    print(f"目標範圍: {start_season} ~ {end_season} (共 {len(to_process)} 季待處理)")

    batch_processed = 0
    total_records = 0

    for idx, season in enumerate(to_process, 1):
        print(f"\n[{idx}/{len(to_process)}] 下載並解析 {season} ...", flush=True)
        try:
            t0 = time.time()
            zip_path = download_season(season)
            if not zip_path:
                print(f"⚠️ {season} 下載失敗，跳過。", flush=True)
                record_checkpoint(season, "failed", 0)
                continue

            df = parse_zip_archive(zip_path, season_code=season)
            if df.empty:
                print(f"ℹ️ {season} 無資料，標記完成。", flush=True)
                record_checkpoint(season, "completed", 0)
                continue

            count = insert_transactions_batch(df, season_code=season)
            record_checkpoint(season, "completed", count)
            total_records += count
            batch_processed += 1
            elapsed = time.time() - t0
            print(f"✅ {season} 入庫完成: {count:,} 筆 (耗時 {elapsed:.1f}s)", flush=True)

            # Refresh aggregations every 4 seasons so UI is immediately updated
            if batch_processed % 4 == 0 or idx == len(to_process):
                print(f"🔄 正在更新季指標統計庫 (已累積 {total_records:,} 筆)...", flush=True)
                refresh_aggregations()
                print(f"✨ 統計庫已同步最新入庫季！", flush=True)

            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {season} 發生異常: {e}", flush=True)
            record_checkpoint(season, "failed", 0)

    print(f"\n🎉 補齊任務全數完成！共處理 {batch_processed} 季，新增 {total_records:,} 筆記錄。")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "104S1"
    end = sys.argv[2] if len(sys.argv) > 2 else "115S1"
    run_continuous_backfill(start, end)
