#!/usr/bin/env python3
"""Phase 2: 历史日线数据全量拉取

对每只ETF，从上市日起拉取全部历史日线数据。

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
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger
from scripts._shared.rate_limiter import MootdxRateLimiter, BatchLimiter

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
        except:
            pass
    return set()


def save_progress(completed_codes):
    """保存进度"""
    pf = get_progress_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    import json
    json.dump({"completed_codes": list(completed_codes), "last_update": datetime.now().isoformat()}, 
              open(str(pf), "w"))


def fetch_daily_history_mootdx(code, exchange="SH"):
    """通过 mootdx 拉取单只ETF的全部历史日线
    
    返回: DataFrame with columns [date, open, high, low, close, volume, amount]
    """
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std', server='223.68.206.204:7702')
        
        # frequency=4 = 日线
        bars_per_call = 800
        all_bars = []
        start = ""
        
        while True:
            bars = client.bars(
                stockid=code,
                frequency=4,  # 日线
                start=start,
                offset=bars_per_call
            )
            
            if bars is None or len(bars) == 0:
                break
            
            all_bars.append(bars)
            
            # 取最早的日期作为下次起始
            earliest_date = bars.iloc[-1]["datetime"]
            start = str(earliest_date)
            
            # 如果返回条数少于预期，说明到头了
            if len(bars) < bars_per_call:
                break
        
        if not all_bars:
            return None
        
        import pandas as pd
        df = pd.concat(all_bars, ignore_index=True)
        return df
        
    except Exception as e:
        logger.error(f"mootdx 拉取 {code} 失败: {e}")
        return None


def parse_mootdx_df(df, code):
    """将 mootdx DataFrame 转换为 security_daily 格式"""
    if df is None or len(df) == 0:
        return []
    
    records = []
    for _, row in df.iterrows():
        records.append({
            "code": code,
            "trade_date": str(row.get("datetime", row.get("date", "")))[:10],
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
        })
    
    return records


def main():
    parser = argparse.ArgumentParser(description="Phase 2: 历史日线全量拉取")
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续")
    parser.add_argument("--codes", type=str, default=None, 
                       help="指定要拉取的ETF代码，逗号分隔")
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("Phase 2: 历史日线数据全量拉取")
    logger.info("=" * 60)
    
    # 初始化
    con = init_database()
    rate_limiter = MootdxRateLimiter(call_interval=0.5, batch_size=100, batch_sleep=5)
    
    # 获取ETF列表
    etfs = con.execute("SELECT code, name, exchange FROM security_info WHERE type='ETF'").fetchall()
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
            df = fetch_daily_history_mootdx(code, exchange or "SH")
            records = parse_mootdx_df(df, code)
            
            if not records:
                logger.warning(f"  {code} 无数据")
                fail_count += 1
                rate_limiter.wait_after_call()
                continue
            
            # 批量插入
            batch_size = 500
            for j in range(0, len(records), batch_size):
                batch = records[j:j+batch_size]
                con.executemany("""
                    INSERT OR REPLACE INTO security_daily 
                    (code, trade_date, "open", "high", "low", "close", 
                     volume, amount, pe_ttm, pb, turnover_rate, amplitude, change_pct, change_amt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [(r["code"], r["trade_date"], r["open"], r["high"], r["low"], r["close"],
                       r["volume"], r["amount"], r["pe_ttm"], r["pb"], r["turnover_rate"],
                       r["amplitude"], r["change_pct"], r["change_amt"]) for r in batch])
            
            con.commit()
            
            bar_count = len(records)
            total_bars += bar_count
            success_count += 1
            
            # 更新 security_info
            dates = [r["trade_date"] for r in records]
            con.execute("""
                UPDATE security_info 
                SET first_trade_date = ?, last_trade_date = ?, total_daily_bars = ?,
                    last_updated = CURRENT_TIMESTAMP
                WHERE code = ?
            """, (min(dates), max(dates), bar_count, code))
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
