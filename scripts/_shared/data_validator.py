"""共享工具：数据验证器"""

import duckdb
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DataValidator:
    """数据完整性验证器"""
    
    def __init__(self, con):
        self.con = con
    
    def validate_daily_bars(self, code=None):
        """验证日线记录数"""
        query = """
            SELECT code, COUNT(*) as bar_count,
                   MIN(trade_date) as first_date,
                   MAX(trade_date) as last_date
            FROM security_daily
            GROUP BY code
            ORDER BY code
        """
        if code:
            query += " WHERE code = ?"

        result = self.con.execute(query, (code,) if code else ()).fetchall()
        
        warnings = []
        errors = []
        
        for row in result:
            c, count, first, last = row
            if count < 30:
                errors.append(f"{c}: 仅 {count} 条记录（最少30条）")
            elif count < 100:
                warnings.append(f"{c}: 仅 {count} 条记录")
        
        return {
            "total_codes": len(result),
            "total_bars": sum(r[1] for r in result),
            "warnings": warnings,
            "errors": errors
        }
    
    def validate_nulls(self, table="security_daily", code=None):
        """验证空值"""
        required_fields = ["open", "high", "low", "close", "volume"]
        fields_str = ", ".join(required_fields)
        
        query = f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN {" OR ".join(f'{f} IS NULL' for f in required_fields)} THEN 1 ELSE 0 END) as null_count
            FROM {table}
        """
        if code:
            query += f" WHERE code = ?"
        
        row = self.con.execute(query, (code,) if code else ()).fetchone()
        total = row[0]
        null_count = row[1]
        null_pct = (null_count / total * 100) if total > 0 else 0
        
        status = "PASS" if null_pct < 0.01 else "FAIL"
        logger.info(f"空值检查: {status} — {null_count}/{total} ({null_pct:.4f}%)")
        
        return {
            "status": status,
            "total": total,
            "null_count": null_count,
            "null_pct": null_pct
        }
    
    def validate_price_logic(self, code=None):
        """验证价格逻辑"""
        query = """
            SELECT COUNT(*) as violations
            FROM security_daily
            WHERE high < GREATEST("open", "close")
               OR low > LEAST("open", "close")
               OR "open" <= 0 OR "high" <= 0 OR "low" <= 0 OR "close" <= 0
        """
        if code:
            query += " AND code = ?"
        
        violations = self.con.execute(query, (code,)).fetchone()[0]
        status = "PASS" if violations == 0 else "FAIL"
        logger.info(f"价格逻辑检查: {status} — {violations} 条异常")
        
        return {"status": status, "violations": violations}
    
    def validate_volume(self, code=None):
        """验证成交量"""
        query = """
            SELECT COUNT(*) as violations
            FROM security_daily
            WHERE volume < 0
        """
        if code:
            query += " AND code = ?"
        
        violations = self.con.execute(query, (code,)).fetchone()[0]
        status = "PASS" if violations == 0 else "FAIL"
        logger.info(f"成交量检查: {status} — {violations} 条异常")
        
        return {"status": status, "violations": violations}
    
    def validate_security_list(self):
        """验证品种列表完整性"""
        # 获取 security_info 中的品种
        info_result = self.con.execute("SELECT code, type FROM security_info").fetchall()
        info_codes = {row[0] for row in info_result}
        
        # 获取 daily 数据中的品种
        daily_result = self.con.execute("SELECT DISTINCT code FROM security_daily").fetchall()
        daily_codes = {row[0] for row in daily_result}
        
        missing_in_daily = info_codes - daily_codes
        extra_in_daily = daily_codes - info_codes
        
        return {
            "info_count": len(info_codes),
            "daily_count": len(daily_codes),
            "missing_in_daily": list(missing_in_daily)[:10],
            "extra_in_daily": list(extra_in_daily)[:10]
        }
    
    def run_full_validation(self):
        """运行完整验证"""
        logger.info("=" * 60)
        logger.info(f"  数据完整性验证报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        results = {}
        
        # 1. 日线记录数
        logger.info("\n[1/5] 日线记录数检查...")
        results["bar_count"] = self.validate_daily_bars()
        logger.info(f"  品种数: {results['bar_count']['total_codes']}, 总记录: {results['bar_count']['total_bars']:,}")
        for w in results["bar_count"]["warnings"][:5]:
            logger.info(f"  ⚠ {w}")
        
        # 2. 空值检查
        logger.info("\n[2/5] 空值检查...")
        results["nulls"] = self.validate_nulls()
        
        # 3. 价格逻辑
        logger.info("\n[3/5] 价格逻辑检查...")
        results["price"] = self.validate_price_logic()
        
        # 4. 成交量
        logger.info("\n[4/5] 成交量检查...")
        results["volume"] = self.validate_volume()
        
        # 5. 品种列表
        logger.info("\n[5/5] 品种列表完整性检查...")
        results["list"] = self.validate_security_list()
        
        # 汇总
        logger.info("\n" + "=" * 60)
        errors = (results.get("bar_count", {}).get("errors", []) +
                  results.get("price", {}).get("violations", 0) > 0 and ["价格逻辑异常"] or [])
        
        if errors:
            logger.info(f"  验证结果: FAIL — {len(errors)} 个错误")
        else:
            warnings_count = len(results.get("bar_count", {}).get("warnings", []))
            logger.info(f"  验证结果: PASS ({warnings_count} 个警告)")
        logger.info("=" * 60)
        
        return results
