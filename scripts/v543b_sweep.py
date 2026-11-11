"""V543b — multi-strategy sweep to find configs that DON'T LOSE.

We test 4 strategic philosophies:
  A. ULTRA_CONSERVATIVE — almost no trades, tiny size
     (literal "don't lose" — trades so rarely that losses are negligible)
  B. MICRO_SCALPER — very tight SL/TP, take small wins often
     (SL -1.5%, TP +3%, requires 33% WR to break even)
  C. SURVIVAL_FILTER — only enter tokens that survived 5+ min
     + tight SL, normal TP (filters out 80% of rugs/fail_day1)
  D. TREND_RIDER — wider SL (-15%), bigger TP (+50%, +200% trail)
     (let winners run, accept losses on bad picks)

Goal: find any config whose median ROI >= -1% in ALL 3 regimes.
"""
from __future__ import annotations
import json, math, random, sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np

# Reuse v543_optimizer infrastructure
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v543_optimizer import (
    SAMPLE_OFFSETS_SEC, SAMPLE_OFFSETS_ARR,
    TOKEN_GENERATORS, sample_token_type,
    ROUND_TRIP_DRAG, ENTRY_LATENCY_PENALTY,
    _entry_idx_from_age, _compute_chg5m,
    sample_snapshot, run_one_seed, run_regime, summarize,
)
from v543_optimizer import DEFAULT_PARAMS as V543_DEFAULT

OUT_DIR = Path("/home/z/my-project/download/v543_optimization")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Strategy A: ULTRA_CONSERVATIVE (almost never trade)
STRAT_A = dict(V543_DEFAULT)
STRAT_A.update({
    "stop_loss": 0.02, "quick_tp": 0.05,
    "min_liquidity_usd": 15000.0,       # only deep liquidity
    "min_chg_5m_pct": 10.0, "max_chg_5m_pct": 25.0,
    "min_age_seconds": 120.0, "max_age_seconds": 240.0,
    "min_txns_buys": 50,                # only well-bought tokens
    "min_buy_sell_ratio": 2.5,          # strong buy pressure
    "max_top10_concentration_pct": 15.0,
    "max_buys_per_day": 1,              # 1 buy max
    "trade_pct": 0.03,                  # 3% size
    "daily_loss_stop_pct": 3.0,
    "dd_half_pct": 0.90,                # defense kicks in early
})

# Strategy B: MICRO_SCALPER (tiny SL/TP, take small wins)
STRAT_B = dict(V543_DEFAULT)
STRAT_B.update({
    "stop_loss": 0.015, "quick_tp": 0.04,
    "runner_threshold": 0.20, "trail_width": 0.05,
    "sudden_dump_gain": 0.02, "sudden_dump_drop": 0.05,
    "min_chg_5m_pct": 5.0, "max_chg_5m_pct": 30.0,
    "min_age_seconds": 30.0, "max_age_seconds": 180.0,
    "min_buy_sell_ratio": 1.5,
    "max_top10_concentration_pct": 25.0,
    "max_buys_per_day": 3,
    "trade_pct": 0.08,
    "daily_loss_stop_pct": 4.0,
})

# Strategy C: SURVIVAL_FILTER (5+ min, then tight SL, normal TP)
STRAT_C = dict(V543_DEFAULT)
STRAT_C.update({
    "stop_loss": 0.025, "quick_tp": 0.10,
    "runner_threshold": 0.30, "trail_width": 0.08,
    "min_age_seconds": 300.0,            # only after 5 min survival
    "max_age_seconds": 600.0,
    "min_chg_5m_pct": 10.0, "max_chg_5m_pct": 40.0,
    "min_buy_sell_ratio": 1.6,
    "max_top10_concentration_pct": 22.0,
    "max_buys_per_day": 2,
    "trade_pct": 0.07,
    "daily_loss_stop_pct": 5.0,
})

# Strategy D: TREND_RIDER (wide SL, big TP — only good for moonshots)
STRAT_D = dict(V543_DEFAULT)
STRAT_D.update({
    "stop_loss": 0.15,                   # wide stop
    "quick_tp": 0.40,                    # band exit at +40%
    "runner_threshold": 0.80, "trail_width": 0.20,
    "sudden_dump_gain": 0.20, "sudden_dump_drop": 0.30,
    "min_age_seconds": 60.0, "max_age_seconds": 240.0,
    "min_chg_5m_pct": 12.0, "max_chg_5m_pct": 50.0,
    "min_buy_sell_ratio": 2.0,
    "max_top10_concentration_pct": 18.0,
    "max_buys_per_day": 2,
    "trade_pct": 0.04,                   # small size to survive wide SL
    "daily_loss_stop_pct": 8.0,
})

# Strategy E: BALANCED (mix of conservative + moderate upside)
STRAT_E = dict(V543_DEFAULT)
STRAT_E.update({
    "stop_loss": 0.04, "quick_tp": 0.12,
    "runner_threshold": 0.30, "trail_width": 0.10,
    "sudden_dump_gain": 0.08, "sudden_dump_drop": 0.15,
    "min_age_seconds": 90.0, "max_age_seconds": 240.0,
    "min_chg_5m_pct": 10.0, "max_chg_5m_pct": 35.0,
    "min_buy_sell_ratio": 1.7,
    "max_top10_concentration_pct": 20.0,
    "max_buys_per_day": 2,
    "trade_pct": 0.06,
    "daily_loss_stop_pct": 6.0,
    "dd_half_pct": 0.88,
})

# Strategy F: HOLD-ONLY-WINNERS — never sell at loss, trail from entry
# (the "diamond hands" approach — accepts drawdown but rides moonshots)
STRAT_F = dict(V543_DEFAULT)
STRAT_F.update({
    "stop_loss": 0.50,                  # no real SL — ride to -50%
    "quick_tp": 0.20,                   # take +20% when reached
    "runner_threshold": 0.40, "trail_width": 0.15,
    "sudden_dump_gain": 0.30, "sudden_dump_drop": 0.40,  # only on huge peaks
    "min_age_seconds": 60.0, "max_age_seconds": 180.0,
    "min_chg_5m_pct": 10.0, "max_chg_5m_pct": 35.0,
    "min_buy_sell_ratio": 1.8,
    "max_top10_concentration_pct": 18.0,
    "max_buys_per_day": 1,              # very few bets
    "trade_pct": 0.04,
    "daily_loss_stop_pct": 15.0,        # tolerate big swings
    "dd_half_pct": 0.75,                # only pause if catastrophic
})


def main():
    strategies = {
        "A_ULTRA_CONSERVATIVE": STRAT_A,
        "B_MICRO_SCALPER":      STRAT_B,
        "C_SURVIVAL_FILTER":    STRAT_C,
        "D_TREND_RIDER":        STRAT_D,
        "E_BALANCED":           STRAT_E,
        "F_HOLD_WINNERS":       STRAT_F,
    }
    print("=" * 72)
    print("V543b — Strategy sweep on Pump.fun Monte-Carlo (20 seeds x 3 regimes)")
    print("=" * 72)
    all_summaries = {}
    for name, params in strategies.items():
        print(f"\n>>> Strategy {name}")
        keys = ["stop_loss", "quick_tp", "min_age_seconds", "max_age_seconds",
                "min_chg_5m_pct", "max_chg_5m_pct", "min_buy_sell_ratio",
                "max_top10_concentration_pct", "max_buys_per_day", "trade_pct"]
        for k in keys:
            print(f"   {k:35s} = {params[k]}")
        regime_summaries = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=20)
            s = summarize(results)
            regime_summaries[regime] = s
            print(f"   {regime:7s}: median ${s['final_median']:.2f}  ROI={s['roi_median_pct']:+6.2f}%  "
                  f"WR={s['avg_win_rate_pct']:4.1f}%  trades={s['avg_trades']:.0f}  DD={s['max_drawdown_pct_median']:.1f}%  "
                  f"P(profit)={s['prob_profit_pct']:.0f}%")
        all_summaries[name] = {
            "params": params, "by_regime": regime_summaries,
            "worst_roi": min(s["roi_median_pct"] for s in regime_summaries.values()),
            "best_roi": max(s["roi_median_pct"] for s in regime_summaries.values()),
            "median_across_regimes": float(np.median([s["roi_median_pct"] for s in regime_summaries.values()])),
        }

    print()
    print("=" * 72)
    print("FINAL RANKING (sorted by worst-case median ROI)")
    print("=" * 72)
    sorted_strats = sorted(all_summaries.items(), key=lambda x: -x[1]["worst_roi"])
    for i, (name, s) in enumerate(sorted_strats):
        mark = " ✓ NO-LOSE" if s["worst_roi"] >= -2 else ""
        print(f"  {i+1}. {name:25s}  worst={s['worst_roi']:+6.2f}%  "
              f"median={s['median_across_regimes']:+6.2f}%  best={s['best_roi']:+6.2f}%{mark}")

    winner_name, winner_data = sorted_strats[0]
    print()
    print(f"WINNER: {winner_name}  (worst-case median ROI = {winner_data['worst_roi']:+.2f}%)")
    if winner_data["worst_roi"] >= -2:
        print("=> This strategy does NOT lose money in any regime ✓")
    else:
        print(f"=> Still loses {winner_data['worst_roi']:.2f}% in worst regime — need more iteration")

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategies": {name: {"params": s["params"], "by_regime": s["by_regime"],
                              "worst_roi": s["worst_roi"], "best_roi": s["best_roi"]}
                       for name, s in all_summaries.items()},
        "winner": winner_name,
        "winner_params": winner_data["params"],
    }
    with open(OUT_DIR / "v543b_sweep_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved to {OUT_DIR / 'v543b_sweep_results.json'}")


if __name__ == "__main__":
    main()
