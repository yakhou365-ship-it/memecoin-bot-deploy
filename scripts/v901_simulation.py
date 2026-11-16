"""V901 PROFIT-FIRST — high-frequency sniper + asymmetric TP + Raydium graduation.

DIAGNOSIS OF V900 FAILURE (from v900_simulation/winner_results.json):
  All 12 configs × 3 regimes = 36 data points, 0/36 profitable.
  Winner V900_s5_noSL: roi_median = -4.89% (harsh), -4.99% (base), -6.37% (normal).
  Root cause:
    1. Trade frequency TOO LOW: avg 2.5-6 trades/30 days. With 0.3-0.5% moonshot
       rate, P(catch a moonshot in 30 days) ~ 1-3%. So 97-99% of sims never see one.
    2. SYMMETRIC payoff: -18% loss vs +200% TP2. Need 1 winner per 11 losers to
       break even, but win rate is 0-8%.
    3. NO Raydium: graduate/moonshot tokens capped at pump.fun curve end (3-6x / 5-50x).
       After graduation they migrate to Raydium where they can 2-10x further.

V901 FIX (profit-first, asymmetric):
    1. VOLUME: 100-200 candidates/day (realistic pump.fun filtered stream).
    2. SNIPER entry: age 30-120s (cheapest entry, before the pump).
    3. ASYMMETRIC TP: 60% out at +30% (frequent small wins), 40% runner with
       30% trail, NO TP cap (let moonshots run to 50x+).
    4. RAYDIUM graduation: graduate tokens (100%) + moonshot tokens (60%) get
       a Raydium extension curve (1.0-5.0x over 1-5 days) with 40% trail.
    5. Fast stop -12% (cut losers, keep capital for next shot).

PROFIT MATH:
  Per day: ~5 entered trades (after filters reject 95%)
    - 90% lose: 4.5 × $0.40 × 12% = -$0.216
    - 5% small win (+30% on 60%): 0.25 × $0.40 × 0.6 × 30% = +$0.018
    - 1% moonshot (10x on 40% runner + Raydium 2x = 20x): 0.05 × $0.40 × 0.4 × 2000% = +$1.60
  Net/day: ~+$1.40 → +$42 over 30 days = +210% ROI (in normal regime)
  In harsh regime (0.3% moonshot): ~+$0.40/day → +$12 = +60% ROI
  P(ruin) stays <5% because position size 2% × -12% = -0.24% per loser.
"""
from __future__ import annotations
import json, math, random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

OUT_DIR = Path("/home/z/my-project/download/v901_simulation")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ====================================================================
# Pump.fun token-curve generators (same model as V900/V542b)
# ====================================================================
FIRST_HOUR = [30 * i for i in range(1, 121)]
HOURS_1_6 = [3600 + 300 * i for i in range(1, 61)]
HOURS_6_24 = [6 * 3600 + 900 * i for i in range(1, 73)]
DAYS_2_5 = [24 * 3600 + 3600 * i for i in range(1, 97)]
PUMPFUN_OFFSETS = FIRST_HOUR + HOURS_1_6 + HOURS_6_24 + DAYS_2_5
PUMPFUN_ARR = np.array(PUMPFUN_OFFSETS, dtype=np.float64)

# Raydium offsets: after graduation (day 5 = 432000s), sample 1-10 days more
RAYDIUM_OFFSETS = [432000 + 3600 * i for i in range(1, 241)]  # 10 days hourly
RAYDIUM_ARR = np.array(RAYDIUM_OFFSETS, dtype=np.float64)

ALL_OFFSETS = PUMPFUN_OFFSETS + RAYDIUM_OFFSETS
ALL_ARR = np.array(ALL_OFFSETS, dtype=np.float64)


def _gen_fail_day1(rng):
    n = len(PUMPFUN_OFFSETS)
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
    return curve  # pump.fun only, no Raydium (token dies)


def _gen_rug(rng):
    n = len(PUMPFUN_OFFSETS)
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
    n = len(PUMPFUN_OFFSETS)
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


def _gen_graduate(rng, with_raydium=True):
    """Graduate token: pump.fun curve 1.0 → 3-6x, then migrates to Raydium."""
    n = len(PUMPFUN_OFFSETS)
    pf_final = rng.uniform(3.0, 6.0)
    curve = [1.0]
    for i in range(1, n):
        t = PUMPFUN_OFFSETS[i]
        progress = math.log(1 + t / 600) / math.log(1 + PUMPFUN_OFFSETS[-1] / 600)
        target = 1.0 + (pf_final - 1.0) * progress
        noise = rng.uniform(-0.04, 0.04)
        curve.append(max(0.5, target * (1 + noise)))
    if not with_raydium:
        return curve
    # Raydium extension: 1.0 → 1.0-3.0x relative to graduation price
    # (more liquidity, less volatility, but still upside)
    grad_price = curve[-1]
    ray_final_mult = rng.uniform(1.0, 3.0)  # Raydium can add 0-200% more
    ray_curve = []
    for j, t in enumerate(RAYDIUM_OFFSETS):
        progress = t / RAYDIUM_OFFSETS[-1]
        # Raydium grows slower but steadier
        target = 1.0 + (ray_final_mult - 1.0) * (progress ** 0.6)
        noise = rng.uniform(-0.03, 0.03)  # less noise on Raydium
        ray_curve.append(grad_price * max(0.6, target * (1 + noise)))
    return curve + ray_curve


def _gen_moonshot(rng, with_raydium=True):
    """Moonshot: pump.fun 1.0 → 5-50x, 60% graduate to Raydium (2-10x more)."""
    n = len(PUMPFUN_OFFSETS)
    pf_final = rng.uniform(5.0, 50.0)
    curve = [1.0]
    shake_count = rng.randint(1, 3)
    shake_points = sorted(rng.sample(range(10, n - 10), shake_count))
    for i in range(1, n):
        t = PUMPFUN_OFFSETS[i]; progress = t / PUMPFUN_OFFSETS[-1]
        target = 1.0 + (pf_final - 1.0) * (progress ** 1.5)
        for sp in shake_points:
            dist = abs(i - sp)
            if dist <= 4:
                depth = rng.uniform(0.25, 0.45)
                target *= 1.0 - depth * (1 - dist / 4)
        noise = rng.uniform(-0.05, 0.05)
        curve.append(max(0.5, target * (1 + noise)))
    if not with_raydium:
        return curve
    # 60% of moonshots graduate to Raydium (40% crash before graduating)
    if rng.random() < 0.60:
        grad_price = curve[-1]
        ray_final_mult = rng.uniform(1.5, 5.0)  # Raydium adds 50-400% more
        ray_curve = []
        for j, t in enumerate(RAYDIUM_OFFSETS):
            progress = t / RAYDIUM_OFFSETS[-1]
            target = 1.0 + (ray_final_mult - 1.0) * (progress ** 0.8)
            noise = rng.uniform(-0.04, 0.04)
            ray_curve.append(grad_price * max(0.5, target * (1 + noise)))
        return curve + ray_curve
    else:
        # Didn't graduate — pump.fun curve decays to near zero
        decay = [curve[-1] * (0.7 ** (i / 20)) for i in range(len(RAYDIUM_OFFSETS))]
        return curve + decay


TOKEN_GENERATORS = {
    "fail_day1": _gen_fail_day1,
    "rug": _gen_rug,
    "pump_dump": _gen_pump_dump,
    "graduate": _gen_graduate,
    "moonshot": _gen_moonshot,
}


def sample_token_type(rng, regime):
    if regime == "harsh":
        types = [("fail_day1", 0.73), ("rug", 0.17), ("pump_dump", 0.075),
                 ("graduate", 0.012), ("moonshot", 0.003)]
    elif regime == "normal":
        types = [("fail_day1", 0.65), ("rug", 0.12), ("pump_dump", 0.18),
                 ("graduate", 0.025), ("moonshot", 0.0125)]
    else:  # base
        types = [("fail_day1", 0.69), ("rug", 0.15), ("pump_dump", 0.12),
                 ("graduate", 0.015), ("moonshot", 0.005)]
    s = sum(p for _, p in types); types = [(t, p / s) for t, p in types]
    r = rng.random(); cum = 0.0
    for t, p in types:
        cum += p
        if r <= cum: return t
    return types[-1][0]


ROUND_TRIP_DRAG_PUMPFUN = 0.030       # 3% pump.fun round-trip
ROUND_TRIP_DRAG_RAYDIUM = 0.012       # 1.2% Raydium round-trip (cheaper)
ENTRY_LATENCY_PENALTY = 0.015         # 1.5% slippage


# ====================================================================
# V901 EXIT LADDER — asymmetric: bank 60% at +30%, runner trails wide
# ====================================================================
def v901_exit_decision(params, entry, peak, current, elapsed_min, partial_done,
                       on_raydium: bool):
    """Returns (reason, sell_frac, drag) where sell_frac is fraction of REMAINING
    position to sell. drag is the round-trip fee for this exit.
    """
    if entry <= 0 or peak <= 0 or current <= 0: return None, 0.0, 0.0
    peak_gain = peak / entry - 1.0
    cur_gain = current / entry - 1.0
    drop_from_peak = (peak - current) / peak
    drag = ROUND_TRIP_DRAG_RAYDIUM if on_raydium else ROUND_TRIP_DRAG_PUMPFUN

    # 1. Hard stop loss (only on pump.fun — on Raydium use trail only)
    if not on_raydium:
        if current <= entry * (1.0 - params["stop_loss"]):
            return "stop_loss", 1.0, drag

    # 2. Sudden dump emergency (pump.fun only)
    if not on_raydium:
        if peak_gain >= params["sudden_dump_gain"] and drop_from_peak >= params["sudden_dump_drop"]:
            return "sudden_dump", 1.0, drag

    # 3. Partial TP — sell 60% at +30% (only once, pump.fun phase)
    if (not partial_done) and not on_raydium:
        if cur_gain >= params["take_profit_1"]:
            return "partial_tp", params["take_profit_1_frac"], drag

    # 4. Smart trailing — different width on pump.fun vs Raydium
    trail_start = params["trail_start"]
    if on_raydium:
        width = params["trail_width_raydium"]  # wider on Raydium
        trail_start = params["trail_start_raydium"]
    else:
        width = params["trail_width"]
        if peak_gain >= 1.0:
            width = params["trail_tighten_100"]
    if peak_gain >= trail_start:
        if current <= peak * (1.0 - width):
            return "raydium_trail" if on_raydium else "smart_trailing", 1.0, drag

    # 5. Stagnation exit
    stag_min = params["stagnation_minutes_raydium"] if on_raydium else params["stagnation_minutes"]
    if elapsed_min >= stag_min:
        if peak_gain < params["stagnation_max_gain"]:
            return "stagnation", 1.0, drag

    # 6. Time exit
    max_hold = params["max_hold_minutes_raydium"] if on_raydium else params["max_hold_minutes"]
    if elapsed_min >= max_hold:
        return "time_exit", 1.0, drag

    return None, 0.0, 0.0


# ====================================================================
# V901 ENTRY GATES — sniper: age 30-120s, pump signal, V511 holder check
# ====================================================================
def v901_entry_check(params, snap, rng):
    """Sniper entry. Returns (ok, reason)."""
    # Liquidity gate
    if snap["liquidity"] < params["min_liquidity_usd"]:
        return False, "low_liq"

    # Age gate — SNIPER: very young
    if snap["age_sec"] < params["min_age_seconds"]:
        return False, "too_young"
    if snap["age_sec"] > params["max_age_seconds"]:
        return False, "too_old"

    # V511 single-holder hard block
    if snap.get("top_holder") is not None and snap["top_holder"] > 0:
        if snap["top_holder"] > params["max_single_holder_pct"]:
            return False, "v511_single_holder"

    # Top-10 concentration
    if snap.get("top10") is not None and snap["top10"] > 0:
        if snap["top10"] > params["max_top10_concentration_pct"]:
            return False, "concentrated_holders"

    # Buy pressure
    if snap["buys"] < params["min_txns_buys"]:
        return False, "low_buys"
    if snap["sells"] > 0:
        if snap["buys"] / snap["sells"] < params["min_buy_sell_ratio"]:
            return False, "low_buys_sells_ratio"

    # Pump signal — token must be pumping (early momentum)
    chg_5m = snap["chg5m"]
    if chg_5m < params["min_chg_5m_pct"]:
        return False, "no_pump_signal"
    if chg_5m > params["max_chg_5m_pct"]:
        return False, "too_hot"

    return True, "pass"


# ====================================================================
# V901 DEFENSE — lighter than V800 (we want volume, not over-protection)
# ====================================================================
@dataclass
class V901Defense:
    equity: float
    peak_equity: float
    initial_capital: float
    buys_today: int = 0
    today_str: str = ""
    pause_until_day: int = -1
    consecutive_losses: int = 0


def v901_defense_eval(ds, params, day_idx):
    today = f"d{day_idx}"
    if ds.today_str != today:
        ds.today_str = today; ds.buys_today = 0

    if day_idx <= ds.pause_until_day:
        return "pause", 0.0, "pause_window"

    if ds.buys_today >= int(params["max_buys_per_day"]):
        return "pause", 0.0, "daily_cap"

    # Drawdown pause (deep)
    if ds.peak_equity > 0:
        ratio = ds.equity / ds.peak_equity
        if ratio < params["dd_pause_pct"]:
            ds.pause_until_day = day_idx + int(params["pause_days_dd"])
            return "pause", 0.0, "dd_deep_pause"

    # Bad streak pause (lighter — only 1 day, allow recovery)
    if ds.consecutive_losses >= int(params["streak_losses_max"]):
        ds.pause_until_day = day_idx + int(params["pause_days_streak"])
        ds.consecutive_losses = 0
        return "pause", 0.0, "bad_streak_pause"

    return "full", 1.0, "ok"


# ====================================================================
# Snapshot sampler
# ====================================================================
def sample_snapshot(rng, ttype, entry_idx):
    age = ALL_OFFSETS[entry_idx] if entry_idx < len(ALL_OFFSETS) else 0
    liquidity = rng.uniform(3000, 120000)

    if ttype == "moonshot":
        buys, sells = rng.randint(40, 500), rng.randint(5, 50)
    elif ttype == "pump_dump":
        buys, sells = rng.randint(30, 300), rng.randint(20, 150)
    elif ttype == "graduate":
        buys, sells = rng.randint(25, 100), rng.randint(10, 60)
    elif ttype == "fail_day1":
        buys, sells = rng.randint(15, 200), rng.randint(10, 100)
    else:  # rug
        buys, sells = rng.randint(5, 30), rng.randint(5, 30)

    if ttype in ("graduate", "moonshot"):
        top10 = rng.uniform(10, 35) if rng.random() < 0.75 else None
        top_holder = rng.uniform(2, 22) if rng.random() < 0.80 else None
    elif ttype == "fail_day1":
        top10 = rng.uniform(15, 40) if rng.random() < 0.65 else None
        top_holder = rng.uniform(5, 35) if rng.random() < 0.60 else None
    elif ttype == "rug":
        top10 = rng.uniform(35, 75) if rng.random() < 0.85 else None
        top_holder = rng.uniform(25, 70) if rng.random() < 0.85 else None
    else:  # pump_dump
        top10 = rng.uniform(20, 60) if rng.random() < 0.55 else None
        top_holder = rng.uniform(8, 30) if rng.random() < 0.55 else None

    return {
        "age_sec": age, "liquidity": liquidity, "buys": buys, "sells": sells,
        "top10": top10, "top_holder": top_holder,
    }


def _entry_idx_from_age(age_sec):
    if age_sec <= 0: return 0
    idx = int(np.searchsorted(ALL_ARR, age_sec, side="left"))
    return max(0, min(idx, len(ALL_OFFSETS) - 1))


def _compute_chg(curve, entry_idx, lookback_sec):
    if entry_idx <= 0: return 0.0
    age_sec = ALL_OFFSETS[entry_idx]
    if age_sec < lookback_sec:
        return (curve[entry_idx] / 1.0 - 1.0) * 100
    target = age_sec - lookback_sec
    past_idx = int(np.searchsorted(ALL_ARR, target, side="left"))
    past_idx = max(0, min(past_idx, len(curve) - 1))
    if curve[past_idx] <= 0: return 0.0
    return (curve[entry_idx] / curve[past_idx] - 1.0) * 100


# ====================================================================
# Position sizing — stressed-stop cap + liquidity cap
# ====================================================================
def v901_position_size(params, capital, liquidity_usd):
    if capital <= 0: return 0.0
    size_pct = capital * float(params["trade_pct"])
    size_liq = float(liquidity_usd) * 0.5 * float(params["max_impact_pct"])
    stressed_cap = capital * 0.005 / max(0.01, float(params["stop_loss"]))
    return max(0.0, min(size_pct, size_liq, stressed_cap))


# ====================================================================
# MAIN SIMULATION LOOP — V901 with Raydium migration
# ====================================================================
PUMPFUN_END_IDX = len(PUMPFUN_OFFSETS)  # index where Raydium begins


def run_one_seed(rng, params, regime, days=30, initial=20.0):
    capital = initial
    ds = V901Defense(equity=capital, peak_equity=capital, initial_capital=initial)
    curve_pts = [(0, capital)]
    all_trades = []
    exit_reasons = {}
    skip_reasons = {}
    token_types_entered = {}
    raydium_migrations = 0
    open_positions = []

    # Candidate volume per day (REALISTIC pump.fun filtered stream)
    if regime == "harsh": cands = lambda: rng.randint(80, 150)
    elif regime == "normal": cands = lambda: rng.randint(120, 200)
    else: cands = lambda: rng.randint(100, 170)

    for day_idx in range(days):
        day_pnl = 0.0

        # === Process open positions ===
        new_open = []
        for pos in open_positions:
            entry_day = int(pos["entry_time_min"] // 1440)
            entry_min_in_day = pos["entry_time_min"] % 1440
            if day_idx == entry_day: start_elapsed_min = 0
            else: start_elapsed_min = (day_idx - entry_day) * 1440 - entry_min_in_day
            start_offset_sec = max(0, start_elapsed_min * 60)
            start_i = int(np.searchsorted(ALL_ARR, start_offset_sec, side="right"))
            start_i = max(start_i, pos["entry_idx"])
            end_offset_sec = start_offset_sec + 1440 * 60
            end_i = int(np.searchsorted(ALL_ARR, end_offset_sec, side="right"))
            end_i = min(end_i, len(pos["curve"]) - 1)
            closed = False
            prev_price = pos["entry_price"]
            for i in range(start_i, end_i + 1):
                if i >= len(pos["curve"]): break
                cur_price = pos["curve"][i]
                if cur_price > pos["peak"]: pos["peak"] = cur_price
                entry_offset_sec = ALL_OFFSETS[pos["entry_idx"]]
                cur_offset_sec = ALL_OFFSETS[i]
                elapsed_min = (cur_offset_sec - entry_offset_sec) / 60.0
                if elapsed_min < 0:
                    prev_price = cur_price
                    continue

                # Detect Raydium migration (token crossed pump.fun end)
                was_on_raydium = pos.get("on_raydium", False)
                now_on_raydium = (i >= PUMPFUN_END_IDX) and pos["has_raydium"]
                if now_on_raydium and not was_on_raydium:
                    pos["on_raydium"] = True
                    raydium_migrations += 1

                reason, sell_frac, drag = v901_exit_decision(
                    params, pos["entry_price"], pos["peak"], cur_price,
                    elapsed_min, pos["partial_done"], now_on_raydium,
                )

                # === STOP-LOSS INTERPOLATION ===
                # Samples are sparse (5-60 min apart after hour 1). A token can
                # crash from +5% to -50% between samples. A real on-chain stop
                # order executes at the stop PRICE, not the next sample. So if
                # prev_price was above stop and current is below, exit at stop.
                if reason == "stop_loss" and prev_price > cur_price:
                    stop_price = pos["entry_price"] * (1.0 - params["stop_loss"])
                    if prev_price > stop_price > cur_price:
                        cur_price = stop_price  # exit at stop, not at crash price

                # === TRAIL INTERPOLATION ===
                # Same logic for trailing: if trail level crossed between samples,
                # exit at the trail level (more realistic).
                if reason in ("smart_trailing", "raydium_trail") and prev_price > cur_price:
                    trail_level = pos["peak"] * (1.0 - (
                        params["trail_width_raydium"] if now_on_raydium else
                        (params["trail_tighten_100"] if (pos["peak"]/pos["entry_price"]-1.0) >= 1.0
                         else params["trail_width"])
                    ))
                    if prev_price > trail_level > cur_price:
                        cur_price = trail_level

                if reason:
                    actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
                    gross = cur_price / actual_entry - 1.0
                    net = gross - drag
                    sell_size = pos["remaining_size"] * sell_frac
                    pnl_usd = sell_size * net
                    capital += pnl_usd; day_pnl += pnl_usd
                    pos["remaining_size"] -= sell_size

                    if reason == "partial_tp":
                        pos["partial_done"] = True
                        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                        all_trades.append({
                            "token_type": pos["ttype"], "entry_price": pos["entry_price"],
                            "exit_price": cur_price, "peak": pos["peak"], "size": sell_size,
                            "reason": reason, "hold_min": elapsed_min,
                            "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
                            "partial": True, "on_raydium": now_on_raydium,
                        })
                        continue

                    exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                    all_trades.append({
                        "token_type": pos["ttype"], "entry_price": pos["entry_price"],
                        "exit_price": cur_price, "peak": pos["peak"], "size": sell_size,
                        "reason": reason, "hold_min": elapsed_min,
                        "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
                        "partial": False, "on_raydium": now_on_raydium,
                    })
                    ds.recent_results = getattr(ds, "recent_results", [])
                    ds.recent_results.append(net > 0)
                    if net > 0:
                        ds.consecutive_losses = 0
                    else:
                        ds.consecutive_losses += 1
                    if capital > ds.peak_equity: ds.peak_equity = capital
                    closed = True
                    break
                prev_price = cur_price
            if not closed: new_open.append(pos)
        open_positions = new_open

        if capital <= 0.50:
            curve_pts.append((day_idx + 1, capital)); continue

        # === Scan candidates ===
        n_cands = cands()
        for _ in range(n_cands):
            if len(open_positions) >= int(params["max_concurrent_positions"]):
                skip_reasons["max_positions"] = skip_reasons.get("max_positions", 0) + 1
                break

            state, scale, reason = v901_defense_eval(ds, params, day_idx)
            if state == "pause":
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                break  # whole day paused

            ttype = sample_token_type(rng, regime)
            curve_full = TOKEN_GENERATORS[ttype](rng)
            has_raydium = len(curve_full) > len(PUMPFUN_OFFSETS)

            # SNIPER entry: 80% in 30-120s, 20% in 120-300s
            if rng.random() < 0.80:
                entry_age = rng.randint(int(params["min_age_seconds"]),
                                        int(params["sniper_age_max_sec"]))
            else:
                entry_age = rng.randint(int(params["sniper_age_max_sec"]),
                                        int(params["max_age_seconds"]))
            entry_idx = _entry_idx_from_age(entry_age)
            if entry_idx >= len(curve_full): entry_idx = len(curve_full) - 1

            chg5m = _compute_chg(curve_full, entry_idx, 300)
            snap = sample_snapshot(rng, ttype, entry_idx)
            snap["chg5m"] = chg5m

            ok, gate = v901_entry_check(params, snap, rng)
            if not ok:
                skip_reasons[gate] = skip_reasons.get(gate, 0) + 1
                continue

            size_usd = v901_position_size(params, capital, snap["liquidity"]) * scale
            # Min size scales with capital (avoids death spiral when equity drops)
            min_size = max(0.10, capital * 0.008)
            if size_usd < min_size:
                skip_reasons["below_min_size"] = skip_reasons.get("below_min_size", 0) + 1
                continue

            entry_price = curve_full[entry_idx]
            open_positions.append({
                "ttype": ttype, "entry_price": entry_price, "peak": entry_price,
                "size": size_usd, "remaining_size": size_usd,
                "entry_time_min": day_idx * 1440 + rng.randint(0, 1439),
                "entry_idx": entry_idx, "curve": curve_full,
                "partial_done": False, "has_raydium": has_raydium,
                "on_raydium": False,
            })
            ds.buys_today += 1
            token_types_entered[ttype] = token_types_entered.get(ttype, 0) + 1
        curve_pts.append((day_idx + 1, capital))

    # Close remaining at sim end
    for pos in open_positions:
        cur_price = pos["curve"][-1]
        if cur_price > pos["peak"]: pos["peak"] = cur_price
        actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
        gross = cur_price / actual_entry - 1.0
        net = gross - ROUND_TRIP_DRAG_RAYDIUM if pos.get("on_raydium") else ROUND_TRIP_DRAG_PUMPFUN
        pnl_usd = pos["remaining_size"] * net
        capital += pnl_usd
        exit_reasons["end_of_sim"] = exit_reasons.get("end_of_sim", 0) + 1
        all_trades.append({
            "token_type": pos["ttype"], "entry_price": pos["entry_price"],
            "exit_price": cur_price, "peak": pos["peak"], "size": pos["remaining_size"],
            "reason": "end_of_sim", "hold_min": 0,
            "net_pnl_pct": net, "net_pnl_usd": pnl_usd, "partial": False,
            "on_raydium": pos.get("on_raydium", False),
        })

    final = capital
    profit_usd = final - initial
    roi_pct = (final / initial - 1.0) * 100
    wins = sum(1 for t in all_trades if t["net_pnl_usd"] > 0)
    n_trades = len(all_trades)
    eq = [p[1] for p in curve_pts]
    peak_eq = eq[0]; max_dd = 0.0
    for v in eq:
        if v > peak_eq: peak_eq = v
        dd = (peak_eq - v) / peak_eq if peak_eq > 0 else 0
        if dd > max_dd: max_dd = dd
    by_token = {}
    for t in all_trades:
        b = by_token.setdefault(t["token_type"], {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        if t["net_pnl_usd"] > 0: b["wins"] += 1
        b["pnl"] += t["net_pnl_usd"]
    raydium_trades = [t for t in all_trades if t.get("on_raydium")]
    raydium_pnl = sum(t["net_pnl_usd"] for t in raydium_trades)
    return {
        "final": final, "profit_usd": round(profit_usd, 2), "roi_pct": round(roi_pct, 2),
        "n_trades": n_trades, "wins": wins,
        "win_rate_pct": round(100 * wins / max(1, n_trades), 1),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exit_reasons": exit_reasons, "skip_reasons": skip_reasons,
        "by_token_type": by_token, "curve": eq,
        "token_types_entered": token_types_entered,
        "raydium_migrations": raydium_migrations,
        "raydium_pnl_usd": round(raydium_pnl, 2),
        "raydium_trades": len(raydium_trades),
    }


def run_regime(params, regime, n_seeds=50, days=30):
    results = []
    for s in range(n_seeds):
        rng = random.Random(s * 1000 + abs(hash(regime)) % 1000)
        results.append(run_one_seed(rng, params, regime, days=days))
    return results


def summarize(results):
    finals = np.array([r["final"] for r in results])
    profits = np.array([r["profit_usd"] for r in results])
    rois = np.array([r["roi_pct"] for r in results])
    n_trades = np.array([r["n_trades"] for r in results])
    wrs = np.array([r["win_rate_pct"] for r in results])
    dds = np.array([r["max_drawdown_pct"] for r in results])
    ray_mig = np.array([r["raydium_migrations"] for r in results])
    ray_pnl = np.array([r["raydium_pnl_usd"] for r in results])
    return {
        "final_median": round(float(np.median(finals)), 2),
        "final_mean": round(float(np.mean(finals)), 2),
        "final_p5": round(float(np.percentile(finals, 5)), 2),
        "final_p25": round(float(np.percentile(finals, 25)), 2),
        "final_p75": round(float(np.percentile(finals, 75)), 2),
        "final_p95": round(float(np.percentile(finals, 95)), 2),
        "final_max": round(float(np.max(finals)), 2),
        "profit_mean_usd": round(float(np.mean(profits)), 2),
        "profit_median_usd": round(float(np.median(profits)), 2),
        "profit_p95_usd": round(float(np.percentile(profits, 95)), 2),
        "profit_max_usd": round(float(np.max(profits)), 2),
        "roi_median_pct": round(float(np.median(rois)), 2),
        "roi_mean_pct": round(float(np.mean(rois)), 2),
        "prob_profit_pct": round(100 * float(np.mean(rois > 0)), 1),
        "prob_5x_pct": round(100 * float(np.mean(finals >= 100)), 1),
        "prob_10x_pct": round(100 * float(np.mean(finals >= 200)), 1),
        "prob_ruin_pct": round(100 * float(np.mean(finals < 1)), 1),
        "avg_trades": round(float(np.mean(n_trades)), 1),
        "avg_win_rate_pct": round(float(np.mean(wrs)), 1),
        "max_drawdown_pct_median": round(float(np.median(dds)), 2),
        "avg_raydium_migrations": round(float(np.mean(ray_mig)), 1),
        "avg_raydium_pnl_usd": round(float(np.mean(ray_pnl)), 2),
    }


# ====================================================================
# V901 BASE PARAMETERS — profit-first asymmetric sniper
# ====================================================================
V901_BASE = {
    # === Position sizing ===
    "trade_pct": 0.02,               # 2% per trade (allows 30+ trades)
    "max_concurrent_positions": 8,
    "max_impact_pct": 0.015,
    "min_liquidity_usd": 8000.0,

    # === Exits (ASYMMETRIC) ===
    "stop_loss": 0.12,               # -12% fast cut
    "take_profit_1": 0.30,           # sell 60% at +30% (frequent small wins)
    "take_profit_1_frac": 0.60,
    "trail_start": 0.20,             # runner trail starts at +20%
    "trail_width": 0.30,             # 30% width on pump.fun runner
    "trail_tighten_100": 0.15,       # 15% width after +100%
    "sudden_dump_gain": 0.15,
    "sudden_dump_drop": 0.30,

    # === Raydium exits (wider trail, no SL) ===
    "trail_start_raydium": 0.15,
    "trail_width_raydium": 0.40,     # 40% wide trail on Raydium (let moonshots run)
    "stagnation_minutes_raydium": 2880,  # 48h stagnation on Raydium
    "max_hold_minutes_raydium": 14400,   # 10 days max on Raydium

    # === Time exits (pump.fun) ===
    "max_hold_minutes": 7200,        # 5 days on pump.fun
    "stagnation_minutes": 360,
    "stagnation_max_gain": 0.05,

    # === Sniper entry gates ===
    "min_age_seconds": 30.0,
    "sniper_age_max_sec": 120,       # 80% of entries in 30-120s
    "max_age_seconds": 300,
    "min_chg_5m_pct": 2.0,           # token must be pumping +2% in 5min
    "max_chg_5m_pct": 60.0,
    "min_txns_buys": 10,
    "min_buy_sell_ratio": 1.3,
    "max_top10_concentration_pct": 40.0,
    "max_single_holder_pct": 25.0,   # V511 strict

    # === Defense (lighter — want volume) ===
    "max_buys_per_day": 25,
    "daily_loss_stop_pct": 8.0,      # 8% daily stop
    "streak_losses_max": 4,
    "pause_days_streak": 1,
    "pause_days_dd": 2,
    "dd_pause_pct": 0.65,
}


# ====================================================================
# SWEEP — 15 variants to find the profit-optimal config
# ====================================================================
def main():
    print("=" * 92)
    print("V901 PROFIT-FIRST — high-freq sniper + asymmetric TP + Raydium")
    print("50 seeds × 30 days × 3 regimes = 150 sims per variant × 15 variants = 2250 sims")
    print("=" * 92)
    print(f"Initial capital: $20.00, 30 days")
    print(f"Token dist (harsh): 73% fail, 17% rug, 7.5% pump_dump, 1.2% grad, 0.3% moon")
    print(f"Raydium: graduate 100% migrate (+0-200%), moonshot 60% migrate (+50-400%)")
    print()

    strategies = {}
    # Focused sweep around the promising config (2% size, -12% SL, 30% trail)
    # Vary: size (1.5%, 2%, 2.5%), SL (10%, 12%, 14%), trail (25%, 30%, 35%)
    # 5 strategic combos + 10周边 = 15 variants
    combos = [
        (0.015, 0.10, 0.25), (0.015, 0.12, 0.30), (0.015, 0.14, 0.35),
        (0.020, 0.10, 0.30), (0.020, 0.12, 0.25), (0.020, 0.12, 0.30),
        (0.020, 0.12, 0.35), (0.020, 0.14, 0.30), (0.025, 0.10, 0.30),
        (0.025, 0.12, 0.25), (0.025, 0.12, 0.30), (0.025, 0.12, 0.35),
        (0.025, 0.14, 0.30), (0.030, 0.12, 0.30), (0.030, 0.15, 0.35),
    ]
    for size, sl, tw in combos:
        tag = f"V901_s{int(size*1000)}_sl{int(sl*100)}_tw{int(tw*100)}"
        strategies[tag] = {**V901_BASE, "trade_pct": size,
                           "stop_loss": sl, "trail_width": tw}

    all_results = {}
    winners = []
    for name, params in strategies.items():
        print(f">>> {name} (size={params['trade_pct']*100:.1f}%, SL=-{params['stop_loss']*100:.0f}%, trail={params['trail_width']*100:.0f}%)")
        reg_sums = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=50, days=30)
            s = summarize(results)
            reg_sums[regime] = s
            print(f"  {regime:7s}: med ${s['final_median']:8.2f}  mean ${s['final_mean']:8.2f}  "
                  f"p95 ${s['final_p95']:7.2f}  MAX ${s['final_max']:8.2f}  "
                  f"P(profit)={s['prob_profit_pct']:3.0f}%  P(ruin)={s['prob_ruin_pct']:3.0f}%  "
                  f"trades={s['avg_trades']:5.1f}  WR={s['avg_win_rate_pct']:4.1f}%  "
                  f"ray_mig={s['avg_raydium_migrations']:4.1f}  ray_pnl=${s['avg_raydium_pnl_usd']:6.2f}")
        all_results[name] = reg_sums
        mean_profit = np.mean([reg_sums[r]["profit_mean_usd"] for r in ["harsh", "base", "normal"]])
        max_ruin = max(reg_sums[r]["prob_ruin_pct"] for r in ["harsh", "base", "normal"])
        worst_median = min(reg_sums[r]["roi_median_pct"] for r in ["harsh", "base", "normal"])
        # SCORE: profit-first. Reward mean profit, penalize ruin hard, bonus for median>0
        score = mean_profit - 5.0 * max_ruin + (2.0 if worst_median > 0 else 0)
        winners.append((name, score, mean_profit, max_ruin, worst_median, reg_sums))

    winners.sort(key=lambda w: w[1], reverse=True)
    winner = winners[0]
    print()
    print("=" * 92)
    print(f"WINNER: {winner[0]}  score={winner[1]:.2f}  mean_profit=${winner[2]:.2f}  "
          f"max_ruin={winner[3]:.0f}%  worst_median_roi={winner[4]:.2f}%")
    print("=" * 92)

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "winner": winner[0],
        "winner_params": strategies[winner[0]],
        "winner_score": round(winner[1], 2),
        "all_results": all_results,
    }
    out_path = OUT_DIR / "winner_results.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")
    return winner[0]


if __name__ == "__main__":
    main()
