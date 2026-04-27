"""
IVMR backtester. Loads bars CSV produced by data.py, runs simulate(), saves trades + metrics.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from strategy import simulate, trades_to_df


def load_bars(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def compute_metrics(trades: pd.DataFrame, bars: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    n_days = bars["date"].nunique()
    pnl = trades["pnl_net"].values
    wins = (pnl > 0).sum()
    sharpe = (pnl.mean() / pnl.std() * np.sqrt(252) ) if pnl.std() > 0 else 0.0
    eq = pnl.cumsum()
    peak = np.maximum.accumulate(eq)
    dd   = eq - peak
    return {
        "trades":       int(len(trades)),
        "trading_days": int(n_days),
        "trades_per_day": round(len(trades) / max(n_days, 1), 2),
        "win_pct":      round(100 * wins / len(trades), 2),
        "avg_pnl_pts":  round(pnl.mean(), 4),
        "median_pnl":   round(float(np.median(pnl)), 4),
        "total_pnl_pts": round(float(pnl.sum()), 2),
        "max_drawdown_pts": round(float(dd.min()), 2),
        "sharpe":       round(float(sharpe), 3),
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
    }


def run(label: str, fname: str, params: dict) -> dict:
    bars = load_bars(ROOT / "doc" / f"bars_{fname}.csv")
    trades = simulate(bars, params)
    df = trades_to_df(trades)
    out = ROOT / "doc" / f"trades_{fname}.csv"
    df.to_csv(out, index=False)
    metrics = compute_metrics(df, bars)
    print(f"\n=== {label} ({fname}) ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    print(f"  Saved {out.name}")
    return metrics


if __name__ == "__main__":
    cfg = json.loads((ROOT / "config" / "strategy_config.json").read_text())
    run("In-sample",     "insample",  cfg)
    run("Out-of-sample", "outsample", cfg)
