"""
IVMR signal logic + event-driven simulator (used by backtest + optimize).

Modes:
  FADE  — mean-reversion: |z|>=th → fade direction (z>0 → SHORT, z<0 → LONG); target = entry_vwap
  TREND — momentum:       |z|>=th → follow direction (z>0 → LONG,  z<0 → SHORT); target = entry ± target_mult*sigma

Stop is always entry ± stop_mult * sigma (locked at entry).
Target is locked at entry too (does not chase moving VWAP).
"""
from dataclasses import dataclass, asdict
import pandas as pd


@dataclass
class Trade:
    entry_time: str
    exit_time:  str
    session:    str
    direction:  str
    entry_px:   float
    exit_px:    float
    z:          float
    vwap_at_entry: float
    sigma_at_entry: float
    target_level: float
    stop_level: float
    bars_held:  int
    pnl_gross:  float
    pnl_net:    float
    exit_reason: str


def simulate(bars: pd.DataFrame, params: dict) -> list[Trade]:
    entry_z       = float(params["entry_z"])
    stop_mult     = float(params["stop_mult"])
    max_hold      = int(params["max_hold_bars"])
    cooldown      = int(params["cooldown_bars"])
    min_bars      = int(params["min_bars_in_session"])
    max_trades    = int(params["max_trades_per_session"])
    n_contracts   = int(params["contracts"])
    cost_per_side = float(params["transaction_cost"]) + float(params["slippage"])
    rt_cost       = 2.0 * cost_per_side * n_contracts
    mode          = params.get("direction_mode", "FADE").upper()
    target_mult   = float(params.get("target_mult", 1.0))   # used in TREND mode

    trades: list[Trade] = []

    for sess_key, sess in bars.groupby("sess_key", sort=False):
        sess = sess.sort_index()
        rows = list(sess.itertuples())
        n = len(rows)

        in_pos = False
        entry_idx = None
        direction = None
        entry_px = None
        sigma_at_entry = None
        vwap_at_entry  = None
        z_at_entry     = None
        target_level   = None
        stop_level     = None
        cooldown_left  = 0
        trades_this_session = 0

        for i, r in enumerate(rows):
            if in_pos:
                bars_held = i - entry_idx
                target_hit = (
                    (direction == "LONG"  and r.high >= target_level) or
                    (direction == "SHORT" and r.low  <= target_level)
                )
                stop_hit = (
                    (direction == "LONG"  and r.low  <= stop_level) or
                    (direction == "SHORT" and r.high >= stop_level)
                )
                last_bar = (i == n - 1)
                time_up  = bars_held >= max_hold

                exit_px = None
                reason  = None
                if stop_hit and target_hit:
                    # both touched intra-bar → assume worst case (stop)
                    exit_px = stop_level
                    reason = "stop"
                elif stop_hit:
                    exit_px = stop_level
                    reason = "stop"
                elif target_hit:
                    exit_px = target_level
                    reason = "target"
                elif last_bar:
                    exit_px = r.close
                    reason = "session_end"
                elif time_up:
                    exit_px = r.close
                    reason = "time"

                if exit_px is not None:
                    pnl_gross = (
                        (exit_px - entry_px) if direction == "LONG"
                        else (entry_px - exit_px)
                    ) * n_contracts
                    pnl_net = pnl_gross - rt_cost
                    trades.append(Trade(
                        entry_time=str(rows[entry_idx].Index),
                        exit_time=str(r.Index),
                        session=str(rows[entry_idx].session),
                        direction=direction,
                        entry_px=float(entry_px),
                        exit_px=float(exit_px),
                        z=float(z_at_entry),
                        vwap_at_entry=float(vwap_at_entry),
                        sigma_at_entry=float(sigma_at_entry),
                        target_level=float(target_level),
                        stop_level=float(stop_level),
                        bars_held=int(bars_held),
                        pnl_gross=float(pnl_gross),
                        pnl_net=float(pnl_net),
                        exit_reason=reason,
                    ))
                    in_pos = False
                    cooldown_left = cooldown
                    trades_this_session += 1
                continue

            if cooldown_left > 0:
                cooldown_left -= 1
                continue

            if r.bar_idx < min_bars:
                continue
            if trades_this_session >= max_trades:
                continue
            if pd.isna(r.sigma) or r.sigma <= 0 or pd.isna(r.z):
                continue
            if i == n - 1:
                continue

            if abs(r.z) >= entry_z:
                if mode == "FADE":
                    direction = "SHORT" if r.z > 0 else "LONG"
                else:  # TREND
                    direction = "LONG" if r.z > 0 else "SHORT"
                entry_px       = float(r.close)
                sigma_at_entry = float(r.sigma)
                vwap_at_entry  = float(r.vwap)
                z_at_entry     = float(r.z)

                if mode == "FADE":
                    target_level = vwap_at_entry
                else:
                    target_level = (
                        entry_px + target_mult * sigma_at_entry if direction == "LONG"
                        else entry_px - target_mult * sigma_at_entry
                    )
                stop_level = (
                    entry_px - stop_mult * sigma_at_entry if direction == "LONG"
                    else entry_px + stop_mult * sigma_at_entry
                )
                in_pos = True
                entry_idx = i

    return trades


def trades_to_df(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([asdict(t) for t in trades])
