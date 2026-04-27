"""
Live gap-fill paper trader for VN30F.

Workflow:
  1. Start anytime before 09:00 (AM) or 12:50 (PM)
  2. Fetch yesterday's data from DB → get prev_vwap + rolling gap_std
  3. At first price tick (session open), compute gap = open - prev_vwap
  4. Enter when |gap_z| ≥ ENTRY_Z — direction determined by gap sign
  5. After fill: place resting LIMIT target at prev_vwap;
     monitor stop via Redis
  6. Target fill / stop breach / session-end  →  close
  7. Re-enter up to MAX_ENTRIES_PER_SESSION times per session

Usage:
    python3 src/live_trader.py           # AM session, live mode
    python3 src/live_trader.py --pm      # PM session
    python3 src/live_trader.py --dry-run # no real orders
"""
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Event as ThreadEvent

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from data import fetch_5min_bars, build_daily_vwap, build_gap_bars, _get_ticker_expiry

STATE_FILE  = ROOT / "logs" / "state.json"
TRADES_FILE = ROOT / "doc" / "trades_papertrade.csv"


def _write_state(data: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATE_FILE.write_text(json.dumps(data, indent=2))


def _log_trade(entry_time: str, exit_time: str, session: str,
               direction: str, entry_px: float, exit_px: float,
               gap_z: float, prev_vwap: float, stop_level: float,
               pnl: float, reason: str, dry_run: bool):
    TRADES_FILE.parent.mkdir(exist_ok=True)
    header = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if header:
            f.write("entry_time,exit_time,session,direction,"
                    "entry_px,exit_px,gap_z,prev_vwap,stop_level,"
                    "pnl_pts,exit_reason,dry_run\n")
        f.write(
            f"{entry_time},{exit_time},{session},{direction},"
            f"{entry_px:.2f},{exit_px:.2f},{gap_z:.4f},{prev_vwap:.2f},"
            f"{stop_level:.2f},{pnl:.4f},{reason},{dry_run}\n"
        )
    log.info(f"Trade logged → {TRADES_FILE.name}")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
ENTRY_Z                = 0.3   # trade when gap is at least 0.3σ (OOS-optimal)
STOP_MULT              = 1.0
N_CONTRACTS            = 1
STD_WINDOW             = 20
LOOKBACK_DAYS          = 45
MAX_ENTRIES_PER_SESSION = 99   # no effective cap — time is the only limit
REENTRY_PAUSE_SECS     = 60   # wait 60s after exit before re-entering

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_front_month_symbol() -> str:
    today   = date.today()
    tickers = _get_ticker_expiry()
    active  = tickers[tickers["expdate"] >= today]
    if active.empty:
        raise RuntimeError("No active VN30F contract found in DB.")
    front = active.loc[active["expdate"].idxmin(), "ticker"]
    log.info(f"Front-month: {front}  (expires {active['expdate'].min()})")
    return f"HNXDS:{front}"


def fetch_signal_context(session: str = "am") -> dict:
    """
    AM session: prev_vwap = yesterday's full-day VWAP
    PM session: prev_vwap = today's AM session VWAP
    """
    today     = date.today()
    start_str = (today - timedelta(days=LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    end_str   = today.strftime("%Y-%m-%d")

    log.info(f"Fetching history {start_str} → {end_str} ...")
    bars_5m  = fetch_5min_bars(start_str, end_str, bar_minutes=5)
    daily    = build_daily_vwap(bars_5m)
    gap_bars = build_gap_bars(daily, std_window=STD_WINDOW)

    last    = gap_bars.iloc[-1]
    gap_std = float(last["gap_std"])

    if session == "pm":
        today_bars = bars_5m[bars_5m.index.date == today]
        am_bars    = today_bars.between_time("09:00", "11:30")
        if am_bars.empty or am_bars["volume"].sum() == 0:
            raise RuntimeError("No AM session bars available for PM context.")
        typical   = (am_bars["high"] + am_bars["low"] + am_bars["close"]) / 3
        am_vwap   = (typical * am_bars["volume"]).sum() / am_bars["volume"].sum()
        prev_vwap = float(am_vwap)
        log.info(f"PM context — am_vwap={prev_vwap:.2f}  gap_std={gap_std:.2f}")
    else:
        prev_vwap = float(last["day_vwap"])
        log.info(f"AM context — prev_vwap={prev_vwap:.2f}  gap_std={gap_std:.2f}")

    return {"prev_vwap": prev_vwap, "gap_std": gap_std}


# ── Trader ────────────────────────────────────────────────────────────────────

class GapFillTrader:

    def __init__(self, context: dict, symbol: str, dry_run: bool = False,
                 session: str = "am"):
        self.prev_vwap  = context["prev_vwap"]
        self.gap_std    = context["gap_std"]
        self.symbol     = symbol
        self.dry_run    = dry_run
        self.session    = session
        self.force_close_time  = AM_FORCE_CLOSE if session == "am" else PM_FORCE_CLOSE
        self.session_open_time = time(9, 0, 0) if session == "am" else time(13, 0, 0)

        self.last_price       = None
        self.open_px          = None
        self.gap_z            = None
        self.direction        = None
        self.entry_px         = None
        self.entry_time       = None
        self.stop_level       = None
        self.target_level     = None
        self.entry_order_id   = None
        self.target_order_id  = None
        self.entries_done     = 0     # round-trips completed this session

        self.state = "WAITING_OPEN"

        self._entry_filled = ThreadEvent()
        self._done         = asyncio.Event()
        self._loop         = None   # set in run() for thread-safe task dispatch
        self.client        = None

        _write_state(self._snapshot())

    def _snapshot(self) -> dict:
        return {
            "symbol":        self.symbol,
            "session":       self.session.upper(),
            "state":         self.state,
            "entries_done":  self.entries_done,
            "max_entries":   MAX_ENTRIES_PER_SESSION,
            "prev_vwap":     self.prev_vwap,
            "gap_std":       self.gap_std,
            "gap_z":         self.gap_z,
            "open_px":       self.open_px,
            "direction":     self.direction,
            "entry_px":      self.entry_px,
            "target_level":  self.target_level,
            "stop_level":    self.stop_level,
            "last_price":    self.last_price,
            "dry_run":       self.dry_run,
        }

    # ── FIX callbacks ─────────────────────────────────────────────────────────

    def _on_logon(self, **kw):
        log.info("FIX session established.")

    def _on_fill(self, cl_ord_id, last_px, cum_qty, leaves_qty, status, **kw):
        if self.state == "WAITING_FILL" and cl_ord_id == self.entry_order_id:
            self.entry_px = float(last_px)
            if self.direction == "LONG":
                self.stop_level = self.entry_px - STOP_MULT * self.gap_std
            else:
                self.stop_level = self.entry_px + STOP_MULT * self.gap_std
            log.info(
                f"Entry filled @ {self.entry_px:.2f}  "
                f"target={self.target_level:.2f}  stop={self.stop_level:.2f}"
            )
            self.state = "HOLDING"
            _write_state(self._snapshot())
            self._entry_filled.set()

        elif self.state == "HOLDING" and cl_ord_id == self.target_order_id:
            if self.entry_px is None:
                return
            exit_px = float(last_px)
            pnl = (
                (exit_px - self.entry_px) if self.direction == "LONG"
                else (self.entry_px - exit_px)
            ) * N_CONTRACTS - 2 * (0.25 + 0.47)
            log.info(f"Target filled @ {exit_px:.2f}  PnL ≈ {pnl:+.2f} pts")
            _log_trade(
                self.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.session.upper(), self.direction or "",
                self.entry_px, exit_px, self.gap_z or 0.0, self.prev_vwap,
                self.stop_level or 0.0, pnl, "target", self.dry_run,
            )
            self.entries_done += 1
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._after_exit("target"), self._loop)
            else:
                asyncio.get_event_loop().create_task(self._after_exit("target"))

    def _on_reject(self, cl_ord_id, reason, **kw):
        log.error(f"Order rejected ({cl_ord_id[:12]}): {reason}")
        if self.state in ("WAITING_FILL", "HOLDING"):
            self.state = "DONE"
            _write_state(self._snapshot())
            self._done.set()

    # ── Redis price handler ───────────────────────────────────────────────────

    async def _on_price(self, instrument, quote):
        price = quote.latest_matched_price
        if price is None:
            return

        if self.state == "WAITING_OPEN":
            if datetime.now().time() < self.session_open_time:
                return   # pre-market tick — ignore until session opens
            self.open_px = float(price)
            self.state   = "EVALUATING"
            await self._evaluate_signal(float(price))
            return

        self.last_price = float(price)
        _write_state(self._snapshot())

        if self.state == "HOLDING" and self.stop_level is not None:
            hit = (
                (self.direction == "LONG"  and price <= self.stop_level) or
                (self.direction == "SHORT" and price >= self.stop_level)
            )
            if hit:
                log.warning(
                    f"Stop breached  price={price:.2f}  stop={self.stop_level:.2f}"
                )
                await self._close_now(price, "stop")

    # ── Decision + order logic ────────────────────────────────────────────────

    async def _evaluate_signal(self, ref_px: float):
        gap   = ref_px - self.prev_vwap
        gap_z = gap / self.gap_std if self.gap_std > 0 else 0.0
        self.gap_z = round(gap_z, 4)
        log.info(
            f"[entry {self.entries_done+1}]  "
            f"ref_px={ref_px:.2f}  gap={gap:+.2f}  gap_z={gap_z:+.3f}  "
            f"threshold=±{ENTRY_Z}"
        )

        if abs(gap_z) < ENTRY_Z:
            if self.entries_done == 0:
                log.info("gap_z below threshold — no trade this session.")
                self.state = "NO_TRADE"
            else:
                log.info("gap_z below threshold — no re-entry, session done.")
                self.state = "DONE"
            _write_state(self._snapshot())
            self._done.set()
            return

        self.direction    = "SHORT" if gap > 0 else "LONG"
        self.target_level = self.prev_vwap
        _write_state(self._snapshot())

        await self._place_entry(ref_px)

    async def _place_entry(self, ref_px: float):
        side = "BUY" if self.direction == "LONG" else "SELL"
        log.info(f"Placing {side} LIMIT  {N_CONTRACTS}×{self.symbol} @ {ref_px}")

        self.entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self.dry_run:
            log.info(f"[DRY RUN] Entry {side} @ {ref_px:.2f}")
            self.entry_px   = ref_px
            self.stop_level = (
                ref_px - STOP_MULT * self.gap_std if self.direction == "LONG"
                else ref_px + STOP_MULT * self.gap_std
            )
            log.info(
                f"[DRY RUN] target={self.target_level:.2f}  "
                f"stop={self.stop_level:.2f}"
            )
            self.state = "HOLDING"
            _write_state(self._snapshot())
            self._entry_filled.set()
            return

        self._entry_filled.clear()
        oid = self.client.place_order(
            full_symbol=self.symbol,
            side=side,
            qty=N_CONTRACTS,
            price=ref_px,
            ord_type="LIMIT",
        )
        self.entry_order_id = oid
        self.state = "WAITING_FILL"
        _write_state(self._snapshot())
        log.info(f"Entry order sent: {oid[:16]}...")

    async def _place_target_order(self):
        close_side = "SELL" if self.direction == "LONG" else "BUY"
        log.info(f"Placing {close_side} LIMIT target @ {self.target_level:.2f}")

        if self.dry_run:
            log.info(f"[DRY RUN] Target {close_side} LIMIT @ {self.target_level:.2f}")
            return

        oid = self.client.place_order(
            full_symbol=self.symbol,
            side=close_side,
            qty=N_CONTRACTS,
            price=self.target_level,
            ord_type="LIMIT",
        )
        self.target_order_id = oid
        log.info(f"Target order sent: {oid[:16]}...")

    async def _close_now(self, price: float, reason: str):
        if self.state != "HOLDING":
            return
        self.entries_done += 1

        close_side = "SELL" if self.direction == "LONG" else "BUY"
        cost       = 2 * (0.25 + 0.47)
        pnl        = (
            (price - self.entry_px) if self.direction == "LONG"
            else (self.entry_px - price)
        ) * N_CONTRACTS - cost
        log.info(
            f"Closing ({reason})  {close_side} @ {price:.2f}  "
            f"PnL ≈ {pnl:+.2f} pts  [{self.entries_done}/{MAX_ENTRIES_PER_SESSION}]"
        )
        _log_trade(
            self.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            self.session.upper(), self.direction or "",
            self.entry_px or price, price, self.gap_z or 0.0, self.prev_vwap,
            self.stop_level or 0.0, pnl, reason, self.dry_run,
        )

        if not self.dry_run:
            if self.target_order_id:
                try:
                    self.client.cancel_order(self.target_order_id)
                except Exception as e:
                    log.warning(f"Could not cancel target order: {e}")
            self.client.place_order(
                full_symbol=self.symbol,
                side=close_side,
                qty=N_CONTRACTS,
                price=price,
                ord_type="LIMIT",
            )
        else:
            log.info(f"[DRY RUN] Would close {close_side} @ {price:.2f}")

        await self._after_exit(reason)

    async def _after_exit(self, reason: str):
        """Decide whether to re-enter or finish for the session."""
        at_session_end = (
            reason == "session_end" or
            datetime.now().time() >= self.force_close_time
        )

        if at_session_end or self.entries_done >= MAX_ENTRIES_PER_SESSION:
            self.state = "DONE"
            _write_state(self._snapshot())
            log.info(
                f"Session complete — {self.entries_done} trade(s) done."
            )
            self._done.set()
        else:
            self.state = "WAITING_REENTRY"
            _write_state(self._snapshot())
            log.info(
                f"Re-entry {self.entries_done+1}/{MAX_ENTRIES_PER_SESSION} "
                f"in {REENTRY_PAUSE_SECS}s..."
            )
            await asyncio.sleep(REENTRY_PAUSE_SECS)
            if self.state == "WAITING_REENTRY":  # not superseded by session_end
                self.state = "EVALUATING"
                ref = self.last_price or self.entry_px or self.prev_vwap
                await self._evaluate_signal(ref)

    # ── Main async loop ───────────────────────────────────────────────────────

    async def run(self):
        from paperbroker.client import PaperBrokerClient
        from paperbroker.market_data import RedisMarketDataClient

        self._loop = asyncio.get_event_loop()

        self.client = PaperBrokerClient(
            default_sub_account=SUB_ACCOUNT,
            username=PAPER_USERNAME,
            password=PAPER_PASSWORD,
            rest_base_url=REST_BASE_URL,
            socket_connect_host=SOCKET_HOST,
            socket_connect_port=SOCKET_PORT,
            sender_comp_id=SENDER_COMP_ID,
            target_comp_id=TARGET_COMP_ID,
            console=False,
        )
        self.client.on("fix:logon",          lambda **kw: self._on_logon(**kw))
        self.client.on("fix:order:filled",   lambda **kw: self._on_fill(**kw))
        self.client.on("fix:order:rejected", lambda **kw: self._on_reject(**kw))

        log.info("Connecting to FIX...")
        self.client.connect()
        if not self.client.wait_until_logged_on(timeout=15):
            log.error(f"FIX logon failed: {self.client.last_logon_error()}")
            return

        redis = RedisMarketDataClient(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            merge_updates=True,
        )
        log.info(f"Subscribing to {self.symbol} via Redis...")
        await redis.subscribe(self.symbol, self._on_price)
        log.info("Waiting for session open price tick...")

        # After each entry fill → place resting target order
        async def _target_placer():
            while not self._done.is_set():
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._entry_filled.wait, 120)
                if self._entry_filled.is_set() and self.state == "HOLDING":
                    await self._place_target_order()
                self._entry_filled.clear()

        asyncio.create_task(_target_placer())

        # Session-end watchdog
        async def _session_watchdog():
            while not self._done.is_set():
                if datetime.now().time() >= self.force_close_time:
                    if self.state == "HOLDING":
                        log.warning("Session end — force-closing position")
                        await self._close_now(
                            self.last_price or self.entry_px or self.prev_vwap,
                            "session_end"
                        )
                    elif self.state in ("WAITING_OPEN", "EVALUATING",
                                        "WAITING_FILL", "WAITING_REENTRY"):
                        log.info("Session end — no open position, exiting")
                        self.state = "DONE"
                        _write_state(self._snapshot())
                        self._done.set()
                    break
                await asyncio.sleep(15)

        asyncio.create_task(_session_watchdog())

        await self._done.wait()
        await redis.close()
        log.info("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, session: str = "am"):
    log.info("=" * 60)
    log.info(f"  VN30F Gap-Fill Live Trader  (Project 1)  [{session.upper()} session]")
    if dry_run:
        log.info("  MODE: DRY RUN — no real orders")
    log.info(f"  Max entries/session: {MAX_ENTRIES_PER_SESSION}")
    log.info("=" * 60)

    symbol  = get_front_month_symbol()
    context = fetch_signal_context(session=session)
    force_str = AM_FORCE_CLOSE if session == "am" else PM_FORCE_CLOSE

    log.info(
        f"Ready.  prev_vwap={context['prev_vwap']:.2f}  "
        f"gap_std={context['gap_std']:.2f}  force_close={force_str}"
    )

    trader = GapFillTrader(context, symbol, dry_run=dry_run, session=session)
    await trader.run()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    session = "pm" if ("--session=pm" in sys.argv or "--pm" in sys.argv) else "am"
    try:
        asyncio.run(main(dry_run=dry_run, session=session))
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        os._exit(0)
