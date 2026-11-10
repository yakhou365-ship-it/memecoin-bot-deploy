import numpy as np
import time

OUT = "/home/z/my-project/download/bot_20usd_simulation"
INITIAL = 20.0
RT_DRAG = (0.995 * 0.99) * (0.99 / 1.005)
ENTRY_PENALTY = 0.985
MIN_TRADE_USD = 0.42
T_MAX = 40
N_SEEDS = 120

REGIMES = {
    "base":   [69.0, 15.0, 12.0, 1.5, 0.5],
    "harsh":  [75.5, 18.0, 5.0, 0.9, 0.3],
    "normal": [60.0, 12.0, 24.0, 2.7, 0.9],
}
F2 = [0.40, 0.45, 0.72, 0.85, 0.60]

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


