"""V547 — MAX PROFIT (no defense, big size, wide trail for moonshots).

V546 failed because the defense (DD pause + streak pause) kicks in
after 2-3 losers and stops trading for 2-3 days. On Pump.fun where
96% of trades lose, the defense triggers within the first few days
and the bot never catches the rare moonshot.

V547 removes the defense entirely (or makes it very loose) and uses:
  - trade_pct = 50% (max bet size)
  - max_buys_per_day = 10 (no limit practically)
  - max_concurrent = 6 (allow many simultaneous positions)
  - SL = -5% (moderate — survive shakeouts but cut real losers)
  - TP = +15% (band exit — covers costs)
  - runner_threshold = +50% (ride real moonshots)
  - trail_width = 25% (WIDE — survive 25-45% shakeouts on moonshots)
  - trail_tighten_50 = 0.6 (15% trail after +50%)
  - trail_tighten_100 = 0.4 (10% trail after +100%)
  - sudden_dump: gain >= 15%, drop >= 25% (only big dumps)
  - min_age = 30s, max_age = 180s
  - min_chg5m = 8%, max_chg5m = 60%
  - min_liquidity = $5k
  - daily_loss_stop = 50% (effectively off)
  - dd_half_pct = 0.30 (effectively off)
  - streak_wr_max = 0.05, streak_pnl_min = -0.50 (effectively off)

The user EXPLICITLY said "I want max absolute profit, not %".
We optimize MAX profit ($), accepting high variance and likely
mean loss.
"""
from __future__ import annotations
import json, math, random, sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np

OUT_DIR = Path("/home/z/my-project/download/v547_maxprofit")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Copy infrastructure from v546 (same token generators, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v546_maxprofit import (
    SAMPLE_OFFSETS_SEC, SAMPLE_OFFSETS_ARR, TOKEN_GENERATORS,
    sample_token_type, ROUND_TRIP_DRAG, ENTRY_LATENCY_PENALTY,
    exit_reason, entry_check, sample_snapshot,
    _entry_idx_from_age, _compute_chg5m, run_one_seed, run_regime, summarize,
)


# V547 — MAX PROFIT (defense off, big bets, wide trail)
V547_BASE = {
    "quick_tp": 0.15, "runner_threshold": 0.50, "trail_width": 0.25,
    "trail_tighten_50": 0.60, "trail_tighten_100": 0.40,
    "sudden_dump_gain": 0.15, "sudden_dump_drop": 0.25,
    "stop_loss": 0.05, "mega_tp": 10.0,
    "stagnation_minutes": 240, "stagnation_max_gain": 0.05,
    "max_hold_minutes": 7200,
    "min_liquidity_usd": 5000.0,
    "min_chg_5m_pct": 8.0, "max_chg_5m_pct": 60.0,
    "min_age_seconds": 30.0, "max_age_seconds": 180.0,
    "min_txns_buys": 20, "min_buy_sell_ratio": 1.3,
    "max_top10_concentration_pct": 30.0,
    # DEFENSE EFFECTIVELY OFF
    "max_buys_per_day": 10, "daily_loss_stop_pct": 50.0,
    "dd_full_pct": 0.10, "dd_half_pct": 0.05,
    "streak_window": 50, "streak_wr_max": 0.05,  # impossible to trigger
    "streak_pnl_min": -0.99, "pause_days_streak": 0, "pause_days_dd": 0,
    "trade_pct": 0.50,
    "max_concurrent_positions": 6,
}

# Variants: try different sizes to find MAX profit
V547_VARIANTS = {
    "V547a_size30": {**V547_BASE, "trade_pct": 0.30},
    "V547b_size50": {**V547_BASE, "trade_pct": 0.50},
    "V547c_size70": {**V547_BASE, "trade_pct": 0.70},
    "V547d_size100": {**V547_BASE, "trade_pct": 1.00},  # ALL IN every trade
}


def main():
    print("=" * 88)
    print("V547 — MAX PROFIT strategy (no defense, big bets, wide moonshot trail)")
    print("=" * 88)
    print(f"Cost model: round-trip drag {ROUND_TRIP_DRAG*100:.1f}%, latency {ENTRY_LATENCY_PENALTY*100:.1f}%")
    print(f"Base V547 params (V547b_size50):")
    for k in ["stop_loss", "quick_tp", "runner_threshold", "trail_width",
              "trail_tighten_50", "trail_tighten_100",
              "sudden_dump_gain", "sudden_dump_drop",
              "min_age_seconds", "max_age_seconds", "min_chg_5m_pct", "max_chg_5m_pct",
              "min_liquidity_usd", "min_txns_buys", "min_buy_sell_ratio",
              "max_buys_per_day", "daily_loss_stop_pct", "trade_pct",
              "max_concurrent_positions", "dd_half_pct"]:
        print(f"  {k:35s} = {V547_BASE[k]}")
    print()
    print("Sweeping 4 trade sizes (30% / 50% / 70% / 100%) with DEFENSE OFF")
    print("Goal: maximize MAX profit $ (not median, not mean)")
    print()

    all_summaries = {}
    all_results = {}
    for name, params in V547_VARIANTS.items():
        print(f">>> {name} (trade_pct = {params['trade_pct']*100:.0f}%)")
        regime_results = {}
        regime_summaries = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=50, days=30)  # more seeds for better p99 estimate
            regime_results[regime] = results
            s = summarize(results)
            regime_summaries[regime] = s
            print(f"  {regime:7s}: mean ${s['final_mean']:9.2f}  median ${s['final_median']:8.2f}  "
                  f"p95 ${s['final_p95']:9.2f}  MAX ${s['final_max']:10.2f}  "
                  f"trades={s['avg_trades']:.0f}  P(profit)={s['prob_profit_pct']:.0f}%  "
                  f"P(5x)={s['prob_5x_pct']:.0f}%  P(10x)={s['prob_10x_pct']:.0f}%  "
                  f"P(ruin)={s['prob_ruin_pct']:.0f}%")
        all_summaries[name] = regime_summaries
        all_results[name] = regime_results
        print()

    print("=" * 88)
    print("FINAL RANKING by MAX profit $ across 3 regimes (50 seeds each)")
    print("=" * 88)
    ranked = []
    for name, reg_sums in all_summaries.items():
        mean_max = np.mean([reg_sums[r]["profit_max_usd"] for r in ["harsh", "base", "normal"]])
        mean_p95 = np.mean([reg_sums[r]["profit_p95_usd"] for r in ["harsh", "base", "normal"]])
        mean_profit = np.mean([reg_sums[r]["profit_mean_usd"] for r in ["harsh", "base", "normal"]])
        max_profit = max([reg_sums[r]["profit_max_usd"] for r in ["harsh", "base", "normal"]])
        ranked.append((name, mean_max, max_profit, mean_p95, mean_profit))
    ranked.sort(key=lambda x: -x[1])  # sort by mean_max profit
    for i, (name, mm, mx, p95, mean) in enumerate(ranked):
        print(f"  {i+1}. {name:20s}  mean MAX ${mm:+8.2f}  best MAX ${mx:+9.2f}  "
              f"mean P95 ${p95:+8.2f}  mean $ {mean:+8.2f}")

    winner = ranked[0][0]
    winner_params = V547_VARIANTS[winner]
    print()
    print(f"WINNER: {winner}  (mean MAX profit = ${ranked[0][1]:+.2f}/month, best = ${ranked[0][2]:+.2f})")

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategies": {name: {"params": V547_VARIANTS[name], "by_regime": all_summaries[name]}
                       for name in V547_VARIANTS},
        "winner": winner,
        "winner_params": winner_params,
        "ranking": [{"name": n, "mean_max_profit_usd": mm, "best_max_profit_usd": mx,
                     "mean_p95_profit_usd": p95, "mean_profit_usd": mp}
                    for n, mm, mx, p95, mp in ranked],
    }
    with open(OUT_DIR / "results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved to {OUT_DIR / 'results.json'}")


if __name__ == "__main__":
    main()
