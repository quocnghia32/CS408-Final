"""Generate equity curve, drawdown, trade distribution PNGs from backtest CSVs."""
import sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "doc"


def plot_one(fname: str, label: str):
    df = pd.read_csv(DOC / f"trades_{fname}.csv")
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df = df.sort_values("entry_time").reset_index(drop=True)
    pnl = df["pnl_net"].values
    eq  = pnl.cumsum()
    peak = np.maximum.accumulate(eq)
    dd   = eq - peak

    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=False)

    axes[0].plot(df["entry_time"], eq, color="steelblue", lw=1)
    axes[0].axhline(0, color="black", lw=0.5, ls="--")
    axes[0].set_title(f"{label} — cumulative PnL (pts)")
    axes[0].grid(alpha=0.3)

    axes[1].fill_between(df["entry_time"], dd, 0, color="indianred", alpha=0.6)
    axes[1].set_title(f"{label} — drawdown (pts)")
    axes[1].grid(alpha=0.3)

    axes[2].hist(pnl, bins=60, color="steelblue", edgecolor="black")
    axes[2].axvline(0, color="black", lw=0.7, ls="--")
    axes[2].set_title(f"{label} — per-trade PnL distribution (pts), n={len(pnl)}")
    axes[2].set_xlabel("pnl_net (pts)")
    axes[2].grid(alpha=0.3)

    plt.tight_layout()
    out = DOC / f"chart_{fname}.png"
    plt.savefig(out, dpi=110)
    plt.close()
    print(f"saved {out.name}")


if __name__ == "__main__":
    plot_one("insample",  "In-sample 2024")
    plot_one("outsample", "Out-of-sample 2025–Apr 2026")
