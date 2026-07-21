#!/usr/bin/env python3
"""Phase 3: 技术指标计算

使用 stockstats 计算 MA/BOLL/MACD/KDJ/RSI 等技术指标。

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/calc_indicators.py [--type daily] [--codes 510050,510300]
"""

import sys
import os
import logging
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import pandas as pd
import numpy as np
from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger

logger = setup_logger("calc_indicators")


def calc_technical_indicators(df, config=None):
    """计算技术指标
    
    Args:
        df: pandas DataFrame with columns [datetime, open, high, low, close, volume]
        config: config dict with indicator params
    
    Returns:
        dict with indicator values
    """
    if config is None:
        config = {}
    
    indicators_config = config.get("indicators", {})
    ma_params = indicators_config.get("ma", [5, 10, 20, 60])
    boll_params = indicators_config.get("boll", [20, 2])
    macd_params = indicators_config.get("macd", [12, 26, 9])
    kdj_params = indicators_config.get("kdj", [9, 3, 3])
    rsi_params = indicators_config.get("rsi", [6, 12, 24])
    
    result = {}
    
    # MA
    for n in ma_params:
        result[f"ma{n}"] = df["close"].rolling(window=n).mean()
    
    # BOLL
    mid = df["close"].rolling(window=boll_params[0]).mean()
    std = df["close"].rolling(window=boll_params[0]).std()
    result["boll_upper"] = mid + boll_params[1] * std
    result["boll_mid"] = mid
    result["boll_lower"] = mid - boll_params[1] * std
    
    # MACD
    exp1 = df["close"].ewm(span=macd_params[0], adjust=False).mean()
    exp2 = df["close"].ewm(span=macd_params[1], adjust=False).mean()
    result["macd_dif"] = exp1 - exp2
    result["macd_dea"] = result["macd_dif"].ewm(span=macd_params[2], adjust=False).mean()
    result["macd_hist"] = 2 * (result["macd_dif"] - result["macd_dea"])
    
    # KDJ
    low_n = df["low"].rolling(window=kdj_params[0]).min()
    high_n = df["high"].rolling(window=kdj_params[0]).max()
    rsv = (df["close"] - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(com=kdj_params[1]-1, adjust=False).mean()
    d = k.ewm(com=kdj_params[2]-1, adjust=False).mean()
    result["kdj_k"] = k
    result["kdj_d"] = d
    result["kdj_j"] = 3 * k - 2 * d
    
    # RSI
    for n in rsi_params:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta).where(delta < 0, 0)
        avg_gain = gain.ewm(alpha=1/n, min_periods=n).mean()
        avg_loss = loss.ewm(alpha=1/n, min_periods=n).mean()
        rs = avg_gain / avg_loss
        result[f"rsi_{n}"] = 100 - (100 / (1 + rs))
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Phase 3: 技术指标计算")
    parser.add_argument("--type", choices=["daily", "all"], default="daily",
                       help="计算类型")
    parser.add_argument("--codes", type=str, default=None,
                       help="指定计算的ETF代码，逗号分隔")
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("Phase 3: 技术指标计算")
    logger.info("=" * 60)
    
    # 加载配置
    try:
        import yaml
        with open("./config.yaml", "r") as f:
            config = yaml.safe_load(f)
    except:
        config = {}
    
    # 初始化数据库
    con = init_database(config)
    
    # 获取ETF列表
    if args.codes:
        target_codes = set(args.codes.split(","))
        etfs = con.execute(
            "SELECT code, name FROM security_info WHERE type='ETF' AND code IN (?)",
            (list(target_codes),)
        ).fetchall()
    else:
        etfs = con.execute(
            "SELECT code, name FROM security_info WHERE type='ETF'"
        ).fetchall()
    
    logger.info(f"共 {len(etfs)} 只ETF需要计算技术指标")
    
    success_count = 0
    fail_count = 0
    total_bars = 0
    
    for code, name in etfs:
        try:
            # 读取日线数据
            df = con.execute("""
                SELECT trade_date, "open", "high", "low", "close", volume
                FROM security_daily 
                WHERE code = ?
                ORDER BY trade_date ASC
            """, (code,)).fetchdf()
            
            if len(df) == 0 or len(df) < 10:
                logger.debug(f"  {code}: 数据不足 ({len(df)} 条)，跳过")
                continue
            
            # 转换为 stockstats 需要的格式
            df["datetime"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]
            df = df.sort_index()
            
            # 计算技术指标
            ind = calc_technical_indicators(df, config)
            
            # 写入数据库
            indicator_cols = [
                "ma5", "ma10", "ma20", "ma60",
                "boll_upper", "boll_mid", "boll_lower",
                "macd_dif", "macd_dea", "macd_hist",
                "kdj_k", "kdj_d", "kdj_j",
                "rsi_6", "rsi_12", "rsi_24"
            ]
            
            batch_records = []
            for idx, row in df.iterrows():
                trade_date = idx.strftime("%Y-%m-%d")
                record = [code, trade_date]
                for col in indicator_cols:
                    val = ind.get(col)
                    if val is not None:
                        v = float(val.loc[idx]) if pd.notna(val.loc[idx]) else None
                    else:
                        v = None
                    record.append(v)
                batch_records.append(tuple(record))
            
            if batch_records:
                con.executemany("""
                    INSERT OR REPLACE INTO security_indicators 
                    (code, trade_date, ma5, ma10, ma20, ma60,
                     boll_upper, boll_mid, boll_lower,
                     macd_dif, macd_dea, macd_hist,
                     kdj_k, kdj_d, kdj_j,
                     rsi_6, rsi_12, rsi_24)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch_records)
                con.commit()
                
                success_count += 1
                total_bars += len(batch_records)
                logger.debug(f"  ✅ {code}: {len(batch_records)} 条指标")
            
        except Exception as e:
            logger.error(f"  ❌ {code} 计算失败: {e}")
            fail_count += 1
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 3 完成摘要:")
    logger.info(f"  成功: {success_count} 只")
    logger.info(f"  失败: {fail_count} 只")
    logger.info(f"  总指标记录: {total_bars:,} 条")
    logger.info("=" * 60)
    
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
