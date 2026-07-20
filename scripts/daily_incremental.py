#!/usr/bin/env python3
"""Phase 5: 每日增量采集

每日运行，采集当日ETF数据并更新技术指标。

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/daily_incremental.py
"""

import sys
import os
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger
from scripts._shared.data_validator import DataValidator
from scripts._shared.rate_limiter import MootdxRateLimiter

logger = setup_logger("daily_incremental")


def fetch_today_daily(code, exchange="SH"):
    """通过 mootdx 拉取单只ETF的当日日线数据"""
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std', server='223.68.206.204:7702')
        
        # 拉取最近3天的数据，确保包含当天
        bars = client.bars(
            stockid=code,
            frequency=4,  # 日线
            start="",
            offset=3
        )
        
        if bars is None or len(bars) == 0:
            return None
        
        today = datetime.now().strftime("%Y-%m-%d")
        # 过滤出今天的数据
        today_bars = bars[bars["datetime"].astype(str).str.startswith(today)]
        
        if len(today_bars) == 0:
            # 如果今天还没开盘，拉取最近一个交易日
            today_bars = bars.tail(1)
        
        return today_bars
        
    except Exception as e:
        logger.error(f"mootdx 拉取 {code} 失败: {e}")
        return None


def parse_daily_record(df, code):
    """将 DataFrame 转换为 insert 参数"""
    if df is None or len(df) == 0:
        return None
    
    row = df.iloc[-1]  # 取最新一条
    trade_date = str(row.get("datetime", ""))[:10]
    
    return {
        "code": code,
        "trade_date": trade_date,
        "open": float(row.get("open", 0)),
        "high": float(row.get("high", 0)),
        "low": float(row.get("low", 0)),
        "close": float(row.get("close", 0)),
        "volume": int(row.get("vol", row.get("volume", 0))),
        "amount": float(row.get("amount", 0)),
        "pe_ttm": None,
        "pb": None,
        "turnover_rate": float(row.get("turn", 0)) if "turn" in row else None,
        "amplitude": float(row.get("amplitude", 0)) if "amplitude" in row else None,
        "change_pct": None,
        "change_amt": None
    }


def main():
    logger.info("=" * 60)
    logger.info(f"Phase 5: 每日增量采集 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    # 初始化
    con = init_database()
    rate_limiter = MootdxRateLimiter(call_interval=0.5, batch_size=100, batch_sleep=5)
    
    # 获取ETF列表
    etfs = con.execute("SELECT code, exchange FROM security_info WHERE type='ETF'").fetchall()
    logger.info(f"共 {len(etfs)} 只ETF需要增量采集")
    
    success_count = 0
    fail_count = 0
    total_new_bars = 0
    
    for i, (code, exchange) in enumerate(etfs, 1):
        try:
            df = fetch_today_daily(code, exchange or "SH")
            record = parse_daily_record(df, code)
            
            if record is None:
                logger.debug(f"  [{i}] {code}: 无新数据")
                continue
            
            # 检查是否已存在
            existing = con.execute(
                "SELECT COUNT(*) FROM security_daily WHERE code=? AND trade_date=?",
                (code, record["trade_date"])
            ).fetchone()[0]
            
            if existing > 0:
                logger.debug(f"  [{i}] {code}: {record['trade_date']} 已存在，跳过")
                continue
            
            # 插入新数据
            con.execute("""
                INSERT INTO security_daily 
                (code, trade_date, "open", "high", "low", "close",
                 volume, amount, pe_ttm, pb, turnover_rate, amplitude, change_pct, change_amt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["code"], record["trade_date"],
                record["open"], record["high"], record["low"], record["close"],
                record["volume"], record["amount"],
                record["pe_ttm"], record["pb"], record["turnover_rate"],
                record["amplitude"], record["change_pct"], record["change_amt"]
            ))
            
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
        con.execute("""
            UPDATE security_info 
            SET last_trade_date = ?, last_updated = CURRENT_TIMESTAMP
            WHERE code IN (SELECT DISTINCT code FROM security_daily WHERE trade_date=?)
        """, (today, today))
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
