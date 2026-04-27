"""
Live dashboard for the VN30F gap-fill trader.
Reads logs/state.json (written by live_trader.py) and shows a terminal display.

Usage:
    python3 src/monitor.py
    Ctrl+C to exit
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

STATE_FILE = ROOT / "logs" / "state.json"

REDIS_HOST     = os.getenv("MARKET_REDIS_HOST")
REDIS_PORT     = int(os.getenv("MARKET_REDIS_PORT", "6380"))
REDIS_PASSWORD = os.getenv("MARKET_REDIS_PASSWORD")

# ANSI color codes
R  = "\033[91m"   # red
G  = "\033[92m"   # green
Y  = "\033[93m"   # yellow
B  = "\033[94m"   # blue
C  = "\033[96m"   # cyan
W  = "\033[97m"   # white bold
DIM = "\033[2m"
RST = "\033[0m"


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _session_status() -> str:
    t = datetime.now().time()
    if   t < __import__("datetime").time(8, 59):  return f"{DIM}Pre-market{RST}"
    elif t < __import__("datetime").time(11, 30): return f"{G}AM session open{RST}"
    elif t < __import__("datetime").time(13,  0): return f"{DIM}Midday break{RST}"
    elif t < __import__("datetime").time(14, 45): return f"{G}PM session open{RST}"
    else:                                          return f"{DIM}After market{RST}"


def _color_state(state: str) -> str:
    colors = {
        "WAITING_OPEN":  Y,
        "EVALUATING":    C,
        "NO_TRADE":      DIM,
        "WAITING_FILL":  Y,
        "HOLDING":       G,
        "DONE":          B,
    }
    return f"{colors.get(state, W)}{state}{RST}"


def _pnl_color(val: float) -> str:
    if val > 0:   return f"{G}+{val:.2f}{RST}"
    elif val < 0: return f"{R}{val:.2f}{RST}"
    return f"{val:.2f}"


def render(state: dict, live_price: float | None):
    os.system("clear")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"{W}{'─'*58}{RST}")
    print(f"{W}  VN30F Gap-Fill Trader — Live Monitor{RST}")
    print(f"{W}{'─'*58}{RST}")
    print(f"  Time     : {now}   {_session_status()}")
    print(f"  Symbol   : {C}{state.get('symbol', '—')}{RST}")
    sess = state.get("session", "")
    if sess:
        print(f"  Session  : {W}{sess}{RST}")
    if state.get("dry_run"):
        print(f"  Mode     : {Y}DRY RUN{RST}")
    print()

    # ── Signal context ─────────────────────────────────────────────────────
    print(f"  {'─'*54}")
    print(f"  {'SIGNAL CONTEXT':}")
    print(f"  {'─'*54}")
    pv  = state.get("prev_vwap")
    gst = state.get("gap_std")
    gz  = state.get("gap_z")
    opx = state.get("open_px")
    print(f"  Prev VWAP  : {W}{pv:.2f}{RST}" if pv else "  Prev VWAP  : —")
    print(f"  Gap Std    : {pv and gst and f'{gst:.2f}' or '—'}")
    if opx:
        print(f"  Open Price : {W}{opx:.2f}{RST}")
    if gz is not None:
        col = G if abs(gz) >= 1.25 else DIM
        print(f"  Gap Z      : {col}{gz:+.3f}{RST}  (threshold ±1.25σ)")
    print()

    # ── Position ───────────────────────────────────────────────────────────
    print(f"  {'─'*54}")
    print(f"  {'POSITION':}")
    print(f"  {'─'*54}")
    trader_state = state.get("state", "—")
    done = state.get("entries_done", 0)
    mx   = state.get("max_entries", 3)
    print(f"  Status     : {_color_state(trader_state)}  ({done}/{mx} trades)")

    direction = state.get("direction")
    entry_px  = state.get("entry_px")
    target    = state.get("target_level")
    stop      = state.get("stop_level")
    price     = live_price or state.get("last_price")

    if direction:
        dir_col = G if direction == "LONG" else R
        print(f"  Direction  : {dir_col}{direction}{RST}")

    if entry_px:
        print(f"  Entry      : {W}{entry_px:.2f}{RST}")
    if target:
        print(f"  Target     : {G}{target:.2f}{RST}")
    if stop:
        print(f"  Stop       : {R}{stop:.2f}{RST}")

    if price:
        print(f"  Last Price : {W}{price:.2f}{RST}")

    # Unrealized PnL + distance bars
    if entry_px and price and direction:
        raw = (price - entry_px) if direction == "LONG" else (entry_px - price)
        pnl = raw - 2 * (0.25 + 0.47)
        print(f"  Unreal PnL : {_pnl_color(pnl)} pts")

        if target and stop:
            rng = abs(target - entry_px) + abs(stop - entry_px)
            if rng > 0:
                if direction == "LONG":
                    pct = (price - stop) / (target - stop) * 100
                else:
                    pct = (stop - price) / (stop - target) * 100
                pct = max(0, min(100, pct))
                bar_len = 30
                filled  = int(pct / 100 * bar_len)
                bar = G + "█" * filled + RST + DIM + "░" * (bar_len - filled) + RST
                print(f"  Progress   : [{bar}] {pct:.0f}%")
                print(f"             {R}stop{RST}{'':>{filled + 1}}{G}target{RST}")
    print()

    # ── Footer ─────────────────────────────────────────────────────────────
    updated = state.get("updated_at", "—")
    print(f"  {DIM}State file updated: {updated}{RST}")
    print(f"  {DIM}Ctrl+C to exit monitor (trader keeps running){RST}")
    print(f"{W}{'─'*58}{RST}")


async def main():
    # Try to connect to Redis for live price
    live_price = None
    redis = None
    symbol = None

    state = _read_state()
    symbol = state.get("symbol")

    if symbol and REDIS_HOST:
        try:
            from paperbroker.market_data import RedisMarketDataClient

            def _on_tick(inst, quote):
                nonlocal live_price
                if quote.latest_matched_price:
                    live_price = float(quote.latest_matched_price)

            redis = RedisMarketDataClient(
                host=REDIS_HOST, port=REDIS_PORT,
                password=REDIS_PASSWORD, merge_updates=True,
            )
            await redis.subscribe(symbol, _on_tick)
        except Exception as e:
            print(f"Redis unavailable ({e}), using state file prices only.")
            await asyncio.sleep(2)

    try:
        while True:
            state = _read_state()
            render(state, live_price)
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if redis:
            await redis.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
