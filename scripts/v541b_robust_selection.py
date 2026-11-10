"""
V541B - ROBUST STRATEGY SELECTION
=================================
Avoids model overfitting: evaluates top ladder configs across 8 PERTURBED
model variants (blip/bait/explosive/pump distributions) x 3 regimes.
Adds loss-streak cooldown (organic regime adaptation) to the sizing search.
Blended objective: median_base + 0.5*median_harsh - penalty(P_lose40_harsh>25%).
"""
from __future__ import annotations

import itertools
import json
import os
import time

import numpy as np

OUT = "/home/z/my-project/download/bot_20usd_simulation"
INITIAL = 20.0
RT_DRAG = (0.995 * 0.99) * (0.99 / 1.005)
MIN_TRADE_USD = 0.42
T_MAX = 34
N_SEEDS = 60

REGIMES = {
    "base":   [69.0, 15.0, 12.0, 1.5, 0.5],
    "harsh":  [75.5, 18.0, 5.0, 0.9, 0.3],
    "normal": [60.0, 12.0, 24.0, 2.7, 0.9],
}
TYPES = 5
FILTERS = {
    "F0_raw":    [1.00, 1.00, 1.00, 1.00, 1.00],   # no filter effect (brutal)
    "F1_current": [0.55, 0.70, 0.85, 0.90, 0.75],
    "F2_strict":  [0.40, 0.45, 0.72, 0.85, 0.60],
}

# model perturbations: (blip_max, bait_lo, bait_hi, explosive_frac, pump_lo, pump_hi)
PERTURBS = {
    "neutral":      (0.25, 1.20, 1.90, 0.25, 2.0, 5.0),
    "blip_low":     (0.15, 1.20, 1.90, 0.25, 2.0, 5.0),
    "blip_high":    (0.40, 1.20, 1.90, 0.25, 2.0, 5.0),
    "bait_wide":    (0.25, 1.20, 2.50, 0.25, 2.0, 5.0),
    "explosive_hi": (0.25, 1.20, 1.90, 0.40, 2.0, 5.0),
    "pump_hi":      (0.25, 1.20, 1.90, 0.25, 2.0, 8.0),
    "blip_low_x_harsh":  (0.15, 1.20, 1.90, 0.25, 2.0, 5.0),
    "bait_wide_x_harsh": (0.25, 1.20, 2.50, 0.25, 2.0, 5.0),
}


def make_paths(pert):
    blip_max, b_lo, b_hi, exp_f, p_lo, p_hi = pert

    def path_fail_day1(rng):
        peak = 1.0 + rng.uniform(0.0, blip_max)
        t_peak = rng.uniform(1.0, 10.0)
        floor = rng.uniform(0.05, 0.15)
        pts = [(0.0, 1.0)]
        for k in range(1, 5):
            f = k / 4
            pts.append((t_peak * f, min(1.0 + (peak - 1.0) * f * rng.uniform(0.9, 1.1), peak)))
        for k in range(1, 11):
            f = k / 10
            p = max((peak + (floor - peak) * (f ** 0.55)) * rng.uniform(0.93, 1.03), 0.01)
            pts.append((t_peak + (90 - t_peak) * f, p))
        return pts

    def path_rug(rng):
        if rng.random() < 0.6:
            peak = rng.uniform(b_lo, b_hi)
            t_peak = rng.uniform(0.5, 5.0)
            pts = [(0.0, 1.0)]
            for k in range(1, 4):
                pts.append((t_peak * k / 3, min(1.0 + (peak - 1.0) * k / 3 * rng.uniform(0.95, 1.1), peak)))
            pts.append((t_peak + 0.3, rng.uniform(0.0, 0.08)))
            pts.append((t_peak + 2.0, 0.01))
            return pts
        return [(0.0, 1.0), (0.4, rng.uniform(0.85, 1.0)), (2.0, rng.uniform(0.0, 0.12)), (5.0, 0.01)]

    def path_pump_dump(rng):
        peak = rng.uniform(p_lo, p_hi)
        t_peak = rng.uniform(5.0, 60.0)
        explosive = rng.random() < exp_f
        pts = [(0.0, 1.0)]
        if explosive:
            pts.append((t_peak * 0.2, rng.uniform(1.5, 2.2)))
            pts.append((t_peak * 0.6, peak * rng.uniform(0.75, 0.95)))
            pts.append((t_peak, peak))
        else:
            for k in range(1, 9):
                f = k / 8
                pts.append((t_peak * f, min(1.0 + (peak - 1.0) * (f ** 1.3) * rng.uniform(0.92, 1.08), peak)))
        dump_to = rng.uniform(0.20, 0.40)
        for k in range(1, 9):
            f = k / 8
            p = max((peak + (dump_to - peak) * (f ** 0.5)) * rng.uniform(0.93, 1.03), 0.01)
            pts.append((t_peak + 30 * f, p))
        return pts

    def path_graduate(rng):
        entry_sol = rng.uniform(25.0, 50.0) / 100.0 * 85.0
        mult = ((30.0 + 85.0) / (30.0 + entry_sol)) ** 2
        post_grad = mult * rng.uniform(0.60, 0.90)
        t_grad = rng.uniform(30.0, 240.0)
        pts = [(0.0, 1.0)]
        if rng.random() < 0.15:
            pts.append((t_grad * 0.3, rng.uniform(1.5, 2.0)))
        for k in range(1, 11):
            f = k / 10
            pts.append((t_grad * f, min(1.0 + (mult - 1.0) * (f ** 1.2) * rng.uniform(0.93, 1.07), mult)))
        for k in range(1, 7):
            f = k / 6
            p = max((mult + (post_grad - mult) * (f ** 0.7)) * rng.uniform(0.95, 1.02), 0.01)
            pts.append((t_grad + 60 * f, p))
        return pts

    def path_moonshot(rng):
        peak = rng.uniform(10.0, 100.0)
        t_peak = rng.uniform(10.0, 300.0)
        pts = [(0.0, 1.0), (t_peak * 0.15, rng.uniform(1.4, 3.0)),
               (t_peak * 0.4, peak * rng.uniform(0.4, 0.7)),
               (t_peak * 0.75, peak * rng.uniform(0.8, 0.95)), (t_peak, peak)]
        for k in range(1, 7):
            f = k / 6
            pts.append((t_peak + 60 * f, max(peak * (1.0 - 0.8 * f ** 0.5), 0.02)))
        return pts

    return [path_fail_day1, path_rug, path_pump_dump, path_graduate, path_moonshot]


def gen_pool(regime: str, pert_name: str, seed0: int = 10000):
    probs = np.array(REGIMES[regime]) / 100.0
    makers = make_paths(PERTURBS[pert_name])
    all_types, all_prices, all_day, all_seed = [], [], [], []
    pass_flags = [[], [], []]
    rng_master = np.random.default_rng(seed0 + hash((regime, pert_name)) % 100000)
    for s in range(N_SEEDS):
        rng = np.random.default_rng(seed0 + s * 131 + hash(pert_name) % 977)
        for day in range(30):
            k = int(rng.integers(2, 7))
            for _ in range(k):
                u = rng.random() * 98.0
                cum, ti = 0.0, 0
                for i, p in enumerate(probs):
                    cum += p * 100.0
                    if u < cum:
                        ti = i
                        break
                pts = makers[ti](rng)
                prices = [p for _, p in pts][:T_MAX]
                prices += [prices[-1]] * (T_MAX - len(prices))
                all_types.append(ti)
                all_prices.append(prices)
                all_day.append(day)
                all_seed.append(s)
                for fi in range(3):
                    pass_flags[fi].append(rng.random() < FILTERS[list(FILTERS)[fi]][ti])
    return (np.array(all_types), np.array(all_prices, dtype=float),
            np.array(all_day), np.array(all_seed), np.array(pass_flags, dtype=bool))


def eval_ladder(P, cfg):
    N, T = P.shape
    peak = np.maximum.accumulate(P, axis=1)
    peak_gain = peak - 1.0
    drop = (peak - P) / np.maximum(peak, 1e-9)
    t_idx = np.broadcast_to(np.arange(T), (N, T))
    qtp, frac, T_q = cfg["qtp"], cfg["frac"], cfg["T_q"]
    sd_g, sd_d = cfg["sd_gain"], cfg["sd_drop"]
    sl = 1.0 - cfg["sl"]
    lock = 1.0 + cfg["lock"] if cfg["lock"] is not None else None
    mega, tw, ratchet = cfg["mega"], cfg["trail_w"], cfg["ratchet"]

    reach = peak_gain >= qtp
    has_t1 = reach.any(axis=1)
    t1 = np.where(has_t1, reach.argmax(axis=1), T - 1)
    p_t1 = P[np.arange(N), t1]
    pg_t1 = peak_gain[np.arange(N), t1]
    full_qtp = has_t1 & (frac >= 0.999) & (pg_t1 < T_q)
    partial = has_t1 & (frac < 0.999)
    stageA_price = np.maximum(1.0 + qtp, p_t1)

    mask_run = t_idx >= t1[:, None]
    sd_trig = (peak_gain >= sd_g) & (drop >= sd_d) & mask_run
    eff_sl = np.full((N, T), sl)
    if lock is not None:
        eff_sl = np.where(t_idx > t1[:, None], np.maximum(sl, lock), sl)
    sl_trig = (P <= eff_sl) & mask_run
    mega_trig = (P >= mega) & mask_run
    if ratchet:
        w_eff = tw * np.where(peak_gain >= 1.0, 0.60, np.where(peak_gain >= 0.5, 0.75, 1.0))
    else:
        w_eff = np.full((N, T), tw)
    trail_trig = (peak_gain >= qtp) & (drop >= w_eff) & mask_run
    mask_early = t_idx < t1[:, None]
    sd_early = (peak_gain >= sd_g) & (drop >= sd_d) & mask_early
    sl_early = (P <= sl) & mask_early

    def first_true(a):
        any_ = a.any(axis=1)
        return np.where(any_, a.argmax(axis=1), T - 1), any_

    t_sd, has_sd = first_true(sd_trig)
    t_sl, has_sl = first_true(sl_trig)
    t_mg, has_mg = first_true(mega_trig)
    t_tr, has_tr = first_true(trail_trig)
    t_sde, has_sde = first_true(sd_early)
    t_sle, has_sle = first_true(sl_early)

    early_time = np.where(has_sde, t_sde, np.where(has_sle, t_sle, T + 1))
    early_exit = has_t1 & (early_time < t1)

    cand_t = np.stack([t_sd, t_sl, t_mg, t_tr], axis=1).astype(float)
    cand_has = np.stack([has_sd, has_sl, has_mg, has_tr], axis=1)
    prio = np.array([0.0, 0.25, 0.5, 0.75])
    scores = np.where(cand_has, cand_t + prio[None, :], np.inf)
    best_reason = scores.argmin(axis=1)
    best_t = np.floor(np.where(np.isfinite(scores.min(axis=1)), scores.min(axis=1), T - 1)).astype(int)
    run_exit = np.isfinite(scores.min(axis=1))

    t_sdN, has_sdN = first_true((peak_gain >= sd_g) & (drop >= sd_d))
    t_slN, has_slN = first_true(P <= sl)
    never = ~has_t1

    net = np.zeros(N)
    p_run = P[np.arange(N), np.clip(best_t, 0, T - 1)]
    p_early = P[np.arange(N), np.clip(np.minimum(early_time, T - 1), 0, T - 1)]
    p_never = P[np.arange(N), np.clip(np.where(has_sdN, t_sdN, t_slN), 0, T - 1)]
    reason_names = ["sudden_dump", "stop_loss", "mega_tp", "trail"]

    for i in range(N):
        r = reason_names[best_reason[i]] if run_exit[i] else "time_exit"
        if never[i]:
            if has_sdN[i] or has_slN[i]:
                net[i] = p_never[i] * RT_DRAG
            else:
                net[i] = P[i, -1] * RT_DRAG
        elif early_exit[i]:
            net[i] = p_early[i] * RT_DRAG
        elif full_qtp[i]:
            net[i] = stageA_price[i] * RT_DRAG
        elif partial[i]:
            rem = (1.0 - frac) * (p_run[i] * RT_DRAG if run_exit[i] else P[i, -1] * RT_DRAG)
            net[i] = frac * stageA_price[i] * RT_DRAG + rem
        else:
            net[i] = p_run[i] * RT_DRAG if run_exit[i] else P[i, -1] * RT_DRAG
    return net


def walk(pool, cfg, net_mult):
    types, prices, day, seed, flags = pool
    trade_pct, max_buys, dloss = cfg["trade_pct"], cfg["max_buys"], cfg["daily_loss"]
    cl_after, cl_days = cfg.get("cl_after", 99), cfg.get("cl_days", 0)
    finals = np.zeros(N_SEEDS)
    for s in range(N_SEEDS):
        idx = np.where((seed == s) & flags[cfg["filter_id"]])[0]
        capital = INITIAL
        if len(idx) == 0:
            finals[s] = capital
            continue
        days = day[idx]
        streak = 0
        cooldown_until = -1
        for d in range(30):
            if capital <= 0.01:
                break
            if d < cooldown_until:
                continue
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
                pnl = size * (net_mult[tid] - 1.0)
                capital += pnl
                buys += 1
                streak = streak + 1 if pnl <= 0 else 0
                if cl_after and streak >= cl_after:
                    cooldown_until = d + 1 + int(cl_days)
                    streak = 0
                    break
                if capital <= 0.01:
                    capital = 0.01
                    break
        finals[s] = capital
    return finals


def sc(finals):
    return {"median": float(np.median(finals)),
            "p5": float(np.percentile(finals, 5)),
            "p95": float(np.percentile(finals, 95)),
            "p_profit": float((finals > INITIAL).mean()),
            "p_lose40": float((finals < INITIAL * 0.6).mean())}


BASE_CFG = dict(qtp=0.15, frac=1.0, T_q=0.50, trail_w=0.30, ratchet=False,
                lock=None, sd_gain=0.10, sd_drop=0.20, sl=0.08, mega=10.0,
                filter_id=1, trade_pct=0.10, max_buys=2, daily_loss=0.20,
                cl_after=0, cl_days=0)

CANDIDATE_LADDERS = []
for qtp, T_q, tw, ratchet, sl, sd in itertools.product(
        [0.15, 0.20, 0.25], [0.35, 0.50], [0.15, 0.20, 0.30], [False, True],
        [0.05, 0.08], [(0.10, 0.20), (0.08, 0.15)]):
    CANDIDATE_LADDERS.append(dict(qtp=qtp, frac=1.0, T_q=T_q, trail_w=tw,
                                  ratchet=ratchet, lock=None, sd_gain=sd[0],
                                  sd_drop=sd[1], sl=sl, mega=10.0))


def main():
    t0 = time.time()
    print("Building pools: 8 perturbation-variants x regimes ...")
    pools = {}
    for pert in PERTURBS:
        regime = "harsh" if "harsh" in pert else ("normal" if pert == "normal" else "base")
        pools[pert] = gen_pool(regime, pert)
    pools["base"] = gen_pool("base", "neutral")
    pools["harsh"] = gen_pool("harsh", "neutral")
    pools["normal"] = gen_pool("normal", "neutral")
    print(f"  {len(pools)} pools ready ({time.time()-t0:.1f}s)")

    # ---- evaluate all candidate ladders (fixed sizing F1) on base+harsh ----
    print(f"\nEvaluating {len(CANDIDATE_LADDERS)} ladders on base + harsh (F1, 10%/2buys/20%) ...")
    rows = []
    base_net_cache, harsh_net_cache = {}, {}
    for L in CANDIDATE_LADDERS + [dict(BASE_CFG, **{k: v for k, v in BASE_CFG.items()})]:
        key = json.dumps({k: L[k] for k in ["qtp", "frac", "T_q", "trail_w", "ratchet",
                                            "lock", "sd_gain", "sd_drop", "sl", "mega"]}, default=str)
        if key not in base_net_cache:
            base_net_cache[key] = eval_ladder(pools["base"][1], L)
            harsh_net_cache[key] = eval_ladder(pools["harsh"][1], L)
        cfg_b = dict(L, filter_id=1, trade_pct=0.10, max_buys=2, daily_loss=0.20, cl_after=0, cl_days=0)
        f_b = walk(pools["base"], cfg_b, base_net_cache[key])
        f_h = walk(pools["harsh"], cfg_b, harsh_net_cache[key])
        rows.append((L, sc(f_b), sc(f_h)))

    # baseline row
    base_key_rows = [r for r in rows if r[0] == dict(BASE_CFG, **{k: BASE_CFG[k] for k in ["qtp", "frac", "T_q", "trail_w", "ratchet", "lock", "sd_gain", "sd_drop", "sl", "mega"]})]
    baseline_row = base_key_rows[0] if base_key_rows else rows[-1]
    bl_base, bl_harsh = baseline_row[1]["median"], baseline_row[2]["median"]

    # blended score: base median + 0.5*harsh median (in $, start 20)
    scored = []
    for L, sb, sh in rows:
        blend = sb["median"] + 0.5 * sh["median"]
        scored.append((blend, L, sb, sh))
    scored.sort(key=lambda x: -x[0])
    print(f"\nBaseline sniper: base=${bl_base:.2f} harsh=${bl_harsh:.2f}")
    print("\nTOP 10 by blended score (base + 0.5*harsh):")
    print(f"  {'qtp':>5} {'Tq':>5} {'tw':>5} {'rat':>4} {'sl':>5} {'sd':>10} | {'base':>7} {'harsh':>7} {'blend':>7} {'Pprof_b':>8} {'Pprof_h':>8}")
    for blend, L, sb, sh in scored[:10]:
        print(f"  {L['qtp']:>5} {L['T_q']:>5} {L['trail_w']:>5} {str(L['ratchet'])[:1]:>4} {L['sl']:>5} "
              f"({L['sd_gain']},{L['sd_drop']}) | ${sb['median']:>6.2f} ${sh['median']:>6.2f} ${blend:>6.2f} "
              f"{sb['p_profit']:>7.0%} {sh['p_profit']:>7.0%}")

    # ---- robustness: top-8 across perturbations ----
    print("\nROBUSTNESS of top-8 across 8 model variants (median $):")
    top8 = [L for _, L, _, _ in scored[:8]]
    rob = {}
    for L in top8 + [baseline_row[0]]:
        rob_meds = []
        for pert in PERTURBS:
            key = json.dumps({k: L[k] for k in ["qtp", "frac", "T_q", "trail_w", "ratchet",
                                                "lock", "sd_gain", "sd_drop", "sl", "mega"]}, default=str)
            net = eval_ladder(pools[pert][1], L)
            cfg_b = dict(L, filter_id=1, trade_pct=0.10, max_buys=2, daily_loss=0.20, cl_after=0, cl_days=0)
            f = walk(pools[pert], cfg_b, net)
            rob_meds.append(sc(f)["median"])
        rob[id(L)] = rob_meds
        worst = min(rob_meds)
        print(f"  qtp={L['qtp']} Tq={L['T_q']} tw={L['trail_w']} r={str(L['ratchet'])[:1]} sl={L['sl']}: "
              f"{['%.1f' % m for m in rob_meds]} worst=${worst:.2f}")

    # pick winner: best blended among top-8 with best worst-case
    print("\nSelection: maximize (blend) with worst-case tiebreak ...")
    top8_scored = [(b, L, sb, sh) for b, L, sb, sh in scored[:8]]
    def worst_of(L):
        return min(rob[id(L)])
    top8_scored.sort(key=lambda x: (-x[0], -worst_of(x[1])))
    winner_ladder, winner_sb, winner_sh = top8_scored[0][1], top8_scored[0][2], top8_scored[0][3]
    print(f"WINNER LADDER: {json.dumps({k: winner_ladder[k] for k in ['qtp','frac','T_q','trail_w','ratchet','lock','sd_gain','sd_drop','sl','mega']}, default=str)}")

    # ---- filter choice on winner (incl. F0_raw honesty check) ----
    print("\nFILTER choice (F0_raw / F1 / F2) on winner ladder, base+harsh:")
    best_filter, best_fblend = 1, -1e9
    for fi, fname in enumerate(FILTERS):
        cfg_b = dict(winner_ladder, filter_id=fi, trade_pct=0.10, max_buys=2, daily_loss=0.20, cl_after=0, cl_days=0)
        netb = eval_ladder(pools["base"][1], cfg_b)
        neth = eval_ladder(pools["harsh"][1], cfg_b)
        fb = sc(walk(pools["base"], cfg_b, netb))
        fh = sc(walk(pools["harsh"], cfg_b, neth))
        blend = fb["median"] + 0.5 * fh["median"]
        print(f"  {fname:<10} base=${fb['median']:.2f} harsh=${fh['median']:.2f} blend=${blend:.2f} "
              f"Pprof_h={fh['p_profit']:.0%}")
        if blend > best_fblend:
            best_fblend, best_filter = blend, fi

    # ---- sizing + cooldown search ----
    print(f"\nSIZING + COOLDOWN search (filter={list(FILTERS)[best_filter]}):")
    best = None
    for tp_, mb, dl, cl_a, cl_d in itertools.product(
            [0.06, 0.08, 0.10], [2, 3, 4], [0.12, 0.15, 0.20], [0, 3], [1]):
        cfg = dict(winner_ladder, filter_id=best_filter, trade_pct=tp_, max_buys=mb,
                   daily_loss=dl, cl_after=cl_a, cl_days=cl_d)
        netb = eval_ladder(pools["base"][1], cfg)
        neth = eval_ladder(pools["harsh"][1], cfg)
        netn = eval_ladder(pools["normal"][1], cfg)
        fb = sc(walk(pools["base"], cfg, netb))
        fh = sc(walk(pools["harsh"], cfg, neth))
        fn = sc(walk(pools["normal"], cfg, netn))
        penalty = 40.0 * max(0.0, fh["p_lose40"] - 0.25)
        blend = fb["median"] + 0.5 * fh["median"] + 0.25 * fn["median"] - penalty
        if best is None or blend > best[0]:
            best = (blend, cfg, fb, fh, fn)
    blend, cfgW, fb, fh, fn = best
    print(f"  pct={cfgW['trade_pct']} buys={cfgW['max_buys']} dloss={cfgW['daily_loss']} "
          f"cl_after={cfgW['cl_after']} cl_days={cfgW['cl_days']}")
    print(f"  base: median=${fb['median']:.2f} P(profit)={fb['p_profit']:.0%} P(lose40)={fb['p_lose40']:.0%}")
    print(f"  harsh: median=${fh['median']:.2f} P(profit)={fh['p_profit']:.0%} P(lose40)={fh['p_lose40']:.0%} p5=${fh['p5']:.2f}")
    print(f"  normal: median=${fn['median']:.2f} P(profit)={fn['p_profit']:.0%}")

    # ---- final: winner full config on all regimes + F0 honesty ----
    print("\nFINAL WINNER - all regimes (F1 and F0_raw):")
    final = {}
    for fi, fname in [(best_filter, list(FILTERS)[best_filter]), (0, "F0_raw")]:
        c = dict(cfgW, filter_id=fi)
        for reg in ["base", "harsh", "normal"]:
            net = eval_ladder(pools[reg][1], c)
            s = sc(walk(pools[reg], c, net))
            final[f"{fname}_{reg}"] = s
            print(f"  {fname:<10} {reg:<7}: median=${s['median']:.2f} ({(s['median']/INITIAL-1)*100:+.0f}%) "
                  f"P(profit)={s['p_profit']:.0%} P(lose40)={s['p_lose40']:.0%} p5=${s['p5']:.2f} p95=${s['p95']:.2f}")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_sniper_cfg": {k: BASE_CFG[k] for k in ["qtp", "T_q", "trail_w", "sl", "sd_gain", "sd_drop"]},
        "winner_ladder": {k: winner_ladder[k] for k in ["qtp", "frac", "T_q", "trail_w", "ratchet",
                                                        "lock", "sd_gain", "sd_drop", "sl", "mega"]},
        "winner_sizing": {k: cfgW[k] for k in ["trade_pct", "max_buys", "daily_loss", "cl_after", "cl_days", "filter_id"]},
        "winner_filter_name": list(FILTERS)[best_filter],
        "final_regime_scores": final,
        "baseline_comparison": {"base": baseline_row[1], "harsh": baseline_row[2]},
    }
    with open(os.path.join(OUT, "strategy_winner.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved {OUT}/strategy_winner.json")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
