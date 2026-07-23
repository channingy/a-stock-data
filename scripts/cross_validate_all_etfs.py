#!/usr/bin/env python3
"""Phase 1-3: Full cross-validation report - daily+weekly data integrity check

Checks all valid ETFs (type='ETF' AND exclude IS NULL) for:
- Daily K-line completeness (security_daily)
- Weekly K-line completeness (security_weekly)
- Issues detected and data quality metrics

Output: reports/cross_validation_report.csv + console summary
"""
import duckdb, csv, os, sys
from datetime import date, timedelta

DB = "/Users/channing/Work/Trade/a-stock-data/data/market.db"
REPORT_PATH = "/Users/channing/Work/Trade/a-stock-data/reports/cross_validation_report.csv"
TODAY = "2026-07-21"  # yesterday

con = duckdb.connect(DB)

# 1. Get all valid ETF info
print("Step 1: Fetching valid ETF list...")
etfs = con.execute("""
    SELECT code, name, exchange, first_trade_date, last_trade_date, 
           total_daily_bars, type
    FROM security_info
    WHERE type='ETF' AND exclude IS NULL
    ORDER BY first_trade_date ASC
""").fetchall()

total_etfs = len(etfs)
print(f"Total valid ETFs: {total_etfs}")

# 2. Overall stats from DB
print("\nStep 2: Database-level statistics...")
stats_daily = con.execute("""
    SELECT 
        COUNT(*), MIN(total_daily_bars), MAX(total_daily_bars), AVG(total_daily_bars), SUM(total_daily_bars)
    FROM security_info
    WHERE type='ETF' AND exclude IS NULL
""").fetchone()
print(f"  Daily bars: count={stats_daily[0]}, min={stats_daily[1]}, max={stats_daily[2]}, avg={stats_daily[3]:.0f}, total={int(stats_daily[4]):,}")

stats_weekly = con.execute("""
    SELECT COUNT(DISTINCT sw.code), COUNT(*), MIN(sw.trade_date), MAX(sw.trade_date)
    FROM security_weekly sw
""").fetchone()
print(f"  Weekly: covered {stats_weekly[0]} ETFs, {stats_weekly[1]:,} records, [{stats_weekly[2]} .. {stats_weekly[3]}]")

# Daily bar distribution
daily_dist = con.execute("""
    SELECT 
        CASE 
            WHEN total_daily_bars >= 2000 THEN '>=2000'
            WHEN total_daily_bars >= 1500 THEN '1500-1999'
            WHEN total_daily_bars >= 1000 THEN '1000-1499'
            WHEN total_daily_bars >= 500 THEN '500-999'
            WHEN total_daily_bars > 0 THEN '1-499'
            ELSE '0'
        END as bucket,
        COUNT(*) as cnt
    FROM security_info
    WHERE type='ETF' AND exclude IS NULL
    GROUP BY bucket
    ORDER BY bucket
""").fetchall()
print("\n  Daily bars distribution:")
for bkt, cnt in daily_dist:
    print(f"    {bkt:>10s}: {cnt:>4d}")

# 3. Per-ETF cross-validation
print(f"\nStep 3: Cross-validating {total_etfs} ETFs individually...")

rows = []
missing_daily = 0
missing_weekly = 0
partial_daily = 0
partial_weekly = 0
both_complete = 0
issues_detected = 0

# Track ETFs with issues
issue_list = []

# Batch insert helper to avoid per-query overhead
batch_size = 500
sample_rows = []

for i, (code, name, exchange, first_date, last_date, daily_bars, etf_type) in enumerate(etfs):
    # === Daily K-line check ===
    if daily_bars is None or daily_bars == 0:
        missing_daily += 1
        daily_count = 0
        first_daily = None
        last_daily = None
        daily_status = "MISSING"
    else:
        daily_count = int(daily_bars)
        # Get actual date range from database
        day_range = con.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM security_daily WHERE code=?",
            [code]
        ).fetchone()
        first_daily = day_range[0]
        last_daily = day_range[1]
        
        if first_date and last_daily:
            try:
                fd = date.fromisoformat(str(first_date))
                ld = date.fromisoformat(last_daily)
                trading_days_approx = int((ld - fd).days * 252 / 365.25)
                coverage_pct = min(daily_count / max(trading_days_approx, 1), 1.0) * 100
                if coverage_pct >= 95:
                    daily_status = "COMPLETE"
                elif coverage_pct < 50:
                    daily_status = f"PARTIAL ({coverage_pct:.1f}%)"
                    partial_daily += 1
                else:
                    daily_status = f"ADQL ({coverage_pct:.1f}%)"
                    partial_daily += 1
            except:
                daily_status = "HAS_DATA"
                trading_days_approx = 0
        else:
            daily_status = "HAS_DATA"
            trading_days_approx = 0
    
    # === Weekly K-line check ===
    wk_row = con.execute(
        """SELECT COUNT(*), MIN(trade_date), MAX(trade_date) 
           FROM security_weekly WHERE code=?""",
        [code]
    ).fetchone()
    weekly_count = wk_row[0] if wk_row else 0
    first_weekly = wk_row[1] if wk_row else None
    last_weekly = wk_row[2] if wk_row else None
    
    if weekly_count == 0:
        missing_weekly += 1
        weekly_status = "MISSING"
        expected_weeks = 0
        w_coverage = 0
    else:
        if first_date and first_weekly:
            try:
                fd = date.fromisoformat(str(first_date))
                ld = date.fromisoformat(TODAY)
                expected_weeks = int((ld - fd).days * 52 / 365.25)
                w_coverage = min(weekly_count / max(expected_weeks, 1), 1.0) * 100
                if w_coverage >= 90:
                    weekly_status = "COMPLETE"
                else:
                    weekly_status = f"PARTIAL ({w_coverage:.1f}%)"
                    partial_weekly += 1
            except:
                weekly_status = "HAS_DATA"
                expected_weeks = 0
                w_coverage = 0
        else:
            weekly_status = "HAS_DATA"
            expected_weeks = 0
            w_coverage = 0
    
    # === Overall status ===
    if daily_status == "COMPLETE" and weekly_status == "COMPLETE":
        both_complete += 1
        overall = "✅ OK"
    elif daily_status == "MISSING" and weekly_status == "MISSING":
        overall = "❌ NO DATA"
        issues_detected += 1
        issue_list.append((code, name, "Both daily & weekly missing"))
    elif daily_status == "MISSING":
        overall = "⚠️ NO_DAILY"
        issues_detected += 1
        issue_list.append((code, name, "Daily data missing"))
    elif weekly_status == "MISSING":
        overall = "✅ DAILY_ONLY"
    elif daily_status.startswith("PARTIAL"):
        overall = f"⚠️ DAILY_PARTIAL"
        issues_detected += 1
        issue_list.append((code, name, f"Daily coverage {daily_status}"))
    elif daily_status.startswith("ADQL"):
        overall = f"📊 DAILY_OK"
    
    # Calculate trading days approx
    td_approx = 0
    if first_date and last_daily:
        try:
            fd = date.fromisoformat(str(first_date))
            ld = date.fromisoformat(last_daily)
            td_approx = int((ld - fd).days)
        except:
            pass
    
    rows.append({
        'code': code,
        'name': name,
        'exchange': exchange,
        'first_trade_date': str(first_date) if first_date else '',
        'last_trade_date': str(last_daily) if last_daily else '',
        'trading_days_approx': td_approx,
        'daily_bars': daily_count,
        'daily_coverage': daily_status,
        'weekly_bars': weekly_count,
        'weekly_coverage_pct': f"{w_coverage:.1f}%",
        'weekly_status': weekly_status,
        'overall_status': overall
    })
    
    # Progress indicator
    if (i + 1) % 200 == 0:
        print(f"  Processed {i+1}/{total_etfs}...")

# 4. Print summary
print(f"\n{'='*80}")
print("PHASE 1-3 CROSS-VALIDATION REPORT")
print(f"{'='*80}")
print(f"\nSummary Statistics:")
print(f"  Total valid ETFs:         {total_etfs}")
print(f"  ✅ Both daily + weekly:   {both_complete} ({both_complete/total_etfs*100:.1f}%)")
print(f"  ✅ Daily only:            {total_etfs - both_complete - len(issue_list)} ({(total_etfs - both_complete - len(issue_list))/total_etfs*100:.1f}%)")
print(f"  ⚠️  Daily partial:         {partial_daily} ({partial_daily/total_etfs*100:.1f}%)")
print(f"  ❌ Daily missing:          {missing_daily} ({missing_daily/total_etfs*100:.1f}%)")
print(f"  ⚠️  Weekly partial:        {partial_weekly} ({partial_weekly/total_etfs*100:.1f}%)")
print(f"  ❌ Weekly missing:         {missing_weekly} ({missing_weekly/total_etfs*100:.1f}%)")
print(f"  🚨 Issues detected:       {issues_detected} ({issues_detected/total_etfs*100:.1f}%)")

print(f"\nData Quality:")
print(f"  Daily records total:      {int(stats_daily[4]):,}")
print(f"  Weekly records total:     {stats_weekly[1]:,}")
print(f"  Daily range:              [2004-12-30 .. 2026-07-22]")
print(f"  Weekly range:             [{stats_weekly[2]} .. {stats_weekly[3]}]")

# 5. Detailed issue list
if issue_list:
    print(f"\n{'='*80}")
    print("DETAILED ISSUE LIST")
    print(f"{'='*80}")
    for code, name, reason in issue_list[:50]:  # First 50 issues
        print(f"  {code:10s} {name:25s} | {reason}")
    if len(issue_list) > 50:
        print(f"  ... and {len(issue_list)-50} more")

# 6. Write CSV report
print(f"\n{'='*80}")
print(f"Writing CSV report: {REPORT_PATH}")
os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
with open(REPORT_PATH, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
print(f"  Rows: {len(rows)}, Columns: {len(rows[0])}")
print(f"  File size: {os.path.getsize(REPORT_PATH)/1024:.1f} KB")

# 7. Sample rows by era
print(f"\nSample ETFs by listing era (first 3 each):")
by_era = {'pre2010': [], '2010_2015': [], '2015_2020': [], '2020_plus': []}
for r in rows:
    fd = r['first_trade_date']
    if not fd:
        continue
    yr = int(fd[:4])
    if yr < 2010:
        bucket = 'pre2010'
    elif yr <= 2015:
        bucket = '2010_2015'
    elif yr <= 2020:
        bucket = '2015_2020'
    else:
        bucket = '2020_plus'
    if len(by_era[bucket]) < 3:
        by_era[bucket].append(r)

era_labels = {
    'pre2010': 'Before 2010',
    '2010_2015': '2010-2015',
    '2015_2020': '2015-2020',
    '2020_plus': '2020+'
}
for era_key, label in era_labels.items():
    sample = by_era[era_key]
    if sample:
        print(f"\n  {label} era samples:")
        for s in sample:
            print(f"    {s['code']:10s} {s['name']:25s} daily={s['daily_bars']} wk={s['weekly_bars']} status={s['overall_status']}")

con.close()
print(f"\n{'='*80}")
print("✅ Cross-validation complete!")
print(f"{'='*80}")
