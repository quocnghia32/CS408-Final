"""
One-shot test: place 1-lot LIMIT BUY for the front-month VN30F at ask price.
"""
import os, sys, time, json, logging
from pathlib import Path
from datetime import date
import redis as r
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from data import _get_ticker_expiry
from paperbroker.client import PaperBrokerClient

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ── Current ask from Redis ─────────────────────────────────────────────────
rc = r.Redis(host=os.getenv("MARKET_REDIS_HOST"),
             port=int(os.getenv("MARKET_REDIS_PORT", 6380)),
             password=os.getenv("MARKET_REDIS_PASSWORD"),
             decode_responses=True)
raw = json.loads(rc.get("HNXDS:VN30F2605"))
ask = raw["ask_price_1"]["value"]
bid = raw["bid_price_1"]["value"]
last = raw["latest_matched_price"]["value"]
log.info(f"Market: bid={bid}  ask={ask}  last={last}")

# ── Symbol ─────────────────────────────────────────────────────────────────
tickers = _get_ticker_expiry()
active  = tickers[tickers["expdate"] >= date.today()]
symbol  = f"HNXDS:{active.loc[active['expdate'].idxmin(), 'ticker']}"
log.info(f"Symbol: {symbol}")

# ── FIX client — use sub-account from API discovery ───────────────────────
SUB = "main"

client = PaperBrokerClient(
    default_sub_account=SUB,
    username=os.getenv("PAPER_USERNAME"),
    password=os.getenv("PAPER_PASSWORD"),
    rest_base_url=os.getenv("PAPER_REST_BASE_URL"),
    socket_connect_host=os.getenv("SOCKET_HOST"),
    socket_connect_port=int(os.getenv("SOCKET_PORT", "5001")),
    sender_comp_id=os.getenv("SENDER_COMP_ID"),
    target_comp_id=os.getenv("TARGET_COMP_ID", "SERVER"),
    console=True,
)

events = []
client.on("fix:order:filled",   lambda **kw: events.append(("filled",   kw)))
client.on("fix:order:rejected", lambda **kw: events.append(("rejected", kw)))

log.info("Connecting FIX...")
client.connect()
ok = client.wait_until_logged_on(timeout=15)
if not ok:
    log.error(f"Logon failed: {client.last_logon_error()}")
    sys.exit(1)
log.info("FIX logon OK")

log.info(f"Placing LIMIT BUY 1 lot @ {ask}  (sub={SUB})")
oid = client.place_order(full_symbol=symbol, side="BUY",
                         qty=1, price=ask, ord_type="LIMIT")
log.info(f"Order ID: {oid}")

for i in range(40):
    time.sleep(0.5)
    if events:
        break
    if i % 6 == 5:
        log.info(f"  ... waiting {(i+1)//2}s")

if events:
    ev, kw = events[0]
    log.info(f"{'='*50}")
    log.info(f"  {ev.upper()}: avg_px={kw.get('avg_px')}  qty={kw.get('cum_qty')}  status={kw.get('status')}")
    log.info(f"{'='*50}")
else:
    st = client.get_order_status(oid)
    log.warning(f"No response in 20s — order status: {st}")

client.disconnect()
log.info("Done.")
