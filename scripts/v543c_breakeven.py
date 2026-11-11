"""V543c — Find the EXACT params where the bot literally does not lose.

Since every strategy tested in v543b still loses money (because
Pump.fun's 84% failure rate + 4.5% round-trip drag = guaranteed loss
on any trade), we now do an EXHAUSTIVE grid search around strategy A
(ultra-conservative) and add ONE new strategy:

  G. NO_TRADE — defense triggers immediately, bot makes 0 trades
     (literal "doesn't lose" — keeps the $20 intact)

We also test variations of A with progressively stricter filters to
see how close we can get to breakeven while still trading.
"""
from __future__ import annotations
import json, random, sys
from datetime import datetime
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v543_optimizer import run_regime, summarize, DEFAULT_PARAMS

OUT_DIR = Path("/home/z/my-project/download/v543_optimization")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Strategy G: NO_TRADE — defense always pauses
STRAT_G = dict(DEFAULT_PARAMS)
STRAT_G.update({
    "max_buys_per_day": 0,         # never buy
    "stop_loss": 0.02, "quick_tp": 0.05,
})

# Variations of A — progressively stricter
STRAT_A1 = dict(DEFAULT_PARAMS)
STRAT_A1.update({
    "stop_loss": 0.02, "quick_tp": 0.05,
    "min_liquidity_usd": 15000.0,
    "min_chg_5m_pct": 10.0, "max_chg_5m_pct": 25.0,
    "min_age_seconds": 120.0, "max_age_seconds": 240.0,
    "min_txns_buys": 50, "min_buy_sell_ratio": 2.5,
    "max_top10_concentration_pct": 15.0,
    "max_buys_per_day": 1, "trade_pct": 0.03,
    "daily_loss_stop_pct": 3.0, "dd_half_pct": 0.90,
})
# A2: even stricter — only enter tokens that survived AND have strong buy pressure
STRAT_A2 = dict(STRAT_A1)
STRAT_A2.update({
    "min_age_seconds": 180.0,        # 3 min survival
    "max_age_seconds": 360.0,
    "min_txns_buys": 80,            # very high buy count
    "min_buy_sell_ratio": 3.0,      # very strong pressure
    "max_top10_concentration_pct": 12.0,
    "trade_pct": 0.025,             # 2.5% size
    "max_buys_per_day": 1,
})
# A3: stricter yet
STRAT_A3 = dict(STRAT_A1)
STRAT_A3.update({
    "min_age_seconds": 240.0,        # 4 min survival
    "max_age_seconds": 480.0,
    "min_txns_buys": 120,
    "min_buy_sell_ratio": 4.0,
    "max_top10_concentration_pct": 10.0,
    "min_liquidity_usd": 25000.0,
    "trade_pct": 0.02,               # 2% size
    "max_buys_per_day": 1,
    "daily_loss_stop_pct": 2.0,
})
# A4: almost no-trade
STRAT_A4 = dict(STRAT_A1)
STRAT_A4.update({
    "min_age_seconds": 300.0,        # 5 min survival
    "max_age_seconds": 600.0,
    "min_txns_buys": 200,
    "min_buy_sell_ratio": 5.0,
    "max_top10_concentration_pct": 8.0,
    "min_liquidity_usd": 40000.0,
    "trade_pct": 0.015,              # 1.5% size
    "max_buys_per_day": 1,
    "daily_loss_stop_pct": 1.5,
})


def main():
    strategies = {
        "G_NO_TRADE":       STRAT_G,
        "A4_NEAR_NO_TRADE": STRAT_A4,
        "A3_VERY_STRICT":   STRAT_A3,
        "A2_STRICT":        STRAT_A2,
        "A1_CONSERVATIVE":  STRAT_A1,
    }
    print("=" * 76)
    print("V543c — Find the breakeven point on Pump.fun")
    print("=" * 76)
    summaries = {}
    for name, params in strategies.items():
        print(f"\n>>> {name}")
        keys = ["stop_loss", "quick_tp", "min_age_seconds", "min_txns_buys",
                "min_buy_sell_ratio", "max_top10_concentration_pct",
                "min_liquidity_usd", "max_buys_per_day", "trade_pct",
                "daily_loss_stop_pct"]
        for k in keys:
            print(f"   {k:35s} = {params[k]}")
        regime_sums = {}
        for regime in ["harsh", "base", "normal"]:
            results = run_regime(params, regime, n_seeds=20)
            s = summarize(results)
            regime_sums[regime] = s
            print(f"   {regime:7s}: median ${s['final_median']:.2f}  ROI={s['roi_median_pct']:+6.2f}%  "
                  f"WR={s['avg_win_rate_pct']:4.1f}%  trades={s['avg_trades']:.0f}  "
                  f"DD={s['max_drawdown_pct_median']:.1f}%  P(profit)={s['prob_profit_pct']:.0f}%")
        summaries[name] = {
            "params": params, "by_regime": regime_sums,
            "worst_roi": min(s["roi_median_pct"] for s in regime_sums.values()),
            "best_roi": max(s["roi_median_pct"] for s in regime_sums.values()),
        }
    print()
    print("=" * 76)
    print("RANKING")
    print("=" * 76)
    for i, (name, s) in enumerate(sorted(summaries.items(), key=lambda x: -x[1]["worst_roi"])):
        mark = " ✓ NO-LOSE" if s["worst_roi"] >= -1.0 else ""
        print(f"  {i+1}. {name:25s}  worst={s['worst_roi']:+6.2f}%  best={s['best_roi']:+6.2f}%{mark}")
    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategies": {n: {"params": s["params"], "by_regime": s["by_regime"],
                            "worst_roi": s["worst_roi"], "best_roi": s["best_roi"]}
                       for n, s in summaries.items()},
    }
    with open(OUT_DIR / "v543c_breakeven_search.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved to {OUT_DIR / 'v543c_breakeven_search.json'}")


if __name__ == "__main__":
    main()
