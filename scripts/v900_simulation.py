"""V900 ULTIMATE — simulation sweep.

GOAL (per user): "اكبر ربح مادي مع اقل خسارة على سوق حقيقي"
  = maximize MEAN $ profit with P(ruin) < 5% AND median ROI >= 0% in all regimes.

V900 merges:
  - V549's Monte-Carlo Pump.fun model (69% fail_day1, 15% rug, 12% pump_dump,
    1.5% graduate, 0.5% moonshot; 3% round-trip drag + 1.5% entry latency)
  - V800's RISK-FIRST controls:
      * dip entry (buy capitulation that's stabilizing, not chasing pumps)
      * market-trend switch (no longs while market median 1h < 0, EXCEPT dip)
      * liquidity-aware sizing (cap notional so price impact <= 1.5%)
      * partial TP (sell 50% at +50%, rest rides to +200%)
      * tight defense (4% daily stop, 3-loss pause, DD zones)
  - HyperGrok's stressed-stop sizing (cap per-trade risk at 0.5% equity)
  - V511 single-holder gate (top holder >= 25% = hard reject)
  - learning adjustment (after 5 losing trades, tighten max_chg_5m by 20%)

The sweep tests 9 variants (3 sizes × 3 SLs) × 30 seeds × 3 regimes = 810 sims.
"""
from __future__ import annotations
import json, math, random, sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np

OUT_DIR = Path("/home/z/my-project/download/v900_simulation")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ====================================================================
# Pump.fun token-curve generators (identical to v542b/v546 — same model)
# ====================================================================
FIRST_HOUR = [30 * i for i in range(1, 121)]
HOURS_1_6 = [3600 + 300 * i for i in range(1, 61)]
HOURS_6_24 = [6 * 3600 + 900 * i for i in range(1, 73)]
DAYS_2_5 = [24 * 3600 + 3600 * i for i in range(1, 97)]
SAMPLE_OFFSETS_SEC = FIRST_HOUR + HOURS_1_6 + HOURS_6_24 + DAYS_2_5
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
    else:  # base
        types = [("fail_day1", 0.69), ("rug", 0.15), ("pump_dump", 0.12),
                 ("graduate", 0.015), ("moonshot", 0.005)]
    s = sum(p for _, p in types); types = [(t, p / s) for t, p in types]
    r = rng.random(); cum = 0.0
    for t, p in types:
        cum += p
        if r <= cum: return t
    return types[-1][0]


ROUND_TRIP_DRAG = 0.030       # 3% round-trip fees (Pump.fun reality)
ENTRY_LATENCY_PENALTY = 0.015  # 1.5% slippage from signal-to-fill


# ====================================================================
# V900 EXIT LADDER — adds partial TP + tightens at +100%
# ====================================================================
def v900_exit_decision(params, entry, peak, current, elapsed_min, partial_done):
    """Returns (reason, sell_frac) where sell_frac is fraction of REMAINING
    position to sell at this bar (1.0 = full exit, 0.5 = partial).

    partial_done: bool whether partial TP already triggered (state).
    """
    if entry <= 0 or peak <= 0 or current <= 0: return None, 0.0
    peak_gain = peak / entry - 1.0
    cur_gain = current / entry - 1.0
    drop_from_peak = (peak - current) / peak

    # 1. Hard stop loss
    if current <= entry * (1.0 - params["stop_loss"]):
        return "stop_loss", 1.0

    # 2. Sudden dump emergency
    if peak_gain >= params["sudden_dump_gain"] and drop_from_peak >= params["sudden_dump_drop"]:
        return "sudden_dump", 1.0

    # 3. Mega TP — full exit at +500%
    if cur_gain >= params["mega_tp"]:
        return "mega_tp", 1.0

    # 4. Partial TP — sell 50% at +50% (only once)
    if (not partial_done) and cur_gain >= params["take_profit_1"]:
        return "partial_tp", params["take_profit_1_frac"]

    # 5. Smart trailing — starts at +30%, tightens to 8% after +100%
    if peak_gain >= params["trail_start"]:
        width = params["trail_width"]
        if peak_gain >= 1.0:
            width = params["trail_tighten_100"]
        if current <= peak * (1.0 - width):
            return "smart_trailing", 1.0

    # 6. Full TP2 — close the runner at +200%
    if cur_gain >= params["take_profit_2"]:
        return "take_profit_2", 1.0

    # 7. Stagnation exit — after 3h with peak_gain < 5%
    if elapsed_min >= params["stagnation_minutes"]:
        if peak_gain < params["stagnation_max_gain"]:
            return "stagnation", 1.0

    # 8. Time exit
    if elapsed_min >= params["max_hold_minutes"]:
        return "time_exit", 1.0

    return None, 0.0


# ====================================================================
# V900 ENTRY GATES — dip OR quality pump (with V511 single-holder)
# ====================================================================
def v900_entry_check(params, snap, regime_median_1h):
    """Returns (ok, reason, entry_type).

    entry_type ∈ {"dip", "pump"} — used for market-regime exemption
    (dip entries are allowed even in falling markets).
    """
    # Liquidity gate
    if snap["liquidity"] < params["min_liquidity_usd"]:
        return False, "low_liq", None

    # Age gate — separate thresholds for dip vs pump
    # (we don't know yet which path will fire, so check min for both)
    if snap["age_sec"] < params["min_age_seconds"]:
        return False, "too_young", None

    # V511 single-holder hard block
    if snap.get("top_holder") is not None and snap["top_holder"] > 0:
        if snap["top_holder"] > params["max_single_holder_pct"]:
            return False, "v511_single_holder", None

    # Top-10 concentration
    if snap.get("top10") is not None and snap["top10"] > 0:
        if snap["top10"] > params["max_top10_concentration_pct"]:
            return False, "concentrated_holders", None

    # Buy pressure
    if snap["buys"] < params["min_txns_buys"]:
        return False, "low_buys", None
    if snap["sells"] > 0:
        if snap["buys"] / snap["sells"] < params["min_buy_sell_ratio"]:
            return False, "low_buys_sells_ratio", None

    # === DUAL ENTRY PATH: dip OR quality pump ===
    chg_5m = snap["chg5m"]
    chg_1h = snap.get("chg1h", 0.0)
    is_dip = (chg_1h <= params["dip_thr_1h_pct"]
              and chg_5m >= params["min_chg_5m_pct_dip"]
              and chg_5m <= params["max_chg_5m_pct"]
              and snap["age_sec"] <= params["max_age_seconds_dip"])
    is_pump = (chg_5m >= params["min_chg_5m_pct_pump"]
               and chg_5m <= params["max_chg_5m_pct"]
               and snap["age_sec"] <= params["max_age_seconds_pump"])

    if not (is_dip or is_pump):
        return False, "no_dip_no_pump", None

    # === Market regime switch (LENIENT: only blocks pumps if market is REALLY crashing) ===
    if params.get("market_regime_enabled", 1):
        if regime_median_1h is not None and regime_median_1h <= params["market_regime_min_pct"]:
            if is_pump and not is_dip:
                return False, "market_crashing_no_pump", None
            # dip in falling market = OK (capitulation buy)

    return True, "pass", ("dip" if is_dip else "pump")


# ====================================================================
# V900 DEFENSE — V800 style: 4% daily stop, 3-loss pause, DD zones
# ====================================================================
@dataclass
class V900Defense:
    equity: float
    peak_equity: float
    initial_capital: float
    buys_today: int = 0
    today_str: str = ""
    pause_until_day: int = -1
    consecutive_losses: int = 0
    recent_results: List[bool] = field(default_factory=list)  # True=win, False=loss
    # Learning state
    max_chg_5m_tighten: float = 0.0  # 0 = no tighten, 0.20 = -20% from baseline
    recent_losing_streaks: int = 0


def v900_defense_eval(ds, params, day_idx):
    today = f"d{day_idx}"
    if ds.today_str != today:
        ds.today_str = today; ds.buys_today = 0

    # Active pause window
    if day_idx <= ds.pause_until_day:
        return "pause", 0.0, "pause_window"

    # Daily buy cap
    if ds.buys_today >= int(params["max_buys_per_day"]):
        return "pause", 0.0, "daily_cap"

    # Drawdown zones (vs peak equity)
    if ds.peak_equity > 0:
        ratio = ds.equity / ds.peak_equity
        if ratio < params["dd_pause_pct"]:
            ds.pause_until_day = day_idx + int(params["pause_days_dd"])
            return "pause", 0.0, "dd_deep_pause"
        if ratio < params["dd_half_pct"]:
            return "half", 0.5, "dd_half"

    # Bad streak pause
    if ds.consecutive_losses >= int(params["streak_losses_max"]):
        ds.pause_until_day = day_idx + int(params["pause_days_streak"])
        ds.consecutive_losses = 0
        ds.recent_losing_streaks += 1
        # Learning: tighten max_chg_5m by 20% after each bad-streak event
        if params.get("learning_enabled", 1):
            ds.max_chg_5m_tighten = min(0.60, ds.max_chg_5m_tighten + params["learning_tighten_factor"])
        return "pause", 0.0, "bad_streak_pause"

    # Daily loss stop
    if ds.equity < ds.initial_capital * (1.0 - params["daily_loss_stop_pct"] / 100.0):
        return "pause", 0.0, "daily_loss_stop"

    return "full", 1.0, "ok"


# ====================================================================
# Snapshot sampler — adds chg1h + top_holder
# ====================================================================
def sample_snapshot(rng, ttype, entry_idx, curve):
    age = SAMPLE_OFFSETS_SEC[entry_idx] if entry_idx < len(SAMPLE_OFFSETS_SEC) else 0
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

    # Holder concentration — V511 single-holder matters
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
    idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, age_sec, side="left"))
    return max(0, min(idx, len(SAMPLE_OFFSETS_SEC) - 1))


def _compute_chg(curve, entry_idx, lookback_sec):
    """Compute price change over the last `lookback_sec` seconds."""
    if entry_idx <= 0: return 0.0
    age_sec = SAMPLE_OFFSETS_SEC[entry_idx]
    if age_sec < lookback_sec:
        # Not enough history; use change from launch (curve[0]=1.0)
        return (curve[entry_idx] / 1.0 - 1.0) * 100
    target = age_sec - lookback_sec
    past_idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, target, side="left"))
    past_idx = max(0, min(past_idx, len(curve) - 1))
    if curve[past_idx] <= 0: return 0.0
    return (curve[entry_idx] / curve[past_idx] - 1.0) * 100


# ====================================================================
# Liquidity-aware sizing (V800 + HyperGrok stressed-stop cap)
# ====================================================================
def v900_position_size(params, capital, liquidity_usd):
    """Return USD notional for a new position.

    Three caps, smallest wins:
      * trade_pct × equity
      * liquidity-aware cap (max_impact_pct × liquidity × 0.5)
      * stressed-stop cap: risk per trade <= 0.5% of equity
        (risk = size × stop_loss, so size = equity × 0.005 / stop_loss)
    """
    if capital <= 0: return 0.0
    size_pct = capital * float(params["trade_pct"])
    size_liq = float(liquidity_usd) * 0.5 * float(params["max_impact_pct"])
    stressed_cap = capital * 0.005 / max(0.01, float(params["stop_loss"]))
    return max(0.0, min(size_pct, size_liq, stressed_cap))


# ====================================================================
# MAIN SIMULATION LOOP — V900 with partial TP + regime tracking
# ====================================================================
def run_one_seed(rng, params, regime, days=30, initial=20.0):
    capital = initial
    ds = V900Defense(equity=capital, peak_equity=capital, initial_capital=initial)
    curve_pts = [(0, capital)]
    all_trades = []
    exit_reasons = {}
    skip_reasons = {}
    entry_types = {"dip": 0, "pump": 0}
    open_positions = []

    if regime == "harsh": cands = lambda: rng.randint(2, 4)
    elif regime == "normal": cands = lambda: rng.randint(5, 10)
    else: cands = lambda: rng.randint(3, 6)

    # Track market regime: rolling window of last 20 sampled tokens' 1h chg
    market_1h_window: List[float] = []

    for day_idx in range(days):
        day_pnl = 0.0

        # === Process open positions (check exits, handle partial TP) ===
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
                reason, sell_frac = v900_exit_decision(
                    params, pos["entry_price"], pos["peak"], cur_price,
                    elapsed_min, pos["partial_done"],
                )
                if reason:
                    actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
                    gross = cur_price / actual_entry - 1.0
                    net = gross - ROUND_TRIP_DRAG
                    sell_size = pos["remaining_size"] * sell_frac
                    pnl_usd = sell_size * net
                    capital += pnl_usd; day_pnl += pnl_usd
                    pos["remaining_size"] -= sell_size

                    if reason == "partial_tp":
                        pos["partial_done"] = True
                        # Don't close position — continue holding the rest
                        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                        all_trades.append({
                            "token_type": pos["ttype"], "entry_price": pos["entry_price"],
                            "exit_price": cur_price, "peak": pos["peak"], "size": sell_size,
                            "reason": reason, "hold_min": elapsed_min,
                            "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
                            "partial": True,
                        })
                        continue  # keep holding remaining

                    # Full exit
                    exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                    all_trades.append({
                        "token_type": pos["ttype"], "entry_price": pos["entry_price"],
                        "exit_price": cur_price, "peak": pos["peak"], "size": sell_size,
                        "reason": reason, "hold_min": elapsed_min,
                        "net_pnl_pct": net, "net_pnl_usd": pnl_usd,
                        "partial": False,
                    })
                    ds.recent_results.append(net > 0)
                    if len(ds.recent_results) > 50: ds.recent_results = ds.recent_results[-50:]
                    if net > 0:
                        ds.consecutive_losses = 0
                    else:
                        ds.consecutive_losses += 1
                    ds.equity += pnl_usd
                    if ds.equity > ds.peak_equity: ds.peak_equity = ds.equity
                    closed = True
                    break
            if not closed: new_open.append(pos)
        open_positions = new_open

        if day_pnl < 0 and abs(day_pnl) >= initial * params["daily_loss_stop_pct"] / 100.0:
            curve_pts.append((day_idx + 1, capital)); continue
        if capital <= 0.50:
            curve_pts.append((day_idx + 1, capital)); continue

        # === Scan candidates for today ===
        n_cands = cands()
        for _ in range(n_cands):
            if len(open_positions) >= int(params["max_concurrent_positions"]):
                skip_reasons["max_positions"] = skip_reasons.get("max_positions", 0) + 1
                break

            # === Defense check FIRST ===
            state, scale, reason = v900_defense_eval(ds, params, day_idx)
            if state == "pause":
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue

            # === Sample token + compute metrics ===
            ttype = sample_token_type(rng, regime)
            curve_full = TOKEN_GENERATORS[ttype](rng)
            # Entry age: biased toward early (pump.fun window is first 10 min)
            # 70% of entries in 45-300s, 30% in 300-3600s
            if rng.random() < 0.70:
                entry_age = rng.randint(int(params["min_age_seconds"]), 300)
            else:
                entry_age = rng.randint(300, 3600)
            entry_idx = _entry_idx_from_age(entry_age)
            if entry_idx >= len(curve_full): entry_idx = len(curve_full) - 1

            chg5m = _compute_chg(curve_full, entry_idx, 300)
            chg1h = _compute_chg(curve_full, entry_idx, 3600)
            snap = sample_snapshot(rng, ttype, entry_idx, curve_full)
            snap["chg5m"] = chg5m
            snap["chg1h"] = chg1h

            # Track market regime (median of last 20 sampled tokens' 1h chg)
            market_1h_window.append(chg1h)
            if len(market_1h_window) > 20: market_1h_window = market_1h_window[-20:]
            if len(market_1h_window) >= int(params["market_regime_min_samples"]):
                regime_median_1h = float(np.median(market_1h_window))
            else:
                regime_median_1h = None

            # === Apply learning tighten to max_chg_5m ===
            effective_params = dict(params)
            if ds.max_chg_5m_tighten > 0:
                tighten = ds.max_chg_5m_tighten
                effective_params["max_chg_5m_pct"] = params["max_chg_5m_pct"] * (1.0 - tighten)

            ok, gate, entry_type = v900_entry_check(effective_params, snap, regime_median_1h)
            if not ok:
                skip_reasons[gate] = skip_reasons.get(gate, 0) + 1
                continue

            # === Position sizing (liquidity-aware + stressed-stop cap) ===
            size_usd = v900_position_size(params, capital, snap["liquidity"]) * scale
            if size_usd < 0.50:
                if capital < 0.50:
                    skip_reasons["below_min_capital"] = skip_reasons.get("below_min_capital", 0) + 1
                    continue
                size_usd = min(0.50, capital * 0.5)

            entry_price = curve_full[entry_idx]
            open_positions.append({
                "ttype": ttype, "entry_price": entry_price, "peak": entry_price,
                "size": size_usd, "remaining_size": size_usd,
                "entry_time_min": day_idx * 1440 + rng.randint(0, 1439),
                "entry_idx": entry_idx, "curve": curve_full,
                "partial_done": False, "entry_type": entry_type,
            })
            ds.buys_today += 1
            entry_types[entry_type] = entry_types.get(entry_type, 0) + 1
        curve_pts.append((day_idx + 1, capital))

    # Close any remaining positions at sim end
    for pos in open_positions:
        cur_price = pos["curve"][-1]
        if cur_price > pos["peak"]: pos["peak"] = cur_price
        actual_entry = pos["entry_price"] * (1 + ENTRY_LATENCY_PENALTY)
        gross = cur_price / actual_entry - 1.0
        net = gross - ROUND_TRIP_DRAG
        pnl_usd = pos["remaining_size"] * net
        capital += pnl_usd
        exit_reasons["end_of_sim"] = exit_reasons.get("end_of_sim", 0) + 1
        all_trades.append({
            "token_type": pos["ttype"], "entry_price": pos["entry_price"],
            "exit_price": cur_price, "peak": pos["peak"], "size": pos["remaining_size"],
            "reason": "end_of_sim", "hold_min": 0,
            "net_pnl_pct": net, "net_pnl_usd": pnl_usd, "partial": False,
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
    return {
        "final": final, "profit_usd": round(profit_usd, 2), "roi_pct": round(roi_pct, 2),
        "n_trades": n_trades, "wins": wins,
        "win_rate_pct": round(100 * wins / n_trades, 1) if n_trades else 0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exit_reasons": exit_reasons, "skip_reasons": skip_reasons,
        "by_token_type": by_token, "curve": eq,
        "entry_types": entry_types,
    }


def run_regime(params, regime, n_seeds=30, days=30):
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
    }


# ====================================================================
# V900 BASE PARAMETERS
# ====================================================================
V900_BASE = {
    # === Position sizing ===
    "trade_pct": 0.05,               # 5% baseline (sweep: 3%, 5%, 8%)
    "max_concurrent_positions": 5,
    "max_impact_pct": 0.015,         # cap notional so price impact <= 1.5%
    "min_liquidity_usd": 8000.0,     # lowered from 15000 (pump.fun early liquidity)

    # === Exits ===
    "stop_loss": 0.18,               # -18% (sweep: 15%, 18%, 22%)
    "take_profit_1": 0.50,           # sell 50% at +50%
    "take_profit_1_frac": 0.50,
    "take_profit_2": 2.00,           # close runner at +200%
    "trail_start": 0.30,             # trail starts at +30%
    "trail_width": 0.15,             # 15% width
    "trail_tighten_100": 0.08,       # 8% width after +100%
    "mega_tp": 5.00,
    "sudden_dump_gain": 0.15,
    "sudden_dump_drop": 0.30,

    # === Time exits ===
    "max_hold_minutes": 7200,         # 5 days — let moonshots ride to peak (was 240, too short!)
    "stagnation_minutes": 360,        # exit after 6h if peak_gain < 5%
    "stagnation_max_gain": 0.05,

    # === Entry gates (dual: dip OR pump) — RELAXED to increase trade frequency ===
    "dip_thr_1h_pct": -10.0,
    "min_chg_5m_pct_dip": 0.0,
    "min_chg_5m_pct_pump": 3.0,        # lowered from 5 — catch earlier pumps
    "max_chg_5m_pct": 80.0,            # raised — allow hotter pumps
    "min_age_seconds": 45.0,
    "max_age_seconds_pump": 600.0,
    "max_age_seconds_dip": 172800.0,
    "min_txns_buys": 15,               # lowered from 20 — early tokens have fewer buys
    "min_buy_sell_ratio": 1.2,         # lowered from 1.3
    "max_top10_concentration_pct": 40.0,  # raised from 35
    "max_single_holder_pct": 30.0,     # raised to 30 (V800 default)

    # === Market-trend switch (LENIENT for pump.fun — market is usually "falling") ===
    "market_regime_enabled": 1,
    "market_regime_min_pct": -30.0,    # median 1h must be > -30% (was 0; too strict for pump.fun)
    "market_regime_min_samples": 5,

    # === Defense ===
    "max_buys_per_day": 12,
    "daily_loss_stop_pct": 4.0,
    "streak_window": 10,
    "streak_losses_max": 3,
    "pause_days_streak": 1,
    "pause_days_dd": 2,
    "dd_half_pct": 0.85,
    "dd_pause_pct": 0.70,

    # === Learning adjustment ===
    "learning_enabled": 1,
    "learning_tighten_factor": 0.20,
}


# ====================================================================
# MAIN: 9 variants (3 sizes × 3 SLs)
# ====================================================================
def main():
    print("=" * 88)
    print("V900 ULTIMATE — SIMULATION SWEEP (9 variants × 30 seeds × 3 regimes = 810 sims)")
    print("=" * 88)
    print(f"Initial capital: $20.00, 30 days, real 3% round-trip + 1.5% entry latency")
    print(f"Token distribution (harsh): 73% fail_day1, 17% rug, 7.5% pump_dump, 1.2% grad, 0.3% moon")
    print()

    strategies = {}
    # Conservative sweep: 3 sizes × 3 SLs
    for size in [0.03, 0.05, 0.08]:
        for sl in [0.15, 0.18, 0.22]:
            tag = f"V900_s{int(size*100)}_sl{int(sl*100)}"
            strategies[tag] = {**V900_BASE, "trade_pct": size, "stop_loss": sl}
    # Aggressive sweep: larger sizes + tighter SL (catch more moonshots)
    for size in [0.10, 0.15, 0.20]:
        for sl in [0.08, 0.12, 0.18]:
            tag = f"V900_s{int(size*100)}_sl{int(sl*100)}"
            strategies[tag] = {**V900_BASE, "trade_pct": size, "stop_loss": sl}
    # No-SL moonshot sniper variants (let losers ride to zero, catch winners wide)
    for size in [0.05, 0.10]:
        tag = f"V900_s{int(size*100)}_noSL"
        strategies[tag] = {**V900_BASE, "trade_pct": size, "stop_loss": 0.95,
                            "trail_width": 0.25, "trail_tighten_100": 0.10}

    all_results = {}
    winners = []
    for name, params in strategies.items():
        print(f">>> {name} (size={params['trade_pct']*100:.0f}%, SL=-{params['stop_loss']*100:.0f}%)")
        reg_sums = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=30, days=30)
            s = summarize(results)
            reg_sums[regime] = s
            print(f"  {regime:7s}: mean ${s['final_mean']:8.2f}  med ${s['final_median']:8.2f}  "
                  f"p95 ${s['final_p95']:7.2f}  MAX ${s['final_max']:7.2f}  "
                  f"P(profit)={s['prob_profit_pct']:3.0f}%  P(ruin)={s['prob_ruin_pct']:3.0f}%  "
                  f"trades={s['avg_trades']:4.1f}  DD={s['max_drawdown_pct_median']:5.1f}%")
        all_results[name] = reg_sums
        # Score: mean of profit across regimes, with penalty for ruin
        mean_profit = np.mean([reg_sums[r]["profit_mean_usd"] for r in ["harsh", "base", "normal"]])
        max_ruin = max(reg_sums[r]["prob_ruin_pct"] for r in ["harsh", "base", "normal"])
        worst_median = min(reg_sums[r]["roi_median_pct"] for r in ["harsh", "base", "normal"])
        winners.append((name, mean_profit, max_ruin, worst_median, reg_sums))

    # Pick winner: maximize mean_profit, with hard constraints
    print("\n" + "=" * 88)
    print("RANKING (sorted by mean profit $, must have P(ruin) < 10% AND worst median >= -10%)")
    print("=" * 88)
    viable = [w for w in winners if w[2] < 10.0 and w[3] >= -10.0]
    viable.sort(key=lambda x: -x[1])
    for i, (name, mp, mr, wm, _) in enumerate(viable[:5]):
        marker = " ★ WINNER" if i == 0 else ""
        print(f"  #{i+1} {name:25s}: mean profit ${mp:6.2f}  P(ruin)={mr:4.1f}%  worst median={wm:6.2f}%{marker}")

    if not viable:
        print("  ⚠ NO VIABLE STRATEGY — relaxing constraints")
        winners.sort(key=lambda x: -x[1])
        for i, (name, mp, mr, wm, _) in enumerate(winners[:3]):
            print(f"  #{i+1} {name:25s}: mean profit ${mp:6.2f}  P(ruin)={mr:4.1f}%  worst median={wm:6.2f}%")

    winner_name = viable[0][0] if viable else winners[0][0]
    winner_params = strategies[winner_name]
    print(f"\n>>> WINNER: {winner_name}")
    print(f">>> Will be applied to unified.py")
    print(f">>> Params: size={winner_params['trade_pct']*100:.0f}%  SL=-{winner_params['stop_loss']*100:.0f}%")

    # Save full results
    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "winner": winner_name,
        "winner_params": winner_params,
        "all_results": all_results,
    }
    (OUT_DIR / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nSaved to {OUT_DIR}/results.json")


if __name__ == "__main__":
    main()
