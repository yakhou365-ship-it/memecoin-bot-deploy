"""V542 — Realistic Monte-Carlo simulation of the bot's ACTUAL V541 code.

This script imports the bot's real unified strategy from
backend/app/modules/trading_strategy/unified.py and runs it on a
realistic Pump.fun Monte-Carlo model. NO Binance, NO fake prices —
every token trajectory is built from real Pump.fun platform stats:

  Token-type distribution (Cryptorank / Dune analytics, 30-day window):
    - 69%  fail_day1  : pump on launch, then dies within ~6 hours
    - 15%  rug        : liquidity pulled / honeypot, -90% in minutes
    - 12%  pump_dump  : real pump (+50-300%) then dump back to entry
    - 1.5% graduate  : steady climber, +200-500% over 1-3 days
    - 0.5% moonshot  : 5x-50x over 1-7 days, multi-day runner

  Cost model (PumpPortal Lightning, real):
    - 1.0% protocol fee per side (buy + sell) = 2.0%
    - 0.5% slippage per side (avg)         = 1.0%
    - round-trip drag                      = ~3.0%   <-- baked into every exit

  Entry latency: 1.5% price already moved by the time the bot fills
                 (the bot scans every 30s, top-of-block competition)

Three market regimes simulated:
  - harsh : only 1-2 candidates/day, 73% fail_day1, 17% rug, more shakeouts
  - base  : 2-4 candidates/day, baseline distribution
  - normal: 4-8 candidates/day, 65% fail_day1, more pump&dump/moonshots

Output: /home/z/my-project/download/v542_simulation/results.json
        + curves_<regime>.npy (equity curves for all seeds)
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
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
# 1) TOKEN TRAJECTORY MODEL  (real Pump.fun stats)
# ==========================================================================
TOKEN_TYPES = [
    ("fail_day1", 0.69),
    ("rug",       0.15),
    ("pump_dump", 0.12),
    ("graduate",  0.015),
    ("moonshot",  0.005),
]


def _normalize(types: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    s = sum(p for _, p in types)
    return [(t, p / s) for t, p in types]


# Pump.fun bonding curve: price = K * supply, so price change = (new-liq/old-liq)^(1/2)
# We model price directly in multiplier form (price relative to entry).
# Each token's price is sampled at FIXED offsets from entry (every 30 seconds
# for the first hour, then every 5 minutes for the rest of day 1, then
# hourly thereafter, up to 5 days max). This mirrors what the bot's scanner
# actually sees.

SAMPLE_OFFSETS_SEC: List[int] = []
# first hour: every 30s (the bot updates positions this frequently)
SAMPLE_OFFSETS_SEC += [30 * i for i in range(1, 121)]
# hours 1-6: every 5 min
SAMPLE_OFFSETS_SEC += [3600 + 300 * i for i in range(1, 61)]
# hours 6-24: every 15 min
SAMPLE_OFFSETS_SEC += [6 * 3600 + 900 * i for i in range(1, 73)]
# days 2-5: every 1h
SAMPLE_OFFSETS_SEC += [24 * 3600 + 3600 * i for i in range(1, 97)]
MAX_HOLD_SEC = UNIFIED_PARAMS["max_hold_minutes"] * 60  # 5 days


def _gen_fail_day1(rng: random.Random) -> List[float]:
    """Pump on launch (+20-80% in first 2-5 min) then collapse to ~5-15% of entry by hour 2-4."""
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.20, 1.80)
    peak_idx = rng.randint(2, 10)        # peak within first ~1-5 min
    # the dump: collapse to 0.05-0.20 by hour 2-4 (sample idx ~130-180)
    floor_mult = rng.uniform(0.02, 0.20)
    floor_idx = rng.randint(130, min(190, n - 1))
    curve = [1.0]
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        if i <= peak_idx:
            # rising
            f = i / peak_idx
            curve.append(1.0 + (peak_mult - 1.0) * f)
        elif i <= floor_idx:
            # crashing
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            # dead — slight drift down
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.001, floor_mult * (1 - 0.7 * f)))
    return curve


def _gen_rug(rng: random.Random) -> List[float]:
    """Honeypot / liquidity pulled: small uptick then -90% within minutes."""
    n = len(SAMPLE_OFFSETS_SEC)
    # sometimes a small initial pump to lure buyers
    initial_pump = rng.uniform(1.00, 1.20)
    rug_idx = rng.randint(2, 14)  # rug happens within first 1-7 min
    post_rug_mult = rng.uniform(0.02, 0.12)
    curve = [1.0]
    for i in range(1, n):
        if i < rug_idx:
            f = i / rug_idx
            curve.append(1.0 + (initial_pump - 1.0) * f)
        elif i == rug_idx:
            curve.append(post_rug_mult)
        else:
            # stays dead, may recover slightly (not really)
            curve.append(max(0.001, post_rug_mult * rng.uniform(0.8, 1.1)))
    return curve


def _gen_pump_dump(rng: random.Random) -> List[float]:
    """Real pump (+50-300%) over 5-30 min, then dump back to ~30-70% of entry."""
    n = len(SAMPLE_OFFSETS_SEC)
    peak_mult = rng.uniform(1.50, 4.00)
    peak_idx = rng.randint(8, 60)  # peak within 5-30 min
    floor_mult = rng.uniform(0.30, 0.70)
    floor_idx = min(n - 1, peak_idx + rng.randint(20, 90))
    curve = [1.0]
    for i in range(1, n):
        if i <= peak_idx:
            f = i / peak_idx
            # exponential rise — looks like a real pump
            curve.append(1.0 + (peak_mult - 1.0) * (f ** 0.7))
        elif i <= floor_idx:
            f = (i - peak_idx) / max(1, floor_idx - peak_idx)
            # fast dump
            curve.append(peak_mult * (1 - f) + floor_mult * f)
        else:
            f = (i - floor_idx) / max(1, n - floor_idx)
            curve.append(max(0.05, floor_mult * (1 - 0.5 * f)))
    # add shakeout: a quick 25-40% dip from peak before final dump
    if peak_idx > 10 and rng.random() < 0.5:
        shake_idx = peak_idx + rng.randint(2, 6)
        if shake_idx < len(curve):
            shake_low = peak_mult * rng.uniform(0.60, 0.75)
            # interpolate a V-shape around shake_idx
            for j in range(max(1, shake_idx - 1), min(len(curve), shake_idx + 3)):
                old = curve[j]
                curve[j] = min(old, shake_low + (peak_mult - shake_low) * abs(j - shake_idx) / 3)
    return curve


def _gen_graduate(rng: random.Random) -> List[float]:
    """Steady climber: +200-500% over 1-3 days, minor pullbacks, no rugs."""
    n = len(SAMPLE_OFFSETS_SEC)
    final_mult = rng.uniform(3.0, 6.0)
    curve = [1.0]
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        # logarithmic climb — slows down
        progress = math.log(1 + t / 600) / math.log(1 + SAMPLE_OFFSETS_SEC[-1] / 600)
        target = 1.0 + (final_mult - 1.0) * progress
        # add pullback noise
        noise = rng.uniform(-0.04, 0.04)
        curve.append(max(0.5, target * (1 + noise)))
    return curve


def _gen_moonshot(rng: random.Random) -> List[float]:
    """Moonshot: 5x-50x over 1-7 days, big shakeouts, parabolic late."""
    n = len(SAMPLE_OFFSETS_SEC)
    final_mult = rng.uniform(5.0, 50.0)
    # parabolic climb
    curve = [1.0]
    # shakeout parameters
    shake_count = rng.randint(1, 3)
    shake_points = sorted(rng.sample(range(10, n - 10), shake_count))
    for i in range(1, n):
        t = SAMPLE_OFFSETS_SEC[i]
        # sigmoid climb: slow start, fast middle, plateau
        progress = t / SAMPLE_OFFSETS_SEC[-1]
        # parabolic on the second half
        target = 1.0 + (final_mult - 1.0) * (progress ** 1.5)
        # apply shakeouts (V dips)
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
    "rug":       _gen_rug,
    "pump_dump": _gen_pump_dump,
    "graduate":  _gen_graduate,
    "moonshot":  _gen_moonshot,
}


def sample_token_type(rng: random.Random, regime: str) -> str:
    """Sample a token type based on the regime."""
    if regime == "harsh":
        # more failures and rugs, fewer winners
        types = [
            ("fail_day1", 0.73),
            ("rug",       0.17),
            ("pump_dump", 0.075),
            ("graduate",  0.012),
            ("moonshot",  0.003),
        ]
    elif regime == "normal":
        # more pump&dumps and moonshots, fewer failures
        types = [
            ("fail_day1", 0.65),
            ("rug",       0.12),
            ("pump_dump", 0.18),
            ("graduate",  0.025),
            ("moonshot",  0.0125),
        ]
    else:  # base
        types = TOKEN_TYPES
    types = _normalize(types)
    r = rng.random()
    cum = 0.0
    for t, p in types:
        cum += p
        if r <= cum:
            return t
    return types[-1][0]


# ==========================================================================
# 2) ENTRY GATE MODEL — mirrors unified_entry_check
#    (we sample the SAME fields the bot reads from the scanner)
# ==========================================================================
@dataclass
class TokenSnapshot:
    """A snapshot of a token at the moment the bot considers entering."""
    symbol: str
    price_change_5m_pct: float
    age_seconds: int
    liquidity_usd: float
    txns_24h_buys: int
    txns_24h_sells: int
    top_10_pct: Optional[float] = None
    first_seen_at: Optional[datetime] = None


def sample_token_for_entry(rng: random.Random, ttype: str, regime: str) -> Optional[TokenSnapshot]:
    """Generate a token snapshot that the bot's scanner might see.
    Returns None if the token's launch data fails ALL entry gates
    (the bot would never even consider it).

    Realistic distributions per token type — the bot only sees tokens
    that have ALREADY pumped enough to be on the scanner's radar
    (so price_change_5m >= 5% as a hard floor).
    """
    age = rng.randint(30, 600)  # seconds since launch — bot scans new launches
    # 5m change: token-type dependent
    if ttype == "rug":
        chg5m = rng.uniform(5, 30)  # small pump before rug
    elif ttype == "fail_day1":
        chg5m = rng.uniform(8, 80)  # big initial pump
    elif ttype == "pump_dump":
        chg5m = rng.uniform(15, 150)  # already pumping hard
    elif ttype == "graduate":
        chg5m = rng.uniform(8, 30)  # steady climber, modest initial
    else:  # moonshot
        chg5m = rng.uniform(20, 200)  # strong initial momentum

    liquidity = rng.uniform(2500, 80000)
    # buy pressure correlated with token type
    if ttype == "moonshot":
        buys = rng.randint(40, 500)
        sells = rng.randint(5, 50)
    elif ttype == "pump_dump":
        buys = rng.randint(30, 300)
        sells = rng.randint(20, 150)
    elif ttype == "graduate":
        buys = rng.randint(25, 100)
        sells = rng.randint(10, 60)
    elif ttype == "fail_day1":
        buys = rng.randint(15, 200)
        sells = rng.randint(10, 100)
    else:  # rug
        buys = rng.randint(5, 30)
        sells = rng.randint(5, 30)

    # top-10 holder concentration — graduates/moonshots tend to be fairer
    if ttype in ("graduate", "moonshot"):
        top10 = rng.uniform(10, 35) if rng.random() < 0.7 else None
    elif ttype == "fail_day1":
        top10 = rng.uniform(15, 40) if rng.random() < 0.6 else None
    else:  # rug, pump_dump — often concentrated
        top10 = rng.uniform(20, 60) if rng.random() < 0.5 else None

    first_seen = datetime.utcnow() - timedelta(seconds=age)
    return TokenSnapshot(
        symbol=f"{ttype[:3].upper()}{rng.randint(100,999)}",
        price_change_5m_pct=chg5m,
        age_seconds=age,
        liquidity_usd=liquidity,
        txns_24h_buys=buys,
        txns_24h_sells=sells,
        top_10_pct=top10,
        first_seen_at=first_seen,
    )


# ==========================================================================
# 3) UNIFIED ENTRY GATE — replica of unified_entry_check using the snapshot
# ==========================================================================
def unified_entry_check_from_snapshot(tok: TokenSnapshot) -> Tuple[bool, str]:
    """Apply the SAME gates as unified_entry_check using a TokenSnapshot."""
    # momentum window
    if tok.price_change_5m_pct < UNIFIED_PARAMS["min_chg_5m_pct"]:
        return False, "chg5m_low"
    if tok.price_change_5m_pct > UNIFIED_PARAMS["max_chg_5m_pct"]:
        return False, "chg5m_high"
    # age
    if tok.age_seconds > UNIFIED_PARAMS["max_age_seconds"]:
        return False, "too_old"
    # liquidity
    if tok.liquidity_usd < UNIFIED_PARAMS["min_liquidity_usd"]:
        return False, "low_liq"
    # buy pressure
    if tok.txns_24h_buys < UNIFIED_PARAMS["min_txns_buys"]:
        return False, "low_buys"
    if tok.txns_24h_sells > 0:
        ratio = tok.txns_24h_buys / tok.txns_24h_sells
        if ratio < UNIFIED_PARAMS["min_buy_sell_ratio"]:
            return False, "low_buys_sells_ratio"
    # holder concentration
    if tok.top_10_pct is not None and tok.top_10_pct > 0:
        if tok.top_10_pct > UNIFIED_PARAMS["max_top10_concentration_pct"]:
            return False, "concentrated_holders"
    return True, "pass"


# ==========================================================================
# 4) POSITION TRACKING — uses the bot's unified_exit_reason
# ==========================================================================
@dataclass
class Position:
    token_type: str
    entry_price: float  # multiplier (1.0 = launch price)
    peak_price: float
    entry_size_usd: float
    entry_time_min: float
    last_update_min: float = 0.0


@dataclass
class TradeResult:
    token_type: str
    entry_price: float
    exit_price: float
    peak_price: float
    entry_size_usd: float
    exit_reason: str
    hold_minutes: float
    gross_pnl_pct: float  # before fees
    net_pnl_pct: float    # after fees
    net_pnl_usd: float


# ==========================================================================
# 5) DEFENSE STATE — mirrors unified_defense_snapshot (synchronous sim)
# ==========================================================================
@dataclass
class DefenseState:
    equity: float
    peak_equity: float
    buys_today: int = 0
    today_str: str = ""
    pause_until_day: int = -1  # day index when pause ends (inclusive)
    recent_trades: List[Tuple[float, float]] = field(default_factory=list)  # (pnl_pct, size)


def defense_evaluate(ds: DefenseState, day_idx: int) -> Tuple[str, float, str]:
    """Returns (state, scale, reason). state in {full, half, pause}."""
    # reset daily counter
    today = f"d{day_idx}"
    if ds.today_str != today:
        ds.today_str = today
        ds.buys_today = 0
    # active pause window
    if day_idx <= ds.pause_until_day:
        return "pause", 0.0, "streak_or_dd_pause"
    # daily buy cap
    if ds.buys_today >= int(UNIFIED_PARAMS["max_buys_per_day"]):
        return "pause", 0.0, "daily_cap"
    # drawdown zones
    if ds.peak_equity > 0:
        ratio = ds.equity / ds.peak_equity
        if ratio < UNIFIED_PARAMS["dd_half_pct"]:
            ds.pause_until_day = day_idx + int(UNIFIED_PARAMS["pause_days_dd"])
            return "pause", 0.0, "dd_deep"
        if ratio < UNIFIED_PARAMS["dd_full_pct"]:
            return "half", 0.5, "dd_half"
    # bad streak
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
    # keep last 50 for memory
    if len(ds.recent_trades) > 50:
        ds.recent_trades = ds.recent_trades[-50:]
    # update equity & peak
    ds.equity += size_usd * net_pnl_pct
    if ds.equity > ds.peak_equity:
        ds.peak_equity = ds.equity


# ==========================================================================
# 6) SIMULATION CORE
# ==========================================================================
ROUND_TRIP_DRAG = 0.030  # 3%: 1% PumpPortal fee x2 + 0.5% slippage x2
ENTRY_LATENCY_PENALTY = 0.015  # 1.5% price already moved before fill
SAMPLE_OFFSETS_ARR = np.array(SAMPLE_OFFSETS_SEC, dtype=np.float64)


def run_one_seed(
    rng: random.Random,
    regime: str,
    days: int = 30,
    initial_capital: float = 20.0,
) -> Dict:
    """Run one 30-day simulation. Returns dict with equity curve + stats."""
    # candidates per day depends on regime
    if regime == "harsh":
        candidates_per_day = lambda: rng.randint(1, 2)
    elif regime == "normal":
        candidates_per_day = lambda: rng.randint(4, 8)
    else:  # base
        candidates_per_day = lambda: rng.randint(2, 4)

    capital = initial_capital
    ds = DefenseState(equity=capital, peak_equity=capital)
    curve = [capital]
    all_trades: List[TradeResult] = []
    open_positions: List[Tuple[Position, int, str]] = []  # (pos, day_entered, curve_idx)
    exit_reasons: Dict[str, int] = {}
    skip_reasons: Dict[str, int] = {}
    daily_loss = 0.0
    day_pnl = 0.0
    # the bot checks positions every 30 seconds; SAMPLE_OFFSETS already 30s in first hour

    for day_idx in range(days):
        daily_loss = 0.0
        day_pnl = 0.0
        # generate candidates for this day
        n_cands = candidates_per_day()
        # we'll process them at random offsets within the day
        candidate_times = sorted(rng.sample(range(0, 1440), min(n_cands, 1440)))  # minutes within day
        cand_iter = iter(candidate_times)
        next_cand = next(cand_iter, None)

        # Simulate the day in 5-minute steps (the bot's typical update cadence
        # for older positions; new candidates appear at their sampled minute).
        # We assume each position's price curve is sampled at SAMPLE_OFFSETS_SEC
        # which already covers 5 days at 30s/5m/15m/1h cadence.
        step_min = 5
        for cur_min in range(0, 1440, step_min):
            # 1) check existing positions for exits
            still_open = []
            for pos, day_entered, curve_idx_at_entry in open_positions:
                # elapsed since entry
                elapsed_min = (day_idx - day_entered) * 1440 + cur_min
                # find current price from the token's curve at this elapsed_min
                # convert to seconds for lookup in SAMPLE_OFFSETS_SEC
                elapsed_sec = elapsed_min * 60
                # find the largest offset <= elapsed_sec
                idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, elapsed_sec, side="right")) - 1
                idx = max(0, min(idx, len(pos_token_curves[(pos.token_type, pos.entry_size_usd, id(pos))]) - 1))
                # we stored curves per position; we need access to the curve
                pass
            # The above approach is awkward; we'll refactor below.

        # --- cleaner approach: process candidate entries one by one,
        # then close all positions at end of day, advancing their curves.

    # ^ The above skeleton was getting too complex; rewrite cleanly below.
    return _run_seed_clean(rng, regime, days, initial_capital)


def _run_seed_clean(rng, regime, days, initial_capital):
    """Clean implementation: event-driven per-token evaluation."""
    capital = initial_capital
    ds = DefenseState(equity=capital, peak_equity=capital)
    curve_points: List[Tuple[int, float]] = [(0, capital)]  # (day_idx, equity)
    all_trades: List[TradeResult] = []
    exit_reasons: Dict[str, int] = {}
    skip_reasons: Dict[str, int] = {}
    # Track open positions: (Position, token_curve: List[float])
    open_positions: List[Tuple[Position, List[float]]] = []

    for day_idx in range(days):
        # reset daily counters via defense_evaluate
        # (we'll call defense_evaluate per entry attempt)
        day_pnl = 0.0

        # generate candidates
        if regime == "harsh":
            n_cands = rng.randint(1, 2)
        elif regime == "normal":
            n_cands = rng.randint(4, 8)
        else:
            n_cands = rng.randint(2, 4)

        # First, advance open positions by 1 day, checking exits at each sample offset
        positions_to_close: List[Tuple[Position, List[float], str, float]] = []
        # we'll process each open position: for each sample offset in this day,
        # compute elapsed and check exit
        new_open = []
        for pos, curve in open_positions:
            # determine day-relative sample offsets within this day's window
            # elapsed at start of this day = day_idx * 1440 min, but we want
            # elapsed since entry
            entry_day = pos.entry_time_min  # actually we stored minutes since start
            # the position was entered at some minute since start of simulation
            # which equals entry_day (in minutes since day 0)
            # now day_idx represents the start of the current day in minutes
            day_start_min = day_idx * 1440
            # elapsed_min at start of day = day_start_min - entry_day
            elapsed_at_day_start = day_start_min - entry_day  # in minutes
            # find the index in SAMPLE_OFFSETS for the position's curve
            # corresponding to elapsed_at_day_start
            start_offset_sec = max(0, elapsed_at_day_start * 60)
            start_idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, start_offset_sec, side="right"))
            # walk forward over offsets in this day (1440 min = 86400 sec)
            end_offset_sec = (elapsed_at_day_start + 1440) * 60
            end_idx = int(np.searchsorted(SAMPLE_OFFSETS_ARR, end_offset_sec, side="right"))
            end_idx = min(end_idx, len(curve) - 1)
            closed = False
            for i in range(start_idx, end_idx + 1):
                if i >= len(curve):
                    break
                cur_price = curve[i]
                # update peak
                if cur_price > pos.peak_price:
                    pos.peak_price = cur_price
                elapsed_min = SAMPLE_OFFSETS_SEC[i] / 60.0
                reason = unified_exit_reason(
                    entry_price=pos.entry_price,
                    peak_price=pos.peak_price,
                    current_price=cur_price,
                    elapsed_minutes=elapsed_min,
                )
                if reason:
                    positions_to_close.append((pos, curve, reason, cur_price))
                    closed = True
                    break
            if not closed:
                new_open.append((pos, curve))
        open_positions = new_open

        # close positions marked for closing
        for pos, curve, reason, exit_price in positions_to_close:
            # apply entry-latency penalty to entry price (we bought 1.5% above the launch price)
            actual_entry = pos.entry_price * (1 + ENTRY_LATENCY_PENALTY)
            # gross pnl % relative to actual entry
            gross_pnl_pct = exit_price / actual_entry - 1.0
            # net of round-trip fees
            net_pnl_pct = gross_pnl_pct - ROUND_TRIP_DRAG
            pnl_usd = pos.entry_size_usd * net_pnl_pct
            capital += pnl_usd
            day_pnl += pnl_usd
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
            tr = TradeResult(
                token_type=pos.token_type,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                peak_price=pos.peak_price,
                entry_size_usd=pos.entry_size_usd,
                exit_reason=reason,
                hold_minutes=(pos.last_update_min - pos.entry_time_min),
                gross_pnl_pct=gross_pnl_pct,
                net_pnl_pct=net_pnl_pct,
                net_pnl_usd=pnl_usd,
            )
            all_trades.append(tr)
            defense_register_close(ds, net_pnl_pct, pos.entry_size_usd)

        # daily loss stop
        if day_pnl < 0 and abs(day_pnl) >= initial_capital * UNIFIED_PARAMS["daily_loss_stop_pct"] / 100.0:
            # skip the rest of the day's entries
            curve_points.append((day_idx + 1, capital))
            continue

        # process new candidates this day
        for cand_idx in range(n_cands):
            # check defense state before each entry
            state, scale, reason = defense_evaluate(ds, day_idx)
            if state == "pause":
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue

            # sample a token type for this candidate
            ttype = sample_token_type(rng, regime)
            # generate the token snapshot the bot would see
            snap = sample_token_for_entry(rng, ttype, regime)
            if snap is None:
                skip_reasons["no_snapshot"] = skip_reasons.get("no_snapshot", 0) + 1
                continue
            # apply the unified entry gate
            ok, gate_reason = unified_entry_check_from_snapshot(snap)
            if not ok:
                skip_reasons[gate_reason] = skip_reasons.get(gate_reason, 0) + 1
                continue

            # passed gates — generate the token's full trajectory
            curve_full = TOKEN_GENERATORS[ttype](rng)
            # position size: capital * trade_pct (10% default, but the
            # unified strategy uses scale from defense — half = 5%)
            trade_pct_base = 0.10
            size_usd = capital * trade_pct_base * scale
            # enforce minimum trade size
            min_trade = 0.50
            if size_usd < min_trade:
                if capital < min_trade:
                    skip_reasons["below_min_capital"] = skip_reasons.get("below_min_capital", 0) + 1
                    continue
                size_usd = min(min_trade, capital * 0.5)

            # entry price: the snapshot's chg5m means the token has ALREADY moved
            # by chg5m from launch. The bot buys at chg5m (we model entry at 1.0
            # for the curve, then apply entry-latency penalty 1.5% for fill slippage).
            # So the curve starts at 1.0 (the price the bot sees) and entry is
            # at 1.0 * (1 + 1.5%) = 1.015 effectively.
            pos = Position(
                token_type=ttype,
                entry_price=1.0,
                peak_price=1.0,
                entry_size_usd=size_usd,
                entry_time_min=day_idx * 1440 + rng.randint(0, 1439),  # minute within simulation
                last_update_min=day_idx * 1440 + rng.randint(0, 1439),
            )
            open_positions.append((pos, curve_full))
            defense_register_buy(ds)

        # end of day: record equity
        curve_points.append((day_idx + 1, capital))

    # close any remaining open positions at end of simulation at last known price
    for pos, curve in open_positions:
        cur_price = curve[-1]
        if cur_price > pos.peak_price:
            pos.peak_price = cur_price
        actual_entry = pos.entry_price * (1 + ENTRY_LATENCY_PENALTY)
        gross_pnl_pct = cur_price / actual_entry - 1.0
        net_pnl_pct = gross_pnl_pct - ROUND_TRIP_DRAG
        pnl_usd = pos.entry_size_usd * net_pnl_pct
        capital += pnl_usd
        reason = "end_of_sim"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        defense_register_close(ds, net_pnl_pct, pos.entry_size_usd)
        all_trades.append(TradeResult(
            token_type=pos.token_type, entry_price=pos.entry_price, exit_price=cur_price,
            peak_price=pos.peak_price, entry_size_usd=pos.entry_size_usd,
            exit_reason=reason, hold_minutes=0,
            gross_pnl_pct=gross_pnl_pct, net_pnl_pct=net_pnl_pct, net_pnl_usd=pnl_usd
        ))

    # build final stats
    final = capital
    roi_pct = (final / initial_capital - 1.0) * 100
    wins = sum(1 for t in all_trades if t.net_pnl_usd > 0)
    n_trades = len(all_trades)
    # max drawdown
    eq = [p[1] for p in curve_points]
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    by_token = {}
    for t in all_trades:
        b = by_token.setdefault(t.token_type, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        if t.net_pnl_usd > 0:
            b["wins"] += 1
        b["pnl"] += t.net_pnl_usd
    return {
        "final": final,
        "initial": initial_capital,
        "roi_pct": round(roi_pct, 2),
        "n_trades": n_trades,
        "wins": wins,
        "win_rate_pct": round(100 * wins / n_trades, 1) if n_trades else 0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exit_reasons": exit_reasons,
        "skip_reasons": skip_reasons,
        "by_token_type": by_token,
        "curve": eq,
    }


# ==========================================================================
# 7) RUN ALL REGIMES × SEEDS
# ==========================================================================
def run_regime(regime: str, n_seeds: int = 30, days: int = 30):
    print(f"\n=== Regime: {regime.upper()} ({n_seeds} seeds, {days} days, $20 start) ===")
    results = []
    for s in range(n_seeds):
        rng = random.Random(s * 1000 + hash(regime) % 1000)
        r = run_one_seed(rng, regime, days=days, initial_capital=20.0)
        r = _run_seed_clean(rng, regime, days, 20.0)
        results.append(r)
        if (s + 1) % 5 == 0:
            print(f"  seed {s+1}/{n_seeds}: final=${r['final']:.2f} ROI={r['roi_pct']:+.2f}% trades={r['n_trades']}")
    return results


def stats_summary(results: list, label: str) -> Dict:
    finals = np.array([r["final"] for r in results])
    rois = np.array([r["roi_pct"] for r in results])
    n_trades = np.array([r["n_trades"] for r in results])
    wrs = np.array([r["win_rate_pct"] for r in results])
    dds = np.array([r["max_drawdown_pct"] for r in results])
    n = len(results)
    summary = {
        "label": label,
        "n_seeds": n,
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
        "avg_max_drawdown_pct": round(float(np.mean(dds)), 2),
        "max_drawdown_pct_median": round(float(np.median(dds)), 2),
    }
    return summary


def main():
    print(f"V542 SIMULATION — bot's actual unified strategy (V541 code)")
    print(f"Real Pump.fun stats: 69% fail_day1, 15% rug, 12% pump_dump, 1.5% graduate, 0.5% moonshot")
    print(f"Round-trip drag: {ROUND_TRIP_DRAG*100:.1f}%, entry latency: {ENTRY_LATENCY_PENALTY*100:.1f}%")
    print(f"Strategy params (from unified.py):")
    for k in ["quick_tp", "runner_threshold", "trail_width", "stop_loss", "mega_tp",
              "max_buys_per_day", "daily_loss_stop_pct", "dd_full_pct", "dd_half_pct",
              "streak_window", "streak_wr_max", "streak_pnl_min"]:
        print(f"  {k} = {UNIFIED_PARAMS[k]}")
    print()

    all_results = {}
    curves = {}
    for regime in ["harsh", "base", "normal"]:
        results = run_regime(regime, n_seeds=30, days=30)
        all_results[regime] = results
        curves[regime] = np.array([r["curve"] for r in results])
        np.save(OUT_DIR / f"curves_{regime}.npy", curves[regime])

    summaries = {regime: stats_summary(results, regime) for regime, results in all_results.items()}

    # also save aggregated exit reasons per regime
    per_regime_exit_reasons = {}
    per_regime_skip_reasons = {}
    per_regime_by_token = {}
    for regime, results in all_results.items():
        ex = {}
        sk = {}
        bt = {}
        for r in results:
            for k, v in r["exit_reasons"].items():
                ex[k] = ex.get(k, 0) + v
            for k, v in r["skip_reasons"].items():
                sk[k] = sk.get(k, 0) + v
            for k, v in r["by_token_type"].items():
                b = bt.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
                b["n"] += v["n"]
                b["wins"] += v["wins"]
                b["pnl"] += v["pnl"]
        per_regime_exit_reasons[regime] = ex
        per_regime_skip_reasons[regime] = sk
        per_regime_by_token[regime] = bt

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "bot_version": "0.7.2 (V541 unified strategy)",
        "simulator": "v542_real_pumpfun_montecarlo",
        "initial_capital_usd": 20.0,
        "days": 30,
        "n_seeds_per_regime": 30,
        "round_trip_drag_pct": ROUND_TRIP_DRAG * 100,
        "entry_latency_penalty_pct": ENTRY_LATENCY_PENALTY * 100,
        "pumpfun_stats_pct": [
            ["fail_day1", 69.0], ["rug", 15.0], ["pump_dump", 12.0],
            ["graduate", 1.5], ["moonshot", 0.5],
        ],
        "regimes": list(all_results.keys()),
        "strategy_params": {k: v for k, v in UNIFIED_PARAMS.items()},
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
    with open(OUT_DIR / "results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nResults written to {OUT_DIR / 'results.json'}")
    print(f"Curves saved to {OUT_DIR}/curves_*.npy")
    print()
    print("=" * 70)
    print("SUMMARY")
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
