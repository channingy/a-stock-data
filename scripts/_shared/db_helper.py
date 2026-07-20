"""共享工具：DuckDB 连接与建表"""

import duckdb
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def get_db_path(config=None):
    """获取数据库文件路径"""
    if config and "storage" in config:
        db_path = config["storage"].get("db_path", "./data/market.db")
    else:
        db_path = "./data/market.db"
    # 转为绝对路径（相对于项目根目录）
    return str(Path(db_path).resolve())


def get_connection(config=None):
    """获取 DuckDB 连接"""
    db_path = get_db_path(config)
    db_dir = os.path.dirname(db_path)
    os.makedirs(db_dir, exist_ok=True)
    
    logger.info(f"DuckDB 数据库路径: {db_path}")
    con = duckdb.connect(db_path)
    return con


def create_tables(con):
    """创建所有表结构"""
    
    # 品种基础信息（统一）
    con.execute("""
        CREATE TABLE IF NOT EXISTS security_info (
            code VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            exchange VARCHAR,
            type VARCHAR NOT NULL DEFAULT 'ETF',
            pe_ttm DECIMAL(12,4),
            pb DECIMAL(12,4),
            total_market_cap DECIMAL(15,2),
            float_market_cap DECIMAL(15,2),
            turnover_rate DECIMAL(8,4),
            first_trade_date DATE,
            last_trade_date DATE,
            total_daily_bars INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 日线K线（统一）
    con.execute("""
        CREATE TABLE IF NOT EXISTS security_daily (
            code VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            "open" DECIMAL(10,4),
            "high" DECIMAL(10,4),
            "low" DECIMAL(10,4),
            "close" DECIMAL(10,4),
            volume BIGINT,
            amount DECIMAL(18,2),
            pe_ttm DECIMAL(12,4),
            pb DECIMAL(12,4),
            turnover_rate DECIMAL(8,4),
            amplitude DECIMAL(8,4),
            change_pct DECIMAL(8,4),
            change_amt DECIMAL(10,4),
            PRIMARY KEY (code, trade_date)
        )
    """)
    
    # 分钟K线（统一）
    con.execute("""
        CREATE TABLE IF NOT EXISTS security_minute (
            code VARCHAR NOT NULL,
            trade_time TIMESTAMP NOT NULL,
            "open" DECIMAL(10,4),
            "high" DECIMAL(10,4),
            "low" DECIMAL(10,4),
            "close" DECIMAL(10,4),
            volume BIGINT,
            amount DECIMAL(18,2),
            frequency VARCHAR NOT NULL,
            PRIMARY KEY (code, trade_time, frequency)
        )
    """)
    
    # 技术指标（统一）
    con.execute("""
        CREATE TABLE IF NOT EXISTS security_indicators (
            code VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            ma5 DECIMAL(10,4),
            ma10 DECIMAL(10,4),
            ma20 DECIMAL(10,4),
            ma60 DECIMAL(10,4),
            boll_upper DECIMAL(10,4),
            boll_mid DECIMAL(10,4),
            boll_lower DECIMAL(10,4),
            macd_dif DECIMAL(10,4),
            macd_dea DECIMAL(10,4),
            macd_hist DECIMAL(10,4),
            kdj_k DECIMAL(10,4),
            kdj_d DECIMAL(10,4),
            kdj_j DECIMAL(10,4),
            rsi_6 DECIMAL(10,4),
            rsi_12 DECIMAL(10,4),
            rsi_24 DECIMAL(10,4),
            PRIMARY KEY (code, trade_date)
        )
    """)
    
    # ETF扩展信息表
    con.execute("""
        CREATE TABLE IF NOT EXISTS etf_extra_info (
            code VARCHAR PRIMARY KEY REFERENCES security_info(code),
            index_code VARCHAR,
            index_name VARCHAR,
            etf_type VARCHAR,
            manager VARCHAR,
            custodian VARCHAR,
            setup_date DATE,
            list_date DATE,
            mgt_fee DECIMAL(6,4)
        )
    """)
    
    # 个股扩展信息表（预留）
    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_extra_info (
            code VARCHAR PRIMARY KEY REFERENCES security_info(code),
            industry VARCHAR,
            industry_code VARCHAR,
            concept_tags TEXT,
            list_date DATE,
            actual_controller VARCHAR,
            business_scope TEXT,
            industry_rank INTEGER,
            pe_rank DECIMAL(6,4),
            market_segment VARCHAR
        )
    """)
    
    logger.info("所有表创建完成")


def init_database(config=None):
    """初始化数据库（连接 + 建表）"""
    con = get_connection(config)
    create_tables(con)
    return con
