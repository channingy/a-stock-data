#!/usr/bin/env python3
"""Phase 3: 补全受限ETF的早期历史数据

策略: 利用腾讯周线接口获取全量历史数据, 补充日线只能覆盖最近8年的不足

数据源: 腾讯财经 API (周线接口)
- day: 最多2000条 (~8年) ❌ 不够
- week: 最多1100条 (~21年) ✅ 够用
- month: 最多200条 (粒度太粗) ❌ 不推荐

防封措施:
  - 每只请求间隔 10-30 秒随机延迟
  - 每20只额外暂停 60 秒
  - 连续失败5次暂停 30 分钟
  - 使用 curl 而非 urllib (避免 TCP 连接复用触发反爬)
  - NO_PROXY=* 直连模式

用法:
  cd /Users/channing/Work/Trade/a-stock-data
  python3 scripts/collect_weekly_history.py [--resume] [--codes 510050,510300]
"""

import sys
import os
import time
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime

# 强制绕过系统代理
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts._shared.db_helper import init_database
from scripts._shared.logger import setup_logger

logger = setup_logger("collect_weekly_history")


def get_progress_file():
    return Path("./logs/progress_weekly.json")


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


def sleep_random(min_sec=10, max_sec=30):
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"  等待 {delay:.1f} 秒...")
    time.sleep(delay)


def fetch_weekly_data_tencent(code, exchange="SH"):
    """通过 curl 调用腾讯财经 API 拉取单只ETF的历史周线
    
    周线接口可获取约21年数据 (~1100条), 远超日线的8年限制.
    """
    prefix = "sh" if exchange == "SH" else "sz"
    symbol = f"{prefix}{code}"
    
    # 使用周线接口 (week)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},week,,,{2000},"
    
    import subprocess
    result = subprocess.run(
        ["curl", "-s", "--connect-timeout", "10", "-m", "20", url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
    )
    
    if result.returncode != 0:
        logger.warning(f"腾讯周线 {code} curl 失败: {result.stderr.decode()[:80]}")
        return None
    
    try:
        import json
        data = json.loads(result.stdout.decode("utf-8"))
    except Exception as e:
        logger.warning(f"腾讯周线 {code} JSON 解析失败: {e}")
        return None
    
    klines = data.get("data", {}).get(symbol, {}).get("week", [])
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


def create_weekly_table(con):
    """创建 security_weekly 表 (如果不存在)"""
    con.execute("""
        CREATE TABLE IF NOT EXISTS security_weekly (
            code VARCHAR,
            trade_date DATE,
            open DECIMAL(12,2),
            high DECIMAL(12,2),
            low DECIMAL(12,2),
            close DECIMAL(12,2),
            volume BIGINT,
            amount DECIMAL(18,2),
            pe_ttm DECIMAL(12,4),
            pb DECIMAL(12,4),
            turnover_rate DECIMAL(8,4),
            amplitude DECIMAL(8,4),
            change_pct DECIMAL(8,4),
            change_amt DECIMAL(12,4),
            PRIMARY KEY (code, trade_date)
        )
    """)
    logger.info("✅ security_weekly 表已就绪")


def main():
    parser = argparse.ArgumentParser(description="Phase 3: 历史周线数据补全")
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续")
    parser.add_argument(
        "--codes", type=str, default=None, help="指定要拉取的ETF代码，逗号分隔"
    )
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info("Phase 3: 历史周线数据补全 (腾讯API周线接口)")
    logger.info("=" * 70)
    
    con = init_database()
    create_weekly_table(con)
    
    # 获取所有有效ETF列表（不仅限于受限ETF）
    all_etfs_query = """
        SELECT code, name, exchange FROM security_info 
        WHERE type='ETF' AND exclude IS NULL
        ORDER BY first_trade_date ASC
    """
    etfs = con.execute(all_etfs_query).fetchall()
    total_count = len(etfs)
    logger.info(f"需要补全周线数据的ETF总数: {total_count} 只")
    
    if args.codes:
        target_codes = set(args.codes.split(","))
        etfs = [e for e in etfs if e[0] in target_codes]
        logger.info(f"指定拉取 {len(etfs)} 只: {list(target_codes)}")
    
    completed_codes = set()
    if args.resume:
        completed_codes = load_progress()
        logger.info(f"断点续传: 已完成 {len(completed_codes)} 只，跳过")
    
    pending = [e for e in etfs if e[0] not in completed_codes]
    logger.info(f"待处理: {len(pending)} 只 / {total_count} 只")
    
    if not pending:
        logger.info("没有待处理品种，退出")
        con.close()
        return 0
    
    total_bars = 0
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    
    for i, (code, name, exchange) in enumerate(pending, 1):
        logger.info(f"[{i}/{len(pending)}] 拉取 {code} ({name})...")
        
        try:
            records = fetch_weekly_data_tencent(code, exchange or "SH")
            
            if not records:
                logger.warning(f"  {code} 无周线数据")
                fail_count += 1
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    pause_time = min(consecutive_failures * 6, 1800)
                    logger.warning(f"连续失败 {consecutive_failures} 次，暂停 {pause_time} 秒...")
                    time.sleep(pause_time)
                sleep_random(3, 8)
                continue
            
            consecutive_failures = 0
            logger.debug(f"  {code}: 获取 {len(records)} 条周线数据")
            
            # 批量插入到 security_weekly 表
            batch_size = 500
            for j in range(0, len(records), batch_size):
                batch = records[j : j + batch_size]
                con.executemany("""
                    INSERT OR REPLACE INTO security_weekly
                    (code, trade_date, open, high, low, close,
                     volume, amount, pe_ttm, pb, turnover_rate, 
                     amplitude, change_pct, change_amt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (code, r["trade_date"], r["open"], r["high"], r["low"], r["close"],
                     r["volume"], r["amount"], None, None, None, None, None, None)
                    for r in batch
                ])
            
            con.commit()
            
            bar_count = len(records)
            total_bars += bar_count
            success_count += 1
            
            dates = [r["trade_date"] for r in records]
            con.execute("""
                UPDATE security_info 
                SET last_updated = CURRENT_TIMESTAMP 
                WHERE code = ?
            """, (code,))
            con.commit()
            
            logger.info(f"  ✅ {code}: {bar_count} 条周线数据 ({dates[0]} ~ {dates[-1]})")
            
            completed_codes.add(code)
            
            # 每20只保存进度并额外暂停
            if i % 20 == 0:
                save_progress(completed_codes)
                extra_pause = 15
                logger.info(f"  【进度 {i}/{len(pending)}】额外暂停 {extra_pause} 秒...")
                time.sleep(extra_pause)
        
        except Exception as e:
            logger.error(f"  ❌ {code} 失败: {e}")
            fail_count += 1
            consecutive_failures += 1
            if consecutive_failures >= 5:
                time.sleep(300)  # 5分钟
    
    save_progress(completed_codes)
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("Phase 3 完成摘要:")
    logger.info(f"  成功: {success_count} 只")
    logger.info(f"  失败: {fail_count} 只")
    logger.info(f"  总周线记录: {total_bars:,} 条")
    logger.info("=" * 70)
    
    con.close()


if __name__ == "__main__":
    sys.exit(main())
