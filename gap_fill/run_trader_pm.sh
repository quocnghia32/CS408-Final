#!/bin/bash
cd /home/tun/Desktop/computational_finance/gap_fill
LOG="logs/trader_$(date +%Y%m%d)_pm.log"
mkdir -p logs
echo "=== Starting PM $(date) ===" >> "$LOG"
python3 src/live_trader.py --pm >> "$LOG" 2>&1
echo "=== Exited PM $(date) ===" >> "$LOG"
