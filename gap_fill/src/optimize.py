"""
Grid-search optimization for Opening Gap Fill strategy on VN30F.
Optimises on in-sample data (2022-2023), ranked by Sharpe ratio.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import sys
import itertools

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest import run_backtest, print_metrics

ROOT = Path(__file__).resolve().parent.parent

PARAM_GRID = {
    "entry_z":   [0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
    "stop_mult": [1.0, 1.5, 2.0, 3.0],
}

MIN_TRADES = 30


def run_grid_search(gap_bars: pd.DataFrame) -> pd.DataFrame:
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    print(f"Running {len(combos)} combinations ...", flush=True)

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        r = run_backtest(gap_bars, **params)
        m = r["metrics"]
        if m.get("n_trades", 0) >= MIN_TRADES:
            results.append({**params, **m})

    df = pd.DataFrame(results).sort_values("sharpe", ascending=False).reset_index(drop=True)
    return df


if __name__ == "__main__":
    gap_is = pd.read_csv(ROOT / "doc" / "gap_insample.csv",
                         index_col=0, parse_dates=True)

    results = run_grid_search(gap_is)
    results.to_csv(ROOT / "doc" / "optimization_results.csv", index=False)

    print(f"\nTotal valid combinations: {len(results)}")
    print("\nTop 10 by Sharpe:")
    print(results.head(10).to_string(index=False))

    if not results.empty:
        best = results.iloc[0]
        print(f"\nBest: entry_z={best['entry_z']}, stop_mult={best['stop_mult']}")
        print(f"      Sharpe={best['sharpe']:.4f}, trades={int(best['n_trades'])}, "
              f"win%={best['win_rate_%']:.1f}, pnl={best['total_pnl_pts']:.2f}")
