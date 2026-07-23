#!/bin/bash
cd /Users/channing/Work/Trade/a-stock-data
LOG="logs/weekly_run_20260723.log"

while true; do
  if pgrep -f "collect_weekly_history.py --resume" > /dev/null 2>&1; then
    done_count=$(grep -c "周线数据 (" "$LOG" 2>/dev/null || echo 0)
    fail_count=$(grep -c "❌" "$LOG" 2>/dev/null || echo 0)
    remaining=$((1355 - done_count))
    if [ "$remaining" -lt 0 ]; then remaining=0; fi
    echo "[$(date '+%H:%M:%S')] $done_count/1355 done, ${fail_count:-0} failed, ~${remaining} remaining"
    last=$(tail -1 "$LOG" 2>/dev/null)
    echo "  Last: ${last:0:120}"
    sleep 90
  else
    done_count=$(grep -c "周线数据 (" "$LOG" 2>/dev/null || echo 0)
    fail_count=$(grep -c "❌" "$LOG" 2>/dev/null || echo 0)
    echo ""
    echo "=== SCRIPT COMPLETED ==="
    echo "Done: $done_count, Failed: ${fail_count:-0}"
    grep "总周线记录" "$LOG" 2>/dev/null || true
    grep "Phase 4" "$LOG" 2>/dev/null | tail -4 || true
    exit 0
  fi
done
