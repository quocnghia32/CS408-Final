"""Show today's trades from broker REST (no FIX, can run alongside live trader)."""
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE = os.getenv("PAPER_REST_BASE_URL", "https://papertrade.algotrade.vn/accounting")


def main():
    s = requests.Session()
    r = s.post(f"{BASE}/api/fix-account-info/get-fix-id",
               json={"username": os.getenv("PAPER_USERNAME"),
                     "password": os.getenv("PAPER_PASSWORD")},
               timeout=10)
    fid = r.json()["fixAccountID"]

    today = date.today().strftime("%Y-%m-%d")
    r2 = s.post(f"{BASE}/api/transaction-by-sub/list",
                json={"fixAccountID": fid, "subAccountID": "main",
                      "startTime": today, "endTime": today},
                timeout=10)
    items = r2.json().get("items", [])

    closed = [it for it in items if (it.get("pnl") or 0) != 0]
    total_pnl = sum(it.get("pnl", 0) or 0 for it in items)
    total_fee = sum(it.get("totalFee", 0) or 0 for it in items)
    net = total_pnl - total_fee

    print(f"=== Group14 / main / {today} ===")
    print(f"Transactions (fills):  {len(items)}")
    print(f"Round-trip closes:     {len(closed)}")
    print(f"Gross PnL:             {total_pnl:>15,.0f} VND")
    print(f"Total fees:            {total_fee:>15,.0f} VND")
    print(f"Net PnL:               {net:>15,.0f} VND")
    print()
    print(f"Last 15 fills (newest first):")
    print(f"  {'time':19} {'side':5} {'qty':3} {'price':>8} {'fee':>8} {'pnl':>10}")
    for it in items[:15]:
        ts = (it.get("sysTimestamp") or "?")[:19]
        print(f"  {ts:19} {it.get('type'):5} {int(it.get('quantity',0)):3} "
              f"{float(it.get('price',0)):>8.1f} {int(it.get('totalFee',0)):>8} "
              f"{int(it.get('pnl',0) or 0):>10,}")


if __name__ == "__main__":
    main()
