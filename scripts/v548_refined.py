"""V548 — Refined V547: tighten SL to reduce per-trade loss.

V547a_size30 had MEAN profit positive ($1.70-$2.39) but P(ruin)=82%.
Each loser cost ~$3 (30% size × -5% SL × 2 costs). Tightening SL
to -3% reduces per-loser cost to ~$2.4, leaving more capital for
moonshots.

Sweep: SL ∈ {-2%, -3%, -4%} with size=25%.
"""
from __future__ import annotations
import json, random, sys
from datetime import datetime
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v546_maxprofit import run_regime, summarize

OUT_DIR = Path("/home/z/my-project/download/v548_refined")
OUT_DIR.mkdir(parents=True, exist_ok=True)

V548_BASE = {
    "quick_tp": 0.15, "runner_threshold": 0.50, "trail_width": 0.25,
    "trail_tighten_50": 0.60, "trail_tighten_100": 0.40,
    "sudden_dump_gain": 0.15, "sudden_dump_drop": 0.25,
    "stop_loss": 0.03, "mega_tp": 10.0,  # tightened SL
    "stagnation_minutes": 240, "stagnation_max_gain": 0.05,
    "max_hold_minutes": 7200,
    "min_liquidity_usd": 5000.0,
    "min_chg_5m_pct": 8.0, "max_chg_5m_pct": 60.0,
    "min_age_seconds": 30.0, "max_age_seconds": 180.0,
    "min_txns_buys": 20, "min_buy_sell_ratio": 1.3,
    "max_top10_concentration_pct": 30.0,
    "max_buys_per_day": 10, "daily_loss_stop_pct": 50.0,
    "dd_full_pct": 0.10, "dd_half_pct": 0.05,
    "streak_window": 50, "streak_wr_max": 0.05,
    "streak_pnl_min": -0.99, "pause_days_streak": 0, "pause_days_dd": 0,
    "trade_pct": 0.25, "max_concurrent_positions": 6,
}

V548_VARIANTS = {
    "V548a_size20_SL3": {**V548_BASE, "trade_pct": 0.20, "stop_loss": 0.03},
    "V548b_size25_SL3": {**V548_BASE, "trade_pct": 0.25, "stop_loss": 0.03},
    "V548c_size30_SL3": {**V548_BASE, "trade_pct": 0.30, "stop_loss": 0.03},
    "V548d_size25_SL2": {**V548_BASE, "trade_pct": 0.25, "stop_loss": 0.02},  # tighter SL
    "V548e_size25_SL4": {**V548_BASE, "trade_pct": 0.25, "stop_loss": 0.04},  # looser SL
}


def main():
    print("=" * 88)
    print("V548 — Refined V547: tight SL (-2/-3/-4%) + size sweep (20-30%)")
    print("=" * 88)
    print()
    all_summaries = {}
    for name, params in V548_VARIANTS.items():
        print(f">>> {name} (size={params['trade_pct']*100:.0f}%, SL=-{params['stop_loss']*100:.0f}%)")
        regime_summaries = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=50, days=30)
            s = summarize(results)
            regime_summaries[regime] = s
            print(f"  {regime:7s}: mean ${s['final_mean']:9.2f}  median ${s['final_median']:8.2f}  "
                  f"p95 ${s['final_p95']:9.2f}  MAX ${s['final_max']:10.2f}  "
                  f"trades={s['avg_trades']:.0f}  P(profit)={s['prob_profit_pct']:.0f}%  "
                  f"P(5x)={s['prob_5x_pct']:.0f}%  P(ruin)={s['prob_ruin_pct']:.0f}%")
        all_summaries[name] = regime_summaries
        print()

    print("=" * 88)
    print("RANKING by MAX profit + (positive mean)")
    print("=" * 88)
    ranked = []
    for name, reg_sums in all_summaries.items():
        mean_max = np.mean([reg_sums[r]["profit_max_usd"] for r in ["harsh", "base", "normal"]])
        max_profit = max([reg_sums[r]["profit_max_usd"] for r in ["harsh", "base", "normal"]])
        mean_p95 = np.mean([reg_sums[r]["profit_p95_usd"] for r in ["harsh", "base", "normal"]])
        mean_profit = np.mean([reg_sums[r]["profit_mean_usd"] for r in ["harsh", "base", "normal"]])
        mean_p_ruin = np.mean([reg_sums[r]["prob_ruin_pct"] for r in ["harsh", "base", "normal"]])
        ranked.append((name, mean_max, max_profit, mean_p95, mean_profit, mean_p_ruin))
    # sort by max profit first
    ranked.sort(key=lambda x: -x[1])
    print(f"  {'Strategy':25s}  {'meanMAX':>10s}  {'bestMAX':>10s}  {'meanP95':>10s}  {'mean$':>10s}  {'meanP(ruin)':>12s}")
    for name, mm, mx, p95, mean, ruin in ranked:
        print(f"  {name:25s}  ${mm:+9.2f}  ${mx:+9.2f}  ${p95:+9.2f}  ${mean:+9.2f}  {ruin:11.1f}%")
    winner = ranked[0][0]
    print(f"\nWINNER: {winner}  (mean MAX ${ranked[0][1]:+.2f}, best MAX ${ranked[0][2]:+.2f})")

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategies": {name: {"params": V548_VARIANTS[name], "by_regime": all_summaries[name]}
                       for name in V548_VARIANTS},
        "winner": winner,
        "winner_params": V548_VARIANTS[winner],
        "ranking": [{"name": n, "mean_max_profit_usd": mm, "best_max_profit_usd": mx,
                     "mean_p95_profit_usd": p95, "mean_profit_usd": mp,
                     "mean_p_ruin_pct": ruin} for n, mm, mx, p95, mp, ruin in ranked],
    }
    with open(OUT_DIR / "results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved to {OUT_DIR / 'results.json'}")


if __name__ == "__main__":
    main()
