import pandas as pd
import numpy as np


def generate_gap_signals(gap_bars: pd.DataFrame, entry_z: float) -> pd.DataFrame:
    """
    Generate gap-fill entry signals.

    When today's open deviates significantly from yesterday's VWAP:
        Long  (fill upward)  : gap_z < -entry_z  (price opened too low)
        Short (fill downward): gap_z >  entry_z  (price opened too high)

    Returns gap_bars with added columns [long_entry, short_entry].
    """
    df = gap_bars.copy()
    df["long_entry"]  = df["gap_z"] < -entry_z
    df["short_entry"] = df["gap_z"] >  entry_z
    return df
