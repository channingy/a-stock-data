#!/usr/bin/env python3
"""Phase 2: 历史日线数据全量拉取

通过东财 push2his API 拉取每只ETF的全部历史日线数据。
东财接口一次可返回全量K线（5000+根），无需分段。

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/collect_daily_history.py [--resume] [--codes 510050,510300]
"""

import sys
import os
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger
from scripts._shared.rate_limiter import MootdxRateLimiter

logger = setup_logger("collect_daily_history")


def get_progress_file():
    """获取进度文件路径"""
    return Path("./logs/progress_daily.json")


def load_progress():
    """加载已完成的品种列表"""
    pf = get_progress_file()
    if pf.exists():
        try:
            import json

            data = json.loads(pf.read_text())
            return set(data.get("completed_codes", []))
        except Exception:
            pass
    return set()


def save_progress(completed_codes):
    """保存进度"""
    pf = get_progress_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    import json

    with open(str(pf), "w") as f:
        json.dump(
            {
                "completed_codes": list(completed_codes),
                "last_update": datetime.now().isoformat(),
            },
            f,
        )


def fetch_daily_history_eastmoney(code, exchange="SH"):
    """通过东财 push2his API 拉取单只ETF的全部历史日线

    Args:
        code: 6位代码如 '510050'
        exchange: 'SH' 或 'SZ'

    Returns:
        list of dicts with OHLCV data, or None on failure
    """
    try:
        import requests

        secid = f"1.{code}" if exchange == "SH" else f"0.{code}"

        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": 101,
            "fqt": 1,
            "beg": 0,
            "end": 20500000,
            "smplct": 0,
            "lmt": 10000,
        }

        resp = requests.get(url, params=params, timeout=15, headers={
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": "Mozilla/5.0",
        })
        data = resp.json()

        if "data" not in data or "klines" not in data["data"]:
            return None

        klines = data["data"]["klines"]
        if not klines:
            return None

        records = []
        for kl in klines:
            parts = kl.split(",")
            if len(parts) < 7:
                continue
            records.append({
                "trade_date": parts[0],
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": int(float(parts[5])),
                "amount": float(parts[6]),
            })

        return records

    except Exception as e:
        logger.error(f"东财拉取 {code} 失败: {e}")
        return None


def fetch_daily_history_tencent(code, exchange="SH"):
    """通过腾讯财经 API 拉取单只ETF的历史日线（备选）

    腾讯接口单次最多返回2000根K线（约8年）。

    Args:
        code: 6位代码如 '510050'
        exchange: 'SH' 或 'SZ'

    Returns:
        list of dicts with OHLCV data, or None on failure
    """
    try:
        import requests

        prefix = "sh" if exchange == "SH" else "sz"
        symbol = f"{prefix}{code}"

        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{2000},"
        resp = requests.get(url, timeout=15)
        data = resp.json()

        klines = data.get("data", {}).get(symbol, {}).get("day", [])
        if not klines:
            klines = data.get("data", {}).get(symbol, {}).get("qfqday", [])

        if not klines:
            return None

        records = []
        for item in klines:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            records.append({
                "trade_date": str(item[0])[:10],
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": int(float(item[5])) if len(item) > 5 else 0,
                "amount": float(item[6]) if len(item) > 6 else 0,
            })

        return records

    except Exception as e:
        logger.error(f"腾讯拉取 {code} 失败: {e}")
        return None


def fetch_daily_history_with_fallback(code, exchange="SH"):
    """优先东财，失败则用腾讯备选"""
    records = fetch_daily_history_eastmoney(code, exchange)
    if records:
        return records

    logger.warning(f"{code} 东财失败，尝试腾讯备选...")
    return fetch_daily_history_tencent(code, exchange)


def main():
    parser = argparse.ArgumentParser(description="Phase 2: 历史日线全量拉取")
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续")
    parser.add_argument(
        "--codes", type=str, default=None, help="指定要拉取的ETF代码，逗号分隔"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Phase 2: 历史日线数据全量拉取")
    logger.info("=" * 60)

    # 绕过系统代理（macOS科学上网工具拦截东财/腾讯API）
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

    # 初始化
    con = init_database()
    rate_limiter = MootdxRateLimiter(call_interval=1.0, batch_size=50, batch_sleep=10)

    # 获取ETF列表
    etfs = con.execute(
        "SELECT code, name, exchange FROM security_info WHERE type='ETF'"
    ).fetchall()
    logger.info(f"数据库中共 {len(etfs)} 只ETF")

    if args.codes:
        target_codes = set(args.codes.split(","))
        etfs = [e for e in etfs if e[0] in target_codes]
        logger.info(f"指定拉取 {len(etfs)} 只ETF: {list(target_codes)}")

    # 加载进度
    completed_codes = set()
    if args.resume:
        completed_codes = load_progress()
        logger.info(f"断点续传: 已完成 {len(completed_codes)} 只，跳过")

    # 待处理的ETF
    pending = [e for e in etfs if e[0] not in completed_codes]
    logger.info(f"待处理: {len(pending)} 只")

    if not pending:
        logger.info("没有待处理品种，退出")
        con.close()
        return 0

    # 开始拉取
    total_bars = 0
    success_count = 0
    fail_count = 0

    for i, (code, name, exchange) in enumerate(pending, 1):
        logger.info(f"[{i}/{len(pending)}] 拉取 {code} ({name})...")

        try:
            records = fetch_daily_history_with_fallback(code, exchange or "SH")

            if not records:
                logger.warning(f"  {code} 无数据")
                fail_count += 1
                rate_limiter.wait_after_call()
                continue

            # 批量插入
            batch_insert_size = 500
            for j in range(0, len(records), batch_insert_size):
                batch = records[j : j + batch_insert_size]
                con.executemany(
                    """
                    INSERT OR REPLACE INTO security_daily
                    (code, trade_date, "open", "high", "low", "close",
                     volume, amount, pe_ttm, pb, turnover_rate, amplitude, change_pct, change_amt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        (
                            code,
                            r["trade_date"],
                            r["open"],
                            r["high"],
                            r["low"],
                            r["close"],
                            r["volume"],
                            r["amount"],
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                        for r in batch
                    ],
                )

            con.commit()

            bar_count = len(records)
            total_bars += bar_count
            success_count += 1

            # 更新 security_info
            dates = [r["trade_date"] for r in records]
            con.execute(
                """
                UPDATE security_info
                SET first_trade_date = ?, last_trade_date = ?, total_daily_bars = ?,
                    last_updated = CURRENT_TIMESTAMP
                WHERE code = ?
            """,
                (min(dates), max(dates), bar_count, code),
            )
            con.commit()

            logger.info(f"  ✅ {code}: {bar_count} 条日线数据 ({dates[0]} ~ {dates[-1]})")

            # 标记完成
            completed_codes.add(code)
            if i % 50 == 0:
                save_progress(completed_codes)

        except Exception as e:
            logger.error(f"  ❌ {code} 失败: {e}")
            fail_count += 1

        rate_limiter.wait_after_call()

    # 最终保存进度
    save_progress(completed_codes)

    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 2 完成摘要:")
    logger.info(f"  成功: {success_count} 只")
    logger.info(f"  失败: {fail_count} 只")
    logger.info(f"  总日线记录: {total_bars:,} 条")
    logger.info("=" * 60)

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
