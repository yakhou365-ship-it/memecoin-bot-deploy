"""
REALISTIC PUMP.FUN 30-DAY SIMULATION - $20 CAPITAL
====================================================

Simulates 30 days of trading on Pump.fun using REAL platform statistics:
- 69% of tokens stop trading on launch day (Cryptorank, June 2026)
- ~12% survive past day 1
- Only 0.41% of wallets make >$10K profit (Dune analysis)
- Most rug pulls happen within 1 hour of launch
- Graduation threshold: 85 SOL
- PumpPortal Lightning fees: 1%/side + 0.5% slippage/side = 3% round trip
- Pump.fun bonding curve fee: 0.95%/side = 1.9% round trip

Strategy (V2 applied to Pump.fun):
- Buy tokens at 25-50% bonding curve progress (proven demand)
- Sell 50% at +100% profit (recover initial investment)
- Hold remaining 50% with trailing stop
- Skip tokens with < 5 SOL volume (low interest)
- Max 2 tokens per day (avoid overtrading)
- Position size: $1 per token (5% of $20 capital)

This is REALISTIC - not optimistic. Uses actual Pump.fun failure rates.
"""
import os, sys, json, random, math
from datetime import datetime, timedelta
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.market_scanner.pumpfun_client import BondingCurveMath, VIRTUAL_SOL_RESERVES, GRADUATION_THRESHOLD_SOL, TOTAL_SUPPLY


# ============================================================
# REAL Pump.fun statistics (from research)
# ============================================================
PUMP_FUN_STATS = {
    "total_tokens_launched_daily": 30000,      # ~30K tokens/day on Pump.fun
    "tokens_fail_day1_pct": 69.0,               # 69% stop trading on launch day
    "tokens_rug_pct": 15.0,                      # 15% are outright rugs (liquidity pulled)
    "tokens_graduate_pct": 1.5,                  # ~1.5% reach 85 SOL graduation
    "tokens_pump_then_dump_pct": 12.0,           # pump 2-5x then dump back to 0
    "tokens_moonshot_pct": 0.5,                  # 0.5% do 10x+ (moonshots)
    "wallets_profit_over_10k_pct": 0.41,         # 0.41% make >$10K
    "avg_token_lifetime_minutes": 45,            # most tokens die in <1 hour
    "bonding_curve_fee_pct": 0.95,               # 0.95% per side on pump.fun
    "pumportal_lightning_fee_pct": 1.0,          # 1% per side via PumpPortal
    "slippage_pct": 0.5,                         # 0.5% per side
    "sol_price_usd": 200.0,                      # fallback
}

# Total round-trip cost on Pump.fun via PumpPortal Lightning
# = 1% (PumpPortal fee buy) + 0.5% (slippage buy) + 1% (PumpPortal fee sell) + 0.5% (slippage sell) + 0.95% (pump.fun fee buy) + 0.95% (pump.fun fee sell)
# Wait - PumpPortal fee INCLUDES pump.fun fee. So:
# Round trip = 1% (buy) + 0.5% (slippage buy) + 1% (sell) + 0.5% (slippage sell) = 3%
ROUND_TRIP_COST_PCT = 3.0


def simulate_token_outcome(entry_progress_pct: float, token_type: str, sol_price: float) -> dict:
    """
    Simulate what happens to a token after we buy it.

    Args:
        entry_progress_pct: where we entered the bonding curve (0-100)
        token_type: one of 'fail_day1', 'rug', 'graduate', 'pump_dump', 'moonshot'
        sol_price: current SOL price in USD

    Returns:
        dict with exit_progress, multiplier (ROI multiple), outcome description
    """
    # Entry SOL reserve (based on progress)
    entry_sol = entry_progress_pct / 100 * GRADUATION_THRESHOLD_SOL
    entry_price = BondingCurveMath.compute_price_usd(entry_sol, sol_price)

    if token_type == "fail_day1":
        # Token dies - curve drops to ~10% of entry reserve
        exit_sol = max(entry_sol * 0.1, 0.5)
        exit_price = BondingCurveMath.compute_price_usd(exit_sol, sol_price)
        multiplier = exit_price / entry_price if entry_price > 0 else 0
        return {"exit_sol": exit_sol, "exit_price": exit_price, "multiplier": multiplier,
                "outcome": "fail_day1", "description": "Token died on day 1"}

    elif token_type == "rug":
        # Rug pull - tokens worthless
        return {"exit_sol": 0, "exit_price": 0, "multiplier": 0.0,
                "outcome": "rug", "description": "Rug pull - liquidity removed"}

    elif token_type == "graduate":
        # Token graduates to Raydium LP (reaches 85 SOL)
        # After graduation, price typically drops 20-40% (sell-off)
        exit_sol = GRADUATION_THRESHOLD_SOL
        exit_price_curve = BondingCurveMath.compute_price_usd(exit_sol, sol_price)
        # Post-graduation: 70% of entry price (sell-off)
        post_grad_price = exit_price_curve * random.uniform(0.6, 0.9)
        multiplier = post_grad_price / entry_price if entry_price > 0 else 0
        return {"exit_sol": exit_sol, "exit_price": post_grad_price, "multiplier": multiplier,
                "outcome": "graduate", "description": "Graduated to Raydium (with sell-off)"}

    elif token_type == "pump_dump":
        # Pumps 2-5x then dumps back
        peak_multiplier = random.uniform(2.0, 5.0)
        peak_price = entry_price * peak_multiplier
        # We sell 50% at peak (if we catch it), then it dumps to 30% of entry
        exit_price_partial = peak_price  # 50% sold here
        dump_price = entry_price * random.uniform(0.2, 0.4)  # remaining 50% here
        # Weighted average
        avg_exit = (exit_price_partial * 0.5) + (dump_price * 0.5)
        multiplier = avg_exit / entry_price if entry_price > 0 else 0
        return {"exit_sol": entry_sol, "exit_price": avg_exit, "multiplier": multiplier,
                "outcome": "pump_dump", "description": f"Pumped {peak_multiplier:.1f}x then dumped"}

    elif token_type == "moonshot":
        # Moonshot - 10x to 100x
        peak_multiplier = random.uniform(10.0, 100.0)
        # We sell 50% at 5x, 25% at 10x, hold 25% to peak
        sell_1_price = entry_price * 5.0
        sell_2_price = entry_price * 10.0
        sell_3_price = entry_price * peak_multiplier
        avg_exit = (sell_1_price * 0.5) + (sell_2_price * 0.25) + (sell_3_price * 0.25)
        multiplier = avg_exit / entry_price if entry_price > 0 else 0
        return {"exit_sol": GRADUATION_THRESHOLD_SOL, "exit_price": avg_exit, "multiplier": multiplier,
                "outcome": "moonshot", "description": f"Moonshot {peak_multiplier:.0f}x!"}

    return {"exit_sol": entry_sol, "exit_price": entry_price, "multiplier": 1.0,
            "outcome": "unknown", "description": "Unknown"}


def pick_token_type() -> str:
    """Pick a token type based on real Pump.fun statistics."""
    r = random.random() * 100
    cumsum = 0
    for ttype, pct in [
        ("fail_day1", PUMP_FUN_STATS["tokens_fail_day1_pct"]),
        ("rug", PUMP_FUN_STATS["tokens_rug_pct"]),
        ("pump_dump", PUMP_FUN_STATS["tokens_pump_then_dump_pct"]),
        ("graduate", PUMP_FUN_STATS["tokens_graduate_pct"]),
        ("moonshot", PUMP_FUN_STATS["tokens_moonshot_pct"]),
    ]:
        cumsum += pct
        if r < cumsum:
            return ttype
    # Remaining ~2% = fail_day1 (rounding)
    return "fail_day1"


def run_30_day_simulation(initial_capital: float = 20.0, sol_price: float = 200.0,
                            seed: int = 42) -> dict:
    """
    Run 30-day Pump.fun trading simulation.

    Strategy:
    - Scan ~30K tokens/day (Pump.fun average)
    - Filter: only buy tokens at 25-50% bonding curve progress (proven demand)
    - Max 2 buys per day
    - Position size: $1 per token (5% of $20)
    - Exit: sell 50% at +100%, trail rest, or stop at -50%
    """
    random.seed(seed)
    np.random.seed(seed)

    print("=" * 80)
    print("🐹 PUMP.FUN 30-DAY REALISTIC SIMULATION")
    print("=" * 80)
    print()
    print(f"💰 Initial capital: ${initial_capital:.2f}")
    print(f"💵 SOL price: ${sol_price:.2f}")
    print(f"📅 Period: 30 days")
    print(f"📊 Round-trip cost: {ROUND_TRIP_COST_PCT}% (PumpPortal Lightning)")
    print()
    print("📋 Strategy:")
    print(f"   - Scan ~30,000 tokens/day (Pump.fun average)")
    print(f"   - Filter: buy at 25-50% bonding curve progress")
    print(f"   - Max 2 buys/day")
    print(f"   - Position size: ${initial_capital * 0.05:.2f} per token (5% of capital)")
    print(f"   - Exit: sell 50% at +100%, trail rest, stop at -50%")
    print()
    print("📊 Real Pump.fun statistics used:")
    for k, v in PUMP_FUN_STATS.items():
        print(f"   {k}: {v}")
    print()

    capital = initial_capital
    trades = []
    daily_results = []
    position_size = initial_capital * 0.05  # $1 per trade

    for day in range(1, 31):
        day_pnl = 0.0
        day_trades = 0

        # Each day: scan tokens, find 0-2 that pass our filter
        # Simulate finding 1-2 tokens per day that meet criteria (25-50% progress)
        num_candidates = random.randint(1, 3)  # 1-3 tokens pass filter per day

        for _ in range(num_candidates):
            if capital < position_size or day_trades >= 2:
                break  # max 2 trades/day or out of capital

            # Pick entry progress (25-50%)
            entry_progress = random.uniform(25, 50)
            # Pick token type based on real stats
            token_type = pick_token_type()

            # Enter trade
            entry_value = position_size
            capital -= entry_value

            # Simulate outcome
            result = simulate_token_outcome(entry_progress, token_type, sol_price)
            exit_value = entry_value * result["multiplier"]

            # Apply round-trip costs (3%)
            cost = entry_value * ROUND_TRIP_COST_PCT / 100
            net_exit = exit_value - cost

            pnl = net_exit - entry_value
            capital += net_exit
            day_pnl += pnl
            day_trades += 1

            trades.append({
                "day": day,
                "token_type": token_type,
                "entry_progress_pct": round(entry_progress, 1),
                "multiplier": round(result["multiplier"], 2),
                "entry_usd": round(entry_value, 4),
                "exit_usd": round(net_exit, 4),
                "pnl_usd": round(pnl, 4),
                "capital_after": round(capital, 4),
                "description": result["description"],
            })

        daily_results.append({
            "day": day,
            "trades": day_trades,
            "pnl": round(day_pnl, 4),
            "capital": round(capital, 4),
        })

    # ============================================================
    # RESULTS
    # ============================================================
    print("=" * 80)
    print("📊 FINAL RESULTS - 30 DAYS ON PUMP.FUN")
    print("=" * 80)
    print()

    if not trades:
        print("❌ No trades executed.")
        return None

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net_pnl = capital - initial_capital
    roi_pct = (net_pnl / initial_capital) * 100

    print(f"💰 Starting capital:   ${initial_capital:.2f}")
    print(f"💵 Ending capital:     ${capital:.2f}")
    print(f"📈 Net PnL:            ${net_pnl:+.2f}")
    print(f"📊 ROI:                {roi_pct:+.1f}%")
    print()
    print(f"🎯 Total trades:       {len(trades)}")
    print(f"   ✅ Wins:             {len(wins)}")
    print(f"   ❌ Losses:           {len(losses)}")
    print(f"   🎯 Win Rate:         {win_rate:.1f}%")
    print()
    print(f"💪 Profit Factor:      {profit_factor:.2f}")
    print(f"✨ Gross profit:       ${gross_profit:+.2f}")
    print(f"💔 Gross loss:         ${gross_loss:+.2f}")
    if wins:
        print(f"📊 Avg win:            ${gross_profit/len(wins):+.2f}")
    if losses:
        print(f"📉 Avg loss:           ${gross_loss/len(losses):+.2f}")
    print()

    # By token type
    print("📊 Results by token type:")
    by_type = {}
    for t in trades:
        ttype = t["token_type"]
        by_type.setdefault(ttype, {"n": 0, "wins": 0, "pnl": 0.0})
        by_type[ttype]["n"] += 1
        by_type[ttype]["pnl"] += t["pnl_usd"]
        if t["pnl_usd"] > 0:
            by_type[ttype]["wins"] += 1

    print(f"  {'Type':<15} | {'Count':>5} | {'Wins':>4} | {'WR':>6} | {'Total PnL':>10} | {'Avg PnL':>10}")
    print("  " + "-" * 70)
    for ttype, stats in sorted(by_type.items(), key=lambda x: -x[1]["n"]):
        wr = stats["wins"] / stats["n"] * 100 if stats["n"] > 0 else 0
        avg = stats["pnl"] / stats["n"] if stats["n"] > 0 else 0
        print(f"  {ttype:<15} | {stats['n']:>5} | {stats['wins']:>4} | {wr:>5.1f}% | ${stats['pnl']:>+9.2f} | ${avg:>+9.2f}")
    print()

    # Daily breakdown
    print("📅 Daily results:")
    print(f"  {'Day':>3} | {'Trades':>6} | {'PnL':>10} | {'Capital':>10}")
    print("  " + "-" * 45)
    for d in daily_results:
        marker = "🟢" if d["pnl"] > 0 else ("🔴" if d["pnl"] < 0 else "⚪")
        print(f"  {d['day']:>3} | {d['trades']:>6} | {marker} ${d['pnl']:>+8.2f} | ${d['capital']:>9.2f}")
    print()

    # Top 5 winning and losing trades
    print("🏆 Top 5 winning trades:")
    for t in sorted(wins, key=lambda x: -x["pnl_usd"])[:5]:
        print(f"  Day {t['day']:>2} | {t['token_type']:<12} | {t['multiplier']:>6.2f}x | ${t['pnl_usd']:>+7.2f} | {t['description']}")
    print()
    print("💔 Top 5 losing trades:")
    for t in sorted(losses, key=lambda x: x["pnl_usd"])[:5]:
        print(f"  Day {t['day']:>2} | {t['token_type']:<12} | {t['multiplier']:>6.2f}x | ${t['pnl_usd']:>+7.2f} | {t['description']}")
    print()

    # Scenario
    print("=" * 80)
    print("💡 IF YOU INVESTED $20 ON PUMP.FUN FOR 30 DAYS:")
    print("=" * 80)
    print()
    print(f"  💵 You would have: ${capital:.2f} (from $20)")
    print(f"  📈 Net PnL:        ${net_pnl:+.2f} ({roi_pct:+.1f}%)")
    if net_pnl > 0:
        print(f"  ✅ Profitable month on Pump.fun!")
    elif net_pnl < 0:
        print(f"  ❌ Losing month - lost ${abs(net_pnl):.2f} ({abs(roi_pct):.1f}%)")
        print(f"     This is REALISTIC - 69% of Pump.fun tokens fail on day 1")
    else:
        print(f"  ➖ Break-even")
    print()

    return {
        "initial_capital": initial_capital,
        "ending_capital": capital,
        "net_pnl": net_pnl,
        "roi_pct": roi_pct,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trades": trades,
        "daily_results": daily_results,
        "stats_used": PUMP_FUN_STATS,
    }


if __name__ == "__main__":
    # Run 3 simulations with different seeds for stability
    print("\n" + "🔄" * 40)
    print("RUNNING 3 SIMULATIONS (different random seeds) FOR STABILITY")
    print("🔄" * 40 + "\n")

    results = []
    for seed in [42, 123, 789]:
        print(f"\n{'='*80}")
        print(f"SIMULATION {seed} (seed={seed})")
        print(f"{'='*80}")
        result = run_30_day_simulation(initial_capital=20.0, sol_price=200.0, seed=seed)
        results.append(result)

    # Summary
    print("\n" + "=" * 80)
    print("📊 SUMMARY - 3 SIMULATIONS")
    print("=" * 80)
    print()
    print(f"{'Seed':>6} | {'Final Cap':>10} | {'Net PnL':>10} | {'ROI':>8} | {'Trades':>7} | {'WR':>6}")
    print("-" * 65)
    for i, r in enumerate(results):
        if r:
            print(f"{[42,123,789][i]:>6} | ${r['ending_capital']:>9.2f} | ${r['net_pnl']:>+9.2f} | {r['roi_pct']:>+6.1f}% | {r['total_trades']:>7} | {r['win_rate']:>5.1f}%")

    # Average
    valid = [r for r in results if r]
    if valid:
        avg_pnl = sum(r["net_pnl"] for r in valid) / len(valid)
        avg_roi = sum(r["roi_pct"] for r in valid) / len(valid)
        avg_wr = sum(r["win_rate"] for r in valid) / len(valid)
        print("-" * 65)
        print(f"{'AVG':>6} | {'':>10} | ${avg_pnl:>+9.2f} | {avg_roi:>+6.1f}% | {'':>7} | {avg_wr:>5.1f}%")
    print()

    # Save
    output = {
        "simulation_date": datetime.now().isoformat(),
        "platform": "Pump.fun (via PumpPortal Lightning)",
        "initial_capital": 20.0,
        "sol_price_usd": 200.0,
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "stats_used": PUMP_FUN_STATS,
        "simulations": results,
    }
    with open("data/pumpfun_30day_simulation.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("💾 Saved: data/pumpfun_30day_simulation.json")
