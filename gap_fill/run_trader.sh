#!/bin/bash
cd /home/tun/Desktop/computational_finance/gap_fill
LOG="logs/trader_$(date +%Y%m%d)_am.log"
mkdir -p logs
echo "=== Starting AM $(date) ===" >> "$LOG"
python3 src/live_trader.py >> "$LOG" 2>&1
echo "=== Exited AM $(date) ===" >> "$LOG"
