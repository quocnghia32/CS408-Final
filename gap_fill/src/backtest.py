"""
Gap-Fill backtest engine for VN30F Opening Gap strategy.

Entry  : at today's open when |gap_z| > entry_z
Target : price returns to prev_vwap (gap fills)
Stop   : price moves stop_mult * gap_std further against us from entry
Day-end: force-close at today's close if neither target nor stop hit

Costs: COST_PER_SIDE applied on both entry and exit.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategy import generate_gap_signals

ROOT = Path(__file__).resolve().parent.parent

COST_PER_SIDE = 0.25 + 0.47   # fee + slippage per side (index points)


def run_backtest(
    gap_bars:     pd.DataFrame,
    entry_z:      float = 1.0,
    stop_mult:    float = 2.0,
    n_contracts:  int   = 1,
) -> dict:
    """
    Backtest the Opening Gap Fill strategy on daily OHLCV+gap data.

    Parameters
    ----------
    gap_bars  : DataFrame from data.build_gap_bars()
                Must have: open, high, low, close, prev_vwap, gap, gap_std, gap_z
    entry_z   : z-score threshold to trigger a trade (|gap_z| > entry_z)
    stop_mult : stop = entry ± stop_mult * gap_std  (opposite side of gap)
    """
    df = generate_gap_signals(gap_bars, entry_z)

    trades     = []
    equity_pts = 0.0
    equity_ts  = []

    for i, (date, row) in enumerate(df.iterrows()):
        if not (row["long_entry"] or row["short_entry"]):
            equity_ts.append({"date": date, "equity": equity_pts})
            continue

        entry_px  = row["open"]
        target    = row["prev_vwap"]     # gap-fill target
        stop_dist = stop_mult * row["gap_std"]

        if row["long_entry"]:
            # Opened too far below prev_vwap → buy, expect price to rise to prev_vwap
            stop_level  = entry_px - stop_dist
            target_hit  = row["high"] >= target
            stop_hit    = row["low"]  <= stop_level
            direction   = "long"
        else:
            # Opened too far above prev_vwap → sell, expect price to fall to prev_vwap
            stop_level  = entry_px + stop_dist
            target_hit  = row["low"]  <= target
            stop_hit    = row["high"] >= stop_level
            direction   = "short"

        # Determine exit
        if target_hit and (not stop_hit):
            exit_px = target
            reason  = "target"
        elif stop_hit and (not target_hit):
            exit_px = stop_level
            reason  = "stop"
        elif target_hit and stop_hit:
            # Both triggered — for gap-fill assume target came first
            exit_px = target
            reason  = "target"
        else:
            exit_px = row["close"]
            reason  = "day_end"

        if direction == "long":
            pnl = (exit_px - entry_px - 2 * COST_PER_SIDE) * n_contracts
        else:
            pnl = (entry_px - exit_px - 2 * COST_PER_SIDE) * n_contracts

        equity_pts += pnl
        trades.append({
            "date":        date,
            "direction":   direction,
            "entry_px":    round(entry_px, 2),
            "exit_px":     round(exit_px, 2),
            "target":      round(target, 2),
            "stop_level":  round(stop_level, 2),
            "gap_z":       round(row["gap_z"], 4),
            "pnl_pts":     round(pnl, 4),
            "exit_reason": reason,
        })
        equity_ts.append({"date": date, "equity": equity_pts})

    trades_df = pd.DataFrame(trades)
    equity_s  = pd.DataFrame(equity_ts).set_index("date")["equity"]
    metrics   = compute_metrics(trades_df, equity_s)
    return {"trades": trades_df, "equity": equity_s, "metrics": metrics}


def compute_metrics(trades: pd.DataFrame, equity: pd.Series) -> dict:
    if trades.empty or len(trades) < 2:
        return {"n_trades": len(trades) if not trades.empty else 0}

    pnl = trades["pnl_pts"]
    n   = len(pnl)
    win_rate     = (pnl > 0).mean()
    total_pnl    = pnl.sum()
    sharpe       = (pnl.mean() / pnl.std() * np.sqrt(n)) if pnl.std() > 0 else 0.0
    downside_std = pnl[pnl < 0].std()
    sortino      = (pnl.mean() / downside_std * np.sqrt(n)) if downside_std > 0 else 0.0
    max_dd       = (equity - equity.cummax()).min()
    gross_profit = pnl[pnl > 0].sum()
    gross_loss   = abs(pnl[pnl < 0].sum())
    pf           = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    return {
        "n_trades":       n,
        "win_rate_%":     round(win_rate * 100, 2),
        "mean_pnl_pts":   round(pnl.mean(), 4),
        "total_pnl_pts":  round(total_pnl, 4),
        "sharpe":         round(sharpe, 4),
        "sortino":        round(sortino, 4),
        "max_drawdown":   round(max_dd, 4),
        "profit_factor":  round(pf, 4),
    }


def print_metrics(label: str, m: dict):
    print(f"\n{'='*54}")
    print(f"  {label}")
    print(f"{'='*54}")
    for k, v in m.items():
        print(f"  {k:<24}: {v}")


if __name__ == "__main__":
    gap_is  = pd.read_csv(ROOT / "doc" / "gap_insample.csv",
                          index_col=0, parse_dates=True)
    gap_oos = pd.read_csv(ROOT / "doc" / "gap_outsample.csv",
                          index_col=0, parse_dates=True)

    for label, gap in [("IN-SAMPLE (2022-2023)", gap_is),
                        ("OUT-OF-SAMPLE (2024-2025)", gap_oos)]:
        r = run_backtest(gap, entry_z=1.0, stop_mult=2.0)
        print_metrics(label, r["metrics"])
        print("  Exit reason breakdown:")
        print("  ", r["trades"]["exit_reason"].value_counts().to_dict())
        print("  Sample trades:")
        print(r["trades"].head(5).to_string(index=False))
