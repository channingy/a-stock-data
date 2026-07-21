#!/usr/bin/env python3
"""Phase 5: 每日增量采集

每日运行，采集当日ETF数据并更新技术指标。
使用腾讯财经 API 作为数据源。

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/daily_incremental.py
"""

import sys
import os
import time
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger
from scripts._shared.data_validator import DataValidator
from scripts._shared.rate_limiter import MootdxRateLimiter

logger = setup_logger("daily_incremental")


def fetch_today_daily(code, exchange="SH"):
    """通过腾讯财经拉取单只ETF的最新日线数据"""
    try:
        import requests

        prefix = "sh" if exchange == "SH" else "sz"
        symbol = f"{prefix}{code}"

        # 拉取最近5根日线
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,5,qfq"
        resp = requests.get(url, timeout=10)
        data = resp.json()

        # 解析返回数据
        key = symbol
        if "data" not in data or key not in data.get("data", {}):
            for k in data.get("data", {}):
                if isinstance(data["data"][k], dict) and "day" in data["data"][k]:
                    key = k
                    break

        klines = data.get("data", {}).get(key, {}).get("day", [])
        if not klines:
            klines = data.get("data", {}).get(key, {}).get("qfqday", [])

        if not klines:
            return None

        # 取最新一条
        latest = klines[-1]
        if not isinstance(latest, (list, tuple)) or len(latest) < 6:
            return None

        return {
            "trade_date": str(latest[0])[:10],
            "open": float(latest[1]),
            "high": float(latest[2]),
            "low": float(latest[3]),
            "close": float(latest[4]),
            "volume": int(float(latest[5])),
            "amount": float(latest[6]) if len(latest) > 6 else 0,
        }

    except Exception as e:
        logger.error(f"腾讯拉取 {code} 失败: {e}")
        return None


def main():
    logger.info("=" * 60)
    logger.info(f"Phase 5: 每日增量采集 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # 初始化
    con = init_database()
    rate_limiter = MootdxRateLimiter(
        call_interval=0.3, batch_size=100, batch_sleep=5
    )

    # 获取ETF列表
    etfs = con.execute(
        "SELECT code, exchange FROM security_info WHERE type='ETF'"
    ).fetchall()
    logger.info(f"共 {len(etfs)} 只ETF需要增量采集")

    success_count = 0
    fail_count = 0
    total_new_bars = 0

    for i, (code, exchange) in enumerate(etfs, 1):
        try:
            record = fetch_today_daily(code, exchange or "SH")

            if record is None:
                logger.debug(f"  [{i}] {code}: 无新数据")
                continue

            # 检查是否已存在
            existing = con.execute(
                "SELECT COUNT(*) FROM security_daily WHERE code=? AND trade_date=?",
                (code, record["trade_date"]),
            ).fetchone()[0]

            if existing > 0:
                logger.debug(
                    f"  [{i}] {code}: {record['trade_date']} 已存在，跳过"
                )
                continue

            # 插入新数据
            con.execute(
                """
                INSERT INTO security_daily
                (code, trade_date, "open", "high", "low", "close",
                 volume, amount, pe_ttm, pb, turnover_rate, amplitude, change_pct, change_amt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    code,
                    record["trade_date"],
                    record["open"],
                    record["high"],
                    record["low"],
                    record["close"],
                    record["volume"],
                    record["amount"],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )

            con.commit()
            total_new_bars += 1
            success_count += 1

            if i % 100 == 0:
                logger.info(f"  进度: {i}/{len(etfs)}")

        except Exception as e:
            logger.error(f"  [{i}] {code} 失败: {e}")
            fail_count += 1

        rate_limiter.wait_after_call()

    # 更新 security_info
    if total_new_bars > 0:
        today = datetime.now().strftime("%Y-%m-%d")
        con.execute(
            """
            UPDATE security_info
            SET last_trade_date = ?, last_updated = CURRENT_TIMESTAMP
            WHERE code IN (SELECT DISTINCT code FROM security_daily WHERE trade_date=?)
        """,
            (today, today),
        )
        con.commit()

    # 数据验证
    logger.info("\n运行数据验证...")
    validator = DataValidator(con)
    validator.run_full_validation()

    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 5 完成摘要:")
    logger.info(f"  成功: {success_count} 只")
    logger.info(f"  失败: {fail_count} 只")
    logger.info(f"  新增日线记录: {total_new_bars} 条")
    logger.info("=" * 60)

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
