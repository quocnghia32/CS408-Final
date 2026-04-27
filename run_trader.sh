#!/bin/bash
# AM session entrypoint (called by systemd timer at 08:55 ICT Mon-Fri)
set -e
cd "$(dirname "$0")"
exec ./.venv/bin/python -u src/live_trader.py
