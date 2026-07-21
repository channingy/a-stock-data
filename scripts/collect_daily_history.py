#!/usr/bin/env python3
"""Phase 2: 历史日线数据全量拉取

对每只ETF，拉取历史日线数据。
使用腾讯财经 API 作为数据源（mootdx 连接不稳定时的备选方案）。

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


def fetch_daily_history_tencent(code, exchange="SH"):
    """通过腾讯财经 API 拉取单只ETF的历史日线数据

    腾讯接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh510050,day,,,100,qfq

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

        # 拉取最近1000根日线（约4年）
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,1000,qfq"
        resp = requests.get(url, timeout=15)
        data = resp.json()

        # 腾讯返回格式: data.{symbol}.day[] 或 data.{symbol}.day[]
        key = symbol
        if "data" not in data or key not in data.get("data", {}):
            # 尝试另一种格式
            for k in data.get("data", {}):
                if isinstance(data["data"][k], dict) and "day" in data["data"][k]:
                    key = k
                    break

        klines = data.get("data", {}).get(key, {}).get("day", [])
        if not klines:
            # 尝试 qfq (前复权) 格式
            klines = data.get("data", {}).get(key, {}).get("qfqday", [])

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

    # 初始化
    con = init_database()
    rate_limiter = MootdxRateLimiter(
        call_interval=0.3, batch_size=100, batch_sleep=5
    )

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
            records = fetch_daily_history_tencent(code, exchange or "SH")

            if not records:
                logger.warning(f"  {code} 无数据")
                fail_count += 1
                rate_limiter.wait_after_call()
                continue

            # 批量插入
            batch_size = 500
            for j in range(0, len(records), batch_size):
                batch = records[j : j + batch_size]
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

            logger.info(f"  ✅ {code}: {bar_count} 条日线数据")

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
