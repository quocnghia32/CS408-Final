# CS408-Final — Intraday VWAP Reversion (IVMR) on VN30F

**Hypothesis:** Intraday deviations from session VWAP on VN30F (5-min bars) follow short-term momentum — when |z| = |close − session_vwap| / sigma ≥ ENTRY_Z, fire in the deviation's direction (TREND mode).

**Selected params (in-sample 2024 → OOS 2025–Q1 2026):**

| Param | Value |
|---|---|
| direction_mode | TREND |
| entry_z | 0.5 |
| stop_mult | 1.0 σ |
| target_mult | 2.0 σ |
| max_hold_bars | 4 (20 min) |
| cooldown_bars | 0 |
| max_trades_per_session | 20 |
| sigma_window_bars | 20 |
| contracts | 1 |

**Backtest:**

| Window | Trades | TPD | Sharpe | Win % | Avg PnL (pts) | Total |
|---|---|---|---|---|---|---|
| In-sample 2024 | 2,091 | 8.36 | -3.05 | 35.5% | -0.36 | -763 pts |
| OOS 2025–Apr 2026 | 2,845 | 8.78 | -0.91 | 39.0% | -0.25 | -715 pts |

**Projected 4-day live (Tue–Fri):** ~33–35 trades, ≈ -1M VND fake P&L expected.

---

## Layout

```
CS408-Final/
├── .env                         # broker + Redis creds (gitignored)
├── database.json                # PostgreSQL creds (gitignored)
├── paperbroker_client-0.2.4-py3-none-any.whl
├── .venv/                       # uv-managed env
├── config/strategy_config.json
├── src/
│   ├── data.py                  # PostgreSQL → 5-min bars + signal
│   ├── strategy.py              # signal logic + event-driven simulator
│   ├── backtest.py              # in/out-sample run + metrics
│   ├── optimize.py              # grid search
│   ├── live_trader.py           # async FIX + Redis live trader
│   └── test_connection.py       # smoke test FIX + Redis
├── doc/
│   ├── bars_{insample,outsample}.csv
│   ├── trades_{insample,outsample}.csv
│   └── optimization_results.csv
├── logs/
│   ├── am.log, pm.log           # systemd run logs
│   └── state.json               # live state snapshot
├── run_trader.sh                # AM entrypoint
└── run_trader_pm.sh             # PM entrypoint
```

## Setup (already done)

```bash
uv venv .venv
UV_HTTP_TIMEOUT=300 uv pip install --python .venv/bin/python \
    psycopg2-binary pandas numpy scipy optuna matplotlib python-dotenv \
    redis "redis[asyncio]>=5.0.0" \
    ./paperbroker_client-0.2.4-py3-none-any.whl
```

## Run

```bash
# Backtest pipeline
.venv/bin/python src/data.py                    # fetch bars (~30s)
.venv/bin/python src/backtest.py                # default config backtest
.venv/bin/python src/optimize.py                # grid search (~5 min)

# Live trader (manual)
.venv/bin/python src/live_trader.py             # AM session (09:00–11:29 ICT)
.venv/bin/python src/live_trader.py --pm        # PM session (13:00–14:44 ICT)
.venv/bin/python src/live_trader.py --dry-run   # no real orders
```

## Systemd timers (deployed)

```bash
systemctl --user list-timers cs408-final-am.timer cs408-final-pm.timer

# Manual control
systemctl --user start  cs408-final-am.service
systemctl --user stop   cs408-final-am.service
systemctl --user disable cs408-final-am.timer cs408-final-pm.timer

# Logs
journalctl --user -u cs408-final-am.service -n 100
tail -f logs/am.log
cat logs/state.json   # current live state
```

| Timer | Schedule (ICT) |
|---|---|
| `cs408-final-am.timer` | Mon–Fri 08:55 |
| `cs408-final-pm.timer` | Mon–Fri 12:55 |

The trader starts 5 min before each session, seeds sigma from the prior 60 days of ticks, connects FIX + Redis, then evaluates every 5-min bar starting at session open. It places **LIMIT orders only** (paper broker rejects MARKET as PendingNew).

## Critical implementation notes

- **Sub-account:** `main` (NOT `D1`) — confirmed via REST `/api/fix-account-info/sub-accounts` and Group14 dashboard.
- **Front-month symbol:** auto-detected via `quote.ticker` table; currently `HNXDS:VN30F2605`.
- **FIX events:** the SDK fires `last_px` (FIX tag 31), not `avg_px`.
- **Cost model:** 0.25 pts per side (no slippage on LIMIT) → 0.5 pts round-trip.

## Known limits

- Group14 dashboard shows large pre-existing unrealized P&L; this trader does not check or close pre-existing positions. Verify portfolio before relying on PnL totals.
- Sigma is seeded once from history; live updates use rolling stdev of 20 most recent bar closes.
- No Kafka fallback — Redis-only market data.
