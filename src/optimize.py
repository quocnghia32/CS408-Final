"""
Grid search over strategy params on in-sample data.
Selection: highest Sharpe among combos with trades_per_day >= MIN_TPD.
If none meet TPD, fall back to highest Sharpe among top-trade-count combos.
Writes optimization_results.csv and prints top 10.
"""
import json
import sys
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from strategy import simulate, trades_to_df
from backtest import load_bars, compute_metrics

MIN_TPD = 7.5

GRID = {
    "direction_mode": ["FADE", "TREND"],
    "entry_z":        [0.7, 0.85, 1.0, 1.25, 1.5],
    "stop_mult":      [1.0, 1.5, 2.0],
    "target_mult":    [1.0, 1.5, 2.0],          # only matters in TREND mode
    "max_hold_bars":  [4, 6, 10],
}


def main():
    cfg = json.loads((ROOT / "config" / "strategy_config.json").read_text())
    bars = load_bars(ROOT / "doc" / "bars_insample.csv")

    rows = []
    keys = list(GRID.keys())
    combos = list(product(*[GRID[k] for k in keys]))
    print(f"Running {len(combos)} param combos on in-sample bars (n={len(bars):,})...")

    for i, combo in enumerate(combos, 1):
        params = dict(cfg)
        for k, v in zip(keys, combo):
            params[k] = v
        # in FADE mode target_mult is ignored — dedupe by skipping duplicate target_mults
        if params["direction_mode"] == "FADE" and params["target_mult"] != 1.0:
            continue
        trades = simulate(bars, params)
        df = trades_to_df(trades)
        m = compute_metrics(df, bars)
        rows.append({**dict(zip(keys, combo)),
                     **{k: v for k, v in m.items() if k != "exit_reasons"}})
        if i % 25 == 0 or i == len(combos):
            print(f"  [{i}/{len(combos)}] sample {dict(zip(keys, combo))} → "
                  f"trades={m.get('trades',0)} sh={m.get('sharpe',0)} "
                  f"tpd={m.get('trades_per_day',0)}")

    res = pd.DataFrame(rows)
    out = ROOT / "doc" / "optimization_results.csv"
    res.to_csv(out, index=False)

    elig = res[res["trades_per_day"] >= MIN_TPD].copy()
    if elig.empty:
        print(f"\nNO combo met trades_per_day >= {MIN_TPD}. Fallback: top 30% by tpd.")
        cutoff = res["trades_per_day"].quantile(0.7)
        elig = res[res["trades_per_day"] >= cutoff].copy()

    elig = elig.sort_values("sharpe", ascending=False)
    print(f"\nTop 15 by Sharpe (filter):")
    print(elig.head(15).to_string(index=False))

    best = elig.iloc[0]
    print("\n=== BEST PARAMS ===")
    for k in keys:
        print(f"  {k}: {best[k]}")
    print(f"  → sharpe={best['sharpe']}  trades={best['trades']}  "
          f"tpd={best['trades_per_day']}  win%={best['win_pct']}  "
          f"avg_pnl={best['avg_pnl_pts']}  total={best['total_pnl_pts']}")
    print(f"\nSaved {out.name}")


if __name__ == "__main__":
    main()
