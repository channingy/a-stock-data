#!/usr/bin/env python3
"""Phase 2: 历史日线数据全量拉取

数据源: 腾讯财经 API (最多2000条/只，约8年历史)
东财API已永久封禁。

防封策略:
  - 每只请求间隔 3-8 秒随机延迟
  - 每100只额外暂停30秒
  - 连续失败10次暂停60秒
  - 使用 curl 替代 urllib (规避 urllib 连接复用触发反爬)

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/collect_daily_history.py [--resume] [--codes 510050,510300]
"""

import sys
import os
import time
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger

logger = setup_logger("collect_daily_history")


def get_progress_file():
    return Path("./logs/progress_daily.json")


def load_progress():
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
    pf = get_progress_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    import json
    with open(str(pf), "w") as f:
        json.dump({
            "completed_codes": list(completed_codes),
            "last_update": datetime.now().isoformat(),
        }, f)


def sleep_random(min_sec=3, max_sec=8):
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"  等待 {delay:.1f} 秒...")
    time.sleep(delay)


def fetch_daily_history_tencent(code, exchange="SH"):
    """通过 curl 调用腾讯财经 API 拉取单只ETF的历史日线
    
    使用 curl 而非 urllib/request，避免 Python HTTP 连接的
    TCP 复用触发反爬机制。curl 的行为与手动终端一致。
    """
    prefix = "sh" if exchange == "SH" else "sz"
    symbol = f"{prefix}{code}"

    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{2000},"

    import subprocess
    result = subprocess.run(
        ["curl", "-s", "--connect-timeout", "10", "-m", "20", url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
    )

    if result.returncode != 0:
        logger.warning(f"腾讯 {code} curl 失败: {result.stderr.decode()[:80]}")
        return None

    try:
        import json
        data = json.loads(result.stdout.decode("utf-8"))
    except Exception as e:
        logger.warning(f"腾讯 {code} JSON 解析失败: {e}")
        return None

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


def main():
    parser = argparse.ArgumentParser(description="Phase 2: 历史日线全量拉取")
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续")
    parser.add_argument(
        "--codes", type=str, default=None, help="指定要拉取的ETF代码，逗号分隔"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Phase 2: 历史日线数据全量拉取 (腾讯API via curl)")
    logger.info("=" * 60)

    con = init_database()

    etfs = con.execute(
        "SELECT code, name, exchange FROM security_info WHERE type='ETF'"
    ).fetchall()
    logger.info(f"数据库中共 {len(etfs)} 只ETF")

    if args.codes:
        target_codes = set(args.codes.split(","))
        etfs = [e for e in etfs if e[0] in target_codes]
        logger.info(f"指定拉取 {len(etfs)} 只ETF: {list(target_codes)}")

    completed_codes = set()
    if args.resume:
        completed_codes = load_progress()
        logger.info(f"断点续传: 已完成 {len(completed_codes)} 只，跳过")

    pending = [e for e in etfs if e[0] not in completed_codes]
    logger.info(f"待处理: {len(pending)} 只")

    if not pending:
        logger.info("没有待处理品种，退出")
        con.close()
        return 0

    total_bars = 0
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    global_failure_threshold = 50  # 达到阈值暂停

    for i, (code, name, exchange) in enumerate(pending, 1):
        logger.info(f"[{i}/{len(pending)}] 拉取 {code} ({name})...")

        try:
            records = fetch_daily_history_tencent(code, exchange or "SH")

            if not records:
                logger.warning(f"  {code} 无数据")
                fail_count += 1
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    pause_time = min(consecutive_failures * 6, 120)
                    logger.warning(f"连续失败 {consecutive_failures} 次，暂停 {pause_time} 秒...")
                    time.sleep(pause_time)
                sleep_random(3, 6)
                continue

            consecutive_failures = 0
            logger.debug(f"  {code}: 获取 {len(records)} 条数据")

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
                        (code, r["trade_date"], r["open"], r["high"], r["low"],
                         r["close"], r["volume"], r["amount"],
                         None, None, None, None, None, None)
                        for r in batch
                    ],
                )

            con.commit()

            bar_count = len(records)
            total_bars += bar_count
            success_count += 1

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

            completed_codes.add(code)

            # 每50只保存进度
            if i % 50 == 0:
                save_progress(completed_codes)

            # 每100只额外暂停
            if i % 100 == 0:
                extra_pause = 30
                logger.info(f"  【进度 {i}/{len(pending)}】额外暂停 {extra_pause} 秒...")
                time.sleep(extra_pause)

        except Exception as e:
            logger.error(f"  ❌ {code} 失败: {e}")
            fail_count += 1
            consecutive_failures += 1
            if consecutive_failures >= 5:
                time.sleep(15)

        # 正常随机延迟 3-8 秒
        sleep_random(3, 8)

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
