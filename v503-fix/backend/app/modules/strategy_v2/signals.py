"""Regime classification (1h) and entry signals (15m). All causal - no look-ahead.

Improvements applied:
- Section 2.7: Extreme volatility filter (ATR > 95th percentile)
- Section 5.1: 3rd volatility state (dead/extreme market)
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .indicators import adx, atr, ema, rsi

BULL, BEAR, RANGE = 1, -1, 0
DEAD, EXTREME = -2, 2


def resample_ohlcv(df: pd.DataFrame, rule: str = "1h") -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna()


def regime_series(df1h: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    c = df1h["close"]
    e = ema(c, cfg.regime_ema)
    slope = e / e.shift(cfg.regime_slope_bars) - 1
    a = adx(df1h)
    if cfg.require_adx_rising:
        adx_rising = a > a.shift(cfg.adx_rising_lookback)
    else:
        adx_rising = True
    bull = (c > e) & (slope > 0) & (a > cfg.regime_adx_min) & adx_rising
    bear = (c < e) & (slope < 0) & (a > cfg.regime_adx_min) & adx_rising
    out = pd.Series(RANGE, index=df1h.index)
    out[bull] = BULL
    out[bear] = BEAR
    out[e.isna() | a.isna() | slope.isna()] = RANGE
    return out


def volatility_state(df15: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    """IMPROVEMENT 5.1: Compute volatility state per bar."""
    a = atr(df15) / df15["close"]
    pct = a.rolling(cfg.volatility_lookback).rank(pct=True) * 100
    out = pd.Series(0, index=df15.index)
    out[pct < cfg.dead_market_pct] = DEAD
    out[pct > cfg.extreme_market_pct] = EXTREME
    out[pct.isna()] = 0
    return out


def align_htf_to_ltf(htf: pd.Series, ltf_index: pd.DatetimeIndex, htf_rule: str = "1h") -> pd.Series:
    shifted = htf.copy()
    shifted.index = shifted.index + pd.tseries.frequencies.to_offset(htf_rule)
    return shifted.reindex(ltf_index, method="ffill")


def atr_percentile(df15: pd.DataFrame, lookback: int = 100) -> pd.Series:
    """IMPROVEMENT 2.7: ATR percentile for extreme volatility detection."""
    a = atr(df15) / df15["close"]
    return a.rolling(lookback).rank(pct=True) * 100


def build_features(df15: pd.DataFrame, cfg: StrategyConfig,
                    market_regime: Optional[pd.Series] = None,
                    btc_df15: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    df = df15.copy()
    h1 = resample_ohlcv(df, "1h")
    reg = align_htf_to_ltf(regime_series(h1, cfg), df.index).fillna(RANGE)
    df["regime"] = reg
    df["vol_state"] = volatility_state(df, cfg)
    df["atr"] = atr(df)
    df["atr_pct"] = df["atr"] / df["close"]
    c = df["close"]
    ema20, ema50 = ema(c, 20), ema(c, 50)
    r = rsi(c)
    lb = cfg.pullback_lookback

    vol_avg = df["volume"].shift(1).rolling(cfg.vol_lookback).mean()
    vol_ok = df["volume"] >= cfg.vol_mult * vol_avg
    cost_ok = df["atr_pct"] >= cfg.min_atr_pct

    # IMPROVEMENT 2.7: Extreme volatility filter
    atr_pct_rank = atr_percentile(df, cfg.volatility_lookback)
    not_extreme_vol = atr_pct_rank < cfg.max_atr_percentile

    prev_c = c.shift(1)
    pulled_l = (r.shift(1).rolling(lb).min() < cfg.pullback_rsi_below) & (r > cfg.pullback_rsi_resume)
    near_l = prev_c <= pd.concat([ema20, ema50], axis=1).max(axis=1).shift(1) * (1 + cfg.near_ma_tolerance)
    trig_l = c > df["high"].shift(1)
    long_sig = (
        (df["regime"] == BULL) &
        cost_ok & vol_ok & pulled_l & near_l & trig_l &
        not_extreme_vol &
        (df["vol_state"] != DEAD)
    )
    if cfg.require_market_ok and market_regime is not None:
        long_sig &= market_regime.reindex(df.index).fillna(RANGE) == BULL
    long_sig = long_sig.fillna(False)

    pulled_s = (r.shift(1).rolling(lb).max() > 100 - cfg.pullback_rsi_below) & (r < 100 - cfg.pullback_rsi_resume)
    near_s = prev_c >= pd.concat([ema20, ema50], axis=1).min(axis=1).shift(1) * (1 - cfg.near_ma_tolerance)
    trig_s = c < df["low"].shift(1)
    short_sig = (df["regime"] == BEAR) & cost_ok & vol_ok & pulled_s & near_s & trig_s
    if cfg.require_market_ok and market_regime is not None:
        short_sig &= market_regime.reindex(df.index).fillna(RANGE) == BEAR
    if not cfg.enable_short:
        short_sig &= False
    short_sig = short_sig.fillna(False)

    df["long_signal"] = long_sig
    df["short_signal"] = short_sig
    df["rsi"] = r
    df["atr_pct_rank"] = atr_pct_rank
    return df
