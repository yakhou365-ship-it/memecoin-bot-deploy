"""
REAL BINANCE BACKTEST - $20 CAPITAL
=====================================

Tests V2 improved strategy on REAL Binance data with $20.
Uses actual 15m candles for 90 days.
"""
import os, sys, json, asyncio
from datetime import datetime

os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{os.path.abspath("data/v2_real.db")}'
os.environ['APP_ENV'] = 'test'
os.environ['APP_LOG_LEVEL'] = 'WARNING'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.strategy_v2.config import StrategyConfig
from app.modules.strategy_v2.backtester import run_backtest
from app.modules.strategy_v2.data import load_universe


def main():
    print("=" * 80)
    print("📊 REAL BINANCE BACKTEST - V2 IMPROVED - $20 CAPITAL")
    print("=" * 80)
    print()

    # Load real data - use 10 high-volume pairs for faster test
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
               "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT"]
    print(f"📥 Loading real Binance 15m data ({len(symbols)} pairs, 90 days)...")

    data = load_universe(symbols, 90)
    if not data:
        print("❌ No data loaded")
        return

    print(f"✅ Loaded {len(data)} symbols")
    print()

    # V2 improved config
    cfg = StrategyConfig().with_(min_notional_usd=0.5)  # lower for $20 capital

    print("📋 V2 IMPROVED Configuration:")
    print(f"   SL: {cfg.sl_atr}×ATR | TP1: {cfg.tp1_atr}×ATR | TP2: {cfg.tp2_atr}×ATR")
    print(f"   Trail: {cfg.trail_atr}×ATR | Partial: {cfg.partial_fraction*100:.0f}% at TP1")
    print(f"   Risk per trade: {cfg.risk_per_trade*100:.1f}% | Max position: {cfg.max_position_pct*100:.0f}%")
    print(f"   Max positions: {cfg.max_open_positions} | Daily loss limit: {cfg.daily_loss_limit*100:.0f}%")
    print(f"   Cooldown after {cfg.cooldown_after_losses} losses")
    print(f"   Cost (round-trip): {cfg.costs.round_trip*100:.3f}%")
    print()

    # Run backtest with $20
    initial = 20.0
    print(f"💰 Running backtest with ${initial:.2f}...")
    result = run_backtest(data, cfg, "BTCUSDT", initial)
    stats = result["stats"]

    # Results
    print()
    print("=" * 80)
    print("📊 RESULTS - REAL BINANCE DATA (90 days, $20)")
    print("=" * 80)
    print()

    final = result["portfolio"]["equity"]
    net_pnl = final - initial
    roi = (net_pnl / initial) * 100

    print(f"💰 Initial capital:   ${initial:.2f}")
    print(f"💵 Final capital:     ${final:.2f}")
    print(f"📈 Net PnL:           ${net_pnl:+.2f}")
    print(f"📊 ROI:               {roi:+.1f}%")
    print(f"💎 Multiplier:        {final/initial:.2f}x")
    print()
    print(f"🎯 Total trades:      {stats['n']}")
    wins = sum(1 for t in result["portfolio"]["trades"] if t["pnl"] > 0)
    losses = stats["n"] - wins
    print(f"   ✅ Wins:            {wins}")
    print(f"   ❌ Losses:          {losses}")
    print(f"   🎯 Win Rate:        {stats['win_rate']*100:.1f}%")
    print()
    print(f"💪 Profit Factor:    {stats['profit_factor']:.2f}")
    ci = stats.get("expectancy_ci95")
    if ci:
        print(f"📊 E[R]:              {stats['expectancy_r']:+.3f} (CI: [{ci[0]:+.3f}, {ci[1]:+.3f}])")
    print(f"📉 Max Drawdown:     {stats['max_drawdown_pct']:.1f}%")
    print(f"📅 Positive months:  {stats['positive_months_pct']:.0f}%")
    print()

    # Exit reasons
    print("📋 Exit reasons:")
    for reason, info in stats.get("exit_reasons", {}).items():
        wr = info["wins"] / info["n"] * 100 if info["n"] > 0 else 0
        print(f"  {reason:<15}: {info['n']:>4} trades | {wr:>5.1f}% WR | avg R = {info['avg_r']:+.3f}")
    print()

    # Verdict
    print("=" * 80)
    print("💡 VERDICT:")
    print("=" * 80)
    if net_pnl > 0:
        print(f"  ✅ PROFITABLE! ${initial:.0f} → ${final:.2f} (+{roi:.1f}%)")
        print(f"  Win Rate: {stats['win_rate']*100:.1f}%")
        print(f"  Profit Factor: {stats['profit_factor']:.2f}")
        print(f"  Max DD: {stats['max_drawdown_pct']:.1f}%")
    elif net_pnl > -1:
        print(f"  ➖ Break-even: ${initial:.0f} → ${final:.2f} ({roi:+.1f}%)")
        print(f"  Strategy is stable - small loss within noise")
    else:
        print(f"  ❌ Loss: ${initial:.0f} → ${final:.2f} ({roi:+.1f}%)")
        print(f"  Win Rate: {stats['win_rate']*100:.1f}%")
        print(f"  Profit Factor: {stats['profit_factor']:.2f}")
    print()

    # Save
    output = {
        "test_date": datetime.now().isoformat(),
        "platform": "Binance (REAL 15m data)",
        "initial_capital": initial,
        "final_capital": final,
        "net_pnl": net_pnl,
        "roi_pct": roi,
        "stats": {k: v for k, v in stats.items() if k not in ["exit_reasons", "skipped"]},
        "exit_reasons": stats.get("exit_reasons", {}),
    }
    with open("data/v2_real_20usd.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("💾 Saved: data/v2_real_20usd.json")


if __name__ == "__main__":
    main()
