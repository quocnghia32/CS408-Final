# CS408-Final — Claude Context

CS408 final project: live paper-broker trading on VN30F to demonstrate ≥30 trades over 4 trading days. Group14 / sub-account `main` on AlgoTrade arena26.

## Goal priority

**Trade COUNT > Profit.** User explicitly chose to maximize fills/round-trips, not P&L. Current plan: aggressive IVMR for first 2 days (Tue 28/4 + Wed 29/4) to bank trade count, then switch to `gap_fill/` (proven Sharpe +1.23) for last 2 days (Mon 4/5 + Tue 5/5) as the "real strategy" to present to teacher.

The Vietnamese national holidays Apr 30 + May 1 + weekend mean only 4 trading days in the window.

## Current strategy (AM + PM live)

`src/live_trader.py`, deployed via systemd user timers:
- `cs408-final-am.timer` → 08:55 Mon-Fri ICT
- `cs408-final-pm.timer` → 12:55 Mon-Fri ICT

`config/strategy_config.json` (current):
- `direction_mode: TREND` — follow z, don't fade
- `entry_z: 0.0` — fire EVERY bar
- `stop_mult: 1.0`, `target_mult: 2.0` (in σ units)
- `max_hold_bars: 1` — exit after 1 bar via time
- `cooldown_bars: 0`
- `live_eval_seconds: 60` — eval cadence (independent of `bar_size_minutes`)
- `max_trades_per_session: 30`

## How to view trade count

**Broker UI** (`https://papertrade.algotrade.vn` → click `main`) only shows **5 latest** transactions. Use the local script instead:

```bash
cd ~/Nghia/CompFin/CS408-Final
.venv/bin/python src/show_trades.py
```

This calls REST `/api/transaction-by-sub/list` (no FIX, runs alongside live service) and prints: total fills, round-trip closes, gross/fees/net PnL, last 15 fills.

## Monitor (tmux)

```bash
tail -f logs/am.log                          # live signals + trades
journalctl --user -u cs408-final-am.service -f  # systemd-side
cat logs/state.json                          # current trader state snapshot
.venv/bin/python src/show_trades.py          # broker truth (REST)
```

## Switch to gap_fill (Wed evening)

After Wed 29/4 PM session ends (~14:45 ICT), run:
```bash
bash scripts/switch_to_gapfill.sh
```
This stops cs408-final-* and enables cs408-gapfill-{am,pm} timers. Mon 4/5 + Tue 5/5 will run gap_fill (proven OOS Sharpe +1.23).

## Common breakages

**FIX logon fails ("Logged out" immediately):** stale FIX state from previous SIGTERM. Fix:
```bash
systemctl --user stop cs408-final-am.service
rm -rf orders.db orders.db-shm orders.db-wal logs/client_fix_messages
systemctl --user start cs408-final-am.service
```

**Self-trade rejection on entry:** broker has resting close order at similar price. Current mitigation: close LIMIT uses `price ± 1.0` aggressive offset (`_close_now` in `src/live_trader.py`). Remaining ~30-50% rejections are accepted to keep pace high — each rejection is a missed opportunity, not a money loss.

**Friend logs into Group14 simultaneously:** kicks our FIX session. Tell friend to use a different group.

## Critical config / creds (gitignored)

- `database.json` — PostgreSQL `cs408_2026 @ api.algotrade.vn:5432/algotradeDB`
- `.env` — paper broker (Group14, sub `main`, sender_comp_id) + Redis (`52.76.242.46:6380`)
- `paperbroker_client-0.2.4-py3-none-any.whl` — Algotrade SDK (download from `https://papertrade.algotrade.vn/static/docs/downloads/`)

## Layout

```
src/
├── live_trader.py        # async FIX + Redis live trader (TREND IVMR)
├── data.py               # PostgreSQL → 5-min bars + signal
├── strategy.py           # event-driven simulator (backtest)
├── backtest.py           # in/out-sample run + metrics
├── optimize.py           # grid search
├── plot_results.py       # equity / drawdown / pnl-distribution PNGs
├── show_trades.py        # query broker REST, print live trade summary
└── test_connection.py    # smoke test FIX + Redis + REST balance
config/strategy_config.json
gap_fill/                 # secondary strategy (proven), enabled via switch script
scripts/switch_to_gapfill.sh
test_src/, test_config/, test_logs/    # parallel forward-test sandbox (dry-run, isolated state)
doc/trades_papertrade.csv  # local trade log (only EXIT events from current run)
logs/am.log, logs/pm.log   # systemd run logs
logs/state.json            # current trader state snapshot
```
