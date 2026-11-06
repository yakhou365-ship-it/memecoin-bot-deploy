"""
Pump.fun Historical Simulation - Real Memecoin Backtest
========================================================

Simulates trading on Pump.fun tokens using the bonding curve math.
Since PumpPortal doesn't provide historical data (only real-time),
we simulate the bonding curve dynamics with realistic scenarios:

1. Token launches at 0 SOL reserve (price = $0.0000056)
2. Buyers enter over time, pushing the curve up
3. At 85 SOL reserve, token graduates to Raydium LP
4. We simulate buying at various curve stages and track ROI

This gives realistic expectations for Pump.fun trading.
"""
import os, sys, json, asyncio
from datetime import datetime
import numpy as np

os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///./data/test.db'
os.environ['APP_ENV'] = 'test'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.market_scanner.pumpfun_client import BondingCurveMath, PumpPortalClient, VIRTUAL_SOL_RESERVES, VIRTUAL_TOKEN_RESERVES, GRADUATION_THRESHOLD_SOL, TOTAL_SUPPLY


async def simulate_pumpfun_trading(initial_sol: float = 0.1):
    """
    Simulate Pump.fun trading scenarios with real bonding curve math.

    Scenarios:
    1. Buy at launch (0% progress) → hold to graduation (100%)
    2. Buy at 25% progress → hold to graduation
    3. Buy at 50% progress → hold to graduation
    4. Buy at 75% progress → hold to graduation
    5. Buy at launch → token fails (curve drops 50%)
    6. Buy at 50% → token rugs (curve drops to 0)
    """
    print("=" * 80)
    print("🐹 PUMP.FUN HISTORICAL SIMULATION - Real Bonding Curve Math")
    print("=" * 80)
    print(f"Initial investment: {initial_sol} SOL per scenario")
    print()

    client = PumpPortalClient()
    sol_price = await client._fetch_sol_price()
    print(f"SOL price: ${sol_price:.2f}")
    print(f"Bonding curve: virtual {VIRTUAL_SOL_RESERVES} SOL / {VIRTUAL_TOKEN_RESERVES:,.0f} tokens")
    print(f"Graduation threshold: {GRADUATION_THRESHOLD_SOL} SOL")
    print(f"Total supply: {TOTAL_SUPPLY:,.0f} tokens")
    print()
    await client.close()

    # Scenarios
    scenarios = [
        ("🟢 Buy at 0% → graduate to 100%", 0, 85, "success"),
        ("🟢 Buy at 25% → graduate to 100%", 21.25, 85, "success"),
        ("🟡 Buy at 50% → graduate to 100%", 42.5, 85, "success"),
        ("🟠 Buy at 75% → graduate to 100%", 63.75, 85, "success"),
        ("🔴 Buy at 0% → curve drops 50%", 0, 0, "fail_half"),
        ("🔴 Buy at 50% → rug pull (curve→0)", 42.5, 0, "rug"),
        ("🟢 Buy at 25% → 2x pump before graduation", 21.25, 85, "success_2x"),
    ]

    results = []
    print(f"{'Scenario':<45} | {'Entry SOL':>10} | {'Exit SOL':>10} | {'ROI':>8} | {'PnL USD':>10}")
    print("-" * 100)

    for name, entry_sol_reserve, exit_sol_reserve, outcome in scenarios:
        # Buy tokens at entry
        tokens_bought = BondingCurveMath.compute_tokens_for_sol(initial_sol, entry_sol_reserve)
        entry_price = BondingCurveMath.compute_price_usd(entry_sol_reserve, sol_price)
        entry_value_usd = initial_sol * sol_price

        # Compute exit value
        if outcome == "success":
            exit_price = BondingCurveMath.compute_price_usd(exit_sol_reserve, sol_price)
            exit_value_usd = tokens_bought * exit_price
        elif outcome == "fail_half":
            # Curve drops to half the entry reserve
            exit_price = BondingCurveMath.compute_price_usd(entry_sol_reserve * 0.5, sol_price)
            exit_value_usd = tokens_bought * exit_price
        elif outcome == "rug":
            # Rug pull - tokens become worthless
            exit_price = 0
            exit_value_usd = 0
        elif outcome == "success_2x":
            # Token does 2x before graduation
            # Price doubles from entry
            exit_price = entry_price * 2
            exit_value_usd = tokens_bought * exit_price

        # Account for fees (3% round trip on pump.fun)
        entry_fee = entry_value_usd * 0.015  # 1.5% entry
        exit_fee = exit_value_usd * 0.015   # 1.5% exit
        net_exit_value = exit_value_usd - exit_fee
        net_cost = entry_value_usd + entry_fee

        pnl_usd = net_exit_value - net_cost
        roi_pct = (pnl_usd / net_cost) * 100 if net_cost > 0 else 0

        results.append({
            "scenario": name,
            "entry_sol_reserve": entry_sol_reserve,
            "exit_sol_reserve": exit_sol_reserve,
            "outcome": outcome,
            "tokens_bought": tokens_bought,
            "entry_price_usd": entry_price,
            "exit_price_usd": exit_price,
            "entry_value_usd": entry_value_usd,
            "exit_value_usd": exit_value_usd,
            "pnl_usd": pnl_usd,
            "roi_pct": roi_pct,
        })

        print(f"{name:<45} | {entry_sol_reserve:>9.1f}  | {exit_sol_reserve:>9.1f}  | {roi_pct:>+7.1f}% | ${pnl_usd:>+9.2f}")

    print()
    print("=" * 80)
    print("📊 PUMP.FUN STRATEGY ANALYSIS")
    print("=" * 80)
    print()
    print("Realistic Win Rate Expectations on Pump.fun:")
    print("  - Pump.fun stats (Sept 2026): Only 0.41% of wallets made >$10K profit")
    print("  - Most tokens FAIL before graduation (~70-80% never reach 85 SOL)")
    print("  - Of those that graduate, only ~30% continue pumping on Raydium")
    print()
    print("Recommended Strategy:")
    print("  1. Only buy tokens with bonding curve progress 25-75% (proven demand)")
    print("  2. Sell 50% at +100% profit (recover initial investment)")
    print("  3. Hold remaining 50% with trailing stop (catch moonshots)")
    print("  4. NEVER hold tokens that show rug patterns (creator sells, liquidity removal)")
    print("  5. Max exposure: 5% of capital to pump.fun (high risk)")
    print()

    # Save results
    output = {
        "simulation_date": datetime.now().isoformat(),
        "initial_sol": initial_sol,
        "sol_price_usd": sol_price,
        "scenarios": results,
        "realistic_expectations": {
            "win_rate_pump_fun": "25-40% (not 80%)",
            "profit_factor_target": "1.5-2.5 (big wins, frequent small losses)",
            "max_capital_exposure_pct": 5,
            "graduation_rate": "~20-30% of tokens reach 85 SOL",
        }
    }
    with open("data/pumpfun_simulation_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"💾 Saved: data/pumpfun_simulation_results.json")


if __name__ == "__main__":
    asyncio.run(simulate_pumpfun_trading(0.1))
