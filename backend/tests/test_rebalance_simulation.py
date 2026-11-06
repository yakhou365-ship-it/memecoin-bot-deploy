"""
Ultimate-bot realistic simulation.

Simulates 100 trades with and without Smart Rebalancing to compare results.
Uses real Pump.fun-style price movements (high volatility, sudden pumps/dumps).
"""
import asyncio
import os
import sys
import random
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_simulation.db"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_ORCHESTRATOR"] = "false"
os.environ["AUTO_START_TRADING"] = "false"
os.environ["API_AUTH_KEY"] = "test-key-123"


def simulate_price_path(start_price: float, steps: int = 120, volatility: float = 0.05) -> list:
    """Simulate a memecoin price path (120 steps = 2 hours at 1min/step).

    Realistic memecoin behavior:
    - Random pumps (+10-50% in 1 step, 5% chance)
    - Random dumps (-10-30% in 1 step, 3% chance)
    - Base volatility 3-8% per step
    - Slight upward drift for "good" tokens, downward for "bad"
    """
    prices = [start_price]
    is_good_token = random.random() > 0.5  # 50% are "good" (upward drift)
    drift = 0.002 if is_good_token else -0.003

    for i in range(steps):
        change = random.gauss(drift, volatility)
        # 5% chance of pump
        if random.random() < 0.05:
            change += random.uniform(0.10, 0.50)
        # 3% chance of dump
        if random.random() < 0.03:
            change -= random.uniform(0.10, 0.30)

        new_price = prices[-1] * (1 + change)
        new_price = max(new_price, start_price * 0.01)  # never goes to 0
        prices.append(new_price)

    return prices


def simulate_trade_no_rebalance(entry_price: float, prices: list, stop_loss: float,
                                 take_profit: float, position_usd: float) -> dict:
    """Simulate a single trade WITHOUT rebalancing. Returns PnL."""
    quantity = position_usd / entry_price
    peak_price = entry_price
    exit_price = entry_price
    exit_reason = "time_exit"

    for i, price in enumerate(prices):
        if price > peak_price:
            peak_price = price

        # SL hit
        if price <= stop_loss:
            exit_price = price
            exit_reason = "stop_loss"
            break

        # TP hit (10x cap)
        if price >= take_profit:
            exit_price = price
            exit_reason = "mega_tp"
            break

        # Trailing stop (V500 logic)
        peak_gain = (peak_price / entry_price - 1)
        if peak_gain >= 5.0:
            trail = 0.45
        elif peak_gain >= 1.0:
            trail = 0.35
        elif peak_gain >= 0.30:
            trail = 0.25
        else:
            trail = None

        if trail is not None:
            drop_from_peak = (peak_price - price) / peak_price
            if drop_from_peak >= trail:
                exit_price = price
                exit_reason = "smart_trailing"
                break

        # Sudden dump (before trailing armed)
        if trail is None and peak_gain >= 0.10:
            drop_from_peak = (peak_price - price) / peak_price
            if drop_from_peak >= 0.25:
                exit_price = price
                exit_reason = "sudden_dump"
                break

    else:
        # Time exit (end of simulation)
        exit_price = prices[-1]
        exit_reason = "time_exit"

    gross_pnl = (exit_price - entry_price) * quantity
    fee_usd = position_usd * 0.005  # 0.5% total fee (PumpPortal 1% per side, but paper uses 0.25%)
    net_pnl = gross_pnl - fee_usd
    pnl_pct = (net_pnl / position_usd) * 100

    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "peak_price": peak_price,
        "exit_reason": exit_reason,
        "net_pnl": net_pnl,
        "pnl_pct": pnl_pct,
        "position_usd": position_usd,
        "quantity": quantity,
    }


def simulate_with_rebalance(trades: list, prices_list: list, wallet_usd: float) -> dict:
    """Simulate a portfolio WITH Smart Rebalancing.

    Trades start at staggered times (more realistic).
    Rebalancer runs every 10 steps and reallocates capital.
    """
    # Stagger trade entry times: each trade enters 5 steps after the previous
    open_positions = []
    for i, (trade, prices) in enumerate(zip(trades, prices_list)):
        open_positions.append({
            "entry_price": trade["entry_price"],
            "current_price": trade["entry_price"],
            "sl": trade["sl"],
            "tp": trade["tp"],
            "position_usd": trade["position_usd"],
            "quantity": trade["position_usd"] / trade["entry_price"],
            "peak_price": trade["entry_price"],
            "prices": prices,
            "entry_step": i * 3,  # enter 3 steps apart (staggered)
            "step_in_trade": 0,
            "closed": False,
            "exit_price": 0,
            "exit_reason": "",
            "net_pnl": 0,
            "is_active": False,  # becomes True after entry_step
        })

    total_pnl = 0.0
    total_trades_closed = 0
    rebalance_count = 0
    max_steps = max(len(p) for p in prices_list) + max(p["entry_step"] for p in open_positions)

    for step in range(max_steps):
        # Activate trades that start at this step
        for pos in open_positions:
            if not pos["is_active"] and step >= pos["entry_step"]:
                pos["is_active"] = True

        # Update current prices for active positions
        for pos in open_positions:
            if pos["closed"] or not pos["is_active"]:
                continue
            trade_step = step - pos["entry_step"]
            if trade_step < len(pos["prices"]):
                pos["current_price"] = pos["prices"][trade_step]
                if pos["current_price"] > pos["peak_price"]:
                    pos["peak_price"] = pos["current_price"]

        # Check exits (SL, TP, trailing, sudden_dump)
        for pos in open_positions:
            if pos["closed"] or not pos["is_active"]:
                continue
            price = pos["current_price"]
            entry = pos["entry_price"]
            peak = pos["peak_price"]

            if price <= pos["sl"]:
                pos["closed"] = True
                pos["exit_price"] = price
                pos["exit_reason"] = "stop_loss"
                pos["net_pnl"] = (price - entry) * pos["quantity"] - pos["position_usd"] * 0.005
                total_pnl += pos["net_pnl"]
                total_trades_closed += 1
                continue

            if price >= pos["tp"]:
                pos["closed"] = True
                pos["exit_price"] = price
                pos["exit_reason"] = "mega_tp"
                pos["net_pnl"] = (price - entry) * pos["quantity"] - pos["position_usd"] * 0.005
                total_pnl += pos["net_pnl"]
                total_trades_closed += 1
                continue

            peak_gain = (peak / entry - 1) if entry > 0 else 0
            if peak_gain >= 5.0:
                trail = 0.45
            elif peak_gain >= 1.0:
                trail = 0.35
            elif peak_gain >= 0.30:
                trail = 0.25
            else:
                trail = None

            if trail is not None:
                drop = (peak - price) / peak if peak > 0 else 0
                if drop >= trail:
                    pos["closed"] = True
                    pos["exit_price"] = price
                    pos["exit_reason"] = "smart_trailing"
                    pos["net_pnl"] = (price - entry) * pos["quantity"] - pos["position_usd"] * 0.005
                    total_pnl += pos["net_pnl"]
                    total_trades_closed += 1
                    continue

            if trail is None and peak_gain >= 0.10:
                drop = (peak - price) / peak if peak > 0 else 0
                if drop >= 0.25:
                    pos["closed"] = True
                    pos["exit_price"] = price
                    pos["exit_reason"] = "sudden_dump"
                    pos["net_pnl"] = (price - entry) * pos["quantity"] - pos["position_usd"] * 0.005
                    total_pnl += pos["net_pnl"]
                    total_trades_closed += 1
                    continue

        # Smart Rebalance every 10 steps
        if step > 0 and step % 10 == 0:
            active = [p for p in open_positions if p["is_active"] and not p["closed"]]
            if len(active) >= 2:
                for p in active:
                    p["gain_pct"] = (p["current_price"] / p["entry_price"] - 1) * 100
                active.sort(key=lambda x: x["gain_pct"], reverse=True)

                star = active[0]
                if star["gain_pct"] >= 10.0:
                    closables = [p for p in active[1:] if 5.0 <= p["gain_pct"] <= 30.0]
                    if closables:
                        rebalance_count += 1
                        release_usd = sum(c["position_usd"] * (1 + c["gain_pct"] / 100) for c in closables)
                        add_usd = release_usd * 0.8
                        # Ultimate-bot: allow star position to grow up to 15% of wallet
                        # (was 5% which blocked all rebalances since initial position = 5%)
                        max_star = wallet_usd * 0.15
                        add_usd = min(add_usd, max(0, max_star - star["position_usd"]))
                        if add_usd > 0.5:  # at least $0.50 to bother
                            for c in closables:
                                c["closed"] = True
                                c["exit_price"] = c["current_price"]
                                c["exit_reason"] = "smart_rebalance"
                                c["net_pnl"] = (c["current_price"] - c["entry_price"]) * c["quantity"] - c["position_usd"] * 0.005
                                total_pnl += c["net_pnl"]
                                total_trades_closed += 1
                            # Add to star — this is the KEY difference:
                            # the star position now has MORE capital, so its
                            # future gains (or losses) are amplified
                            additional_qty = add_usd / star["current_price"]
                            star["position_usd"] += add_usd
                            star["quantity"] += additional_qty
                            # Update SL to protect the new larger position
                            # (keep same % distance from entry)
                            # DON'T change entry_price — the new buy is at current_price

    # Close remaining
    for pos in open_positions:
        if not pos["closed"] and pos["is_active"]:
            pos["closed"] = True
            pos["exit_price"] = pos["current_price"]
            pos["exit_reason"] = "time_exit"
            pos["net_pnl"] = (pos["current_price"] - pos["entry_price"]) * pos["quantity"] - pos["position_usd"] * 0.005
            total_pnl += pos["net_pnl"]
            total_trades_closed += 1

    return {
        "total_pnl": total_pnl,
        "trades_closed": total_trades_closed,
        "rebalances": rebalance_count,
        "final_positions": open_positions,
    }


def run_simulation():
    """Run 10 rounds of 100-trade simulations and compare."""
    print("=" * 80)
    print("Ultimate-bot Smart Rebalancing Simulation")
    print("=" * 80)
    print()

    ROUNDS = 10
    TRADES_PER_ROUND = 50  # 50 concurrent trades (5 batches of 10)
    WALLET_USD = 100.0  # $100 starting wallet
    POSITION_USD = 5.0  # $5 per trade (5% of wallet)

    all_no_rebal = []
    all_with_rebal = []

    for round_num in range(1, ROUNDS + 1):
        random.seed(round_num * 42)  # reproducible

        # Generate trades + price paths
        trades = []
        prices_list = []
        for _ in range(TRADES_PER_ROUND):
            entry = random.uniform(0.0001, 0.01)
            sl = entry * 0.85  # -15% SL
            tp = entry * 11.0  # 10x cap
            trades.append({"entry_price": entry, "sl": sl, "tp": tp, "position_usd": POSITION_USD})
            prices_list.append(simulate_price_path(entry, steps=120, volatility=random.uniform(0.03, 0.08)))

        # Without rebalance: simulate each trade independently
        no_rebal_pnl = 0.0
        no_rebal_wins = 0
        no_rebal_losses = 0
        for trade, prices in zip(trades, prices_list):
            result = simulate_trade_no_rebalance(
                trade["entry_price"], prices, trade["sl"], trade["tp"], trade["position_usd"]
            )
            no_rebal_pnl += result["net_pnl"]
            if result["net_pnl"] > 0:
                no_rebal_wins += 1
            else:
                no_rebal_losses += 1

        # With rebalance: simulate as portfolio
        rebal_result = simulate_with_rebalance(trades, prices_list, WALLET_USD)
        with_rebal_pnl = rebal_result["total_pnl"]

        all_no_rebal.append(no_rebal_pnl)
        all_with_rebal.append(with_rebal_pnl)

        no_rebal_roi = (no_rebal_pnl / (TRADES_PER_ROUND * POSITION_USD)) * 100
        with_rebal_roi = (with_rebal_pnl / (TRADES_PER_ROUND * POSITION_USD)) * 100

        print(f"Round {round_num:2d}: No-Rebal PnL=${no_rebal_pnl:+.2f} ({no_rebal_roi:+.1f}%)  |  "
              f"Rebal PnL=${with_rebal_pnl:+.2f} ({with_rebal_roi:+.1f}%)  |  "
              f"Rebalances: {rebal_result['rebalances']}")

    print()
    print("=" * 80)
    print("SUMMARY (10 rounds × 50 trades each)")
    print("=" * 80)

    avg_no = sum(all_no_rebal) / ROUNDS
    avg_with = sum(all_with_rebal) / ROUNDS
    wins_no = sum(1 for x in all_no_rebal if x > 0)
    wins_with = sum(1 for x in all_with_rebal if x > 0)

    print(f"  {'Metric':<30s} {'No Rebalance':>15s} {'With Rebalance':>15s} {'Δ':>10s}")
    print(f"  {'-'*30} {'-'*15} {'-'*15} {'-'*10}")
    print(f"  {'Avg PnL per round':<30s} ${avg_no:>+14.2f} ${avg_with:>+14.2f} ${avg_with-avg_no:>+9.2f}")
    print(f"  {'Profitable rounds':<30s} {wins_no:>14d}/10 {wins_with:>14d}/10 {wins_with-wins_no:>+9d}")
    print(f"  {'Best round':<30s} ${max(all_no_rebal):>+14.2f} ${max(all_with_rebal):>+14.2f}")
    print(f"  {'Worst round':<30s} ${min(all_no_rebal):>+14.2f} ${min(all_with_rebal):>+14.2f}")
    print(f"  {'Total PnL (10 rounds)':<30s} ${sum(all_no_rebal):>+14.2f} ${sum(all_with_rebal):>+14.2f} ${sum(all_with_rebal)-sum(all_no_rebal):>+9.2f}")

    improvement = ((avg_with - avg_no) / abs(avg_no) * 100) if avg_no != 0 else 0
    print()
    if avg_with > avg_no:
        print(f"  ✅ Smart Rebalancing IMPROVES results by {improvement:.1f}%")
    else:
        print(f"  ⚠️  Smart Rebalancing does NOT improve results ({improvement:+.1f}%)")

    print()
    print("=" * 80)
    return avg_with > avg_no


if __name__ == "__main__":
    is_better = run_simulation()
    sys.exit(0 if is_better else 1)
