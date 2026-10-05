import sys
import argparse
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import EARLIEST_SEASON, DB_PATH
from geo.geo_loader import ensure_geojson_assets
from db.database import init_db
from db.aggregator import refresh_aggregations
from crawler.pipeline import crawl_latest, backfill_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("cli")


def cmd_crawl_latest(args):
    """Crawl and ingest the latest 10-day open data batch."""
    print("🚀 啟動最新批次實價登錄爬蟲...")
    res = crawl_latest(force=args.force)
    if res.get("status") == "success":
        refresh_aggregations()
        print("✅ 最新資料爬取與統計聚合完成！")
    else:
        print(f"❌ 執行結束: {res}")


def cmd_backfill(args):
    """Backfill historical seasons."""
    print(f"📦 啟動歷史實價登錄資料補齊 (自 {args.start} 開始)...")
    res = backfill_history(
        start_season=args.start,
        end_season=args.end,
        delay_between_seasons=args.delay,
        force=args.force
    )
    if res.get("status") == "completed":
        refresh_aggregations()
        print("✅ 歷史資料補齊與統計聚合完成！")


def cmd_aggregate(args):
    """Recompute aggregation tables."""
    print("📊 重新計算統計聚合與市場熱度指數...")
    refresh_aggregations(exclude_special=not args.include_special)
    print("✅ 統計聚合更新完成！")


def cmd_serve(args):
    """Start the web server and visualization dashboard."""
    import uvicorn
    ensure_geojson_assets()
    init_db()

    print("\n" + "=" * 65)
    print("  🇹🇼 台灣房地產實價登錄可視化與市場熱度觀測站")
    print(f"  伺服器已啟動: http://localhost:{args.port}")
    print("  請在瀏覽器中開啟上方網址查看地圖可視化與量價熱度分析！")
    print("=" * 65 + "\n")

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


def main():
    parser = argparse.ArgumentParser(
        description="台灣房地產實價登錄爬蟲、歷史資料庫與地圖可視化分析系統"
    )
    subparsers = parser.add_subparsers(dest="command", help="子指令")

    # crawl-latest
    p_latest = subparsers.add_parser("crawl-latest", help="抓取最新發布之實價登錄批次資料 (每月1、11、21日更新)")
    p_latest.add_argument("--force", action="store_true", help="強制重新下載覆蓋快取")

    # backfill
    p_backfill = subparsers.add_parser("backfill", help="回溯補齊歷史實價登錄資料 (從101S3起)")
    p_backfill.add_argument("--start", default=EARLIEST_SEASON, help=f"起始季度代碼 (預設: {EARLIEST_SEASON})")
    p_backfill.add_argument("--end", default=None, help="結束季度代碼 (留空至當前季度)")
    p_backfill.add_argument("--delay", type=float, default=1.0, help="季度下載間隔秒數 (預設: 1.0s)")
    p_backfill.add_argument("--force", action="store_true", help="強制重新抓取已完成季度")

    # aggregate
    p_agg = subparsers.add_parser("aggregate", help="重新計算縣市與鄉鎮量價統計與熱度指數")
    p_agg.add_argument("--include-special", action="store_true", help="納入親友/特殊交易 (預設排除特殊交易以反映市場真實現況)")

    # serve
    p_serve = subparsers.add_parser("serve", help="啟動互動式地圖視覺化儀表板 Web 伺服器")
    p_serve.add_argument("--host", default="0.0.0.0", help="監聽 Host (預設 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8000, help="連接埠 (預設 8000)")
    p_serve.add_argument("--reload", action="store_true", help="開發模式熱重載")

    args = parser.parse_args()

    if args.command == "crawl-latest":
        cmd_crawl_latest(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "aggregate":
        cmd_aggregate(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
