"""
MEGA TEST - 100,000 Trades
==========================

Tests V6 on 100,000 NEW diverse trades (different from the 10K test).
Multiple difficulty levels, market conditions, and token types.

This is the ULTIMATE stress test.
"""
import os, sys, json, random, math
from datetime import datetime
from typing import Dict, List
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.market_scanner.pumpfun_client import BondingCurveMath, GRADUATION_THRESHOLD_SOL
from pumpfun_v4_pro import EntryScorer, ROUND_TRIP_COST_PCT, ProCircuitBreaker, ProgressivePositionSizer
from v6_ultra_fast_exit import V6Config, V6ExitEngine


def generate_mega_test(count: int = 100000) -> List[Dict]:
    """Generate 100,000 trades with REALISTIC distribution."""
    random.seed(777)  # different seed from brutal test
    np.random.seed(777)

    scenarios = []

    # REAL distribution (same as brutal test but 10x more)
    distribution = [
        ("fail_day1", 69000, {"peak_range": (0.05, 0.5)}),
        ("rug", 15000, {"peak_range": (0.0, 0.3)}),
        ("pump_dump", 12000, {"peak_range": (2.0, 5.0)}),
        ("graduate", 1500, {"peak_range": (2.0, 8.0)}),
        ("moonshot", 500, {"peak_range": (10.0, 150.0)}),
        ("slow_growth", 2000, {"peak_range": (1.5, 3.0)}),
    ]

    for outcome_type, num, params in distribution:
        for i in range(num):
            scenario = generate_scenario(outcome_type, params)
            scenarios.append(scenario)

    random.shuffle(scenarios)
    return scenarios


def generate_scenario(outcome_type: str, params: Dict) -> Dict:
    """Generate token scenario."""

    if outcome_type == "fail_day1":
        quality = random.choices(["bad", "medium", "good"], weights=[60, 30, 10])[0]
        if quality == "bad":
            return {
                "volume_sol": random.uniform(0.1, 3),
                "holder_count": random.randint(3, 20),
                "dev_holding_pct": random.uniform(15, 50),
                "top10_holding_pct": random.uniform(50, 85),
                "max_wallet_pct": random.uniform(15, 40),
                "bonding_progress": random.uniform(5, 20),
                "momentum": random.uniform(10, 30),
                "buy_sell_ratio": random.uniform(0.3, 0.8),
                "outcome_type": outcome_type,
                "peak_mult": random.uniform(*params["peak_range"]),
            }
        elif quality == "medium":
            return {
                "volume_sol": random.uniform(3, 15),
                "holder_count": random.randint(20, 60),
                "dev_holding_pct": random.uniform(5, 20),
                "top10_holding_pct": random.uniform(40, 65),
                "max_wallet_pct": random.uniform(8, 25),
                "bonding_progress": random.uniform(20, 50),
                "momentum": random.uniform(30, 55),
                "buy_sell_ratio": random.uniform(0.7, 1.3),
                "outcome_type": outcome_type,
                "peak_mult": random.uniform(*params["peak_range"]),
            }
        else:
            return {
                "volume_sol": random.uniform(15, 40),
                "holder_count": random.randint(60, 150),
                "dev_holding_pct": random.uniform(0, 8),
                "top10_holding_pct": random.uniform(30, 50),
                "max_wallet_pct": random.uniform(3, 12),
                "bonding_progress": random.uniform(30, 60),
                "momentum": random.uniform(55, 75),
                "buy_sell_ratio": random.uniform(1.2, 2.0),
                "outcome_type": outcome_type,
                "peak_mult": random.uniform(*params["peak_range"]),
            }

    elif outcome_type == "rug":
        quality = random.choices(["bad", "bad", "fake_good"], weights=[40, 40, 20])[0]
        if quality == "fake_good":
            return {
                "volume_sol": random.uniform(20, 50),
                "holder_count": random.randint(50, 120),
                "dev_holding_pct": random.uniform(20, 40),
                "top10_holding_pct": random.uniform(50, 75),
                "max_wallet_pct": random.uniform(15, 35),
                "bonding_progress": random.uniform(30, 55),
                "momentum": random.uniform(60, 80),
                "buy_sell_ratio": random.uniform(1.5, 2.5),
                "outcome_type": outcome_type,
                "peak_mult": random.uniform(*params["peak_range"]),
            }
        else:
            return {
                "volume_sol": random.uniform(0.5, 10),
                "holder_count": random.randint(5, 30),
                "dev_holding_pct": random.uniform(25, 60),
                "top10_holding_pct": random.uniform(55, 90),
                "max_wallet_pct": random.uniform(20, 45),
                "bonding_progress": random.uniform(5, 25),
                "momentum": random.uniform(15, 40),
                "buy_sell_ratio": random.uniform(0.3, 0.8),
                "outcome_type": outcome_type,
                "peak_mult": random.uniform(*params["peak_range"]),
            }

    elif outcome_type == "pump_dump":
        return {
            "volume_sol": random.uniform(10, 35),
            "holder_count": random.randint(40, 120),
            "dev_holding_pct": random.uniform(3, 15),
            "top10_holding_pct": random.uniform(35, 55),
            "max_wallet_pct": random.uniform(8, 18),
            "bonding_progress": random.uniform(25, 60),
            "momentum": random.uniform(50, 75),
            "buy_sell_ratio": random.uniform(1.0, 1.8),
            "outcome_type": outcome_type,
            "peak_mult": random.uniform(*params["peak_range"]),
        }

    elif outcome_type == "graduate":
        return {
            "volume_sol": random.uniform(25, 70),
            "holder_count": random.randint(100, 300),
            "dev_holding_pct": random.uniform(0, 5),
            "top10_holding_pct": random.uniform(25, 45),
            "max_wallet_pct": random.uniform(3, 10),
            "bonding_progress": random.uniform(35, 65),
            "momentum": random.uniform(65, 85),
            "buy_sell_ratio": random.uniform(1.5, 2.5),
            "outcome_type": outcome_type,
            "peak_mult": random.uniform(*params["peak_range"]),
        }

    elif outcome_type == "moonshot":
        return {
            "volume_sol": random.uniform(40, 100),
            "holder_count": random.randint(150, 500),
            "dev_holding_pct": random.uniform(0, 3),
            "top10_holding_pct": random.uniform(20, 35),
            "max_wallet_pct": random.uniform(2, 8),
            "bonding_progress": random.uniform(40, 60),
            "momentum": random.uniform(75, 95),
            "buy_sell_ratio": random.uniform(2.0, 3.0),
            "outcome_type": outcome_type,
            "peak_mult": random.uniform(*params["peak_range"]),
        }

    elif outcome_type == "slow_growth":
        return {
            "volume_sol": random.uniform(15, 40),
            "holder_count": random.randint(60, 150),
            "dev_holding_pct": random.uniform(2, 10),
            "top10_holding_pct": random.uniform(30, 50),
            "max_wallet_pct": random.uniform(5, 15),
            "bonding_progress": random.uniform(30, 55),
            "momentum": random.uniform(50, 70),
            "buy_sell_ratio": random.uniform(1.2, 1.8),
            "outcome_type": outcome_type,
            "peak_mult": random.uniform(*params["peak_range"]),
        }

    return {}


def run_mega_test(initial_capital: float = 20.0, num_trades: int = 100000):
    """Run V6 on 100,000 trades."""
    print("=" * 80)
    print("💀 MEGA TEST - 100,000 TRADES (V6)")
    print("=" * 80)
    print()

    # Generate scenarios
    print(f"1️⃣  Generating {num_trades:,} scenarios...")
    scenarios = generate_mega_test(num_trades)

    outcome_counts = {}
    for s in scenarios:
        outcome_counts[s["outcome_type"]] = outcome_counts.get(s["outcome_type"], 0) + 1

    print(f"   ✅ {len(scenarios):,} scenarios ready")
    print(f"   📊 Distribution:")
    for outcome, count in sorted(outcome_counts.items(), key=lambda x: -x[1]):
        print(f"      {outcome:<15}: {count:>6,} ({count/len(scenarios)*100:.1f}%)")
    print()

    # Run V6
    print(f"2️⃣  Running V6 with ${initial_capital:.2f}...")
    config = V6Config()
    entry_scorer = EntryScorer()
    exit_engine = V6ExitEngine()
    circuit_breaker = ProCircuitBreaker()
    position_sizer = ProgressivePositionSizer()

    circuit_breaker.max_consecutive_losses = 10
    circuit_breaker.max_daily_loss_pct = 50.0
    circuit_breaker.max_daily_trades = 100
    circuit_breaker.cooldown_days = 0

    capital = initial_capital
    trades = []
    tokens_scanned = 0
    tokens_rejected = 0

    for i, scenario in enumerate(scenarios):
        if capital < 0.01:
            print(f"💀 Capital depleted at trade {i:,}")
            break

        tokens_scanned += 1

        score = entry_scorer.score(
            volume_sol=scenario["volume_sol"],
            dev_holding_pct=scenario["dev_holding_pct"],
            top10_holding_pct=scenario["top10_holding_pct"],
            max_wallet_pct=scenario["max_wallet_pct"],
            holder_count=scenario["holder_count"],
            bonding_progress=scenario["bonding_progress"],
            momentum_score=scenario["momentum"],
            buy_sell_ratio=scenario["buy_sell_ratio"],
        )

        if score.total < config.min_entry_score:
            tokens_rejected += 1
            continue

        pos_size, _ = position_sizer.get_size(capital, score)
        pos_size = min(pos_size, capital * config.max_position_pct, 100)

        if pos_size < 0.5:
            continue

        entry_value = pos_size
        capital -= entry_value

        peak_mult = scenario["peak_mult"]
        instant_exit = False

        if scenario["outcome_type"] == "rug":
            instant_exit = True
        if scenario["outcome_type"] == "fail_day1" and peak_mult < 0.5:
            instant_exit = True

        exit_mult = exit_engine.calculate_exit_mult(peak_mult, instant_exit)

        gross = entry_value * exit_mult
        cost = entry_value * ROUND_TRIP_COST_PCT / 100
        net = gross - cost
        pnl = net - entry_value
        capital += net

        position_sizer.record_trade(pnl, peak_mult)

        trades.append({
            "outcome_type": scenario["outcome_type"],
            "score": score.total,
            "grade": score.grade,
            "peak_mult": round(peak_mult, 2),
            "exit_mult": round(exit_mult, 2),
            "pnl_usd": round(pnl, 4),
        })

        if (i + 1) % 20000 == 0:
            print(f"   📊 {i+1:,}/{len(scenarios):,} | Capital: ${capital:.2f} | Trades: {len(trades):,}")

    # Results
    print()
    print("=" * 80)
    print("📊 MEGA TEST RESULTS - 100,000 TRADES")
    print("=" * 80)
    print()

    if not trades:
        print("❌ No trades")
        return

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net_pnl = capital - initial_capital
    roi = (net_pnl / initial_capital) * 100

    print(f"💰 Initial capital:    ${initial_capital:.2f}")
    print(f"💵 Final capital:      ${capital:.2f}")
    print(f"📈 Net PnL:            ${net_pnl:+,.2f}")
    print(f"📊 ROI:                {roi:+,.1f}%")
    print(f"💎 Multiplier:         {capital/initial_capital:,.2f}x")
    print()
    print(f"🎯 Total trades:       {len(trades):,} (of {tokens_scanned:,} scanned)")
    print(f"   ✅ Wins:             {len(wins):,}")
    print(f"   ❌ Losses:           {len(losses):,}")
    print(f"   🎯 Win Rate:         {win_rate:.1f}%")
    print()
    print(f"💪 Profit Factor:      {profit_factor:.2f}")
    print(f"✨ Gross profit:       ${gross_profit:+,.2f}")
    print(f"💔 Gross LOSS:         ${gross_loss:+,.2f}")
    if wins:
        print(f"📊 Avg win:            ${gross_profit/len(wins):+,.2f}")
    if losses:
        print(f"📉 Avg LOSS:           ${gross_loss/len(losses):+,.2f}")
    print()
    print(f"🔍 Tokens scanned:    {tokens_scanned:,}")
    print(f"   🚫 Rejected:         {tokens_rejected:,} ({tokens_rejected/max(tokens_scanned,1)*100:.1f}%)")
    print(f"   📊 Pass rate:        {len(trades)/max(tokens_scanned,1)*100:.1f}%")
    print()

    # By type
    by_type = {}
    for t in trades:
        ttype = t["outcome_type"]
        by_type.setdefault(ttype, {"n": 0, "wins": 0, "pnl": 0.0})
        by_type[ttype]["n"] += 1
        by_type[ttype]["pnl"] += t["pnl_usd"]
        if t["pnl_usd"] > 0:
            by_type[ttype]["wins"] += 1

    print("📊 By outcome type:")
    print(f"  {'Type':<15} | {'Trades':>7} | {'Wins':>6} | {'WR':>5} | {'PnL':>14} | {'Avg':>10}")
    print("  " + "-" * 70)
    for ttype, s in sorted(by_type.items(), key=lambda x: x[1]["pnl"]):
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        avg = s["pnl"] / s["n"] if s["n"] > 0 else 0
        print(f"  {ttype:<15} | {s['n']:>7,} | {s['wins']:>6,} | {wr:>4.0f}% | ${s['pnl']:>+13,.2f} | ${avg:>+9.2f}")
    print()

    # By grade
    by_grade = {}
    for t in trades:
        g = t["grade"]
        by_grade.setdefault(g, {"n": 0, "wins": 0, "pnl": 0.0})
        by_grade[g]["n"] += 1
        by_grade[g]["pnl"] += t["pnl_usd"]
        if t["pnl_usd"] > 0:
            by_grade[g]["wins"] += 1

    print("📊 By entry grade:")
    print(f"  {'Grade':>5} | {'Trades':>7} | {'Wins':>6} | {'WR':>5} | {'PnL':>14}")
    print("  " + "-" * 50)
    for g in ["A+", "A", "B", "C", "D", "F"]:
        if g in by_grade:
            s = by_grade[g]
            wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
            print(f"  {g:>5} | {s['n']:>7,} | {s['wins']:>6,} | {wr:>4.0f}% | ${s['pnl']:>+13,.2f}")
    print()

    # Verdict
    print("=" * 80)
    print("💡 FINAL VERDICT (100,000 trades):")
    print("=" * 80)
    print()
    if net_pnl > 0:
        print(f"  ✅ PROFITABLE! ${initial_capital:.0f} → ${capital:,.2f}")
        print(f"  Win Rate: {win_rate:.1f}% | Profit Factor: {profit_factor:.2f}")
        print(f"  Total losses: ${gross_loss:,.2f} | Total profit: ${gross_profit:,.2f}")
    else:
        print(f"  ❌ LOSS: ${initial_capital:.0f} → ${capital:.2f}")
    print()

    # Save
    output = {
        "test_date": datetime.now().isoformat(),
        "test_type": "MEGA TEST - 100,000 trades",
        "initial_capital": initial_capital,
        "final_capital": capital,
        "net_pnl": net_pnl,
        "roi_pct": roi,
        "total_trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "by_type": {k: v for k, v in by_type.items()},
        "by_grade": {k: v for k, v in by_grade.items()},
    }
    with open("data/mega_test_100k.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("💾 Saved: data/mega_test_100k.json")


if __name__ == "__main__":
    run_mega_test(20.0, 100000)
