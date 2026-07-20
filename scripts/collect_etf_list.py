#!/usr/bin/env python3
"""Phase 1: 采集ETF列表 + 实时行情快照

步骤:
  1. 初始化 DuckDB 数据库和表结构
  2. 调用东财 push2 API 获取全部ETF列表
  3. 调用腾讯财经批量获取实时行情
  4. 合并数据写入 DuckDB
  5. 输出统计摘要

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/collect_etf_list.py
"""

import sys
import os
import time
import logging
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger
from scripts._shared.rate_limiter import RateLimiter

logger = setup_logger("collect_etf_list")


def fetch_etf_list_from_eastmoney():
    """从东财 push2 API 获取全部ETF列表
    
    返回: list of dicts with code, name, exchange
    """
    logger.info("正在获取东财ETF列表...")
    
    try:
        from eastmoney import em_get
        result = em_get("fund_etf_category_em", fields="ts_code,symbol,name,industry")
        if result is None or len(result) == 0:
            logger.warning("东财API返回空结果，尝试备用方法...")
            return fetch_etf_list_fallback()
        
        # 解析结果
        etfs = []
        for item in result:
            ts_code = item.get("ts_code", "")
            symbol = item.get("symbol", "")
            name = item.get("name", "")
            industry = item.get("industry", "")
            
            if not ts_code or not name:
                continue
            
            # ts_code 格式: 510050.SH
            parts = ts_code.split(".")
            code = parts[0] if parts else symbol
            exchange = parts[1] if len(parts) > 1 else "SH"
            
            etfs.append({
                "code": code,
                "name": name,
                "exchange": exchange,
                "industry": industry
            })
        
        logger.info(f"东财ETF列表: {len(etfs)} 只")
        return etfs
        
    except ImportError:
        logger.warning("eastmoney 模块不可用，使用备用方法")
        return fetch_etf_list_fallback()


def fetch_etf_list_fallback():
    """备用方法：通过 mootdx 获取ETF列表"""
    logger.info("使用 mootdx 获取ETF列表...")
    
    try:
        from mootdx.quotes import Quotes
        
        client = Quotes.factory(market='std', server='223.68.206.204:7702')
        
        etfs = []
        
        # 上交所ETF
        sh_etfs = client.bars(stockid="1", frequency=9, start="2024-01-01", offset=1000)
        if sh_etfs is not None and len(sh_etfs) > 0:
            # 提取代码
            codes = set()
            for _, row in sh_etfs.iterrows():
                code = str(row["code"])
                if code.startswith("51") or code.startswith("58"):
                    codes.add(code)
            for code in codes:
                etfs.append({"code": code, "name": f"{code}", "exchange": "SH"})
        
        # 深交所ETF
        sz_etfs = client.bars(stockid="0", frequency=9, start="2024-01-01", offset=1000)
        if sz_etfs is not None and len(sz_etfs) > 0:
            codes = set()
            for _, row in sz_etfs.iterrows():
                code = str(row["code"])
                if code.startswith("15") or code.startswith("16"):
                    codes.add(code)
            for code in codes:
                etfs.append({"code": code, "name": f"{code}", "exchange": "SZ"})
        
        logger.info(f"mootdx ETF列表: {len(etfs)} 只")
        return etfs
        
    except Exception as e:
        logger.error(f"mootdx 获取ETF列表失败: {e}")
        return []


def fetch_realtime_quotes(etfs):
    """从腾讯财经获取实时行情
    
    Args:
        etfs: ETF列表 [{'code': '510050', 'name': '...', 'exchange': 'SH'}, ...]
    
    Returns:
        dict: code -> quote_data
    """
    logger.info(f"正在获取 {len(etfs)} 只ETF的实时行情...")
    
    quotes = {}
    
    # 腾讯行情格式: sh510050, sz159919
    codes_for_tencent = []
    for etf in etfs:
        exchange_prefix = "sh" if etf["exchange"] == "SH" else "sz"
        codes_for_tencent.append(f"{exchange_prefix}{etf['code']}")
    
    try:
        import requests
        
        # 腾讯批量接口
        symbols = ",".join(codes_for_tencent[:500])  # 分批请求
        url = f"https://qt.gtimg.cn/q={symbols}"
        
        resp = requests.get(url, timeout=10)
        resp.encoding = "gbk"
        
        for line in resp.text.strip().split(";"):
            if not line or "=" not in line:
                continue
            parts = line.split("~")
            if len(parts) < 35:
                continue
            
            # 腾讯返回字段索引
            code = parts[2]  # 股票代码
            name = parts[1]  # 股票名称
            price = parts[3]
            open_price = parts[5]
            prev_close = parts[4]
            volume = parts[6]
            amount = parts[7]
            
            # 计算涨跌幅
            try:
                p = float(price)
                pc = float(prev_close)
                pct = ((p - pc) / pc * 100) if pc > 0 else 0
            except (ValueError, ZeroDivisionError):
                p, pct = 0, 0
            
            quotes[code] = {
                "name": name,
                "close": float(p),
                "open": float(open_price) if open_price else 0,
                "prev_close": float(prev_close) if prev_close else 0,
                "volume": float(volume) if volume else 0,
                "amount": float(amount) if amount else 0,
                "change_pct": float(pct),
                "trade_date": time.strftime("%Y-%m-%d")
            }
        
        logger.info(f"腾讯行情获取成功: {len(quotes)} 只")
        
    except Exception as e:
        logger.error(f"腾讯行情获取失败: {e}")
    
    return quotes


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("Phase 1: ETF列表 + 实时行情快照")
    logger.info("=" * 60)
    
    # 1. 初始化数据库
    logger.info("初始化 DuckDB 数据库...")
    con = init_database()
    
    # 2. 获取ETF列表
    etfs = fetch_etf_list_from_eastmoney()
    if not etfs:
        logger.error("无法获取ETF列表，退出")
        con.close()
        return 1
    
    logger.info(f"共获取 {len(etfs)} 只ETF")
    
    # 3. 获取实时行情
    quotes = fetch_realtime_quotes(etfs)
    
    # 4. 合并数据写入数据库
    logger.info("合并数据并写入数据库...")
    
    # 插入/更新 security_info
    for etf in etfs:
        code = etf["code"]
        quote = quotes.get(code, {})
        
        con.execute("""
            INSERT OR REPLACE INTO security_info 
            (code, name, exchange, type, last_updated)
            VALUES (?, ?, ?, 'ETF', CURRENT_TIMESTAMP)
        """, (code, etf.get("name", ""), etf.get("exchange", "")))
        
        # 如果有实时行情，更新估值字段
        if quote:
            con.execute("""
                UPDATE security_info 
                SET pe_ttm = ?, total_market_cap = ?, 
                    float_market_cap = ?, turnover_rate = ?,
                    last_trade_date = ?, last_updated = CURRENT_TIMESTAMP
                WHERE code = ?
            """, (
                quote.get("close", 0),  # 用收盘价暂代PE（后续从其他源补充）
                quote.get("amount", 0) / 100000000 if quote.get("amount") else None,  # 成交额转亿
                None,  # 流通市值
                None,  # 换手率
                quote.get("trade_date"),
                code
            ))
    
    con.commit()
    
    # 5. 输出统计摘要
    total = con.execute("SELECT COUNT(*) FROM security_info WHERE type='ETF'").fetchone()[0]
    with_daily = con.execute("""
        SELECT COUNT(DISTINCT code) FROM security_daily
    """).fetchone()[0]
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 1 完成摘要:")
    logger.info(f"  ETF总数: {total}")
    logger.info(f"  有实时行情: {len(quotes)} 只")
    logger.info(f"  有日线数据: {with_daily} 只")
    logger.info("=" * 60)
    
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
