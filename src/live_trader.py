"""
Live IVMR (Intraday VWAP Reversion) trader for VN30F on AlgoTrade paper broker.

Workflow:
  1. Start before session open (08:50 AM or 12:50 PM).
  2. Fetch last LOOKBACK_DAYS of 5-min bars → seed sigma estimate.
  3. Subscribe to Redis live ticks; build live session VWAP from ticks.
  4. Every BAR_SECONDS (default 30s) re-evaluate signal:
        z = (last_price - session_vwap) / sigma
        |z| >= ENTRY_Z → fade with LIMIT order at last_price
  5. After fill: monitor for target (session_vwap), stop, time-up; close.
  6. Re-enter up to MAX_TRADES_PER_SESSION times after COOLDOWN_BARS.
  7. Force-close at session end.

Usage:
    python3 src/live_trader.py             # AM session
    python3 src/live_trader.py --pm        # PM session
    python3 src/live_trader.py --dry-run   # no real orders
"""
import asyncio
import json
import logging
import os
import sys
from collections import deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Event as ThreadEvent

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from data import fetch_5min_bars, add_signal, get_ticker_expiry

STATE_FILE  = ROOT / "logs" / "state.json"
TRADES_FILE = ROOT / "doc" / "trades_papertrade.csv"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Strategy parameters (loaded from config but overridable) ──────────────────
_CFG = json.loads((ROOT / "config" / "strategy_config.json").read_text())
ENTRY_Z              = float(_CFG["entry_z"])
STOP_MULT            = float(_CFG["stop_mult"])
TARGET_MULT          = float(_CFG.get("target_mult", 2.0))
DIRECTION_MODE       = str(_CFG.get("direction_mode", "TREND")).upper()
MAX_HOLD_BARS        = int(_CFG["max_hold_bars"])
COOLDOWN_BARS        = int(_CFG["cooldown_bars"])
MIN_BARS_IN_SESSION  = int(_CFG["min_bars_in_session"])
MAX_TRADES_PER_SESSION = int(_CFG["max_trades_per_session"])
SIGMA_WINDOW_BARS    = int(_CFG["sigma_window_bars"])
N_CONTRACTS          = int(_CFG["contracts"])
COST_PER_SIDE        = float(_CFG["transaction_cost"]) + float(_CFG["slippage"])
BAR_SECONDS          = int(_CFG["bar_size_minutes"]) * 60
LOOKBACK_DAYS        = 30

# ── Session guards ────────────────────────────────────────────────────────────
AM_FORCE_CLOSE = time(11, 29, 0)
PM_FORCE_CLOSE = time(14, 44, 0)

# ── Broker / Redis config ─────────────────────────────────────────────────────
PAPER_USERNAME = os.getenv("PAPER_USERNAME")
PAPER_PASSWORD = os.getenv("PAPER_PASSWORD")
SENDER_COMP_ID = os.getenv("SENDER_COMP_ID")
TARGET_COMP_ID = os.getenv("TARGET_COMP_ID", "SERVER")
REST_BASE_URL  = os.getenv("PAPER_REST_BASE_URL")
SOCKET_HOST    = os.getenv("SOCKET_HOST")
SOCKET_PORT    = int(os.getenv("SOCKET_PORT", "5001"))
SUB_ACCOUNT    = os.getenv("PAPER_ACCOUNT_ID_D1", "main")
REDIS_HOST     = os.getenv("MARKET_REDIS_HOST")
REDIS_PORT     = int(os.getenv("MARKET_REDIS_PORT", "6380"))
REDIS_PASSWORD = os.getenv("MARKET_REDIS_PASSWORD")


def _write_state(data: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATE_FILE.write_text(json.dumps(data, indent=2, default=str))


def _log_trade(row: dict):
    TRADES_FILE.parent.mkdir(exist_ok=True)
    header = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if header:
            f.write(",".join(row.keys()) + "\n")
        f.write(",".join(str(v) for v in row.values()) + "\n")


def get_front_month_symbol() -> str:
    today   = date.today()
    tickers = get_ticker_expiry()
    active  = tickers[tickers["expdate"] >= today]
    if active.empty:
        raise RuntimeError("No active VN30F contract found.")
    front = active.loc[active["expdate"].idxmin(), "ticker"]
    log.info(f"Front-month: {front}  (expires {active['expdate'].min()})")
    return f"HNXDS:{front}"


def seed_sigma() -> float:
    today     = date.today()
    start_str = (today - timedelta(days=LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    end_str   = today.strftime("%Y-%m-%d")
    log.info(f"Seeding sigma from history {start_str} → {end_str} ...")
    bars = fetch_5min_bars(start_str, end_str, bar_minutes=5)
    bars = add_signal(bars, sigma_window=SIGMA_WINDOW_BARS)
    sigma = float(bars["sigma"].dropna().iloc[-1])
    log.info(f"Seed sigma = {sigma:.3f} pts")
    return sigma


class IVMRTrader:
    def __init__(self, symbol: str, sigma_seed: float, dry_run: bool, session: str):
        self.symbol     = symbol
        self.sigma      = sigma_seed
        self.dry_run    = dry_run
        self.session    = session
        self.force_close_time  = AM_FORCE_CLOSE if session == "am" else PM_FORCE_CLOSE
        self.session_open_time = time(9, 0, 0) if session == "am" else time(13, 0, 0)

        self.cum_pv  = 0.0
        self.cum_vol = 0.0
        self.last_vol = None
        self.last_price = None
        self.session_vwap = None
        self.bar_count = 0
        self.recent_closes = deque(maxlen=SIGMA_WINDOW_BARS)
        self.last_bar_t = None
        self.last_bar_close = None

        self.in_pos = False
        self.entry_px = None
        self.entry_time = None
        self.direction = None
        self.stop_level = None
        self.bars_held = 0
        self.entry_z = None
        self.entry_vwap = None
        self.entry_sigma = None
        self.entry_bar = None

        self.trades_done = 0
        self.cooldown_left = 0
        self.entry_order_id = None
        self.target_order_id = None
        self.client = None
        self._loop  = None
        self._done  = asyncio.Event()
        self._entry_filled = ThreadEvent()
        _write_state(self._snapshot())

    def _snapshot(self) -> dict:
        return {
            "symbol":        self.symbol,
            "session":       self.session.upper(),
            "trades_done":   self.trades_done,
            "max_trades":    MAX_TRADES_PER_SESSION,
            "in_pos":        self.in_pos,
            "direction":     self.direction,
            "entry_px":      self.entry_px,
            "stop_level":    self.stop_level,
            "session_vwap":  self.session_vwap,
            "sigma":         self.sigma,
            "last_price":    self.last_price,
            "bar_count":     self.bar_count,
            "cooldown_left": self.cooldown_left,
            "dry_run":       self.dry_run,
        }

    # ── FIX callbacks ────────────────────────────────────────────────────────
    def _on_logon(self, **kw):
        log.info("FIX session established.")

    def _on_fill(self, cl_ord_id, last_px, **kw):
        if not self.in_pos and cl_ord_id == self.entry_order_id:
            self.entry_px = float(last_px)
            self.stop_level = (
                self.entry_px - STOP_MULT * self.entry_sigma if self.direction == "LONG"
                else self.entry_px + STOP_MULT * self.entry_sigma
            )
            self.target_level = (
                self.entry_px + TARGET_MULT * self.entry_sigma if self.direction == "LONG"
                else self.entry_px - TARGET_MULT * self.entry_sigma
            )
            self.in_pos = True
            self.bars_held = 0
            self.entry_bar = self.bar_count
            log.info(f"ENTRY filled @ {self.entry_px:.2f}  "
                     f"target={self.target_level:.2f}  stop={self.stop_level:.2f}")
            _write_state(self._snapshot())
            self._entry_filled.set()
        elif self.in_pos and cl_ord_id == self.target_order_id:
            exit_px = float(last_px)
            self._record_exit(exit_px, "target")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._after_exit("target"), self._loop)

    def _on_reject(self, cl_ord_id, reason, **kw):
        log.error(f"Order rejected: {reason}")
        if self.in_pos:
            self.in_pos = False
        # cancel pending entry
        self.entry_order_id = None
        _write_state(self._snapshot())

    # ── Tick handler (Redis) ─────────────────────────────────────────────────
    async def _on_price(self, instrument, quote):
        price = quote.latest_matched_price
        if price is None:
            return
        self.last_price = float(price)
        # accumulate session VWAP from total_matched_quantity diffs
        vol_total = quote.total_matched_quantity
        if vol_total is not None:
            if self.last_vol is None:
                # first tick of session: treat as cumulative starting point
                self.last_vol = float(vol_total)
                inc = 0.0
            else:
                inc = max(float(vol_total) - self.last_vol, 0.0)
                self.last_vol = float(vol_total)
            if inc > 0:
                self.cum_pv  += float(price) * inc
                self.cum_vol += inc
            if self.cum_vol > 0:
                self.session_vwap = self.cum_pv / self.cum_vol
        if self.session_vwap is None:
            self.session_vwap = float(price)

        # Stop / target monitoring (intra-bar)
        if self.in_pos and self.stop_level is not None:
            stop_hit = ((self.direction == "LONG"  and price <= self.stop_level) or
                        (self.direction == "SHORT" and price >= self.stop_level))
            tgt_hit  = ((self.direction == "LONG"  and price >= self.target_level) or
                        (self.direction == "SHORT" and price <= self.target_level))
            if stop_hit:
                log.warning(f"STOP hit @ {price:.2f}  stop={self.stop_level:.2f}")
                await self._close_now(float(price), "stop")
            elif tgt_hit:
                log.info(f"TARGET hit @ {price:.2f}  target={self.target_level:.2f}")
                await self._close_now(float(price), "target")

    # ── Decision loop (every BAR_SECONDS) ────────────────────────────────────
    async def _evaluate_loop(self):
        now_t = datetime.now().time()
        if now_t >= self.force_close_time:
            log.warning(f"Already past session end {self.force_close_time} "
                        f"(now={now_t}). Exiting — systemd timer will run again next session.")
            self._done.set()
            return
        # wait until session opens
        while datetime.now().time() < self.session_open_time:
            await asyncio.sleep(1)
        log.info(f"Session {self.session.upper()} opened — starting evaluation loop.")
        # reset session VWAP at first session bar
        self.cum_pv = 0.0
        self.cum_vol = 0.0
        self.last_vol = None
        await asyncio.sleep(BAR_SECONDS)  # wait first bar

        while not self._done.is_set():
            now = datetime.now().time()
            if now >= self.force_close_time:
                if self.in_pos:
                    await self._close_now(self.last_price or self.entry_px, "session_end")
                self._done.set()
                break

            self.bar_count += 1
            if self.last_price is not None:
                self.recent_closes.append(self.last_price)
                if len(self.recent_closes) >= 10:
                    import statistics as _stats
                    self.sigma = max(_stats.pstdev(self.recent_closes), 0.5)

            # update bars_held + time-up exit
            if self.in_pos:
                self.bars_held = self.bar_count - (self.entry_bar or self.bar_count)
                if self.bars_held >= MAX_HOLD_BARS:
                    await self._close_now(self.last_price or self.entry_px, "time")

            # decision: try to enter
            if (not self.in_pos
                and self.cooldown_left == 0
                and self.trades_done < MAX_TRADES_PER_SESSION
                and self.bar_count >= MIN_BARS_IN_SESSION
                and self.session_vwap is not None
                and self.last_price is not None
                and self.sigma > 0):
                z = (self.last_price - self.session_vwap) / self.sigma
                log.info(f"[bar {self.bar_count}] px={self.last_price:.2f} "
                         f"vwap={self.session_vwap:.2f} sigma={self.sigma:.2f} "
                         f"z={z:+.3f} thresh=±{ENTRY_Z}")
                if abs(z) >= ENTRY_Z:
                    if DIRECTION_MODE == "FADE":
                        self.direction = "SHORT" if z > 0 else "LONG"
                    else:  # TREND
                        self.direction = "LONG" if z > 0 else "SHORT"
                    self.entry_z     = z
                    self.entry_sigma = self.sigma
                    self.entry_vwap  = self.session_vwap
                    await self._place_entry(self.last_price)

            if self.cooldown_left > 0:
                self.cooldown_left -= 1
            _write_state(self._snapshot())
            await asyncio.sleep(BAR_SECONDS)

    async def _place_entry(self, ref_px: float):
        side = "BUY" if self.direction == "LONG" else "SELL"
        log.info(f"Placing {side} LIMIT  {N_CONTRACTS}×{self.symbol} @ {ref_px:.2f}")
        self.entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.dry_run:
            self.entry_px   = ref_px
            self.stop_level = (ref_px - STOP_MULT * self.entry_sigma if self.direction == "LONG"
                               else ref_px + STOP_MULT * self.entry_sigma)
            self.target_level = (ref_px + TARGET_MULT * self.entry_sigma if self.direction == "LONG"
                                 else ref_px - TARGET_MULT * self.entry_sigma)
            self.in_pos = True
            self.entry_bar = self.bar_count
            log.info(f"[DRY] entry={ref_px:.2f} target={self.target_level:.2f} stop={self.stop_level:.2f}")
            _write_state(self._snapshot())
            return
        oid = self.client.place_order(
            full_symbol=self.symbol, side=side, qty=N_CONTRACTS,
            price=ref_px, ord_type="LIMIT",
        )
        self.entry_order_id = oid
        log.info(f"Entry order sent: {oid[:16]}...")

    async def _close_now(self, price: float, reason: str):
        if not self.in_pos:
            return
        close_side = "SELL" if self.direction == "LONG" else "BUY"
        if self.dry_run:
            log.info(f"[DRY] close {close_side} @ {price:.2f}  reason={reason}")
        else:
            try:
                if self.target_order_id:
                    self.client.cancel_order(self.target_order_id)
            except Exception as e:
                log.warning(f"cancel target failed: {e}")
            self.client.place_order(
                full_symbol=self.symbol, side=close_side, qty=N_CONTRACTS,
                price=price, ord_type="LIMIT",
            )
        self._record_exit(price, reason)
        await self._after_exit(reason)

    def _record_exit(self, exit_px: float, reason: str):
        pnl_gross = ((exit_px - self.entry_px) if self.direction == "LONG"
                     else (self.entry_px - exit_px)) * N_CONTRACTS
        pnl_net = pnl_gross - 2 * COST_PER_SIDE * N_CONTRACTS
        log.info(f"EXIT {reason} @ {exit_px:.2f}  PnL={pnl_net:+.2f} pts  "
                 f"[trade {self.trades_done+1}/{MAX_TRADES_PER_SESSION}]")
        _log_trade({
            "entry_time": self.entry_time or "",
            "exit_time":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session":    self.session.upper(),
            "direction":  self.direction or "",
            "entry_px":   f"{self.entry_px:.2f}" if self.entry_px else "",
            "exit_px":    f"{exit_px:.2f}",
            "z":          f"{self.entry_z:.3f}" if self.entry_z is not None else "",
            "vwap":       f"{self.entry_vwap:.2f}" if self.entry_vwap else "",
            "sigma":      f"{self.entry_sigma:.2f}" if self.entry_sigma else "",
            "stop":       f"{self.stop_level:.2f}" if self.stop_level else "",
            "pnl_pts":    f"{pnl_net:.4f}",
            "exit_reason": reason,
            "dry_run":    str(self.dry_run),
        })
        self.in_pos = False
        self.trades_done += 1

    async def _after_exit(self, reason: str):
        self.cooldown_left = COOLDOWN_BARS
        self.entry_px = None
        self.stop_level = None
        if (self.trades_done >= MAX_TRADES_PER_SESSION
            or datetime.now().time() >= self.force_close_time):
            log.info(f"Session done — {self.trades_done} trade(s).")
            self._done.set()

    async def run(self):
        self._loop = asyncio.get_event_loop()
        from paperbroker.market_data import RedisMarketDataClient

        redis = RedisMarketDataClient(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
            merge_updates=True,
        )
        tag = "[DRY] " if self.dry_run else ""
        log.info(f"{tag}Subscribing to {self.symbol} via Redis...")
        await redis.subscribe(self.symbol, self._on_price)
        log.info("Redis subscription ready.")

        if not self.dry_run:
            from paperbroker.client import PaperBrokerClient
            self.client = PaperBrokerClient(
                default_sub_account=SUB_ACCOUNT,
                username=PAPER_USERNAME, password=PAPER_PASSWORD,
                rest_base_url=REST_BASE_URL,
                socket_connect_host=SOCKET_HOST, socket_connect_port=SOCKET_PORT,
                sender_comp_id=SENDER_COMP_ID, target_comp_id=TARGET_COMP_ID,
                console=False,
            )
            self.client.on("fix:logon",          lambda **kw: self._on_logon(**kw))
            self.client.on("fix:order:filled",   lambda **kw: self._on_fill(**kw))
            self.client.on("fix:order:rejected", lambda **kw: self._on_reject(**kw))
            log.info("Connecting to FIX...")
            self.client.connect()
            if not self.client.wait_until_logged_on(timeout=15):
                log.error(f"FIX logon failed: {self.client.last_logon_error()}")
                await redis.close()
                return
            log.info("FIX ready.")

        log.info("Starting evaluation loop.")
        await asyncio.gather(self._evaluate_loop())
        await redis.close()
        log.info("Done.")


async def main(dry_run: bool, session: str):
    log.info("=" * 60)
    log.info(f"  VN30F IVMR Live Trader  [{session.upper()}]  "
             f"{'DRY-RUN' if dry_run else 'LIVE'}")
    log.info(f"  mode={DIRECTION_MODE}  entry_z={ENTRY_Z}  "
             f"stop={STOP_MULT}σ  target={TARGET_MULT}σ  "
             f"max_hold={MAX_HOLD_BARS}  max_trades={MAX_TRADES_PER_SESSION}")
    log.info("=" * 60)
    symbol     = get_front_month_symbol()
    sigma_seed = seed_sigma()
    trader = IVMRTrader(symbol, sigma_seed, dry_run=dry_run, session=session)
    await trader.run()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    session = "pm" if any(a in sys.argv for a in ("--pm", "--session=pm")) else "am"
    try:
        asyncio.run(main(dry_run=dry_run, session=session))
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        os._exit(0)
