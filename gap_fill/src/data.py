"""
Data pipeline for VWAP Opening-Gap strategy on VN30F.

Two outputs:
  1. daily_bars.csv  — daily OHLC + end-of-day VWAP for each session
  2. gap_bars.csv    — gap signal table: (open - prev_vwap) with rolling std
"""
import json
import psycopg2
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import time as dtime

ROOT = Path(__file__).resolve().parent.parent


def get_connection():
    with open(ROOT / "database.json") as f:
        creds = json.load(f)
    return psycopg2.connect(
        host=creds["host"], port=creds["port"],
        database=creds["database"], user=creds["user"], password=creds["password"],
    )


def _query(sql: str, params: dict | None = None) -> pd.DataFrame:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    # Map psycopg2 type OIDs to expected Python types so we cast only numeric cols
    import psycopg2.extensions as ext
    numeric_oids = {
        ext.DECIMAL.values[0], ext.INTEGER.values[0],
        700,   # float4
        701,   # float8
        1700,  # numeric
        23,    # int4
        20,    # int8
        21,    # int2
    }
    type_oids = [d[1] for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    conn.close()
    for col, oid in zip(cols, type_oids):
        if oid in numeric_oids:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _get_ticker_expiry() -> pd.DataFrame:
    df = _query("""
        SELECT tickersymbol AS ticker, expdate
        FROM quote.ticker
        WHERE instrumenttype = 'futures' AND tickersymbol LIKE 'VN30F%%'
    """)
    df["expdate"] = pd.to_datetime(df["expdate"]).dt.date
    return df


def _front_month_map(dates, ticker_info: pd.DataFrame) -> dict:
    """Return {date: front_month_ticker} mapping."""
    mapping = {}
    for d in dates:
        active = ticker_info[ticker_info["expdate"] >= d]
        if not active.empty:
            mapping[d] = active.loc[active["expdate"].idxmin(), "ticker"]
    return mapping


def fetch_5min_bars(start_date: str, end_date: str, bar_minutes: int = 5) -> pd.DataFrame:
    """Fetch tick data and resample into 5-min OHLCV bars with intraday VWAP."""
    print(f"  Fetching ticks {start_date} → {end_date} ...", flush=True)
    raw = _query("""
        SELECT m.datetime, m.price, mv.quantity, m.tickersymbol AS ticker
        FROM quote.matched m
        JOIN quote.matchedvolume mv
            ON m.datetime = mv.datetime AND m.tickersymbol = mv.tickersymbol
        JOIN quote.ticker t ON m.tickersymbol = t.tickersymbol
        WHERE t.instrumenttype = 'futures'
          AND m.tickersymbol LIKE 'VN30F%%'
          AND m.datetime >= %(start)s AND m.datetime < %(end)s
          AND mv.quantity > 0
        ORDER BY m.datetime
    """, {"start": start_date, "end": end_date})

    raw["datetime"] = pd.to_datetime(raw["datetime"])
    raw["date"] = raw["datetime"].dt.date
    print(f"  Raw ticks: {len(raw):,}", flush=True)

    ticker_info = _get_ticker_expiry()
    date_to_front = _front_month_map(raw["date"].unique(), ticker_info)
    raw["front"] = raw["date"].map(date_to_front)
    raw = raw[raw["ticker"] == raw["front"]].drop(columns=["date", "front"]).reset_index(drop=True)

    df = raw.sort_values("datetime").set_index("datetime")

    idx_time = df.index.time
    morning   = (idx_time >= dtime(9, 0))  & (idx_time <= dtime(11, 30))
    afternoon = (idx_time >= dtime(13, 0)) & (idx_time <= dtime(14, 45))
    df = df[morning | afternoon]

    freq = f"{bar_minutes}min"
    ohlcv = df["price"].resample(freq, label="right", closed="right").ohlc()
    ohlcv.columns = ["open", "high", "low", "close"]
    ohlcv["volume"] = df["quantity"].resample(freq, label="right", closed="right").sum()
    ohlcv = ohlcv.dropna(subset=["close"])
    ohlcv = ohlcv[ohlcv["volume"] > 0].copy()

    typical = (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3.0
    ohlcv["tp_vol"] = typical * ohlcv["volume"]
    ohlcv["date"]    = ohlcv.index.date
    ohlcv["session"] = ["AM" if t.hour < 12 else "PM" for t in ohlcv.index.time]
    ohlcv["sess_key"] = ohlcv["date"].astype(str) + "_" + ohlcv["session"]

    cum_tp  = ohlcv.groupby("sess_key")["tp_vol"].cumsum()
    cum_vol = ohlcv.groupby("sess_key")["volume"].cumsum()
    ohlcv["vwap"] = cum_tp / cum_vol
    ohlcv = ohlcv.drop(columns=["tp_vol", "sess_key"])
    return ohlcv


def build_daily_vwap(bars_5m: pd.DataFrame) -> pd.DataFrame:
    """
    Compute end-of-day metrics from 5-min bars.
    Returns one row per trading day: [date, open, high, low, close, volume, day_vwap]
    where day_vwap = volume-weighted average over the FULL day (both sessions).
    """
    df = bars_5m.copy()
    df["date"] = df.index.date
    df["tp_vol"] = ((df["high"] + df["low"] + df["close"]) / 3) * df["volume"]

    daily = df.groupby("date").agg(
        open    = ("open",   "first"),
        high    = ("high",   "max"),
        low     = ("low",    "min"),
        close   = ("close",  "last"),
        volume  = ("volume", "sum"),
        tp_vol  = ("tp_vol", "sum"),
    )
    daily["day_vwap"] = daily["tp_vol"] / daily["volume"]
    daily = daily.drop(columns=["tp_vol"])
    daily.index = pd.to_datetime(daily.index)
    return daily


def build_gap_bars(daily: pd.DataFrame, std_window: int = 20) -> pd.DataFrame:
    """
    Build the gap signal table.

    gap       = today's open − previous day's VWAP  (overnight deviation)
    gap_pct   = gap / prev_vwap * 100
    gap_std   = rolling std of gap over std_window days (normalisation)
    gap_z     = gap / gap_std  (how extreme the gap is in std units)
    """
    df = daily.copy()
    df["prev_vwap"] = df["day_vwap"].shift(1)
    df["gap"]       = df["open"] - df["prev_vwap"]
    df["gap_std"]   = df["gap"].rolling(std_window, min_periods=10).std()
    df["gap_z"]     = df["gap"] / df["gap_std"]
    df = df.dropna(subset=["gap_z"])
    return df


if __name__ == "__main__":
    for label, start, end, fname in [
        ("In-sample",      "2022-01-01", "2024-01-01", "insample"),
        ("Out-of-sample",  "2024-01-01", "2026-01-01", "outsample"),
    ]:
        print(f"\n=== {label} ===")
        bars = fetch_5min_bars(start, end)
        daily = build_daily_vwap(bars)
        gap   = build_gap_bars(daily)
        print(f"  Trading days: {len(daily)}, gap bars: {len(gap)}")
        print(f"  Gap mean={gap['gap'].mean():.3f}, std={gap['gap'].std():.3f}")
        print(f"  Gap |z| > 1: {(gap['gap_z'].abs() > 1).sum()} days")
        daily.to_csv(ROOT / "doc" / f"daily_{fname}.csv")
        gap.to_csv(ROOT / "doc" / f"gap_{fname}.csv")
        print(f"  Saved daily_{fname}.csv and gap_{fname}.csv")
