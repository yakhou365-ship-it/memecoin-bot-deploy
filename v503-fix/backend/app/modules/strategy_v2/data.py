"""Data loading: Pump.fun token data (cached to CSV) and a synthetic generator for self-tests."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Dict, List

import numpy as np
import pandas as pd

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT", "LTCUSDT", "TRXUSDT", "SUIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT", "FETUSDT",
    "RENDERUSDT", "TIAUSDT", "SEIUSDT", "AAVEUSDT", "UNIUSDT", "FILUSDT",
]


def _get(url: str):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_klines(symbol: str, days: int = 180, interval: str = "15m", cache_dir: str = "data/cache") -> pd.DataFrame:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}_{days}d.csv")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    step = {"15m": 15 * 60_000, "1h": 3_600_000}[interval]
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows: List[list] = []
    while start < end:
        raw = _get(f"https://api.pumpportal.fun/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start}&limit=1000")
        if not raw:
            break
        rows += raw
        start = raw[-1][0] + step
        if len(raw) < 1000:
            break
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["ts", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    df = df.astype(float)
    df = df[~df.index.duplicated()].iloc[:-1]       # drop the still-forming last candle
    df.to_csv(path)
    return df


def load_universe(symbols: List[str], days: int, min_bars: int = 1500) -> Dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        df = fetch_klines(s, days)
        if len(df) >= min_bars:
            out[s] = df
    return out


def synthetic_market(n_symbols: int = 8, days: int = 120, seed: int = 1, drift: float = 0.0,
                     vol_15m: float = 0.004, regime_switching: bool = True) -> Dict[str, pd.DataFrame]:
    """Random-walk OHLCV (+ optional slow regime drift). With drift=0 there is NO edge by construction,
    so any reliable profit on it would indicate a backtester bug (look-ahead / cost leak)."""
    rng = np.random.default_rng(seed)
    n = days * 96
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    base = rng.normal(0, 1, n) * 0.0
    if regime_switching:
        state, mu = 0.0, np.zeros(n)
        for i in range(n):
            if i % 2000 == 0:
                state = rng.choice([-1, 0, 1]) * 0.00025
            mu[i] = state
        base = mu
    out = {}
    for k in range(n_symbols):
        r = base + drift + rng.normal(0, vol_15m, n) * (1 + 0.5 * np.sin(np.arange(n) / 300 + k))
        close = 100 * np.exp(np.cumsum(r))
        open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.0005, n))
        spread = np.abs(rng.normal(0, vol_15m * 0.6, n))
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)
        vol = rng.lognormal(10, 0.5, n) * (1 + 2 * (np.abs(r) > vol_15m * 1.5))
        out[f"SYN{k}"] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
    return out
