"""Strategy V2 backtest / walk-forward CLI.

Examples
  python backtest_v2.py --days 180                       # real Binance 15m data (needs internet)
  python backtest_v2.py --days 365 --walk-forward        # out-of-sample evaluation
  python backtest_v2.py --synthetic                      # offline sanity run (NO real edge expected)
  python backtest_v2.py --days 180 --ablation            # which component actually adds value
Success criteria are printed at the end (>=200 OOS trades, expectancy CI lower bound > 0, PF>=1.3, DD<=10%).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.strategy_v2 import StrategyConfig
from app.modules.strategy_v2.backtester import run_backtest, walk_forward
from app.modules.strategy_v2.data import DEFAULT_SYMBOLS, load_universe, synthetic_market

GRID = [dict(sl_atr=s, tp1_atr=t, trail_atr=tr, min_atr_pct=m)
        for s in (1.2, 1.5, 2.0) for t in (1.0, 1.5) for tr in (2.0, 3.0) for m in (0.005, 0.008)]

ABLATIONS = {
    "full_v2": {},
    "no_partial_tp (runner only)": dict(partial_fraction=0.0),
    "no_cost_filter": dict(min_atr_pct=0.0),
    "no_market_filter": dict(require_market_ok=False),
    "tighter_sl_1.0": dict(sl_atr=1.0, tp1_atr=1.0),
    "wider_trail_3.5": dict(trail_atr=3.5),
    "no_time_stop": dict(time_stop_bars=10_000),
    "with_short (futures)": dict(enable_short=True, venue="binance_futures"),
}


def fmt(s):
    if s.get("n", 0) == 0:
        return f"no trades {s.get('skipped', '')}"
    ci = s["expectancy_ci95"]
    ci_s = f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else "n/a (<10)"
    return (f"n={s['n']:>4} WR={s['win_rate']*100:5.1f}% E[R]={s['expectancy_r']:+.3f} CI95={ci_s} "
            f"PF={s['profit_factor']:.2f} ret={s['return_pct']:+.1f}% DD={s['max_drawdown_pct']:.1f}% "
            f"+months={s['positive_months_pct']:.0f}%")


def verdict(s):
    if s.get("n", 0) < 200:
        return f"NOT PROVEN: only {s.get('n', 0)} trades (need >= 200)"
    ci = s["expectancy_ci95"]
    checks = {"expectancy CI lower bound > 0": ci and ci[0] > 0, "PF >= 1.3": s["profit_factor"] >= 1.3,
              "max DD <= 10%": s["max_drawdown_pct"] <= 10, ">=70% positive months": s["positive_months_pct"] >= 70}
    return "; ".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--ablation", action="store_true")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--out", default="data/backtest_v2_results.json")
    a = ap.parse_args()

    data = synthetic_market(10, days=a.days) if a.synthetic else load_universe(a.symbols, a.days)
    if not data:
        sys.exit("No data loaded (check internet / symbols).")
    ref = "BTCUSDT" if "BTCUSDT" in data else None
    print(f"Loaded {len(data)} symbols, ~{a.days} days of 15m candles. Market filter ref: {ref}")
    cfg = StrategyConfig()
    out = {}

    if a.ablation:
        print("\n== ABLATION (in-sample; use to REMOVE components, not to tune) ==")
        for name, kw in ABLATIONS.items():
            r = run_backtest(data, cfg.with_(**kw), ref, a.equity)["stats"]
            print(f"{name:<30} {fmt(r)}")
            out[name] = {k: v for k, v in r.items() if k != "exit_reasons"}
    else:
        res = run_backtest(data, cfg, ref, a.equity)["stats"]
        print("\n== IN-SAMPLE ==\n" + fmt(res))
        print("exit reasons:", json.dumps(res.get("exit_reasons", {})))
        print("skipped by risk limits:", res.get("skipped"))
        out["in_sample"] = res

    if a.walk_forward:
        wf = walk_forward(data, cfg, GRID, market_ref=ref, initial_equity=a.equity)
        print("\n== WALK-FORWARD (out-of-sample only) ==")
        for w in wf["windows"]:
            print(w)
        print(fmt(wf["oos_stats"]))
        print("VERDICT:", verdict(wf["oos_stats"]))
        out["walk_forward"] = wf
    else:
        print("\nNOTE: in-sample result. Run with --walk-forward for the number that matters.")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=2, default=str)
    print("saved ->", a.out)


if __name__ == "__main__":
    main()
