# CS408-Final — Intraday VWAP Reversion (IVMR) on VN30F

Intraday VWAP-deviation TREND-following on VN30F front-month, deployed live to AlgoTrade
arena26 paper broker (Group14). Goal: ≥ 30 paper-broker trades in 4 trading days.

See **[REPORT.md](REPORT.md)** for the academic write-up (hypothesis, math, backtest results, charts).

---

## Quick start (fresh clone)

### 1. Prerequisites

```bash
# uv (Python package manager — required, do not use raw pip)
curl -LsSf https://astral.sh/uv/install.sh | sh

# system tools used below
sudo apt install -y systemd python3-venv build-essential
```

### 2. Clone + create venv

```bash
git clone https://github.com/quocnghia32/CS408-Final.git
cd CS408-Final

uv venv .venv
```

### 3. Download the paperbroker SDK wheel

The wheel is **NOT in the repo** (proprietary, not on PyPI). Download from the AlgoTrade
docs page (login required at https://papertrade.algotrade.vn/accounts/login/):

```bash
curl -sSLo paperbroker_client-0.2.4-py3-none-any.whl \
  "https://papertrade.algotrade.vn/static/docs/downloads/paperbroker_client-0.2.4-py3-none-any.64a14680f78f.whl"
```

If that exact filename fails (the suffix hash may rotate), browse to
https://papertrade.algotrade.vn/docs/ → **Downloads → paperbroker_client-0.2.4-py3-none-any.whl**.

### 4. Install Python dependencies

```bash
UV_HTTP_TIMEOUT=300 uv pip install --python .venv/bin/python \
    psycopg2-binary pandas numpy scipy optuna matplotlib python-dotenv \
    redis "redis[asyncio]>=5.0.0" \
    ./paperbroker_client-0.2.4-py3-none-any.whl

# Sanity check
.venv/bin/python -c "from paperbroker.client import PaperBrokerClient; print('ok')"
```

### 5. Credentials

Create **two** gitignored files at the repo root.

**`database.json`** — PostgreSQL (AlgoTrade course DB, ask your instructor):

```json
{
    "host": "api.algotrade.vn",
    "port": 5432,
    "database": "algotradeDB",
    "user":     "<cs408_2026 or your account>",
    "password": "<password>"
}
```

**`.env`** — paper broker + Redis (from your arena26 group account):

```env
PAPER_USERNAME=<your_group_name>
PAPER_PASSWORD=<your_password>
SENDER_COMP_ID=<your_fix_sender_comp_id>
TARGET_COMP_ID=SERVER
PAPER_ACCOUNT_ID_D1=main
PAPER_REST_BASE_URL=https://papertrade.algotrade.vn/accounting
SOCKET_HOST=papertrade.algotrade.vn
SOCKET_PORT=5001

MARKET_REDIS_HOST=52.76.242.46
MARKET_REDIS_PORT=6380
MARKET_REDIS_PASSWORD=<from algotrade>
```

`SENDER_COMP_ID` is shown on the FIX Accounts page after login at
https://papertrade.algotrade.vn/internal-accounts/fix-accounts/.

`PAPER_ACCOUNT_ID_D1` for arena26 is **`main`** (NOT `D1`). Confirm via the dashboard at
https://papertrade.algotrade.vn/.

```bash
chmod 600 .env database.json   # don't leak credentials
```

### 6. Smoke-test connections

```bash
.venv/bin/python src/test_connection.py
```

Expected: `fix_ok=True`, prints your sub-account balance. If after-hours, Redis ticks = 0
(market hours 09:00–11:30 + 13:00–14:45 ICT, Mon–Fri).

---

## Run the backtest pipeline

```bash
# 1. fetch 5-min bars from PostgreSQL → doc/bars_{insample,outsample}.csv (~30 s)
.venv/bin/python src/data.py

# 2. backtest with current config → doc/trades_*.csv + metrics
.venv/bin/python src/backtest.py

# 3. (optional) grid search over 270 combos → doc/optimization_results.csv (~5 min)
.venv/bin/python src/optimize.py

# 4. PNG charts of equity / drawdown / trade distribution → doc/chart_*.png
.venv/bin/python src/plot_results.py
```

Backtest CSVs and charts are gitignored (regenerate locally).

Tweak parameters in **`config/strategy_config.json`**.

---

## Run live (paper broker)

### Manual

```bash
.venv/bin/python src/live_trader.py             # AM session 09:00–11:29 ICT
.venv/bin/python src/live_trader.py --pm        # PM session 13:00–14:44 ICT
.venv/bin/python src/live_trader.py --dry-run   # init flow, no real orders
```

The trader auto-detects the front-month VN30F symbol, seeds σ from the prior 60 days of
ticks, connects FIX + Redis, and starts the 5-min evaluation loop at session open.

### Automated via systemd user timers (recommended)

Service files live at `~/.config/systemd/user/cs408-final-{am,pm}.{service,timer}`. Install with:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cs408-final-am.service <<EOF
[Unit]
Description=CS408-Final IVMR Live Trader (AM session)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/run_trader.sh
StandardOutput=append:$(pwd)/logs/am.log
StandardError=append:$(pwd)/logs/am.log
TimeoutStopSec=10

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/cs408-final-am.timer <<EOF
[Unit]
Description=Run CS408-Final IVMR AM trader at 08:55 Mon-Fri ICT

[Timer]
OnCalendar=Mon..Fri *-*-* 08:55:00
Persistent=false
Unit=cs408-final-am.service

[Install]
WantedBy=timers.target
EOF
```

(Repeat for `cs408-final-pm.{service,timer}` with `run_trader_pm.sh` + `OnCalendar=Mon..Fri *-*-* 12:55:00`.)

Enable, then **enable lingering** so the timer fires when no shell is open:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cs408-final-am.timer cs408-final-pm.timer

sudo loginctl enable-linger $USER

# verify
systemctl --user list-timers cs408-final-am.timer cs408-final-pm.timer
loginctl show-user $USER --property=Linger     # must be Linger=yes
```

Also disable laptop sleep on AC + battery, otherwise systemd won't fire on a sleeping
machine:

```bash
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
```

### Monitor live runs

```bash
systemctl --user list-timers cs408-final-am.timer cs408-final-pm.timer
journalctl --user -u cs408-final-am.service -f
tail -f logs/am.log
cat  logs/state.json                 # current trader state
cat  doc/trades_papertrade.csv       # filled live trades
```

---

## Layout

```
CS408-Final/
├── README.md                 # this file
├── REPORT.md                 # academic write-up (hypothesis, math, backtest, results)
├── .gitignore
├── config/strategy_config.json
├── src/
│   ├── data.py               # PostgreSQL → 5-min bars + signal
│   ├── strategy.py           # signal logic + event-driven simulator
│   ├── backtest.py           # in/out-sample run + metrics
│   ├── optimize.py           # grid search
│   ├── plot_results.py       # equity / drawdown / pnl-distribution PNGs
│   ├── live_trader.py        # async FIX + Redis live trader
│   └── test_connection.py    # smoke test
├── run_trader.sh             # AM entrypoint (called by systemd)
├── run_trader_pm.sh          # PM entrypoint
├── doc/                      # backtest outputs (CSVs gitignored, PNGs committed)
└── logs/                     # live run logs + state.json (gitignored)
```

Files **not** in the repo (you must create or download):
- `database.json`, `.env` — credentials
- `paperbroker_client-0.2.4-py3-none-any.whl` — broker SDK
- `.venv/` — Python environment
- `doc/bars_*.csv`, `doc/trades_*.csv`, `doc/optimization_results.csv` — regenerable
- `logs/*` — created on first run

---

## Critical implementation notes

- **LIMIT orders only.** The exchange silently parks MARKET orders as `PendingNew` forever. The trader uses `ord_type="LIMIT"` exclusively.
- **Sub-account is `main`.** Wrong sub-account → silent `PendingNew`.
- **Fill event uses `last_px` (FIX tag 31)**, not `avg_px`.
- **Redis quote field name** is `total_matched_quantity` (cumulative session volume); diffs give per-tick increment.
- **Cost model**: 0.25 pts per side, slippage 0 (LIMIT fills at chosen price) → 0.5 pts round-trip.
- **Late-night exit guard**: if launched outside session hours, the trader exits cleanly — systemd will retry on the next scheduled fire.

---

## Strategy summary (full version in REPORT.md)

| | |
|---|---|
| Mode | TREND (follow VWAP deviation, do **not** fade) |
| Entry | `\|z\| ≥ 0.3` where `z = (close − session_VWAP) / σ_20` |
| Stop  | `entry ∓ 1.0 × σ_entry` |
| Target | `entry ± 2.0 × σ_entry` |
| Time exit | 1 bar (5 min) |
| Re-entry | cooldown = 0, max 30 trades / session |
| Sizing | 1 contract |

OOS 2025-01 → 2026-04: **18.5 trades/day**, Sharpe −2.46, avg PnL −0.42 pts/trade.
4-trading-day live projection: **~74 trades**, ~−3.1 M VND fake-money cost (≈ 0.7 % of 449.7 M VND paper balance).
