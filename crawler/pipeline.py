import time
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from config import EARLIEST_SEASON
from db.database import (
    init_db,
    insert_transactions_batch,
    record_checkpoint,
    get_checkpoint,
    list_completed_seasons,
)
from crawler.downloader import (
    download_season,
    download_latest,
    generate_season_list,
)
from crawler.parser import parse_zip_archive

logger = logging.getLogger(__name__)


def crawl_latest(force: bool = False) -> Dict[str, Any]:
    """
    Crawls and ingests the latest 10-day incremental batch from MOI Open Data.
    """
    init_db()
    today_str = datetime.now().strftime("%Y%m%d")
    batch_tag = f"latest_{today_str}"

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始下載最新實價登錄資料批次...")
    zip_path = download_latest(force=force)
    if not zip_path or not zip_path.exists():
        msg = "下載最新資料失敗"
        print(f"❌ {msg}")
        return {"status": "failed", "error": msg}

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始解析 ZIP 檔案並進行標準化處理...")
    t0 = time.time()
    df = parse_zip_archive(zip_path, season_code=batch_tag)
    elapsed = time.time() - t0

    if df.empty:
        msg = "解析結果為空"
        print(f"⚠️ {msg}")
        return {"status": "empty", "record_count": 0}

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 解析完成，共 {len(df):,} 筆交易，耗時 {elapsed:.2f} 秒。開始寫入資料庫...")
    inserted_count = insert_transactions_batch(df, season_code=batch_tag)
    record_checkpoint(batch_tag, "completed", inserted_count)
    print(f"✅ 最新資料入庫完成！共新增 {inserted_count:,} 筆記錄。")

    return {
        "status": "success",
        "batch_tag": batch_tag,
        "record_count": inserted_count,
        "elapsed_seconds": round(elapsed, 2)
    }


def backfill_history(
    start_season: str = EARLIEST_SEASON,
    end_season: Optional[str] = None,
    delay_between_seasons: float = 1.0,
    force: bool = False
) -> Dict[str, Any]:
    """
    Backfills historical seasons from start_season (e.g. 101S3) to end_season.
    Skips seasons that have already been parsed and completed.
    """
    init_db()
    all_seasons = generate_season_list(start_season, end_season)
    completed_seasons = set(list_completed_seasons()) if not force else set()

    to_process = [s for s in all_seasons if s not in completed_seasons]
    print(f"=== 歷史實價登錄資料補齊作業 ===")
    print(f"涵蓋範圍: {start_season} 至 {all_seasons[-1]} (共 {len(all_seasons)} 季)")
    print(f"已完成: {len(completed_seasons)} 季，待處理: {len(to_process)} 季\n")

    total_inserted = 0
    success_count = 0
    failed_seasons = []

    for idx, season in enumerate(to_process, 1):
        print(f"[{idx}/{len(to_process)}] 正在處理 {season} 季資料...")
        try:
            zip_path = download_season(season, force=force)
            if not zip_path:
                print(f"⚠️ {season} 下載失敗或資料不存在 (可能已超過政府提供範圍)")
                record_checkpoint(season, "failed", 0)
                failed_seasons.append(season)
                continue

            t0 = time.time()
            df = parse_zip_archive(zip_path, season_code=season)
            elapsed = time.time() - t0

            if df.empty:
                print(f"ℹ️ {season} 無買賣交易資料，標記完成。")
                record_checkpoint(season, "completed", 0)
                success_count += 1
                continue

            count = insert_transactions_batch(df, season_code=season)
            record_checkpoint(season, "completed", count)
            total_inserted += count
            success_count += 1
            print(f"  -> {season} 完成: {count:,} 筆入庫 (解析耗時 {elapsed:.2f}s)")

            if delay_between_seasons > 0:
                time.sleep(delay_between_seasons)

        except Exception as e:
            logger.error(f"Error processing season {season}: {e}")
            print(f"❌ {season} 處理失敗: {e}")
            record_checkpoint(season, "failed", 0)
            failed_seasons.append(season)

    print(f"\n🎉 歷史資料補齊作業完成！成功 {success_count} 季，共新增 {total_inserted:,} 筆記錄。")
    if failed_seasons:
        print(f"⚠️ 失敗或跳過季度: {', '.join(failed_seasons)}")

    return {
        "status": "completed",
        "processed_seasons": success_count,
        "total_records": total_inserted,
        "failed_seasons": failed_seasons
    }
