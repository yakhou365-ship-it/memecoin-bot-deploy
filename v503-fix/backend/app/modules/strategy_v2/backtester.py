"""Realistic backtester for Strategy V2.

Fixes vs the old scripts: signal on a CLOSED bar -> entry at NEXT bar open; the initial stop is
always enforced; one position per symbol; portfolio-level limits applied chronologically across
symbols; costs charged on every trade; results reported in R (expectancy), not just win rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .position import Position
from .signals import BULL, build_features, resample_ohlcv, regime_series, align_htf_to_ltf

BAR_MIN = 15


@dataclass
class Trade:
    symbol: str
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    sl_pct: float
    pnl_frac: float       # net return per unit of NOTIONAL (after costs)
    r: float              # net R multiple
    bars: int
    reason: str           # reason of the LAST fill


def market_regime_15m(df_ref15: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    """Broad-market regime (e.g. BTC) aligned to 15m, using completed hourly bars only."""
    reg = regime_series(resample_ohlcv(df_ref15, "1h"), cfg)
    return align_htf_to_ltf(reg, df_ref15.index).fillna(0)


def simulate_symbol(symbol: str, df15: pd.DataFrame, cfg: StrategyConfig,
                    market_regime: Optional[pd.Series] = None,
                    btc_df15: Optional[pd.DataFrame] = None) -> List[Trade]:
    f = build_features(df15, cfg, market_regime, btc_df15)
    o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr_v = f["atr"].to_numpy(float)
    ls, ss = f["long_signal"].to_numpy(bool), f["short_signal"].to_numpy(bool)
    idx, n, rt = f.index, len(f), cfg.costs.round_trip
    fund_bar = cfg.costs.funding_per_8h / 32.0
    # IMPROVEMENT 4.3: Pass EMA20 and RSI for momentum exit
    try:
        from .indicators import ema as _ema, rsi as _rsi
        ema20_v = _ema(pd.Series(c), 20).to_numpy()
        rsi_v = _rsi(pd.Series(c)).to_numpy()
    except Exception:
        ema20_v = None
        rsi_v = None
    trades: List[Trade] = []
    i = 0
    while i < n - 2:
        side = 1 if ls[i] else (-1 if ss[i] else 0)
        if side == 0 or not np.isfinite(atr_v[i]) or atr_v[i] <= 0:
            i += 1
            continue
        pos = Position(side=side, entry=o[i + 1], atr=atr_v[i], cfg=cfg)
        j = i + 1
        while j < n and not pos.done:
            ema20_val = float(ema20_v[j]) if ema20_v is not None and j < len(ema20_v) else 0.0
            rsi_val = float(rsi_v[j]) if rsi_v is not None and j < len(rsi_v) else 50.0
            pos.on_bar(o[j], h[j], l[j], c[j], ema20_val, rsi_val)
            j += 1
        if not pos.done:
            pos._close(pos.remaining, c[n - 1], "data_end")
            j = n
        gross = sum(fl.fraction * side * (fl.price - pos.entry) / pos.entry for fl in pos.fills)
        net = gross - rt - fund_bar * pos.bars
        sl_pct = pos.sl_pct
        trades.append(Trade(symbol, side, idx[i + 1], idx[j - 1], pos.entry, sl_pct, net,
                            net / (sl_pct + rt), pos.bars, pos.fills[-1].reason))
        i = j
    return trades


# ----------------------------------------------------------------------------------------
def portfolio(trades: Sequence[Trade], cfg: StrategyConfig, initial_equity: float = 1000.0) -> Dict:
    """Apply sizing and portfolio risk limits chronologically. Returns accepted trades + equity curve."""
    trades = sorted(trades, key=lambda t: t.entry_time)
    equity, peak = initial_equity, initial_equity
    open_pos: List[Dict] = []
    done: List[Dict] = []
    day_pnl: Dict = {}
    week_pnl: Dict = {}
    consec, cool_until = 0, None
    halt_until = None
    skipped = {"max_positions": 0, "daily_limit": 0, "weekly_limit": 0, "cooldown": 0,
               "min_notional": 0, "drawdown_halt": 0}
    curve = [(trades[0].entry_time if trades else pd.Timestamp(0), equity)]

    def realize(upto):
        nonlocal equity, peak, consec, cool_until
        for p in sorted([p for p in open_pos if p["exit"] <= upto], key=lambda p: p["exit"]):
            open_pos.remove(p)
            equity += p["pnl"]
            peak = max(peak, equity)
            d, w = p["exit"].normalize(), p["exit"].to_period("W")
            day_pnl[d] = day_pnl.get(d, 0) + p["pnl"]
            week_pnl[w] = week_pnl.get(w, 0) + p["pnl"]
            consec = consec + 1 if p["pnl"] <= 0 else 0
            if consec >= cfg.cooldown_after_losses:
                cool_until = p["exit"] + pd.Timedelta(minutes=BAR_MIN * cfg.cooldown_bars)
                consec = 0
            done.append(p)
            curve.append((p["exit"], equity))

    for t in trades:
        realize(t.entry_time)
        if halt_until is not None and t.entry_time >= halt_until:
            halt_until, peak = None, equity          # resume with a fresh peak
        if halt_until is None and equity <= peak * (1 - cfg.max_drawdown_halt):
            halt_until = t.entry_time + pd.Timedelta(days=cfg.halt_days)
        if halt_until is not None:
            skipped["drawdown_halt"] += 1; continue
        if len(open_pos) >= cfg.max_open_positions:
            skipped["max_positions"] += 1; continue
        d, w = t.entry_time.normalize(), t.entry_time.to_period("W")
        if day_pnl.get(d, 0) <= -cfg.daily_loss_limit * equity:
            skipped["daily_limit"] += 1; continue
        if week_pnl.get(w, 0) <= -cfg.weekly_loss_limit * equity:
            skipped["weekly_limit"] += 1; continue
        if cool_until is not None and t.entry_time < cool_until:
            skipped["cooldown"] += 1; continue
        notional = min(equity * cfg.risk_per_trade / (t.sl_pct + cfg.costs.round_trip),
                       equity * cfg.max_position_pct)
        if notional < cfg.min_notional_usd:
            skipped["min_notional"] += 1; continue
        open_pos.append({"t": t, "exit": t.exit_time, "pnl": notional * t.pnl_frac, "notional": notional})
    realize(pd.Timestamp.max)
    return {"trades": done, "curve": curve, "equity": equity, "initial": initial_equity, "skipped": skipped}


def bootstrap_ci(x: np.ndarray, n_boot: int = 2000, seed: int = 7, q=(2.5, 97.5)):
    if len(x) < 10:
        return None
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(m, q[0])), float(np.percentile(m, q[1]))


def stats(pf: Dict) -> Dict:
    d = pf["trades"]
    if not d:
        return {"n": 0, "note": "no trades", "skipped": pf["skipped"]}
    pnl = np.array([p["pnl"] for p in d])
    r = np.array([p["t"].r for p in d])
    eq = np.array([e for _, e in pf["curve"]])
    dd = (np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    ser = pd.Series(pnl, index=[p["exit"] for p in d]).sort_index()
    monthly = ser.groupby(ser.index.to_period("M")).sum()
    reasons: Dict[str, Dict] = {}
    for p in d:
        rr = reasons.setdefault(p["t"].reason, {"n": 0, "wins": 0, "avg_r": 0.0})
        rr["n"] += 1; rr["wins"] += int(p["pnl"] > 0); rr["avg_r"] += p["t"].r
    for v in reasons.values():
        v["avg_r"] = round(v["avg_r"] / v["n"], 3)
    return {
        "n": len(d), "win_rate": float((pnl > 0).mean()), "avg_r": float(r.mean()),
        "expectancy_r": float(r.mean()), "expectancy_ci95": bootstrap_ci(r),
        "profit_factor": float(gp / gl) if gl > 0 else float("inf"),
        "return_pct": float((pf["equity"] / pf["initial"] - 1) * 100),
        "max_drawdown_pct": float(dd.max() * 100),
        "months": int(len(monthly)), "positive_months_pct": float((monthly > 0).mean() * 100),
        "avg_win_r": float(r[r > 0].mean()) if (r > 0).any() else 0.0,
        "avg_loss_r": float(r[r <= 0].mean()) if (r <= 0).any() else 0.0,
        "exit_reasons": reasons, "skipped": pf["skipped"],
    }


def run_backtest(data: Dict[str, pd.DataFrame], cfg: StrategyConfig, market_ref: Optional[str] = None,
                 initial_equity: float = 1000.0) -> Dict:
    """data: {symbol: 15m OHLCV with DatetimeIndex}. market_ref: symbol used as broad-market filter."""
    mreg = market_regime_15m(data[market_ref], cfg) if market_ref and market_ref in data else None
    btc_df = data.get(market_ref) if market_ref else None
    trades: List[Trade] = []
    for sym, df in data.items():
        trades += simulate_symbol(sym, df, cfg, mreg, btc_df)
    pf = portfolio(trades, cfg, initial_equity)
    return {"stats": stats(pf), "portfolio": pf, "all_trades": trades}


def walk_forward(data: Dict[str, pd.DataFrame], base: StrategyConfig, grid: List[Dict],
                 train_days: int = 90, test_days: int = 30, min_train_trades: int = 30,
                 market_ref: Optional[str] = None, initial_equity: float = 1000.0) -> Dict:
    """Pick the best grid point on each train window (by expectancy R), score ONLY on the next test window."""
    runs = []
    for g in grid:
        cfg = base.with_(**g)
        mreg = market_regime_15m(data[market_ref], cfg) if market_ref and market_ref in data else None
        btc_df = data.get(market_ref) if market_ref else None
        tr: List[Trade] = []
        for sym, df in data.items():
            tr += simulate_symbol(sym, df, cfg, mreg, btc_df)
        runs.append((g, cfg, tr))
    t0 = min(df.index[0] for df in data.values()) + pd.Timedelta(days=0)
    t_end = max(df.index[-1] for df in data.values())
    start, windows, oos = t0, [], []
    while start + pd.Timedelta(days=train_days + test_days) <= t_end:
        a, b, c_ = start, start + pd.Timedelta(days=train_days), start + pd.Timedelta(days=train_days + test_days)
        best, best_e = None, -1e9
        for g, cfg, tr in runs:
            sel = [t for t in tr if a <= t.entry_time < b]
            if len(sel) >= min_train_trades:
                e = float(np.mean([t.r for t in sel]))
                if e > best_e:
                    best, best_e = (g, cfg, tr), e
        if best is not None and best_e > 0:
            g, cfg, tr = best
            test = [t for t in tr if b <= t.entry_time < c_]
            oos += test
            windows.append({"train": (str(a.date()), str(b.date())), "test": (str(b.date()), str(c_.date())),
                            "chosen": g, "train_exp_r": round(best_e, 3), "test_n": len(test),
                            "test_exp_r": round(float(np.mean([t.r for t in test])), 3) if test else None})
        else:
            windows.append({"train": (str(a.date()), str(b.date())), "test": (str(b.date()), str(c_.date())),
                            "chosen": None, "note": "no config with positive train edge - stayed flat"})
        start += pd.Timedelta(days=test_days)
    pf = portfolio(oos, base, initial_equity)
    return {"windows": windows, "oos_stats": stats(pf)}
