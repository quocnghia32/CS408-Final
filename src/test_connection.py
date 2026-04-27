"""Smoke test: FIX logon + Redis subscribe + REST balance fetch."""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from paperbroker.client import PaperBrokerClient
from paperbroker.market_data import RedisMarketDataClient


def test_fix_logon() -> bool:
    print("\n=== FIX LOGON ===")
    client = PaperBrokerClient(
        default_sub_account=os.getenv("PAPER_ACCOUNT_ID_D1", "main"),
        username=os.getenv("PAPER_USERNAME"),
        password=os.getenv("PAPER_PASSWORD"),
        rest_base_url=os.getenv("PAPER_REST_BASE_URL"),
        socket_connect_host=os.getenv("SOCKET_HOST"),
        socket_connect_port=int(os.getenv("SOCKET_PORT", "5001")),
        sender_comp_id=os.getenv("SENDER_COMP_ID"),
        target_comp_id=os.getenv("TARGET_COMP_ID", "SERVER"),
        console=False,
    )
    logged_in = {"v": False}
    client.on("fix:logon", lambda **kw: logged_in.__setitem__("v", True))
    print("  connecting...")
    client.connect()
    ok = client.wait_until_logged_on(timeout=20)
    print(f"  logon: {ok}  flag: {logged_in['v']}")
    if not ok:
        print(f"  last error: {client.last_logon_error()}")
        return False

    try:
        bal = client.get_cash_balance()
        print(f"  balance: {bal}")
    except Exception as e:
        print(f"  balance fetch failed: {e}")

    try:
        client.disconnect()
    except Exception:
        pass
    return True


async def test_redis():
    print("\n=== REDIS MARKET DATA ===")
    rc = RedisMarketDataClient(
        host=os.getenv("MARKET_REDIS_HOST"),
        port=int(os.getenv("MARKET_REDIS_PORT", "6380")),
        password=os.getenv("MARKET_REDIS_PASSWORD"),
        merge_updates=True,
    )
    seen = {"n": 0, "px": None}

    async def on_q(instr, q):
        seen["n"] += 1
        if seen["n"] <= 3:
            print(f"  tick #{seen['n']}: {instr} px={q.latest_matched_price}")
        seen["px"] = q.latest_matched_price

    # Front-month: VN30F2605 (May 2026 expiry per arena)
    await rc.subscribe("HNXDS:VN30F2605", on_q)
    print("  subscribed; waiting 10s for ticks...")
    await asyncio.sleep(10)
    print(f"  total ticks: {seen['n']}  last px: {seen['px']}")
    await rc.close()


def main():
    fix_ok = test_fix_logon()
    asyncio.run(test_redis())
    print(f"\nSUMMARY: fix_ok={fix_ok}")


if __name__ == "__main__":
    main()
