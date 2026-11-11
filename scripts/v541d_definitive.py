"""
V541D - DEFINITIVE STRATEGY (with entry penalty + escalating defense)
=====================================================================
Model upgrades:
  - entry_penalty 1.5% (signal latency / buying into the pump) — applied to ALL nets
  - defense v2: trigger (WR6 <= 33% AND pnl6 <= -8%) -> escalating pauses [2,3,7] days,
    resume at half size / 1 buy per day; drawdown 20% -> pause 3 days
Selection: blend = base + 0.6*harsh + 0.2*normal - penalty, require base median >= $22.
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
ENTRY_PENALTY = 0.985          # buy 1.5% worse than signal price
MIN_TRADE_USD = 0.42
T_MAX = 40
N_SEEDS = 120

REGIMES = {
    "base":   [69.0, 15.0, 12.0, 1.5, 0.5],
    "harsh":  [75.5, 18.0, 5.0, 0.9, 0.3],
    "normal": [60.0, 12.0, 24.0, 2.7, 0.9],
}
F2 = [0.40, 0.45, 0.72, 0.85, 0.60]   # strict filter captures

PERTURBS = {
    "neutral":      (0.25, 1.20, 1.90, 0.25, 2.0, 5.0),
    "blip_low":     (0.15, 1.20, 1.90, 0.25, 2.0, 5.0),
    "blip_high":    (0.40, 1.20, 1.90, 0.25, 2.0, 5.0),
    "bait_wide":    (0.25, 1.20, 2.50, 0.25, 2.0, 5.0),
    "explosive_hi": (0.25, 1.20, 1.90, 0.40, 2.0, 5.0),
    "pump_hi":      (0.25, 1.20, 1.90, 0.25, 2.0, 8.0),
    "blip_low_h":   (0.15, 1.20, 1.90, 0.25, 2.0, 5.0),
    "bait_wide_h":  (0.25, 1.20, 2.50, 0.25, 2.0, 5.0),
}


def make_paths(pert):
    blip_max, b_lo, b_hi, exp_f, p_lo, p_hi = pert

    def maybe_shakeout(rng, pts):
        if rng.random() < 0.35:
            dip = rng.uniform(0.90, 0.96)
            n = int(rng.integers(2, 4))
            return [(0.0, 1.0)] + [(k * 0.5, 1.0 + (dip - 1.0) * k / n) for k in range(1, n + 1)] + \
                   [(n * 0.5 + 0.5, 1.0)] + [(t + n * 0.5 + 0.5, p) for t, p in pts[1:]]
        return pts

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
        return pts if explosive else maybe_shakeout(rng, pts)

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
        return maybe_shakeout(rng, pts)

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


def gen_pool(regime, pert_name, seed0=90000):
    probs = np.array(REGIMES[regime]) / 100.0
    makers = make_paths(PERTURBS[pert_name])
    all_types, all_prices, all_day, all_seed, all_pass = [], [], [], [], []
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
                all_pass.append(rng.random() < F2[ti])
    return (np.array(all_types), np.array(all_prices, dtype=float),
            np.array(all_day), np.array(all_seed), np.array(all_pass, dtype=bool))


def eval_ladder(P, cfg):
    N, T = P.shape
    peak = np.maximum.accumulate(P, axis=1)
    peak_gain = peak - 1.0
    drop = (peak - P) / np.maximum(peak, 1e-9)
    t_idx = np.broadcast_to(np.arange(T), (N, T))
    qtp, T_q = cfg["qtp"], cfg["T_q"]
    sd_g, sd_d = 0.10, 0.20
    sl = 1.0 - cfg["sl"]
    mega, tw = 10.0, cfg["trail_w"]

    reach = peak_gain >= qtp
    has_t1 = reach.any(axis=1)
    t1 = np.where(has_t1, reach.argmax(axis=1), T - 1)
    p_t1 = P[np.arange(N), t1]
    pg_t1 = peak_gain[np.arange(N), t1]
    full_qtp = has_t1 & (pg_t1 < T_q)
    stageA_price = np.maximum(1.0 + qtp, p_t1)

    mask_run = t_idx >= t1[:, None]
    sd_trig = (peak_gain >= sd_g) & (drop >= sd_d) & mask_run
    sl_trig = (P <= sl) & mask_run
    mega_trig = (P >= mega) & mask_run
    w_eff = tw * np.where(peak_gain >= 1.0, 0.60, np.where(peak_gain >= 0.5, 0.75, 1.0))
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
    best_t = np.floor(np.where(np.isfinite(scores.min(axis=1)), scores.min(axis=1), T - 1)).astype(int)
    run_exit = np.isfinite(scores.min(axis=1))
    t_sdN, has_sdN = first_true((peak_gain >= sd_g) & (drop >= sd_d))
    t_slN, has_slN = first_true(P <= sl)
    never = ~has_t1

    p_run = P[np.arange(N), np.clip(best_t, 0, T - 1)]
    p_early = P[np.arange(N), np.clip(np.minimum(early_time, T - 1), 0, T - 1)]
    p_never = P[np.arange(N), np.clip(np.where(has_sdN, t_sdN, t_slN), 0, T - 1)]

    net = np.where(never,
                   np.where(has_sdN | has_slN, p_never * RT_DRAG, P[:, -1] * RT_DRAG),
          np.where(early_exit, p_early * RT_DRAG,
          np.where(full_qtp, stageA_price * RT_DRAG,
          np.where(run_exit, p_run * RT_DRAG, P[:, -1] * RT_DRAG))))
    return net * ENTRY_PENALTY


def walk(pool, cfg, net_mult):
    """Defense v2: (WR6<=1/3 AND pnl6<=-8%) -> escalating pause [2,3,7]d,
    resume half-size/1-buy; drawdown 20% from peak -> pause 3d."""
    types, prices, day, seed, pass_f = pool
    trade_pct, max_buys, dloss = cfg["trade_pct"], cfg["max_buys"], cfg["daily_loss"]
    defense = cfg.get("defense", "v2")
    finals = np.zeros(N_SEEDS)
    for s in range(N_SEEDS):
        idx = np.where((seed == s) & pass_f)[0]
        capital = INITIAL
        if len(idx) == 0:
            finals[s] = capital
            continue
        days = day[idx]
        peak_eq = INITIAL
        pause_until = -1
        n_pauses = 0
        hist = []
        for d in range(30):
            if capital <= 0.01:
                break
            if d < pause_until:
                continue
            sel = idx[days == d]
            day_start = capital
            buys = 0
            eff_pct, eff_buys = trade_pct, max_buys
            if defense == "v2" and len(hist) >= 6:
                last6 = hist[-6:]
                wr6 = sum(1 for pnl, _ in last6 if pnl > 0) / 6
                pnl6 = sum(pnl for pnl, _ in last6) / max(sum(sz for _, sz in last6), 1e-9)
                if wr6 <= 1 / 3 and pnl6 <= -0.08:
                    n_pauses += 1
                    pause_len = [2, 3, 7][min(n_pauses - 1, 2)]
                    pause_until = d + 1 + pause_len
                    # resume at half size after first pause
                    continue
                elif n_pauses >= 1:
                    eff_pct, eff_buys = trade_pct * 0.5, min(max_buys, 2)
            for tid in sel:
                if buys >= eff_buys or capital < 1.0:
                    break
                size = capital * eff_pct
                if size < MIN_TRADE_USD:
                    break
                if day_start - capital >= day_start * dloss:
                    break
                pnl = size * (net_mult[tid] - 1.0)
                capital += pnl
                hist.append((pnl, size))
                buys += 1
                peak_eq = max(peak_eq, capital)
                if defense in ("v2", "v1") and capital < peak_eq * 0.80:
                    pause_until = d + 3
                    break
                if capital <= 0.01:
                    capital = 0.01
                    break
        finals[s] = capital
    return finals


def sc(finals):
    return {"median": float(np.median(finals)),
            "mean": float(np.mean(finals)),
            "p5": float(np.percentile(finals, 5)),
            "p95": float(np.percentile(finals, 95)),
            "p_profit": float((finals > INITIAL).mean()),
            "p_lose40": float((finals < INITIAL * 0.6).mean())}


BASELINE = dict(qtp=0.15, T_q=0.50, trail_w=0.30, sl=0.08,
                trade_pct=0.10, max_buys=2, daily_loss=0.20, defense="none")


def main():
    t0 = time.time()
    print("Pools (120 seeds, shakeouts, F2 filter, entry penalty 1.5%)...")
    pools = {reg: gen_pool(reg, "neutral") for reg in REGIMES}
    print(f"  ready ({time.time()-t0:.1f}s)")

    def run3(cfg):
        netb = eval_ladder(pools["base"][1], cfg)
        neth = eval_ladder(pools["harsh"][1], cfg)
        netn = eval_ladder(pools["normal"][1], cfg)
        return (sc(walk(pools["base"], cfg, netb)),
                sc(walk(pools["harsh"], cfg, neth)),
                sc(walk(pools["normal"], cfg, netn)))

    bb, bh, bn = run3(BASELINE)
    print(f"\nBASELINE (current sniper): base=${bb['median']:.2f} ({(bb['median']/INITIAL-1)*100:+.0f}%) "
          f"harsh=${bh['median']:.2f} normal=${bn['median']:.2f} | Pprof b/h/n={bb['p_profit']:.0%}/{bh['p_profit']:.0%}/{bn['p_profit']:.0%}")

    print("\nLadder grid (defense=v2, 10%/4buys/15%):")
    rows = []
    for qtp, tw, sl in itertools.product([0.15, 0.20], [0.15, 0.20], [0.05, 0.06, 0.08]):
        cfg = dict(qtp=qtp, T_q=0.35, trail_w=tw, sl=sl,
                   trade_pct=0.10, max_buys=4, daily_loss=0.15, defense="v2")
        sb, sh, sn = run3(cfg)
        blend = sb["median"] + 0.6 * sh["median"] + 0.2 * sn["median"] - 40 * max(0.0, sh["p_lose40"] - 0.15)
        rows.append((blend, cfg, sb, sh, sn))
        print(f"  qtp={qtp} tw={tw} sl={sl}: base=${sb['median']:.2f} harsh=${sh['median']:.2f} "
              f"normal=${sn['median']:.2f} blend={blend:.2f} PL40_h={sh['p_lose40']:.0%}")
    rows.sort(key=lambda x: -x[0])
    _, wl, sb, sh, sn = rows[0]
    print(f"  -> LADDER: qtp={wl['qtp']} Tq={wl['T_q']} tw={wl['trail_w']} sl={wl['sl']}")

    print("\nDefense comparison (winner ladder, 10%/4buys/15%):")
    for dv in ["none", "v2"]:
        cfg = dict(wl, defense=dv)
        sbd, shd, snd = run3(cfg)
        print(f"  defense={dv:<5}: base=${sbd['median']:.2f} harsh=${shd['median']:.2f} "
              f"normal=${snd['median']:.2f} Pprof_h={shd['p_profit']:.0%} PL40_h={shd['p_lose40']:.0%} "
              f"p5_h=${shd['p5']:.2f}")

    print("\nSizing grid (defense=v2):")
    best = None
    for tp_, mb, dl in itertools.product([0.08, 0.10], [3, 4], [0.12, 0.15]):
        cfg = dict(wl, trade_pct=tp_, max_buys=mb, daily_loss=dl, defense="v2")
        sbz, shz, snz = run3(cfg)
        blend = sbz["median"] + 0.6 * shz["median"] + 0.2 * snz["median"] - 40 * max(0.0, shz["p_lose40"] - 0.15)
        if sbz["median"] >= 22.0 and (best is None or blend > best[0]):
            best = (blend, cfg, sbz, shz, snz)
        print(f"  pct={tp_} buys={mb} dl={dl}: base=${sbz['median']:.2f} harsh=${shz['median']:.2f} "
              f"normal=${snz['median']:.2f} blend={blend:.2f}")
    if best is None:
        best = rows_best = (rows[0][0], dict(wl, trade_pct=0.10, max_buys=4, daily_loss=0.15, defense="v2"), sb, sh, sn)
    blend, W, sbz, shz, snz = best
    print(f"\nWINNER: {json.dumps({k: W[k] for k in ['qtp','T_q','trail_w','sl','trade_pct','max_buys','daily_loss','defense']})}")
    print(f"  base  : median=${sbz['median']:.2f} ({(sbz['median']/INITIAL-1)*100:+.0f}%) Pprof={sbz['p_profit']:.0%} p5=${sbz['p5']:.2f}")
    print(f"  harsh : median=${shz['median']:.2f} ({(shz['median']/INITIAL-1)*100:+.0f}%) Pprof={shz['p_profit']:.0%} PL40={shz['p_lose40']:.0%} p5=${shz['p5']:.2f}")
    print(f"  normal: median=${snz['median']:.2f} ({(snz['median']/INITIAL-1)*100:+.0f}%) Pprof={snz['p_profit']:.0%}")

    # perturbation robustness
    print("\nPerturbation robustness (winner):")
    robust = {}
    for pert in PERTURBS:
        regime = "harsh" if pert.endswith("_h") else "base"
        pp = gen_pool(regime, pert)
        net = eval_ladder(pp[1], W)
        s = sc(walk(pp, W, net))
        robust[pert] = s
        print(f"  {pert:<13} ({regime}): median=${s['median']:.2f} Pprof={s['p_profit']:.0%} PL40={s['p_lose40']:.0%}")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": "Pump.fun MC + shakeouts + entry penalty 1.5% + F2 strict filter, 120 seeds",
        "baseline": {"cfg": dict(BASELINE), "scores": {"base": bb, "harsh": bh, "normal": bn}},
        "winner": {
            "ladder": {k: W[k] for k in ["qtp", "T_q", "trail_w", "sl"]},
            "sizing": {k: W[k] for k in ["trade_pct", "max_buys", "daily_loss", "defense"]},
            "fixed": {"sd_gain": 0.10, "sd_drop": 0.20, "mega_tp": 10.0, "ratchet": True,
                      "filter": "F2_strict (liq>=4k, chg5m 8-60%, age<=3min, holders, risk)"},
            "scores": {"base": sbz, "harsh": shz, "normal": snz},
            "robustness": robust,
        },
    }
    with open(os.path.join(OUT, "strategy_definitive.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved {OUT}/strategy_definitive.json ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
