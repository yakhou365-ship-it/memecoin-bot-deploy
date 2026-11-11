"""V542b — REALISTIC Monte-Carlo of V541 unified strategy.

Fixed entry model: the bot enters at the PRICE corresponding to the
scanner's `chg_5m_pct` (i.e., AFTER the token has pumped). It then
sees the future trajectory of the curve from that point on, NOT the
full curve from launch. This is what actually happens on Pump.fun:
the bot can only enter tokens that have already pumped enough to be
on the scanner's radar.

Three regimes, 30 seeds, 30 days, $20 start.
"""
from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# --- import the bot's ACTUAL unified strategy -----------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.modules.trading_strategy.unified import (  # noqa: E402
    UNIFIED_PARAMS,
    unified_exit_reason,
)

OUT_DIR = Path("/home/z/my-project/download/v542_simulation")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ==========================================================================
# 1) TOKEN CURVE GENERATORS  (price vs sample_idx; sample_idx 0 = launch)
# ==========================================================================
# Sample offsets in seconds (mirror the bot's update cadence)
FIRST_HOUR = [30 * i for i in range(1, 121)]                       # 30s..1h
HOURS_1_6 = [3600 + 300 * i for i in range(1, 61)]                 # 1h..6h
HOURS_6_24 = [6 * 3600 + 900 * i for i in range(1, 73)]            # 6h..24h
DAYS_2_5 = [24 * 3600 + 3600 * i for i in range(1, 97)]            # 24h..120h
SAMPLE_OFFSETS_SEC: List[int] = FIRST_HOUR + HOURS_1_6 + HOURS_6_24 + DAYS_2_5
SAMPLE_OFFSETS_ARR = np.array(SAMPLE_OFFSETS_SEC, dtype=np.float64)


def _gen_fail_day1(rng: random.Random) -> List[float]:
    """Big pump on launch (+20-80%) within 1-5 min, then collapse to ~5-15% by hour 2-4."""
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.20, 1.80)
    peak_idx = rng.randint(2, 10)
    floor_mult = rng.uniform(0.02, 0.20)
    floor_idx = rng.randint(130, min(190, n - 1))
    curve = [1.0]
    for i in range(1, n):
        if i <= peak_idx:
            f = i / peak_idx
            curve.append(1.0 + (peak_mult - 1.0) * f)
        elif i <= floor_idx:
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.001, floor_mult * (1 - 0.7 * f)))
    return curve


def _gen_rug(rng: random.Random) -> List[float]:
    """Honeypot: small pump then rug pull -90% in minutes."""
    n = len(SAMPLE_OFFSETS_SEC)
    initial_pump = rng.uniform(1.05, 1.30)
    rug_idx = rng.randint(3, 18)
    post_rug_mult = rng.uniform(0.02, 0.12)
    curve = [1.0]
    for i in range(1, n):
        if i < rug_idx:
            f = i / rug_idx
            curve.append(1.0 + (initial_pump - 1.0) * f)
        elif i == rug_idx:
            curve.append(post_rug_mult)
        else:
            curve.append(max(0.001, post_rug_mult * rng.uniform(0.85, 1.10)))
    return curve


def _gen_pump_dump(rng: random.Random) -> List[float]:
    """Real pump (+50-300%) over 5-30 min, then dump back to ~30-70%."""
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.50, 4.00)
    peak_idx = rng.randint(8, 60)
    floor_mult = rng.uniform(0.30, 0.70)
    floor_idx = min(n - 1, peak_idx + rng.randint(20, 90))
    curve = [1.0]
    for i in range(1, n):
        if i <= peak_idx:
            f = i / peak_idx
            curve.append(1.0 + (peak_mult - 1.0) * (f ** 0.7))
        elif i <= floor_idx:
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.05, floor_mult * (1 - 0.5 * f)))
    return curve


def _gen_graduate(rng: random.Random) -> List[float]:
    """Steady climber: +200-500% over 1-3 days with pullbacks."""
    n = len(SAMPLE_OFFSETS_SEC)
    final_mult = rng.uniform(3.0, 6.0)
    curve = [1.0]
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        progress = math.log(1 + t / 600) / math.log(1 + SAMPLE_OFFSETS_SEC[-1] / 600)
        target = 1.0 + (final_mult - 1.0) * progress
        noise = rng.uniform(-0.04, 0.04)
        curve.append(max(0.5, target * (1 + noise)))
    return curve


def _gen_moonshot(rng: random.Random) -> List[float]:
    """Moonshot: 5x-50x over 1-7 days with shakeouts."""
    n = len(SAMPLE_OFFSETS_SEC)
    final_mult = rng.uniform(5.0, 50.0)
    curve = [1.0]
    shake_count = rng.randint(1, 3)
    shake_points = sorted(rng.sample(range(10, n - 10), shake_count))
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        progress = t / SAMPLE_OFFSETS_SEC[-1]
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
    "fail_day1": _gen_fail_day1,
    "rug": _gen_rug,
    "pump_dump": _gen_pump_dump,
    "graduate": _gen_graduate,
    "moonshot": _gen_moonshot,
}


def sample_token_type(rng: random.Random, regime: str) -> str:
    if regime == "harsh":
        types = [("fail_day1", 0.73), ("rug", 0.17), ("pump_dump", 0.075),
                 ("graduate", 0.012), ("moonshot", 0.003)]
    elif regime == "normal":
        types = [("fail_day1", 0.65), ("rug", 0.12), ("pump_dump", 0.18),
                 ("graduate", 0.025), ("moonshot", 0.0125)]
    else:
        types = [("fail_day1", 0.69), ("rug", 0.15), ("pump_dump", 0.12),
                 ("graduate", 0.015), ("moonshot", 0.005)]
    s = sum(p for _, p in types)
    types = [(t, p / s) for t, p in types]
    r = rng.random()
    cum = 0.0
    for t, p in types:
        cum += p
        if r <= cum:
            return t
    return types[-1][0]


# ==========================================================================
# 2) ENTRY GATE — same logic as unified_entry_check
# ==========================================================================
@dataclass
class TokenSnapshot:
    symbol: str
    price_change_5m_pct: float
    age_seconds: int
    liquidity_usd: float
    txns_24h_buys: int
    txns_24h_sells: int
    top_10_pct: Optional[float] = None


def sample_token_for_entry(rng: random.Random, ttype: str, regime: str,
                            entry_idx_in_curve: int) -> Optional[TokenSnapshot]:
    """Generate a snapshot. The chg5m is computed from the curve so it
    matches reality: chg5m = (curve[entry_idx] / curve[max(0, entry_idx-10)] - 1) * 100
    for tokens age>=300s; for younger tokens, chg5m = (curve[entry_idx] / 1.0 - 1) * 100.
    We then subject the snapshot to the bot's entry gate.
    """
    age = SAMPLE_OFFSETS_SEC[entry_idx_in_curve] if entry_idx_in_curve < len(SAMPLE_OFFSETS_SEC) else 0
    age = int(age)
    # liquidity, buy pressure, holder concentration as before
    liquidity = rng.uniform(2500, 80000)
    if ttype == "moonshot":
        buys = rng.randint(40, 500); sells = rng.randint(5, 50)
    elif ttype == "pump_dump":
        buys = rng.randint(30, 300); sells = rng.randint(20, 150)
    elif ttype == "graduate":
        buys = rng.randint(25, 100); sells = rng.randint(10, 60)
    elif ttype == "fail_day1":
        buys = rng.randint(15, 200); sells = rng.randint(10, 100)
    else:  # rug
        buys = rng.randint(5, 30); sells = rng.randint(5, 30)
    if ttype in ("graduate", "moonshot"):
        top10 = rng.uniform(10, 35) if rng.random() < 0.7 else None
    elif ttype == "fail_day1":
        top10 = rng.uniform(15, 40) if rng.random() < 0.6 else None
    else:
        top10 = rng.uniform(20, 60) if rng.random() < 0.5 else None
    # We do NOT preset chg5m — it will be computed from the curve at the
    # point of entry by the caller. The bot reads chg5m from the Pump.fun API.
    return TokenSnapshot(
        symbol=f"{ttype[:3].upper()}{rng.randint(100,999)}",
        price_change_5m_pct=0.0,  # filled by caller
        age_seconds=age,
        liquidity_usd=liquidity,
        txns_24h_buys=buys, txns_24h_sells=sells,
        top_10_pct=top10,
    )


def unified_entry_check_from_snapshot(tok: TokenSnapshot) -> Tuple[bool, str]:
    if tok.price_change_5m_pct < UNIFIED_PARAMS["min_chg_5m_pct"]:
        return False, "chg5m_low"
    if tok.price_change_5m_pct > UNIFIED_PARAMS["max_chg_5m_pct"]:
        return False, "chg5m_high"
    if tok.age_seconds > UNIFIED_PARAMS["max_age_seconds"]:
        return False, "too_old"
    if tok.liquidity_usd < UNIFIED_PARAMS["min_liquidity_usd"]:
        return False, "low_liq"
    if tok.txns_24h_buys < UNIFIED_PARAMS["min_txns_buys"]:
        return False, "low_buys"
    if tok.txns_24h_sells > 0:
        ratio = tok.txns_24h_buys / tok.txns_24h_sells
        if ratio < UNIFIED_PARAMS["min_buy_sell_ratio"]:
            return False, "low_buys_sells_ratio"
    if tok.top_10_pct is not None and tok.top_10_pct > 0:
        if tok.top_10_pct > UNIFIED_PARAMS["max_top10_concentration_pct"]:
            return False, "concentrated_holders"
    return True, "pass"


# ==========================================================================
# 3) POSITION + DEFENSE
# ==========================================================================
@dataclass
class Position:
    token_type: str
    entry_price: float
    peak_price: float
    entry_size_usd: float
    entry_time_min: float
    entry_idx_in_curve: int  # which sample offset the bot entered at
    curve: List[float]  # full curve from launch (we track forward from entry_idx)


@dataclass
class TradeResult:
    token_type: str
    entry_price: float
    exit_price: float
    peak_price: float
    entry_size_usd: float
    exit_reason: str
    hold_minutes: float
    net_pnl_pct: float
    net_pnl_usd: float


@dataclass
class DefenseState:
    equity: float
    peak_equity: float
    buys_today: int = 0
    today_str: str = ""
    pause_until_day: int = -1
    recent_trades: List[Tuple[float, float]] = field(default_factory=list)


def defense_evaluate(ds: DefenseState, day_idx: int) -> Tuple[str, float, str]:
    today = f"d{day_idx}"
    if ds.today_str != today:
        ds.today_str = today
        ds.buys_today = 0
    if day_idx <= ds.pause_until_day:
        return "pause", 0.0, "streak_or_dd_pause"
    if ds.buys_today >= int(UNIFIED_PARAMS["max_buys_per_day"]):
        return "pause", 0.0, "daily_cap"
    if ds.peak_equity > 0:
        ratio = ds.equity / ds.peak_equity
        if ratio < UNIFIED_PARAMS["dd_half_pct"]:
            ds.pause_until_day = day_idx + int(UNIFIED_PARAMS["pause_days_dd"])
            return "pause", 0.0, "dd_deep"
        if ratio < UNIFIED_PARAMS["dd_full_pct"]:
            return "half", 0.5, "dd_half"
    if len(ds.recent_trades) >= int(UNIFIED_PARAMS["streak_window"]):
        window = ds.recent_trades[-int(UNIFIED_PARAMS["streak_window"]):]
        wins = sum(1 for p, _ in window if p > 0)
        wr = wins / len(window)
        total_pnl = sum(p for p, _ in window)
        total_size = sum(s for _, s in window)
        pnl_pct = total_pnl / total_size if total_size > 0 else 0.0
        if wr <= UNIFIED_PARAMS["streak_wr_max"] and pnl_pct <= UNIFIED_PARAMS["streak_pnl_min"]:
            ds.pause_until_day = day_idx + int(UNIFIED_PARAMS["pause_days_streak"])
            return "pause", 0.0, "bad_streak"
    return "full", 1.0, "ok"


def defense_register_buy(ds: DefenseState):
    ds.buys_today += 1


def defense_register_close(ds: DefenseState, net_pnl_pct: float, size_usd: float):
    ds.recent_trades.append((net_pnl_pct, size_usd))
    if len(ds.recent_trades) > 50:
        ds.recent_trades = ds.recent_trades[-50:]
    ds.equity += size_usd * net_pnl_pct
    if ds.equity > ds.peak_equity:
        ds.peak_equity = ds.equity


# ==========================================================================
# 4) SIMULATION CORE
# ==========================================================================
ROUND_TRIP_DRAG = 0.030
ENTRY_LATENCY_PENALTY = 0.015


def _entry_idx_from_age(age_sec: int) -> int:
    """Find the sample index closest to a given age (seconds since launch)."""
    if age_sec <= 0:
        return 0
    idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, age_sec, side="left"))
    return max(0, min(idx, len(SAMPLE_OFFSETS_SEC) - 1))


def _compute_chg5m(curve: List[float], entry_idx: int) -> float:
    """chg5m = (price now / price 5min ago - 1) * 100.
    If age < 5min, chg5m is computed from launch (price=1.0)."""
    if entry_idx <= 0:
        return 0.0
    age_sec = SAMPLE_OFFSETS_SEC[entry_idx]
    if age_sec < 300:
        # bot sees change since launch (or since first sample)
        ref_price = 1.0  # launch price
        return (curve[entry_idx] / ref_price - 1.0) * 100
    # find sample 5min ago
    target = age_sec - 300
    past_idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, target, side="left"))
    past_idx = max(0, min(past_idx, len(curve) - 1))
    return (curve[entry_idx] / curve[past_idx] - 1.0) * 100


def run_one_seed(rng: random.Random, regime: str, days: int = 30,
                  initial_capital: float = 20.0) -> Dict:
    capital = initial_capital
    ds = DefenseState(equity=capital, peak_equity=capital)
    curve_points: List[Tuple[int, float]] = [(0, capital)]
    all_trades: List[TradeResult] = []
    exit_reasons: Dict[str, int] = {}
    skip_reasons: Dict[str, int] = {}
    open_positions: List[Position] = []

    if regime == "harsh":
        cands_per_day = lambda: rng.randint(1, 2)
    elif regime == "normal":
        cands_per_day = lambda: rng.randint(4, 8)
    else:
        cands_per_day = lambda: rng.randint(2, 4)

    for day_idx in range(days):
        day_pnl = 0.0
        # 1) advance open positions by 1 day, checking exits at each sample offset
        new_open = []
        for pos in open_positions:
            # how many minutes has this position been open at the start of today?
            entry_day = int(pos.entry_time_min // 1440)
            entry_min_in_day = pos.entry_time_min % 1440
            # at the start of today (minute 0 of today), elapsed since entry:
            if day_idx == entry_day:
                start_elapsed_min = 0  # entered today, will be checked from entry
            else:
                start_elapsed_min = (day_idx - entry_day) * 1440 - entry_min_in_day
            start_offset_sec = max(0, start_elapsed_min * 60)
            start_i = int(np.searchsorted(SAMPLE_OFFSETS_ARR, start_offset_sec, side="right"))
            start_i = max(start_i, pos.entry_idx_in_curve)
            end_offset_sec = start_offset_sec + 1440 * 60
            end_i = int(np.searchsorted(SAMPLE_OFFSETS_ARR, end_offset_sec, side="right"))
            end_i = min(end_i, len(pos.curve) - 1)

            closed = False
            for i in range(start_i, end_i + 1):
                if i >= len(pos.curve):
                    break
                cur_price = pos.curve[i]
                if cur_price > pos.peak_price:
                    pos.peak_price = cur_price
                # elapsed minutes since entry = (sample offset - entry offset) / 60
                entry_offset_sec = SAMPLE_OFFSETS_SEC[pos.entry_idx_in_curve]
                cur_offset_sec = SAMPLE_OFFSETS_SEC[i]
                elapsed_min = (cur_offset_sec - entry_offset_sec) / 60.0
                if elapsed_min < 0:
                    continue
                reason = unified_exit_reason(
                    entry_price=pos.entry_price,
                    peak_price=pos.peak_price,
                    current_price=cur_price,
                    elapsed_minutes=elapsed_min,
                )
                if reason:
                    actual_entry = pos.entry_price * (1 + ENTRY_LATENCY_PENALTY)
                    gross_pnl_pct = cur_price / actual_entry - 1.0
                    net_pnl_pct = gross_pnl_pct - ROUND_TRIP_DRAG
                    pnl_usd = pos.entry_size_usd * net_pnl_pct
                    capital += pnl_usd
                    day_pnl += pnl_usd
                    exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                    all_trades.append(TradeResult(
                        token_type=pos.token_type,
                        entry_price=pos.entry_price, exit_price=cur_price,
                        peak_price=pos.peak_price, entry_size_usd=pos.entry_size_usd,
                        exit_reason=reason, hold_minutes=elapsed_min,
                        net_pnl_pct=net_pnl_pct, net_pnl_usd=pnl_usd,
                    ))
                    defense_register_close(ds, net_pnl_pct, pos.entry_size_usd)
                    closed = True
                    break
            if not closed:
                new_open.append(pos)
        open_positions = new_open

        # daily loss stop
        if day_pnl < 0 and abs(day_pnl) >= initial_capital * UNIFIED_PARAMS["daily_loss_stop_pct"] / 100.0:
            curve_points.append((day_idx + 1, capital))
            continue

        # 2) new candidates this day
        n_cands = cands_per_day()
        # the bot scans continuously; we sample entry ages uniformly in [30, 180] seconds
        for _ in range(n_cands):
            state, scale, reason = defense_evaluate(ds, day_idx)
            if state == "pause":
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue
            ttype = sample_token_type(rng, regime)
            curve_full = TOKEN_GENERATORS[ttype](rng)
            # bot enters at age in [30, 180] seconds
            entry_age = rng.randint(30, 180)
            entry_idx = _entry_idx_from_age(entry_age)
            if entry_idx >= len(curve_full):
                entry_idx = len(curve_full) - 1
            # compute the chg5m the bot would see at this point
            chg5m = _compute_chg5m(curve_full, entry_idx)
            snap = sample_token_for_entry(rng, ttype, regime, entry_idx)
            snap.price_change_5m_pct = chg5m
            # apply entry gate
            ok, gate_reason = unified_entry_check_from_snapshot(snap)
            if not ok:
                skip_reasons[gate_reason] = skip_reasons.get(gate_reason, 0) + 1
                continue
            # passed gates -> open position
            trade_pct_base = 0.10
            size_usd = capital * trade_pct_base * scale
            min_trade = 0.50
            if size_usd < min_trade:
                if capital < min_trade:
                    skip_reasons["below_min_capital"] = skip_reasons.get("below_min_capital", 0) + 1
                    continue
                size_usd = min(min_trade, capital * 0.5)
            entry_price = curve_full[entry_idx]
            pos = Position(
                token_type=ttype,
                entry_price=entry_price,
                peak_price=entry_price,
                entry_size_usd=size_usd,
                entry_time_min=day_idx * 1440 + rng.randint(0, 1439),
                entry_idx_in_curve=entry_idx,
                curve=curve_full,
            )
            open_positions.append(pos)
            defense_register_buy(ds)
        curve_points.append((day_idx + 1, capital))

    # close remaining open positions at end of simulation
    for pos in open_positions:
        cur_price = pos.curve[-1]
        if cur_price > pos.peak_price:
            pos.peak_price = cur_price
        actual_entry = pos.entry_price * (1 + ENTRY_LATENCY_PENALTY)
        gross_pnl_pct = cur_price / actual_entry - 1.0
        net_pnl_pct = gross_pnl_pct - ROUND_TRIP_DRAG
        pnl_usd = pos.entry_size_usd * net_pnl_pct
        capital += pnl_usd
        exit_reasons["end_of_sim"] = exit_reasons.get("end_of_sim", 0) + 1
        all_trades.append(TradeResult(
            token_type=pos.token_type, entry_price=pos.entry_price, exit_price=cur_price,
            peak_price=pos.peak_price, entry_size_usd=pos.entry_size_usd,
            exit_reason="end_of_sim", hold_minutes=0,
            net_pnl_pct=net_pnl_pct, net_pnl_usd=pnl_usd,
        ))
        defense_register_close(ds, net_pnl_pct, pos.entry_size_usd)

    final = capital
    roi_pct = (final / initial_capital - 1.0) * 100
    wins = sum(1 for t in all_trades if t.net_pnl_usd > 0)
    n_trades = len(all_trades)
    eq = [p[1] for p in curve_points]
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        if v > peak: peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    by_token = {}
    for t in all_trades:
        b = by_token.setdefault(t.token_type, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        if t.net_pnl_usd > 0: b["wins"] += 1
        b["pnl"] += t.net_pnl_usd
    return {
        "final": final, "initial": initial_capital, "roi_pct": round(roi_pct, 2),
        "n_trades": n_trades, "wins": wins,
        "win_rate_pct": round(100 * wins / n_trades, 1) if n_trades else 0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exit_reasons": exit_reasons, "skip_reasons": skip_reasons,
        "by_token_type": by_token, "curve": eq,
    }


def run_regime(regime: str, n_seeds: int = 30, days: int = 30):
    print(f"\n=== Regime: {regime.upper()} ({n_seeds} seeds, {days} days, $20 start) ===")
    results = []
    for s in range(n_seeds):
        rng = random.Random(s * 1000 + hash(regime) % 1000)
        r = run_one_seed(rng, regime, days=days, initial_capital=20.0)
        results.append(r)
        if (s + 1) % 5 == 0:
            print(f"  seed {s+1}/{n_seeds}: final=${r['final']:.2f} ROI={r['roi_pct']:+.2f}% trades={r['n_trades']} WR={r['win_rate_pct']:.0f}%")
    return results


def stats_summary(results: list, label: str) -> Dict:
    finals = np.array([r["final"] for r in results])
    rois = np.array([r["roi_pct"] for r in results])
    n_trades = np.array([r["n_trades"] for r in results])
    wrs = np.array([r["win_rate_pct"] for r in results])
    dds = np.array([r["max_drawdown_pct"] for r in results])
    n = len(results)
    return {
        "label": label, "n_seeds": n,
        "final_median": round(float(np.median(finals)), 2),
        "final_mean": round(float(np.mean(finals)), 2),
        "final_p5": round(float(np.percentile(finals, 5)), 2),
        "final_p25": round(float(np.percentile(finals, 25)), 2),
        "final_p75": round(float(np.percentile(finals, 75)), 2),
        "final_p95": round(float(np.percentile(finals, 95)), 2),
        "final_min": round(float(np.min(finals)), 2),
        "final_max": round(float(np.max(finals)), 2),
        "roi_median_pct": round(float(np.median(rois)), 2),
        "roi_mean_pct": round(float(np.mean(rois)), 2),
        "prob_profit_pct": round(100 * float(np.mean(rois > 0)), 1),
        "prob_double_pct": round(100 * float(np.mean(rois >= 100)), 1),
        "prob_lose_half_pct": round(100 * float(np.mean(finals < 10)), 1),
        "prob_ruin_pct": round(100 * float(np.mean(finals < 5)), 1),
        "avg_trades": round(float(np.mean(n_trades)), 1),
        "avg_win_rate_pct": round(float(np.mean(wrs)), 1),
        "max_drawdown_pct_median": round(float(np.median(dds)), 2),
        "max_drawdown_pct_mean": round(float(np.mean(dds)), 2),
    }


def main():
    print(f"V542b SIMULATION — bot's actual unified strategy (V541 code)")
    print(f"Real Pump.fun: 69% fail_day1, 15% rug, 12% pump_dump, 1.5% graduate, 0.5% moonshot")
    print(f"Round-trip drag: {ROUND_TRIP_DRAG*100:.1f}%, entry latency: {ENTRY_LATENCY_PENALTY*100:.1f}%")
    print(f"Key strategy params:")
    for k in ["quick_tp", "runner_threshold", "trail_width", "stop_loss", "mega_tp",
              "max_buys_per_day", "daily_loss_stop_pct", "dd_full_pct", "dd_half_pct"]:
        print(f"  {k} = {UNIFIED_PARAMS[k]}")
    print()

    all_results = {}
    curves = {}
    for regime in ["harsh", "base", "normal"]:
        results = run_regime(regime, n_seeds=30, days=30)
        all_results[regime] = results
        curves[regime] = np.array([r["curve"] for r in results])
        np.save(OUT_DIR / f"curves_{regime}_v542b.npy", curves[regime])
    summaries = {regime: stats_summary(results, regime) for regime, results in all_results.items()}

    per_regime_exit_reasons = {}
    per_regime_skip_reasons = {}
    per_regime_by_token = {}
    for regime, results in all_results.items():
        ex, sk, bt = {}, {}, {}
        for r in results:
            for k, v in r["exit_reasons"].items(): ex[k] = ex.get(k, 0) + v
            for k, v in r["skip_reasons"].items(): sk[k] = sk.get(k, 0) + v
            for k, v in r["by_token_type"].items():
                b = bt.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
                b["n"] += v["n"]; b["wins"] += v["wins"]; b["pnl"] += v["pnl"]
        per_regime_exit_reasons[regime] = ex
        per_regime_skip_reasons[regime] = sk
        per_regime_by_token[regime] = bt

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "bot_version": "0.7.2 (V541 unified strategy)",
        "simulator": "v542b_real_pumpfun_montecarlo_fixed_entry",
        "initial_capital_usd": 20.0,
        "days": 30,
        "n_seeds_per_regime": 30,
        "round_trip_drag_pct": ROUND_TRIP_DRAG * 100,
        "entry_latency_penalty_pct": ENTRY_LATENCY_PENALTY * 100,
        "pumpfun_stats_pct": [
            ["fail_day1", 69.0], ["rug", 15.0], ["pump_dump", 12.0],
            ["graduate", 1.5], ["moonshot", 0.5],
        ],
        "summary_by_regime": summaries,
        "exit_reasons_by_regime": per_regime_exit_reasons,
        "skip_reasons_by_regime": per_regime_skip_reasons,
        "by_token_type_by_regime": per_regime_by_token,
        "individual_runs": {
            regime: [
                {"seed": i + 1, "final": r["final"], "roi_pct": r["roi_pct"],
                 "n_trades": r["n_trades"], "wins": r["wins"],
                 "win_rate_pct": r["win_rate_pct"],
                 "max_drawdown_pct": r["max_drawdown_pct"]}
                for i, r in enumerate(results)
            ] for regime, results in all_results.items()
        },
    }
    with open(OUT_DIR / "results_v542b.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nResults written to {OUT_DIR / 'results_v542b.json'}")
    print()
    print("=" * 70)
    print("SUMMARY (V542b — fixed entry timing)")
    print("=" * 70)
    for regime in ["harsh", "base", "normal"]:
        s = summaries[regime]
        print(f"\n{regime.upper()} market ({s['n_seeds']} seeds, $20 start, 30 days):")
        print(f"  Median final: ${s['final_median']:.2f}  (ROI: {s['roi_median_pct']:+.2f}%)")
        print(f"  Mean   final: ${s['final_mean']:.2f}  (ROI: {s['roi_mean_pct']:+.2f}%)")
        print(f"  p5  / p25 / p75 / p95:")
        print(f"      ${s['final_p5']:.2f}  ${s['final_p25']:.2f}  ${s['final_p75']:.2f}  ${s['final_p95']:.2f}")
        print(f"  P(profit)     = {s['prob_profit_pct']:.1f}%")
        print(f"  P(double)     = {s['prob_double_pct']:.1f}%")
        print(f"  P(lose >50%)  = {s['prob_lose_half_pct']:.1f}%")
        print(f"  P(ruin <25%)  = {s['prob_ruin_pct']:.1f}%")
        print(f"  Avg trades    = {s['avg_trades']:.0f}, avg WR = {s['avg_win_rate_pct']:.1f}%")
        print(f"  Median max DD = {s['max_drawdown_pct_median']:.1f}%")


if __name__ == "__main__":
    main()
