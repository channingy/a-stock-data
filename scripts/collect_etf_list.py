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
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

# 绕过系统代理（macOS科学上网工具拦截东财/腾讯API）
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger

logger = setup_logger("collect_etf_list")


def fetch_etf_list_from_eastmoney():
    """从东财 push2 API 获取全部ETF列表

    返回: list of dicts with code, name, exchange
    """
    logger.info("正在获取东财ETF列表...")

    try:
        import requests

        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": 5000,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0+t:60+m:1+t:80",  # 沪深A股
            "fields": "f12,f14",
        }

        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if data.get("rc") != 0:
            logger.warning(f"东财API返回异常: rc={data.get('rc')}")
            return []

        items = data.get("data", {}).get("diff", [])
        if not items:
            logger.warning("东财API返回空列表")
            return []

        # 过滤出ETF (代码以 51/15/16/11 开头的通常是ETF)
        etf_pattern = re.compile(r"^(51|15|16|11)\d{4}$")
        etfs = []
        for item in items:
            code = str(item.get("f12", ""))
            name = str(item.get("f14", ""))
            if not code or not name:
                continue
            # ETF 代码特征: 51xxxx(沪ETF), 15xxxx(深ETF), 16xxxx(LOF), 11xxxx(债券)
            if etf_pattern.match(code):
                exchange = "SH" if code.startswith("5") else "SZ"
                etfs.append({"code": code, "name": name, "exchange": exchange})

        logger.info(f"东财筛选出 {len(etfs)} 只ETF")
        return etfs

    except ImportError:
        logger.error("requests 模块不可用")
        return []
    except Exception as e:
        logger.warning(f"东财API获取失败: {e}，尝试腾讯财经备选方案...")
        return fetch_etf_list_from_tencent()


def fetch_etf_list_from_tencent():
    """通过腾讯财经批量行情接口获取ETF列表（备选方案）

    扫描所有可能的ETF代码范围(51xxxx, 58xxxx, 159xxx, 15xxxx, 16xxxx)，
    通过腾讯行情接口批量查询，筛选出有有效价格的品种。
    """
    logger.info("使用腾讯财经备选方案获取ETF列表...")

    try:
        import requests

        # 定义ETF代码范围
        ranges = [
            ("sh", 510000, 519999),  # 沪ETF
            ("sh", 588000, 588999),  # 科创板ETF
            ("sz", 159000, 159999),  # 深ETF
            ("sz", 150000, 150999),  # 深LOF/ETF
            ("sz", 160000, 169999),  # 深LOF
        ]

        all_candidates = []
        for prefix, start, end in ranges:
            for i in range(start, end + 1):
                all_candidates.append(f"{prefix}{i}")

        logger.info(f"扫描 {len(all_candidates)} 个代码...")

        # 分批查询（每批500个）
        found = {}
        batch_size = 500
        for batch_start in range(0, len(all_candidates), batch_size):
            batch = all_candidates[batch_start : batch_start + batch_size]
            symbols = ",".join(batch)
            url = f"https://qt.gtimg.cn/q={symbols}"
            resp = requests.get(url, timeout=15)
            resp.encoding = "gbk"

            for line in resp.text.strip().split(";"):
                if "=" not in line:
                    continue
                parts = line.split("~")
                if len(parts) < 5:
                    continue
                code = parts[2]
                name = parts[1]
                price = parts[3]
                try:
                    float(price)
                    found[code] = {"name": name, "price": price}
                except ValueError:
                    pass

        # 筛选ETF
        etfs = []
        for code, info in found.items():
            if code.startswith(("51", "58", "159", "150", "16")):
                exchange = "SH" if code.startswith(("51", "58")) else "SZ"
                etfs.append(
                    {"code": code, "name": info["name"], "exchange": exchange}
                )

        logger.info(f"腾讯备选方案获取到 {len(etfs)} 只ETF")
        return etfs

    except Exception as e:
        logger.error(f"腾讯备选方案获取失败: {e}")
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

        # 腾讯接口每次最多查500个
        batch_size = 500
        for batch_start in range(0, len(codes_for_tencent), batch_size):
            batch = codes_for_tencent[batch_start : batch_start + batch_size]
            symbols = ",".join(batch)
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
                code = parts[2]  # 股票代码 (如 "510050")
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
                    "trade_date": time.strftime("%Y-%m-%d"),
                }

        logger.info(f"腾讯行情获取成功: {len(quotes)} 只")

    except ImportError:
        logger.error("requests 模块不可用")
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

        con.execute(
            """
            INSERT OR REPLACE INTO security_info
            (code, name, exchange, type, last_updated)
            VALUES (?, ?, ?, 'ETF', CURRENT_TIMESTAMP)
        """,
            (code, etf.get("name", ""), etf.get("exchange", "")),
        )

        # 如果有实时行情，更新估值字段
        if quote:
            con.execute(
                """
                UPDATE security_info
                SET pe_ttm = ?, total_market_cap = ?,
                    float_market_cap = ?, turnover_rate = ?,
                    last_trade_date = ?, last_updated = CURRENT_TIMESTAMP
                WHERE code = ?
            """,
                (
                    quote.get("close", 0),  # 用收盘价暂代PE（后续从其他源补充）
                    (
                        quote.get("amount", 0) / 100000000
                        if quote.get("amount")
                        else None
                    ),  # 成交额转亿
                    None,  # 流通市值
                    None,  # 换手率
                    quote.get("trade_date"),
                    code,
                ),
            )

    con.commit()

    # 5. 输出统计摘要
    total = con.execute("SELECT COUNT(*) FROM security_info WHERE type='ETF'").fetchone()[
        0
    ]
    with_daily = con.execute("SELECT COUNT(DISTINCT code) FROM security_daily").fetchone()[
        0
    ]

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
