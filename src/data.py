"""
Data pipeline for Intraday VWAP Reversion (IVMR) on VN30F.

Output per bar (5-min):
    open, high, low, close, volume, session, vwap, dev, sigma, z

Hypothesis: intraday deviations from session VWAP mean-revert.
"""
import json
import sys
from pathlib import Path
from datetime import time as dtime

import psycopg2
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent


def get_connection():
    for candidate in (ROOT / "database.json", PROJECT_ROOT / "database.json"):
        if candidate.exists():
            with open(candidate) as f:
                creds = json.load(f)
            return psycopg2.connect(
                host=creds["host"], port=creds["port"],
                database=creds["database"], user=creds["user"],
                password=creds["password"],
            )
    raise FileNotFoundError("database.json not found in vwap_revert/ or project root")


def _query(sql: str, params: dict | None = None) -> pd.DataFrame:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    import psycopg2.extensions as ext
    numeric_oids = {ext.DECIMAL.values[0], ext.INTEGER.values[0], 700, 701, 1700, 23, 20, 21}
    type_oids = [d[1] for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    conn.close()
    for col, oid in zip(cols, type_oids):
        if oid in numeric_oids:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_ticker_expiry() -> pd.DataFrame:
    df = _query("""
        SELECT tickersymbol AS ticker, expdate
        FROM quote.ticker
        WHERE instrumenttype = 'futures' AND tickersymbol LIKE 'VN30F%%'
    """)
    df["expdate"] = pd.to_datetime(df["expdate"]).dt.date
    return df


def _front_month_map(dates, ticker_info: pd.DataFrame) -> dict:
    mapping = {}
    for d in dates:
        active = ticker_info[ticker_info["expdate"] >= d]
        if not active.empty:
            mapping[d] = active.loc[active["expdate"].idxmin(), "ticker"]
    return mapping


def fetch_5min_bars(start_date: str, end_date: str, bar_minutes: int = 5) -> pd.DataFrame:
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

    ticker_info = get_ticker_expiry()
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
    ohlcv["tp_vol"]  = typical * ohlcv["volume"]
    ohlcv["date"]    = ohlcv.index.date
    ohlcv["session"] = ["AM" if t.hour < 12 else "PM" for t in ohlcv.index.time]
    ohlcv["sess_key"] = ohlcv["date"].astype(str) + "_" + ohlcv["session"]

    cum_tp  = ohlcv.groupby("sess_key")["tp_vol"].cumsum()
    cum_vol = ohlcv.groupby("sess_key")["volume"].cumsum()
    ohlcv["vwap"]    = cum_tp / cum_vol
    ohlcv["bar_idx"] = ohlcv.groupby("sess_key").cumcount()
    ohlcv = ohlcv.drop(columns=["tp_vol"])
    return ohlcv


def add_signal(bars: pd.DataFrame, sigma_window: int = 20) -> pd.DataFrame:
    df = bars.copy()
    df["dev"]   = df["close"] - df["vwap"]
    df["sigma"] = df["close"].rolling(sigma_window, min_periods=10).std()
    df["z"]     = df["dev"] / df["sigma"]
    return df


if __name__ == "__main__":
    cfg = json.loads((ROOT / "config" / "strategy_config.json").read_text())
    win = int(cfg["sigma_window_bars"])
    bar = int(cfg["bar_size_minutes"])

    for label, start, end, fname in [
        ("In-sample",     cfg["in_sample_start"],  cfg["in_sample_end"],  "insample"),
        ("Out-of-sample", cfg["out_sample_start"], cfg["out_sample_end"], "outsample"),
    ]:
        print(f"\n=== {label} ===")
        bars = fetch_5min_bars(start, end, bar_minutes=bar)
        bars = add_signal(bars, sigma_window=win)
        bars = bars.dropna(subset=["sigma"])
        print(f"  Bars: {len(bars):,}  Days: {bars['date'].nunique()}")
        print(f"  |z|>=1.0: {(bars['z'].abs() >= 1.0).sum():,}  "
              f"|z|>=1.25: {(bars['z'].abs() >= 1.25).sum():,}  "
              f"|z|>=1.5: {(bars['z'].abs() >= 1.5).sum():,}")
        out = ROOT / "doc" / f"bars_{fname}.csv"
        bars.to_csv(out)
        print(f"  Saved {out.name}")
