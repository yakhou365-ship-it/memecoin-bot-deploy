"""
V540 - REAL $20 SIMULATION OF THE MEMECOIN BOT
================================================
Part A: Pump.fun sniper Monte-Carlo, $20, 30 days, 20 seeds
        - Real platform statistics (Cryptorank/Dune research used by the repo)
        - The bot's ACTUAL sniper-profile exit ladder (quick TP +15%,
          moonshot +50%/trail 30%, sudden_dump +10%/-20%, SL -8%, mega TP 10x)
        - Real Pump.fun bonding-curve math (price ~ (30+sol)^2)
        - Real costs: PumpPortal Lightning 1%/side fee + 0.5%/side slippage (~3% RT)
        - Current SOL price from CoinGecko (live)
Part B: Strategy V2 backtest on FRESH Binance 15m candles (last 30 days), $20
        - Real fees for Binance spot (0.1%/side + 0.05% slippage)
        - Honest min-notional constraint ($5) + theoretical run ($0.5)

Outputs -> /home/z/my-project/download/bot_20usd_simulation/
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np

BACKEND = "/home/z/my-project/backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)  # data/cache etc.

OUT = "/home/z/my-project/download/bot_20usd_simulation"
os.makedirs(OUT, exist_ok=True)

# ----------------------------------------------------------------------
# Bot configuration (from app/core/config.py -> sniper profile, default)
# ----------------------------------------------------------------------
SNIPER = {
    "trade_pct": 0.10,             # sniper_trade_pct = 10% of equity per trade
    "quick_tp": 0.15,              # sniper_quick_take_profit_pct
    "moonshot_threshold": 0.50,    # sniper_moonshot_threshold
    "moonshot_trail": 0.30,        # trailing_tiers[0] = (0.50, 0.30)
    "sudden_dump_min_gain": 0.10,  # sudden_dump_min_gain (sniper)
    "sudden_dump_drop": 0.20,      # sudden_dump_drop_threshold (sniper)
    "stop_loss": 0.08,             # sniper_stop_loss_pct
    "mega_tp": 10.0,               # 1000% cap
    "max_positions": 8,            # sniper_max_positions
    "daily_cap_pct": 0.60,         # sniper_daily_cap_pct
    "daily_loss_pct": 0.20,        # sniper_loss_pct (stop for the day)
    "min_trade_usd": 0.42,         # v300_min_trade_sol = 0.005 SOL @ ~$83
}
# PumpPortal Lightning: 1% fee/side + 0.5% slippage/side
# net_multiple = gross * (0.995*0.99) * (0.99/1.005) ~= gross * 0.9703
RT_DRAG = (0.995 * 0.99) * (0.99 / 1.005)

# Real Pump.fun statistics (research embedded in pumpfun_30day_backtest.py)
PUMP_STATS = [
    ("fail_day1", 69.0),
    ("rug", 15.0),
    ("pump_dump", 12.0),
    ("graduate", 1.5),
    ("moonshot", 0.5),
]  # remaining ~2% -> fail_day1

VIRTUAL_SOL = 30.0
GRAD_SOL = 85.0
TOTAL_SUPPLY = 1_073_000_000.0


def curve_price_sol(real_sol: float) -> float:
    """Pump.fun constant-product curve: price ~ (V+real)^2 / (VTR*V)."""
    if real_sol <= 0:
        return VIRTUAL_SOL / TOTAL_SUPPLY
    tokens_bought = TOTAL_SUPPLY * real_sol / (VIRTUAL_SOL + real_sol)
    eff_tokens = TOTAL_SUPPLY - tokens_bought
    return (VIRTUAL_SOL + real_sol) / eff_tokens


def sol_price_live() -> float:
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return float(json.loads(r.read())["solana"]["usd"])
    except Exception:
        return 83.34


# ----------------------------------------------------------------------
# Token price-path generators (checkpoints = price-feed updates)
# ----------------------------------------------------------------------
def path_fail_day1(rng):
    """69% of tokens: brief blip, then die to ~5-15% of entry."""
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
    """15% rugs: 60% bait-pump then liquidity pulled; 40% instant dump."""
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
    pts = [(0.0, 1.0), (0.4, rng.uniform(0.85, 1.0)), (2.0, rng.uniform(0.0, 0.12)), (5.0, 0.01)]
    return pts


def path_pump_dump(rng):
    """12% pump 2-5x then dump to 20-40% of entry."""
    peak = rng.uniform(2.0, 5.0)
    t_peak = rng.uniform(5.0, 60.0)
    explosive = rng.random() < 0.25  # fast pumps gap past the quick-TP band
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
    """1.5% graduate to Raydium at 85 SOL. Entry at 25-50% curve progress.
    price ratio = ((30+85)/(30+entry_sol))^2."""
    entry_sol = rng.uniform(25.0, 50.0) / 100.0 * GRAD_SOL
    mult = curve_price_sol(GRAD_SOL) / curve_price_sol(entry_sol)  # 2.5x-5x
    post_grad = mult * rng.uniform(0.60, 0.90)  # sell-off after graduation
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
    """0.5% moonshots 10-100x. Explosive phases gap the quick-TP band."""
    peak = rng.uniform(10.0, 100.0)
    t_peak = rng.uniform(10.0, 300.0)
    pts = [(0.0, 1.0)]
    # explosive phases = few large steps (feed cadence gaps)
    pts.append((t_peak * 0.15, rng.uniform(1.4, 3.0)))
    pts.append((t_peak * 0.4, peak * rng.uniform(0.4, 0.7)))
    pts.append((t_peak * 0.75, peak * rng.uniform(0.8, 0.95)))
    pts.append((t_peak, peak))
    # after peak: dump (sudden_dump/trailing will fire before this matters)
    m = 6
    for k in range(1, m + 1):
        f = k / m
        pts.append((t_peak + 60 * f, max(peak * (1.0 - 0.8 * f ** 0.5), 0.02)))
    return pts


PATH_MAKERS = {
    "fail_day1": path_fail_day1,
    "rug": path_rug,
    "pump_dump": path_pump_dump,
    "graduate": path_graduate,
    "moonshot": path_moonshot,
}


def pick_token_type(rng):
    r = rng.random() * 100.0
    cum = 0.0
    for ttype, pct in PUMP_STATS:
        cum += pct
        if r < cum:
            return ttype
    return "fail_day1"


# ----------------------------------------------------------------------
# The bot's ACTUAL sniper exit ladder (paper_trading/engine.py V500 order)
# ----------------------------------------------------------------------
def sniper_exit(pts, rng):
    """Walk price checkpoints; return (gross_exit_multiple, reason)."""
    peak_seen = 1.0
    sl_price = 1.0 - SNIPER["stop_loss"]
    qtp = 1.0 + SNIPER["quick_tp"]
    for _, p in pts:
        peak_seen = max(peak_seen, p)
        peak_gain = peak_seen - 1.0
        drop = (peak_seen - p) / peak_seen
        # 1. sudden dump: peak_gain >= 10% and drop >= 20% from peak
        if peak_gain >= SNIPER["sudden_dump_min_gain"] and drop >= SNIPER["sudden_dump_drop"]:
            return p * rng.uniform(0.97, 1.0), "sudden_dump"
        # 2. hard SL (market sell after detection -> small extra slippage)
        if p <= sl_price:
            return p * rng.uniform(0.95, 1.0), "stop_loss"
        # 3. mega TP (10x cap)
        if p >= SNIPER["mega_tp"]:
            return p, "mega_tp"
        # 4a. quick TP: peak in [+15%, +50%) -> exit at current
        if SNIPER["quick_tp"] <= peak_gain < SNIPER["moonshot_threshold"]:
            return p * rng.uniform(0.99, 1.0), "quick_tp"
        # 4b. moonshot trail handled by sudden_dump (20% drop fires first)
    # 5/6. stagnation / time exit
    return pts[-1][1] * rng.uniform(0.97, 1.0), "time_exit"


# ----------------------------------------------------------------------
# Part A: 30-day Monte-Carlo with $20
# ----------------------------------------------------------------------
def run_30days(seed: int, initial: float, candidates: tuple, max_buys: int):
    rng = np.random.default_rng(seed)
    capital = initial
    curve = [capital]
    trades = []
    for day in range(30):
        day_start = capital
        spent_today = 0.0
        n_cand = int(rng.integers(candidates[0], candidates[1] + 1))
        buys = 0
        for _ in range(n_cand):
            if buys >= max_buys or capital < 1.0:
                break
            size = capital * SNIPER["trade_pct"]
            if size < SNIPER["min_trade_usd"]:
                break
            # daily cap 60% & daily loss limit 20%
            if spent_today + size > day_start * SNIPER["daily_cap_pct"]:
                break
            if day_start - capital >= day_start * SNIPER["daily_loss_pct"]:
                break
            ttype = pick_token_type(rng)
            pts = PATH_MAKERS[ttype](rng)
            gross, reason = sniper_exit(pts, rng)
            net_mult = gross * RT_DRAG
            pnl = size * (net_mult - 1.0)
            capital += pnl
            spent_today += size
            buys += 1
            trades.append({
                "day": day + 1, "type": ttype, "reason": reason,
                "gross_mult": round(float(gross), 3),
                "net_mult": round(float(net_mult), 3),
                "size": round(float(size), 3), "pnl": round(float(pnl), 4),
            })
            if capital <= 0.01:
                capital = 0.01
                curve.append(capital)
                return capital, curve, trades
        curve.append(capital)
    return capital, curve, trades


def scenario_stats(results, initial):
    finals = np.array([r["final"] for r in results])
    n_tr = np.array([len(r["trades"]) for r in results])
    wr = []
    for r in results:
        if r["trades"]:
            wr.append(100.0 * sum(1 for t in r["trades"] if t["pnl"] > 0) / len(r["trades"]))
    return {
        "final_median": float(np.median(finals)),
        "final_mean": float(np.mean(finals)),
        "final_p5": float(np.percentile(finals, 5)),
        "final_p25": float(np.percentile(finals, 25)),
        "final_p75": float(np.percentile(finals, 75)),
        "final_p95": float(np.percentile(finals, 95)),
        "final_min": float(finals.min()),
        "final_max": float(finals.max()),
        "roi_median_pct": float((np.median(finals) / initial - 1) * 100),
        "prob_profit_pct": float(100.0 * (finals > initial).mean()),
        "prob_lose_half_pct": float(100.0 * (finals < initial * 0.5).mean()),
        "prob_ruin_pct": float(100.0 * (finals < initial * 0.2).mean()),
        "avg_trades": float(n_tr.mean()),
        "avg_win_rate": float(np.mean(wr)) if wr else 0.0,
    }


def aggregate_trades(results):
    agg = {}
    for r in results:
        for t in r["trades"]:
            k = t["type"]
            agg.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
            agg[k]["n"] += 1
            agg[k]["pnl"] += t["pnl"]
            agg[k]["wins"] += int(t["pnl"] > 0)
    reasons = {}
    for r in results:
        for t in r["trades"]:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return agg, reasons


# ----------------------------------------------------------------------
# Part B: fresh Binance data + Strategy V2 backtest, $20
# ----------------------------------------------------------------------
def fetch_binance(symbol: str, interval: str = "15m", days: int = 30) -> object:
    import pandas as pd
    step_ms = {"15m": 900_000}[interval]
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows = []
    url0 = "https://api.binance.com/api/v3/klines"
    while start < end:
        url = f"{url0}?symbol={symbol}&interval={interval}&startTime={start}&limit=1000"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = json.loads(r.read())
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
                raw = None
        if not raw:
            break
        rows += raw
        start = raw[-1][0] + step_ms
        if len(raw) < 1000:
            break
        time.sleep(0.12)
    if not rows:
        return None
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["ts", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    df = df.astype(float)
    df = df[~df.index.duplicated()].iloc[:-1]  # drop forming candle
    return df


def run_part_b(initial=20.0):
    from app.modules.strategy_v2.config import VENUES, VenueCosts, StrategyConfig
    from app.modules.strategy_v2.backtester import run_backtest

    # Binance spot: 0.1% fee/side + 0.05% slippage/side (no BNB discount) = 0.3% RT
    VENUES["binance"] = VenueCosts("binance", 0.001, 0.0005)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
               "PEPEUSDT", "SHIBUSDT", "BONKUSDT", "WIFUSDT"]
    data = {}
    for s in symbols:
        df = fetch_binance(s, "15m", 30)
        if df is not None and len(df) >= 2000:
            data[s] = df
            print(f"  {s}: {len(df)} candles  ({df.index[0]} -> {df.index[-1]})")
        else:
            print(f"  {s}: SKIPPED (insufficient data)")

    out = {"symbols": list(data.keys()), "candles": {s: len(d) for s, d in data.items()}}
    if not data:
        return out

    for label, min_notional in [("realistic_min_notional_5usd", 5.0),
                                ("theoretical_min_notional_0_5usd", 0.5)]:
        cfg = StrategyConfig().with_(venue="binance", min_notional_usd=min_notional)
        res = run_backtest(data, cfg, "BTCUSDT", initial)
        st = res["stats"]
        pf = res["portfolio"]
        final = pf["equity"]
        out[label] = {
            "initial": initial,
            "final": round(final, 4),
            "roi_pct": round((final / initial - 1) * 100, 3),
            "n_trades": st.get("n", 0),
            "win_rate": round(st.get("win_rate", 0) * 100, 2),
            "profit_factor": round(st.get("profit_factor", 0), 3) if st.get("n") else None,
            "max_drawdown_pct": round(st.get("max_drawdown_pct", 0), 2),
            "skipped": st.get("skipped", {}),
            "exit_reasons": st.get("exit_reasons", {}),
        }
        print(f"  [{label}] trades={st.get('n',0)} final=${final:.2f} "
              f"roi={(final/initial-1)*100:+.1f}% skipped={st.get('skipped',{})}")
    return out


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    t0 = time.time()
    sol_usd = sol_price_live()
    print("=" * 72)
    print("REAL $20 SIMULATION - MEMECOIN BOT (sniper profile, live settings)")
    print("=" * 72)
    print(f"Live SOL price: ${sol_usd:.2f}")
    print(f"Round-trip cost drag: {(1 - RT_DRAG) * 100:.2f}%")
    print()

    initial = 20.0
    seeds = list(range(1, 21))  # 20 Monte-Carlo runs

    scenarios = {
        "baseline_1_3_candidates_max2": {"candidates": (1, 3), "max_buys": 2},
        "bot_config_limits_max6": {"candidates": (2, 6), "max_buys": 6},
    }

    results_all = {}
    for name, sc in scenarios.items():
        print(f"--- Scenario: {name} (candidates/day {sc['candidates']}, max buys {sc['max_buys']}) ---")
        runs = []
        for seed in seeds:
            final, curve, trades = run_30days(seed, initial, sc["candidates"], sc["max_buys"])
            runs.append({"seed": seed, "final": final, "curve": curve, "trades": trades})
        results_all[name] = runs
        st = scenario_stats(runs, initial)
        agg, reasons = aggregate_trades(runs)
        results_all[name + "__stats"] = st
        results_all[name + "__by_type"] = agg
        results_all[name + "__exit_reasons"] = reasons
        print(f"  median final: ${st['final_median']:.2f} ({st['roi_median_pct']:+.1f}%) | "
              f"P(profit): {st['prob_profit_pct']:.0f}% | P(lose>50%): {st['prob_lose_half_pct']:.0f}%")
        print(f"  p5=${st['final_p5']:.2f} p95=${st['final_p95']:.2f} | "
              f"avg trades: {st['avg_trades']:.0f} | avg WR: {st['avg_win_rate']:.1f}%")
        for k, v in sorted(agg.items(), key=lambda x: -x[1]["n"]):
            wr = 100 * v["wins"] / v["n"]
            print(f"    {k:<11} n={v['n']:>4}  WR={wr:>5.1f}%  totalPnL=${v['pnl']:>+8.2f}")
        print(f"    exit reasons: {reasons}")
        print()

    print("--- Part B: Strategy V2 on FRESH Binance 15m data (30 days) ---")
    part_b = run_part_b(initial)
    print()

    # ---------------- save ----------------
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bot_version": "0.7.2 (V538)",
        "profile": "sniper (default)",
        "initial_capital_usd": initial,
        "sol_price_usd": sol_usd,
        "round_trip_drag_pct": round((1 - RT_DRAG) * 100, 3),
        "sniper_params": SNIPER,
        "pumpfun_stats_pct": PUMP_STATS,
        "scenarios": {},
        "part_b_binance_v2": part_b,
    }
    for name in scenarios:
        out["scenarios"][name] = {
            "stats": results_all[name + "__stats"],
            "by_token_type": results_all[name + "__by_type"],
            "exit_reasons": results_all[name + "__exit_reasons"],
            "runs_summary": [
                {"seed": r["seed"], "final": round(r["final"], 4),
                 "n_trades": len(r["trades"]),
                 "wins": sum(1 for t in r["trades"] if t["pnl"] > 0)}
                for r in results_all[name]
            ],
        }

    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)

    # curves for charts
    np.save(os.path.join(OUT, "curves_baseline.npy"),
            np.array([r["curve"] for r in results_all["baseline_1_3_candidates_max2"]]))
    np.save(os.path.join(OUT, "curves_botmax.npy"),
            np.array([r["curve"] for r in results_all["bot_config_limits_max6"]]))

    print(f"Saved results.json + curves to {OUT}")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
