#!/bin/bash
# PM session entrypoint (called by systemd timer at 12:55 ICT Mon-Fri)
set -e
cd "$(dirname "$0")"
exec ./.venv/bin/python -u src/live_trader.py --pm
