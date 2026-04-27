#!/bin/bash
# Switch live trading from IVMR (cs408-final) → gap_fill (cs408-gapfill).
# Run AFTER the last IVMR session of Wed 29/4 PM (i.e., after 14:45 ICT Wed 29/4).
#
# Effect:
#   - Stops any running cs408-final-* services
#   - Disables IVMR timers (so no fire next morning)
#   - Enables gap_fill timers (will fire 08:55 + 12:55 next trading day)

set -e
echo "=== Stopping IVMR (cs408-final) ==="
systemctl --user stop  cs408-final-am.service cs408-final-pm.service 2>/dev/null || true
systemctl --user disable --now cs408-final-am.timer cs408-final-pm.timer

echo ""
echo "=== Enabling gap_fill (cs408-gapfill) ==="
mkdir -p /home/nghia-nguyen11/Nghia/CompFin/CS408-Final/gap_fill/logs
systemctl --user daemon-reload
systemctl --user enable --now cs408-gapfill-am.timer cs408-gapfill-pm.timer

echo ""
echo "=== Verify ==="
systemctl --user list-timers cs408-final-am.timer cs408-final-pm.timer cs408-gapfill-am.timer cs408-gapfill-pm.timer --all --no-pager
echo ""
echo "Switch complete. Tomorrow's AM session (08:55) will run gap_fill."
echo "Monitor:  tail -f /home/nghia-nguyen11/Nghia/CompFin/CS408-Final/gap_fill/logs/am.log"
