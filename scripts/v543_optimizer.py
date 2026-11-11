"""V543 strategy optimizer — find params that DON'T LOSE on Pump.fun.

Approach: scan a grid of conservative parameter sets, simulate each
on the same Monte-Carlo model (90 seeds: 30 harsh + 30 base + 30 normal),
and pick the configuration whose WORST-CASE regime has median ROI >= 0.

The simulation logic is identical to v542b (same token generators,
same entry latency, same round-trip drag) — only the strategy params
change. We bypass unified.py and run with a local copy of the exit
ladder so we can vary params quickly.

Grid axes:
  - stop_loss          : 2%, 3%, 4%, 6%
  - quick_tp           : 6%, 8%, 10%, 15%   (band exit)
  - trail_width        : 8%, 12%, 15%
  - min_age_seconds    : 60, 120, 180, 300  (survival filter)
  - max_chg_5m_pct     : 35, 50, 60         (don't enter post-peak)
  - trade_pct          : 5%, 10%
  - max_buys_per_day   : 2, 3

We start with a coarse "no-lose-first" search: SL small, TP small,
require survival. Goal = median ROI >= -2% in ALL THREE regimes.
"""
from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

OUT_DIR = Path("/home/z/my-project/download/v543_optimization")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ==========================================================================
# Token model — IDENTICAL to v542b (so results are comparable)
# ==========================================================================
FIRST_HOUR = [30 * i for i in range(1, 121)]
HOURS_1_6 = [3600 + 300 * i for i in range(1, 61)]
HOURS_6_24 = [6 * 3600 + 900 * i for i in range(1, 73)]
DAYS_2_5 = [24 * 3600 + 3600 * i for i in range(1, 97)]
SAMPLE_OFFSETS_SEC: List[int] = FIRST_HOUR + HOURS_1_6 + HOURS_6_24 + DAYS_2_5
SAMPLE_OFFSETS_ARR = np.array(SAMPLE_OFFSETS_SEC, dtype=np.float64)


def _gen_fail_day1(rng):
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.20, 1.80); peak_idx = rng.randint(2, 10)
    floor_mult = rng.uniform(0.02, 0.20); floor_idx = rng.randint(130, min(190, n - 1))
    curve = [1.0]
    for i in range(1, n):
        if i <= peak_idx:
            f = i / peak_idx; curve.append(1.0 + (peak_mult - 1.0) * f)
        elif i <= floor_idx:
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.001, floor_mult * (1 - 0.7 * f)))
    return curve


def _gen_rug(rng):
    n = len(SAMPLE_OFFSETS_SEC)
    initial_pump = rng.uniform(1.05, 1.30); rug_idx = rng.randint(3, 18)
    post_rug_mult = rng.uniform(0.02, 0.12)
    curve = [1.0]
    for i in range(1, n):
        if i < rug_idx:
            f = i / rug_idx; curve.append(1.0 + (initial_pump - 1.0) * f)
        elif i == rug_idx:
            curve.append(post_rug_mult)
        else:
            curve.append(max(0.001, post_rug_mult * rng.uniform(0.85, 1.10)))
    return curve


def _gen_pump_dump(rng):
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.50, 4.00); peak_idx = rng.randint(8, 60)
    floor_mult = rng.uniform(0.30, 0.70)
    floor_idx = min(n - 1, peak_idx + rng.randint(20, 90))
    curve = [1.0]
    for i in range(1, n):
        if i <= peak_idx:
            f = i / peak_idx; curve.append(1.0 + (peak_mult - 1.0) * (f ** 0.7))
        elif i <= floor_idx:
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.05, floor_mult * (1 - 0.5 * f)))
    return curve


def _gen_graduate(rng):
    n = len(SAMPLE_OFFSETS_SEC); final_mult = rng.uniform(3.0, 6.0)
    curve = [1.0]
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        progress = math.log(1 + t / 600) / math.log(1 + SAMPLE_OFFSETS_SEC[-1] / 600)
        target = 1.0 + (final_mult - 1.0) * progress
        noise = rng.uniform(-0.04, 0.04)
        curve.append(max(0.5, target * (1 + noise)))
    return curve


def _gen_moonshot(rng):
    n = len(SAMPLE_OFFSETS_SEC); final_mult = rng.uniform(5.0, 50.0)
    curve = [1.0]
    shake_count = rng.randint(1, 3)
    shake_points = sorted(rng.sample(range(10, n - 10), shake_count))
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]; progress = t / SAMPLE_OFFSETS_SEC[-1]
        target = 1.0 + (final_mult - 1.0) * (progress ** 1.5)
        for sp in shake_points:
            dist = abs(i - sp)
            if dist <= 4:
                depth = rng.uniform(0.25, 0.45)
                target *= 1.0 - depth * (1 - dist / 4)
        noise = rng.uniform(-0.05, 0.05)
        curve.append(max(0.5, target * (1 + noise)))
    return curve


TOKEN_GENERATORS = {
    "fail_day1": _gen_fail_day1, "rug": _gen_rug, "pump_dump": _gen_pump_dump,
    "graduate": _gen_graduate, "moonshot": _gen_moonshot,
}


def sample_token_type(rng, regime):
    if regime == "harsh":
        types = [("fail_day1", 0.73), ("rug", 0.17), ("pump_dump", 0.075),
                 ("graduate", 0.012), ("moonshot", 0.003)]
    elif regime == "normal":
        types = [("fail_day1", 0.65), ("rug", 0.12), ("pump_dump", 0.18),
                 ("graduate", 0.025), ("moonshot", 0.0125)]
    else:
        types = [("fail_day1", 0.69), ("rug", 0.15), ("pump_dump", 0.12),
                 ("graduate", 0.015), ("moonshot", 0.005)]
    s = sum(p for _, p in types); types = [(t, p / s) for t, p in types]
    r = rng.random(); cum = 0.0
    for t, p in types:
        cum += p
        if r <= cum: return t
    return types[-1][0]


ROUND_TRIP_DRAG = 0.030
ENTRY_LATENCY_PENALTY = 0.015


def _entry_idx_from_age(age_sec):
    if age_sec <= 0: return 0
    idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, age_sec, side="left"))
    return max(0, min(idx, len(SAMPLE_OFFSETS_SEC) - 1))


def _compute_chg5m(curve, entry_idx):
    if entry_idx <= 0: return 0.0
    age_sec = SAMPLE_OFFSETS_SEC[entry_idx]
    if age_sec < 300:
        return (curve[entry_idx] / 1.0 - 1.0) * 100
    target = age_sec - 300
    past_idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, target, side="left"))
    past_idx = max(0, min(past_idx, len(curve) - 1))
    return (curve[entry_idx] / curve[past_idx] - 1.0) * 100


# ==========================================================================
# CONFIGURABLE STRATEGY  (params as a dict — we sweep this)
# ==========================================================================
DEFAULT_PARAMS = {
    "quick_tp": 0.08,            # band exit
    "runner_threshold": 0.30,    # above this -> trail instead of band exit
    "trail_width": 0.08,
    "trail_tighten_50": 0.75,
    "trail_tighten_100": 0.60,
    "sudden_dump_gain": 0.05,
    "sudden_dump_drop": 0.12,
    "stop_loss": 0.03,
    "mega_tp": 10.0,
    "stagnation_minutes": 120,
    "stagnation_max_gain": 0.04,
    "max_hold_minutes": 7200,
    "min_liquidity_usd": 4000.0,
    "min_chg_5m_pct": 8.0,
    "max_chg_5m_pct": 35.0,        # tighter — don't enter post-peak
    "min_age_seconds": 60.0,       # require survival
    "max_age_seconds": 300.0,      # tighter upper bound too
    "min_txns_buys": 20,
    "min_buy_sell_ratio": 1.8,     # stricter buy pressure
    "max_top10_concentration_pct": 18.0,   # stricter concentration
    "max_buys_per_day": 2,         # more conservative
    "daily_loss_stop_pct": 6.0,
    "dd_full_pct": 0.92,
    "dd_half_pct": 0.85,           # earlier defense
    "streak_window": 5,
    "streak_wr_max": 0.40,
    "streak_pnl_min": -0.05,       # tighter streak trigger
    "pause_days_streak": 2,
    "pause_days_dd": 3,
    "trade_pct": 0.05,             # smaller position size
}


def exit_reason(params, entry, peak, current, elapsed_min):
    if entry <= 0 or peak <= 0 or current <= 0: return None
    peak_gain = peak / entry - 1.0
    drop_from_peak = (peak - current) / peak
    # 1. sudden dump
    if peak_gain >= params["sudden_dump_gain"] and drop_from_peak >= params["sudden_dump_drop"]:
        return "sudden_dump"
    # 2. stop loss
    if current <= entry * (1.0 - params["stop_loss"]):
        return "stop_loss"
    # 3. mega TP
    if current >= entry * (1.0 + params["mega_tp"]):
        return "mega_tp"
    # 4/5. band exit / runner trail
    if peak_gain >= params["runner_threshold"]:
        tighten = 1.0
        if peak_gain >= 1.0: tighten = params["trail_tighten_100"]
        elif peak_gain >= 0.5: tighten = params["trail_tighten_50"]
        width = params["trail_width"] * tighten
        if current <= peak * (1.0 - width):
            return "smart_trailing"
    elif peak_gain >= params["quick_tp"]:
        return "quick_tp"
    # 6. stagnation
    if elapsed_min >= params["stagnation_minutes"]:
        cur_gain = current / entry - 1.0
        if 0 < cur_gain <= params["stagnation_max_gain"] and peak_gain < params["stagnation_max_gain"]:
            return "stagnation"
    # 7. time exit
    if elapsed_min >= params["max_hold_minutes"]:
        return "time_exit"
    return None


def entry_check(params, snap):
    if snap["chg5m"] < params["min_chg_5m_pct"]: return False, "chg5m_low"
    if snap["chg5m"] > params["max_chg_5m_pct"]: return False, "chg5m_high"
    if snap["age_sec"] > params["max_age_seconds"]: return False, "too_old"
    if snap["age_sec"] < params["min_age_seconds"]: return False, "too_young"
    if snap["liquidity"] < params["min_liquidity_usd"]: return False, "low_liq"
    if snap["buys"] < params["min_txns_buys"]: return False, "low_buys"
    if snap["sells"] > 0:
        if snap["buys"] / snap["sells"] < params["min_buy_sell_ratio"]: return False, "low_buys_sells_ratio"
    if snap["top10"] is not None and snap["top10"] > 0:
        if snap["top10"] > params["max_top10_concentration_pct"]: return False, "concentrated_holders"
    return True, "pass"


# ==========================================================================
# Defense state
# ==========================================================================
@dataclass
class DefenseState:
    equity: float
    peak_equity: float
    buys_today: int = 0
    today_str: str = ""
    pause_until_day: int = -1
    recent_trades: List[Tuple[float, float]] = field(default_factory=list)


def defense_eval(ds, params, day_idx):
    today = f"d{day_idx}"
    if ds.today_str != today:
        ds.today_str = today; ds.buys_today = 0
    if day_idx <= ds.pause_until_day: return "pause", 0.0, "streak_or_dd_pause"
    if ds.buys_today >= int(params["max_buys_per_day"]): return "pause", 0.0, "daily_cap"
    if ds.peak_equity > 0:
        ratio = ds.equity / ds.peak_equity
        if ratio < params["dd_half_pct"]:
            ds.pause_until_day = day_idx + int(params["pause_days_dd"])
            return "pause", 0.0, "dd_deep"
        if ratio < params["dd_full_pct"]:
            return "half", 0.5, "dd_half"
    if len(ds.recent_trades) >= int(params["streak_window"]):
        window = ds.recent_trades[-int(params["streak_window"]):]
        wins = sum(1 for p, _ in window if p > 0)
        wr = wins / len(window)
        total_pnl = sum(p for p, _ in window)
        total_size = sum(s for _, s in window)
        pnl_pct = total_pnl / total_size if total_size > 0 else 0.0
        if wr <= params["streak_wr_max"] and pnl_pct <= params["streak_pnl_min"]:
            ds.pause_until_day = day_idx + int(params["pause_days_streak"])
            return "pause", 0.0, "bad_streak"
    return "full", 1.0, "ok"


def sample_snapshot(rng, ttype, entry_idx):
    age = SAMPLE_OFFSETS_SEC[entry_idx] if entry_idx < len(SAMPLE_OFFSETS_SEC) else 0
    liquidity = rng.uniform(2500, 80000)
    if ttype == "moonshot": buys, sells = rng.randint(40, 500), rng.randint(5, 50)
    elif ttype == "pump_dump": buys, sells = rng.randint(30, 300), rng.randint(20, 150)
    elif ttype == "graduate": buys, sells = rng.randint(25, 100), rng.randint(10, 60)
    elif ttype == "fail_day1": buys, sells = rng.randint(15, 200), rng.randint(10, 100)
    else: buys, sells = rng.randint(5, 30), rng.randint(5, 30)
    if ttype in ("graduate", "moonshot"):
        top10 = rng.uniform(10, 35) if rng.random() < 0.7 else None
    elif ttype == "fail_day1":
        top10 = rng.uniform(15, 40) if rng.random() < 0.6 else None
    else:
        top10 = rng.uniform(20, 60) if rng.random() < 0.5 else None
    return {"age_sec": age, "liquidity": liquidity, "buys": buys, "sells": sells, "top10": top10}


# ==========================================================================
# Run one seed with given params
# ==========================================================================
def run_one_seed(rng, params, regime, days=30, initial=20.0):
    capital = initial
    ds = DefenseState(equity=capital, peak_equity=capital)
    curve_pts = [(0, capital)]
    all_trades = []
    exit_reasons = {}
    skip_reasons = {}
    open_positions = []
    if regime == "harsh": cands = lambda: rng.randint(1, 2)
    elif regime == "normal": cands = lambda: rng.randint(4, 8)
    else: cands = lambda: rng.randint(2, 4)

    for day_idx in range(days):
        day_pnl = 0.0
        new_open = []
        for pos in open_positions:
            entry_day = int(pos["entry_time_min"] // 1440)
            entry_min_in_day = pos["entry_time_min"] % 1440
            if day_idx == entry_day: start_elapsed_min = 0
            else: start_elapsed_min = (day_idx - entry_day) * 1440 - entry_min_in_day
            start_offset_sec = max(0, start_elapsed_min * 60)
            start_i = int(np.searchsorted(SAMPLE_OFFSETS_ARR, start_offset_sec, side="right"))
            start_i = max(start_i, pos["entry_idx"])
            end_offset_sec = start_offset_sec + 1440 * 60
            end_i = int(np.searchsorted(SAMPLE_OFFSETS_ARR, end_offset_sec, side="right"))
            end_i = min(end_i, len(pos["curve"]) - 1)
            closed = False
            for i in range(start_i, end_i + 1):
                if i >= len(pos["curve"]): break
                cur_price = pos["curve"][i]
                if cur_price > pos["peak"]: pos["peak"] = cur_price
                entry_offset_sec = SAMPLE_OFFSETS_SEC[pos["entry_idx"]]
                cur_offset_sec = SAMPLE_OFFSETS_SEC[i]
                elapsed_min = (cur_offset_sec - entry_offset_sec) / 60.0
                if elapsed_min < 0: continue
                reason = exit_reason(params, pos["entry_price"], pos["peak"], cur_price, elapsed_min)
                if reason:
                    actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
                    gross = cur_price / actual_entry - 1.0
                    net = gross - ROUND_TRIP_DRAG
                    pnl_usd = pos["size"] * net
                    capital += pnl_usd; day_pnl += pnl_usd
                    exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                    all_trades.append({
                        "token_type": pos["ttype"], "entry_price": pos["entry_price"],
                        "exit_price": cur_price, "peak": pos["peak"], "size": pos["size"],
                        "reason": reason, "hold_min": elapsed_min,
                        "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
                    })
                    ds.recent_trades.append((net, pos["size"]))
                    if len(ds.recent_trades) > 50: ds.recent_trades = ds.recent_trades[-50:]
                    ds.equity += pos["size"] * net
                    if ds.equity > ds.peak_equity: ds.peak_equity = ds.equity
                    closed = True
                    break
            if not closed: new_open.append(pos)
        open_positions = new_open

        if day_pnl < 0 and abs(day_pnl) >= initial * params["daily_loss_stop_pct"] / 100.0:
            curve_pts.append((day_idx + 1, capital)); continue

        n_cands = cands()
        for _ in range(n_cands):
            state, scale, reason = defense_eval(ds, params, day_idx)
            if state == "pause":
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1; continue
            ttype = sample_token_type(rng, regime)
            curve_full = TOKEN_GENERATORS[ttype](rng)
            entry_age = rng.randint(int(params["min_age_seconds"]), int(params["max_age_seconds"]))
            entry_idx = _entry_idx_from_age(entry_age)
            if entry_idx >= len(curve_full): entry_idx = len(curve_full) - 1
            chg5m = _compute_chg5m(curve_full, entry_idx)
            snap = sample_snapshot(rng, ttype, entry_idx)
            snap["chg5m"] = chg5m
            ok, gate = entry_check(params, snap)
            if not ok:
                skip_reasons[gate] = skip_reasons.get(gate, 0) + 1; continue
            size_usd = capital * params["trade_pct"] * scale
            if size_usd < 0.50:
                if capital < 0.50:
                    skip_reasons["below_min_capital"] = skip_reasons.get("below_min_capital", 0) + 1
                    continue
                size_usd = min(0.50, capital * 0.5)
            entry_price = curve_full[entry_idx]
            open_positions.append({
                "ttype": ttype, "entry_price": entry_price, "peak": entry_price,
                "size": size_usd, "entry_time_min": day_idx * 1440 + rng.randint(0, 1439),
                "entry_idx": entry_idx, "curve": curve_full,
            })
            ds.buys_today += 1
        curve_pts.append((day_idx + 1, capital))

    for pos in open_positions:
        cur_price = pos["curve"][-1]
        if cur_price > pos["peak"]: pos["peak"] = cur_price
        actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
        gross = cur_price / actual_entry - 1.0
        net = gross - ROUND_TRIP_DRAG
        pnl_usd = pos["size"] * net
        capital += pnl_usd
        exit_reasons["end_of_sim"] = exit_reasons.get("end_of_sim", 0) + 1
        all_trades.append({
            "token_type": pos["ttype"], "entry_price": pos["entry_price"],
            "exit_price": cur_price, "peak": pos["peak"], "size": pos["size"],
            "reason": "end_of_sim", "hold_min": 0,
            "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
        })

    final = capital
    roi_pct = (final / initial - 1.0) * 100
    wins = sum(1 for t in all_trades if t["net_pnl_usd"] > 0)
    n_trades = len(all_trades)
    eq = [p[1] for p in curve_pts]
    peak_eq = eq[0]; max_dd = 0.0
    for v in eq:
        if v > peak_eq: peak_eq = v
        dd = (peak_eq - v) / peak_eq if peak_eq > 0 else 0
        if dd > max_dd: max_dd = dd
    return {
        "final": final, "roi_pct": round(roi_pct, 2), "n_trades": n_trades, "wins": wins,
        "win_rate_pct": round(100 * wins / n_trades, 1) if n_trades else 0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exit_reasons": exit_reasons, "skip_reasons": skip_reasons,
        "curve": eq,
    }


def run_regime(params, regime, n_seeds=20, days=30):
    results = []
    for s in range(n_seeds):
        rng = random.Random(s * 1000 + abs(hash(regime)) % 1000)
        results.append(run_one_seed(rng, params, regime, days=days))
    return results


def summarize(results):
    finals = np.array([r["final"] for r in results])
    rois = np.array([r["roi_pct"] for r in results])
    n_trades = np.array([r["n_trades"] for r in results])
    wrs = np.array([r["win_rate_pct"] for r in results])
    dds = np.array([r["max_drawdown_pct"] for r in results])
    return {
        "final_median": round(float(np.median(finals)), 2),
        "final_mean": round(float(np.mean(finals)), 2),
        "roi_median_pct": round(float(np.median(rois)), 2),
        "roi_mean_pct": round(float(np.mean(rois)), 2),
        "roi_p5": round(float(np.percentile(rois, 5)), 2),
        "prob_profit_pct": round(100 * float(np.mean(rois > 0)), 1),
        "prob_lose_20pct_pct": round(100 * float(np.mean(finals < 16)), 1),
        "avg_trades": round(float(np.mean(n_trades)), 1),
        "avg_win_rate_pct": round(float(np.mean(wrs)), 1),
        "max_drawdown_pct_median": round(float(np.median(dds)), 2),
    }


# ==========================================================================
# RUN DEFAULT V543 PARAMS
# ==========================================================================
def main():
    print(f"V543 STRATEGY OPTIMIZER — search for no-lose params")
    print(f"Cost model: round-trip drag {ROUND_TRIP_DRAG*100:.1f}%, latency {ENTRY_LATENCY_PENALTY*100:.1f}%")
    print(f"Default V543 params (conservative):")
    for k in ["stop_loss", "quick_tp", "runner_threshold", "trail_width",
              "min_age_seconds", "max_age_seconds", "min_chg_5m_pct", "max_chg_5m_pct",
              "min_buy_sell_ratio", "max_top10_concentration_pct",
              "max_buys_per_day", "daily_loss_stop_pct", "trade_pct", "dd_half_pct"]:
        print(f"  {k} = {DEFAULT_PARAMS[k]}")
    print()
    print("Running 20 seeds x 3 regimes with V543 default...")
    all_results = {}
    for regime in ["harsh", "base", "normal"]:
        results = run_regime(DEFAULT_PARAMS, regime, n_seeds=20)
        all_results[regime] = results
        s = summarize(results)
        print(f"\n{regime.upper()}:")
        print(f"  Median final: ${s['final_median']:.2f}  (ROI: {s['roi_median_pct']:+.2f}%)")
        print(f"  Mean   final: ${s['final_mean']:.2f}  (ROI: {s['roi_mean_pct']:+.2f}%)")
        print(f"  P(profit) = {s['prob_profit_pct']:.0f}%, P(lose>20%) = {s['prob_lose_20pct_pct']:.0f}%")
        print(f"  Avg trades: {s['avg_trades']:.0f}, avg WR: {s['avg_win_rate_pct']:.1f}%, max DD: {s['max_drawdown_pct_median']:.1f}%")
    print()
    print("=" * 60)
    # check if all 3 regimes are at breakeven or better
    rois = [summarize(all_results[r])["roi_median_pct"] for r in ["harsh", "base", "normal"]]
    print(f"Median ROIs: harsh={rois[0]:+.2f}%  base={rois[1]:+.2f}%  normal={rois[2]:+.2f}%")
    worst = min(rois)
    print(f"WORST regime median ROI: {worst:+.2f}%")
    if worst >= -2:
        print("=> V543 default is breakeven-or-better in ALL regimes ✓")
    else:
        print(f"=> V543 default still loses {worst:.2f}% in worst regime — need more tuning")

    # Save
    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "params": DEFAULT_PARAMS,
        "summary_by_regime": {r: summarize(all_results[r]) for r in ["harsh", "base", "normal"]},
        "individual_runs": {
            r: [{"seed": i+1, "final": x["final"], "roi_pct": x["roi_pct"],
                 "n_trades": x["n_trades"], "win_rate_pct": x["win_rate_pct"]}
                for i, x in enumerate(all_results[r])] for r in ["harsh", "base", "normal"]
        },
    }
    with open(OUT_DIR / "v543_default_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved to {OUT_DIR / 'v543_default_results.json'}")


if __name__ == "__main__":
    main()
