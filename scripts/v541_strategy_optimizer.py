"""
V541 STRATEGY OPTIMIZER - Unified Memecoin Sniping Strategy
============================================================
Finds the optimal (exit ladder x entry filter x sizing) on the
Pump.fun Monte-Carlo model (real platform stats, real curve math,
3% round-trip costs), then stress-tests the winner on harsh regimes.

Stages:
  1. Exit-ladder grid (filter + sizing fixed)   -> top candidates
  2. Filter strictness on top ladders
  3. Sizing on top (ladder, filter) pairs
  4. Robustness: winner on base/harsh/normal regimes x 200 seeds

Ladder structure (unified, parameterized):
  - Stage A @ first peak>=qtp: sell `frac` at max(1+qtp, p_t1)
    (frac=1.0 -> full exit unless peak_t1 >= T_q -> pure runner)
  - Runner (t >= t1): sudden_dump > SL(locked at 1+lock) > mega_tp > trail
    trail width ratchets tighter at peak>=+50% (x0.75) and >=+100% (x0.6)
  - Early zone (t < t1): SL + sudden_dump only
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import time

import numpy as np

OUT = "/home/z/my-project/download/bot_20usd_simulation"
os.makedirs(OUT, exist_ok=True)

# ---------------- model constants ----------------
INITIAL = 20.0
RT_DRAG = (0.995 * 0.99) * (0.99 / 1.005)   # 1% fee + 0.5% slip per side
MIN_TRADE_USD = 0.42
T_MAX = 34

REGIMES = {
    "base":   [69.0, 15.0, 12.0, 1.5, 0.5],
    "harsh":  [75.5, 18.0, 5.0, 0.9, 0.3],
    "normal": [60.0, 12.0, 24.0, 2.7, 0.9],
}
TYPES = ["fail_day1", "rug", "pump_dump", "graduate", "moonshot"]

# Filter capture: P(token of type appears in buyable stream | scanner saw it)
FILTERS = {
    "F1_current":  [0.55, 0.70, 0.85, 0.90, 0.75],
    "F2_strict":   [0.40, 0.45, 0.72, 0.85, 0.60],
    "F3_vstrict":  [0.28, 0.30, 0.58, 0.80, 0.50],
}

# ---------------- path generators (same as v540) ----------------
def path_fail_day1(rng):
    peak = 1.0 + rng.uniform(0.0, 0.25)
    t_peak = rng.uniform(1.0, 10.0)
    floor = rng.uniform(0.05, 0.15)
    pts = [(0.0, 1.0)]
    n = 4
    for k in range(1, n + 1):
        f = k / n
        p = min(1.0 + (peak - 1.0) * f * rng.uniform(0.9, 1.1), peak)
        pts.append((t_peak * f, p))
    m = 10
    for k in range(1, m + 1):
        f = k / m
        p = max((peak + (floor - peak) * (f ** 0.55)) * rng.uniform(0.93, 1.03), 0.01)
        pts.append((t_peak + (90 - t_peak) * f, p))
    return pts


def path_rug(rng):
    if rng.random() < 0.6:
        peak = rng.uniform(1.2, 1.9)
        t_peak = rng.uniform(0.5, 5.0)
        pts = [(0.0, 1.0)]
        n = 3
        for k in range(1, n + 1):
            pts.append((t_peak * k / n, min(1.0 + (peak - 1.0) * k / n * rng.uniform(0.95, 1.1), peak)))
        pts.append((t_peak + 0.3, rng.uniform(0.0, 0.08)))
        pts.append((t_peak + 2.0, 0.01))
        return pts
    return [(0.0, 1.0), (0.4, rng.uniform(0.85, 1.0)), (2.0, rng.uniform(0.0, 0.12)), (5.0, 0.01)]


def path_pump_dump(rng):
    peak = rng.uniform(2.0, 5.0)
    t_peak = rng.uniform(5.0, 60.0)
    explosive = rng.random() < 0.25
    pts = [(0.0, 1.0)]
    if explosive:
        pts.append((t_peak * 0.2, rng.uniform(1.5, 2.2)))
        pts.append((t_peak * 0.6, peak * rng.uniform(0.75, 0.95)))
        pts.append((t_peak, peak))
    else:
        n = 8
        for k in range(1, n + 1):
            f = k / n
            p = 1.0 + (peak - 1.0) * (f ** 1.3) * rng.uniform(0.92, 1.08)
            pts.append((t_peak * f, min(p, peak)))
    dump_to = rng.uniform(0.20, 0.40)
    m = 8
    for k in range(1, m + 1):
        f = k / m
        p = max((peak + (dump_to - peak) * (f ** 0.5)) * rng.uniform(0.93, 1.03), 0.01)
        pts.append((t_peak + 30 * f, p))
    return pts


def path_graduate(rng):
    entry_sol = rng.uniform(25.0, 50.0) / 100.0 * 85.0
    mult = ((30.0 + 85.0) / (30.0 + entry_sol)) ** 2
    post_grad = mult * rng.uniform(0.60, 0.90)
    t_grad = rng.uniform(30.0, 240.0)
    explosive = rng.random() < 0.15
    pts = [(0.0, 1.0)]
    if explosive:
        pts.append((t_grad * 0.3, rng.uniform(1.5, 2.0)))
    n = 10
    for k in range(1, n + 1):
        f = k / n
        p = 1.0 + (mult - 1.0) * (f ** 1.2) * rng.uniform(0.93, 1.07)
        pts.append((t_grad * f, min(p, mult)))
    m = 6
    for k in range(1, m + 1):
        f = k / m
        p = max((mult + (post_grad - mult) * (f ** 0.7)) * rng.uniform(0.95, 1.02), 0.01)
        pts.append((t_grad + 60 * f, p))
    return pts


def path_moonshot(rng):
    peak = rng.uniform(10.0, 100.0)
    t_peak = rng.uniform(10.0, 300.0)
    pts = [(0.0, 1.0)]
    pts.append((t_peak * 0.15, rng.uniform(1.4, 3.0)))
    pts.append((t_peak * 0.4, peak * rng.uniform(0.4, 0.7)))
    pts.append((t_peak * 0.75, peak * rng.uniform(0.8, 0.95)))
    pts.append((t_peak, peak))
    m = 6
    for k in range(1, m + 1):
        f = k / m
        pts.append((t_peak + 60 * f, max(peak * (1.0 - 0.8 * f ** 0.5), 0.02)))
    return pts


PATH_MAKERS = [path_fail_day1, path_rug, path_pump_dump, path_graduate, path_moonshot]

# ---------------- pre-generate token pools ----------------
def gen_pool(n_seeds: int, regime: str, seed0: int = 10000):
    """Per seed: 30 days x K~U(2,6) candidates.
    Returns arrays: types (N,), prices (N, T_MAX), day (N,), seed (N,),
                    pass flags (3, N) for F1/F2/F3, buy order index within seed."""
    probs = np.array(REGIMES[regime]) / 100.0
    probs = np.append(probs, max(0.0, 1.0 - probs.sum()))  # remainder -> fail_day1 handled below
    all_types, all_prices, all_day, all_seed = [], [], [], []
    pass_flags = [[], [], []]
    for s in range(n_seeds):
        rng = np.random.default_rng(seed0 + s)
        for day in range(30):
            k = int(rng.integers(2, 7))
            for _ in range(k):
                u = rng.random() * 100.0
                cum = 0.0
                ti = 0
                for i, p in enumerate(probs[:5]):
                    cum += p * 100.0
                    if u < cum:
                        ti = i
                        break
                pts = PATH_MAKERS[ti](rng)
                prices = [p for _, p in pts][:T_MAX]
                prices += [prices[-1]] * (T_MAX - len(prices))
                all_types.append(ti)
                all_prices.append(prices)
                all_day.append(day)
                all_seed.append(s)
                for fi in range(3):
                    pass_flags[fi].append(rng.random() < FILTERS[list(FILTERS)[fi]][ti])
    return (np.array(all_types), np.array(all_prices, dtype=float),
            np.array(all_day), np.array(all_seed),
            np.array(pass_flags, dtype=bool))


# ---------------- vectorized unified ladder ----------------
def eval_ladder(P: np.ndarray, cfg: dict) -> tuple:
    """P: (N, T) prices (entry=1.0). Returns net_mult (N,), reason ids (N,)."""
    N, T = P.shape
    peak = np.maximum.accumulate(P, axis=1)
    peak_gain = peak - 1.0
    drop = (peak - P) / np.maximum(peak, 1e-9)
    t_idx = np.broadcast_to(np.arange(T), (N, T))

    qtp, frac, T_q = cfg["qtp"], cfg["frac"], cfg["T_q"]
    sd_g, sd_d = cfg["sd_gain"], cfg["sd_drop"]
    sl = 1.0 - cfg["sl"]
    lock = 1.0 + cfg["lock"] if cfg["lock"] is not None else None
    mega = cfg["mega"]
    tw = cfg["trail_w"]
    ratchet = cfg["ratchet"]

    reach = peak_gain >= qtp
    has_t1 = reach.any(axis=1)
    t1 = np.where(has_t1, reach.argmax(axis=1), T - 1)

    # --- stage A: partial or full quick-TP ---
    p_t1 = P[np.arange(N), t1]
    pg_t1 = peak_gain[np.arange(N), t1]
    full_qtp = has_t1 & (frac >= 0.999) & (pg_t1 < T_q)
    partial = has_t1 & (frac < 0.999)
    stageA_price = np.maximum(1.0 + qtp, p_t1)

    # --- runner zone (t >= t1) triggers ---
    mask_run = t_idx >= t1[:, None]
    # sudden dump
    sd_trig = (peak_gain >= sd_g) & (drop >= sd_d) & mask_run
    # SL with breakeven lock after t1
    if lock is not None:
        eff_sl = np.where(t_idx > t1[:, None], np.maximum(sl, lock), sl)
    else:
        eff_sl = np.full((N, T), sl)
    sl_trig = (P <= eff_sl) & mask_run
    mega_trig = (P >= mega) & mask_run
    if ratchet:
        w_eff = tw * np.where(peak_gain >= 1.0, 0.60, np.where(peak_gain >= 0.5, 0.75, 1.0))
    else:
        w_eff = np.full((N, T), tw)
    trail_trig = (peak_gain >= qtp) & (drop >= w_eff) & mask_run

    # early zone (t < t1): SL + sudden dump for the not-yet-partial part
    mask_early = t_idx < t1[:, None]
    sd_early = (peak_gain >= sd_g) & (drop >= sd_d) & mask_early
    sl_early = (P <= sl) & mask_early

    # first trigger in each zone
    def first_true(a):
        any_ = a.any(axis=1)
        return np.where(any_, a.argmax(axis=1), T - 1), any_

    t_sd, has_sd = first_true(sd_trig)
    t_sl, has_sl = first_true(sl_trig)
    t_mg, has_mg = first_true(mega_trig)
    t_tr, has_tr = first_true(trail_trig)
    t_sde, has_sde = first_true(sd_early)
    t_sle, has_sle = first_true(sl_early)

    # early-zone exit (before ever reaching t1): sudden_dump > SL
    t_early = np.where(has_sde, t_sde, t_sle)
    early_exit = has_t1 & np.isin(np.minimum(t_sde if has_sde.any() else t_sle, T + 1), [])  # placeholder
    # simpler: early exit valid if early trigger time < t1
    early_time = np.where(has_sde | has_sle, t_early, T + 1)
    early_exit = has_t1 & (early_time < t1)

    # runner exit: priority sd > sl > mega > trail, earliest time
    cand_t = np.stack([t_sd, t_sl, t_mg, t_tr], axis=1)
    cand_has = np.stack([has_sd, has_sl, has_mg, has_tr], axis=1)
    # add +0.25/+0.5/+0.75 priority offsets to break ties by priority
    prio = np.array([0.0, 0.25, 0.5, 0.75])
    scores = np.where(cand_has, cand_t + prio[None, :], np.inf)
    best_reason = scores.argmin(axis=1)
    best_t = np.floor(np.min(scores, axis=1)).astype(int)
    run_exit = scores.min(axis=1) < np.inf

    # tokens that never reach t1: whole position governed by early zone then
    # runner triggers never apply (mask_run empty for t<t1 only) -> use early
    never = ~has_t1
    t_sdN, has_sdN = first_true((peak_gain >= sd_g) & (drop >= sd_d))
    t_slN, has_slN = first_true(P <= sl)
    t_never = np.where(has_sdN, t_sdN, t_slN)
    never_exit = has_sdN | has_slN

    reason_names = ["sudden_dump", "stop_loss", "mega_tp", "trail"]
    net = np.zeros(N)
    reason_out = np.empty(N, dtype=object)
    p_run = P[np.arange(N), np.clip(best_t, 0, T - 1)]
    p_early = P[np.arange(N), np.clip(np.minimum(early_time, T - 1), 0, T - 1)]
    p_never = P[np.arange(N), np.clip(t_never, 0, T - 1)]

    for i in range(N):
        r = reason_names[best_reason[i]] if run_exit[i] else None
        if never[i]:
            if never_exit[i]:
                net[i] = p_never[i] * RT_DRAG
                reason_out[i] = "sudden_dump" if has_sdN[i] else "stop_loss"
            else:
                net[i] = P[i, -1] * RT_DRAG
                reason_out[i] = "time_exit"
        elif early_exit[i]:
            net[i] = p_early[i] * RT_DRAG
            reason_out[i] = "sudden_dump" if has_sde[i] else "stop_loss"
        elif full_qtp[i]:
            net[i] = stageA_price[i] * RT_DRAG
            reason_out[i] = "quick_tp"
        elif partial[i]:
            rem = (1.0 - frac) * p_run[i] * RT_DRAG if run_exit[i] else (1.0 - frac) * P[i, -1] * RT_DRAG
            net[i] = frac * stageA_price[i] * RT_DRAG + rem
            reason_out[i] = r or "time_exit"
        else:  # full runner (frac=1, pg_t1 >= T_q)
            net[i] = p_run[i] * RT_DRAG if run_exit[i] else P[i, -1] * RT_DRAG
            reason_out[i] = r or "time_exit"
    return net, reason_out


# ---------------- 30-day portfolio walk ----------------
def walk(seed_data, cfg, net_mult, n_seeds):
    """seed_data: (types, prices, day, seed, pass_flags). Returns finals (n_seeds,)."""
    types, prices, day, seed, flags = seed_data
    trade_pct = cfg["trade_pct"]
    max_buys = cfg["max_buys"]
    dloss = cfg["daily_loss"]
    finals = np.zeros(n_seeds)
    for s in range(n_seeds):
        idx = np.where((seed == s) & flags[cfg["filter_id"]])[0]
        if len(idx) == 0:
            finals[s] = INITIAL
            continue
        days = day[idx]
        capital = INITIAL
        for d in range(30):
            sel = idx[days == d]
            day_start = capital
            buys = 0
            for tid in sel:
                if buys >= max_buys or capital < 1.0:
                    break
                size = capital * trade_pct
                if size < MIN_TRADE_USD:
                    break
                if day_start - capital >= day_start * dloss:
                    break
                capital += size * (net_mult[tid] - 1.0)
                buys += 1
                if capital <= 0.01:
                    capital = 0.01
                    break
            if capital <= 0.01:
                break
        finals[s] = capital
    return finals


def score(finals):
    return {
        "median": float(np.median(finals)),
        "mean": float(np.mean(finals)),
        "p5": float(np.percentile(finals, 5)),
        "p25": float(np.percentile(finals, 25)),
        "p95": float(np.percentile(finals, 95)),
        "p_profit": float((finals > INITIAL).mean()),
        "p_lose40": float((finals < INITIAL * 0.6).mean()),
        "p_ruin": float((finals < INITIAL * 0.2).mean()),
    }


# ---------------- main ----------------
def main():
    t0 = time.time()
    N_SEEDS = 60
    print("Generating token pools (60 seeds x 30 days)...")
    pools = {reg: gen_pool(N_SEEDS, reg) for reg in REGIMES}
    for reg, sd in pools.items():
        print(f"  {reg}: {len(sd[0])} candidate tokens")
    print()

    # current sniper baseline (config #0)
    BASE_CFG = dict(qtp=0.15, frac=1.0, T_q=0.50, trail_w=0.30, ratchet=False,
                    lock=None, sd_gain=0.10, sd_drop=0.20, sl=0.08, mega=10.0,
                    filter_id=0, trade_pct=0.10, max_buys=2, daily_loss=0.20)

    # ---------------- STAGE 1: exit ladder ----------------
    grid_qtp = [0.10, 0.15, 0.20]
    grid_frac = [0.35, 0.50, 1.00]
    grid_Tq = [0.35, 0.60]
    grid_tw = [0.15, 0.20, 0.30]
    grid_ratchet = [False, True]
    grid_lock = [None, 0.03]
    grid_sd = [(0.10, 0.20), (0.08, 0.15)]
    grid_sl = [0.05, 0.08]

    types, prices, day, seed, flags = pools["base"]
    results = []
    combos = list(itertools.product(grid_qtp, grid_frac, grid_Tq, grid_tw,
                                    grid_ratchet, grid_lock, grid_sd, grid_sl))
    print(f"STAGE 1: {len(combos) + 1} ladder configs (filter=F1, sizing=10%/2/20%)")

    # baseline
    net, _ = eval_ladder(prices, BASE_CFG)
    finals = walk(pools["base"], BASE_CFG, net, N_SEEDS)
    sc = score(finals)
    results.append((dict(BASE_CFG), sc))
    print(f"  BASELINE sniper: median=${sc['median']:.2f} P(profit)={sc['p_profit']:.0%} "
          f"P(lose40)={sc['p_lose40']:.0%}")

    for combo in combos:
        cfg = dict(qtp=combo[0], frac=combo[1], T_q=combo[2], trail_w=combo[3],
                   ratchet=combo[4], lock=combo[5], sd_gain=combo[6][0],
                   sd_drop=combo[6][1], sl=combo[7], mega=10.0,
                   filter_id=0, trade_pct=0.10, max_buys=2, daily_loss=0.20)
        net, _ = eval_ladder(prices, cfg)
        finals = walk(pools["base"], cfg, net, N_SEEDS)
        results.append((cfg, score(finals)))

    results.sort(key=lambda x: -x[1]["median"])
    print("\nTOP 12 ladders (by median final, $20 start):")
    print(f"  {'qtp':>5} {'frac':>5} {'Tq':>5} {'tw':>5} {'ratch':>6} {'lock':>5} {'sd':>10} {'sl':>5} | "
          f"{'median':>7} {'p95':>7} {'P(prof)':>8} {'P(l40)':>7}")
    for cfg, sc in results[:12]:
        print(f"  {cfg['qtp']:>5} {cfg['frac']:>5} {cfg['T_q']:>5} {cfg['trail_w']:>5} "
              f"{str(cfg['ratchet'])[:1]:>6} {str(cfg['lock']):>5} "
              f"({cfg['sd_gain']},{cfg['sd_drop']}) {cfg['sl']:>5} | "
              f"${sc['median']:>6.2f} ${sc['p95']:>6.2f} {sc['p_profit']:>7.0%} {sc['p_lose40']:>6.0%}")
    print(f"  (baseline median: ${results[[r[0] for r in results].index(dict(BASE_CFG))][1]['median'] if dict(BASE_CFG) in [r[0] for r in results] else 0:.2f})")

    # ---------------- STAGE 2: filter ----------------
    print("\nSTAGE 2: filter strictness on top-8 ladders")
    top_ladders = [cfg for cfg, _ in results[:8]]
    stage2 = []
    for cfg in top_ladders:
        for fi, fname in enumerate(FILTERS):
            c = dict(cfg, filter_id=fi)
            net, _ = eval_ladder(prices, c)
            finals = walk(pools["base"], c, net, N_SEEDS)
            stage2.append((c, score(finals), fname))
    stage2.sort(key=lambda x: -x[1]["median"])
    for c, sc, fname in stage2[:8]:
        print(f"  {fname:<10} qtp={c['qtp']} frac={c['frac']} tw={c['trail_w']} | "
              f"median=${sc['median']:.2f} P(profit)={sc['p_profit']:.0%} P(lose40)={sc['p_lose40']:.0%}")

    # ---------------- STAGE 3: sizing ----------------
    print("\nSTAGE 3: sizing on top-5 (ladder x filter)")
    top_lf = [(c, sc) for c, sc, _ in stage2[:5]]
    stage3 = []
    for cfg, _sc in top_lf:
        for tp_, mb, dl in itertools.product([0.06, 0.08, 0.10], [2, 3, 4], [0.15, 0.20]):
            c = dict(cfg, trade_pct=tp_, max_buys=mb, daily_loss=dl)
            net, _ = eval_ladder(prices, c)
            finals = walk(pools["base"], c, net, N_SEEDS)
            stage3.append((c, score(finals)))
    stage3.sort(key=lambda x: -x[1]["median"])
    for c, sc in stage3[:8]:
        print(f"  pct={c['trade_pct']} buys={c['max_buys']} dloss={c['daily_loss']} "
              f"filt={list(FILTERS)[c['filter_id']][:10]} | median=${sc['median']:.2f} "
              f"P(profit)={sc['p_profit']:.0%} P(lose40)={sc['p_lose40']:.0%} p5=${sc['p5']:.2f}")

    # ---------------- STAGE 4: robustness ----------------
    print("\nSTAGE 4: robustness (base / harsh / normal regimes)")
    winner_cfg, winner_sc = stage3[0]
    robust = {}
    for reg in REGIMES:
        types_r, prices_r, day_r, seed_r, flags_r = pools[reg]
        net, _ = eval_ladder(prices_r, winner_cfg)
        finals = walk(pools[reg], winner_cfg, net, N_SEEDS)
        robust[reg] = score(finals)
        print(f"  {reg:<7}: median=${robust[reg]['median']:.2f} ({(robust[reg]['median']/INITIAL-1)*100:+.0f}%) "
              f"P(profit)={robust[reg]['p_profit']:.0%} P(lose40)={robust[reg]['p_lose40']:.0%} "
              f"p5=${robust[reg]['p5']:.2f}")

    # baseline robustness for comparison
    base_robust = {}
    for reg in REGIMES:
        types_r, prices_r, day_r, seed_r, flags_r = pools[reg]
        net, _ = eval_ladder(prices_r, BASE_CFG)
        finals = walk(pools[reg], BASE_CFG, net, N_SEEDS)
        base_robust[reg] = score(finals)
        print(f"  (baseline {reg:<7}: median=${base_robust[reg]['median']:.2f} "
              f"P(profit)={base_robust[reg]['p_profit']:.0%})")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "initial": INITIAL,
        "baseline_sniper": {"cfg": {k: v for k, v in BASE_CFG.items()},
                            "base_regime": results[0][1] if False else None},
        "winner": {"cfg": {k: (v if not isinstance(v, np.generic) else v.item())
                           for k, v in winner_cfg.items()},
                   "scores": winner_sc, "robustness": robust},
        "baseline_robustness": base_robust,
        "top_ladders": [
            {"cfg": {k: (v if not isinstance(v, np.generic) else v.item()) for k, v in c.items()}, "score": s}
            for c, s in results[:12]
        ],
        "top_stage3": [
            {"cfg": {k: (v if not isinstance(v, np.generic) else v.item()) for k, v in c.items()}, "score": s}
            for c, s in stage3[:8]
        ],
    }
    # fix baseline score
    base_idx = next(i for i, (c, _) in enumerate(results) if c == BASE_CFG)
    out["baseline_sniper"]["base_regime"] = results[base_idx][1]

    with open(os.path.join(OUT, "strategy_optimizer.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved {OUT}/strategy_optimizer.json")
    print(f"WINNER CONFIG: {json.dumps({k: v for k, v in winner_cfg.items()}, default=str)}")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
