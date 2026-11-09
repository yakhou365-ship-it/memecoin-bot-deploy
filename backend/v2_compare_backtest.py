"""
REAL 30-DAY BACKTEST V2 - $20 CAPITAL
=======================================

Runs the improved V2 strategy on REAL Binance 15m data for the last 30 days
AND for a bull market period (Nov 4 - Dec 4, 2024).

Compares V2 (improved) vs V1 (old SmartDecisionEngine) results.
"""
import asyncio
import os
import sys
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{os.path.abspath("data/v2_compare.db")}'
os.environ['APP_ENV'] = 'test'
os.environ['APP_LOG_LEVEL'] = 'WARNING'
os.makedirs('data', exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.strategy_v2.config import StrategyConfig, VENUES
from app.modules.strategy_v2.backtester import run_backtest, market_regime_15m


async def fetch_klines(symbol: str, days: int = 60, end_date: str = None) -> pd.DataFrame:
    """Fetch REAL 15m candles from Binance."""
    import httpx
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000) if end_date else int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    params = {"symbol": symbol, "interval": "15m", "startTime": start_ms, "endTime": end_ms, "limit": 1000}
    rows = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        cur_start = start_ms
        while cur_start < end_ms:
            params["startTime"] = cur_start
            for attempt in range(3):
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        break
                    elif resp.status_code in (418, 429):
                        print(f"  ⏳ {symbol}: rate limited, waiting 60s...")
                        await asyncio.sleep(60)
                    else:
                        print(f"  ❌ {symbol}: HTTP {resp.status_code}")
                        return pd.DataFrame()
                except Exception as e:
                    print(f"  ❌ {symbol}: {e}")
                    await asyncio.sleep(3)
            else:
                return pd.DataFrame()
            raw = resp.json()
            if not raw:
                break
            rows += raw
            cur_start = raw[-1][0] + 15 * 60_000
            if len(raw) < 1000:
                break
            await asyncio.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["ts", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    df = df.astype(float)
    df = df[~df.index.duplicated()].iloc[:-1]
    return df


async def load_universe(symbols, days, end_date=None):
    """Load 15m data for all symbols."""
    data = {}
    for s in symbols:
        df = await fetch_klines(s, days, end_date)
        if len(df) >= 1500:
            data[s] = df
            print(f"  ✅ {s}: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
        else:
            print(f"  ❌ {s}: only {len(df)} bars")
        await asyncio.sleep(0.2)
    return data


async def run_period(period_name: str, end_date: str, days: int, equity: float = 20.0):
    """Run V2 backtest for a specific period."""
    print(f"\n{'='*80}")
    print(f"📊 V2 BACKTEST: {period_name}")
    print(f"📅 End date: {end_date} | Days: {days} | Capital: ${equity}")
    print(f"{'='*80}\n")

    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT",
        "LTCUSDT", "TRXUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
        "INJUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT", "FETUSDT",
        "RENDERUSDT", "TIAUSDT", "SEIUSDT", "AAVEUSDT", "UNIUSDT", "FILUSDT",
    ]

    print(f"📥 Fetching 15m candles for {len(symbols)} symbols...")
    data = await load_universe(symbols, days, end_date)
    if not data:
        print("❌ No data fetched")
        return None

    ref = "BTCUSDT" if "BTCUSDT" in data else list(data.keys())[0]
    cfg = StrategyConfig()

    print(f"\n📋 V2 Config (IMPROVED):")
    print(f"   SL: {cfg.sl_atr}×ATR | TP1: {cfg.tp1_atr}×ATR | TP2: {cfg.tp2_atr}×ATR")
    print(f"   Trailing: {cfg.trail_atr}×ATR | Partial: {cfg.partial_fraction*100:.0f}% at TP1")
    print(f"   Binance round-trip cost: {cfg.costs.round_trip*100:.3f}%")
    print(f"   Min ATR%: {cfg.min_atr_pct*100:.1f}% | Risk per trade: {cfg.risk_per_trade*100:.2f}%")
    print(f"   Max positions: {cfg.max_open_positions} | Min notional: ${cfg.min_notional_usd}")
    print()

    # Lower min_notional for $20 capital test
    cfg = cfg.with_(min_notional_usd=0.5)  # allow $0.5 minimum trades for $20 test

    print("Running V2 backtest...")
    result = run_backtest(data, cfg, ref, equity)
    stats = result["stats"]

    print(f"\n{'='*80}")
    print(f"📊 V2 RESULTS: {period_name}")
    print(f"{'='*80}")
    print(f"   📅 Period: {days} days ending {end_date}")
    print(f"   💵 Initial capital: ${equity:.2f}")
    print(f"   💵 Final equity:    ${result['portfolio']['equity']:.2f}")
    print(f"   📈 Net PnL:         ${result['portfolio']['equity'] - equity:+.2f}")
    print(f"   📊 ROI:             {stats['return_pct']:+.2f}%")
    print()
    print(f"   🎯 Total trades:    {stats['n']}")
    print(f"   ✅ Win Rate:        {stats['win_rate']*100:.1f}%")
    print(f"   📈 E[R]:            {stats['expectancy_r']:+.3f}")
    ci = stats.get('expectancy_ci95')
    if ci:
        print(f"   📊 CI 95%:          [{ci[0]:+.3f}, {ci[1]:+.3f}]")
    print(f"   💪 Profit Factor:   {stats['profit_factor']:.2f}")
    print(f"   📉 Max Drawdown:    {stats['max_drawdown_pct']:.1f}%")
    print(f"   📅 Positive months: {stats['positive_months_pct']:.0f}%")
    print()
    print(f"   📋 Exit reasons:")
    for reason, info in stats.get('exit_reasons', {}).items():
        wr = info['wins'] / info['n'] * 100 if info['n'] > 0 else 0
        print(f"      {reason:<15}: {info['n']:>4} trades | {wr:>5.1f}% WR | avg R = {info['avg_r']:+.3f}")
    print()
    print(f"   🚫 Skipped by limits:")
    for reason, count in stats.get('skipped', {}).items():
        if count > 0:
            print(f"      {reason:<20}: {count}")

    return {
        "period": period_name,
        "end_date": end_date,
        "days": days,
        "initial_equity": equity,
        "final_equity": result['portfolio']['equity'],
        "net_pnl": result['portfolio']['equity'] - equity,
        "roi_pct": stats['return_pct'],
        "total_trades": stats['n'],
        "win_rate": stats['win_rate'],
        "expectancy_r": stats['expectancy_r'],
        "profit_factor": stats['profit_factor'],
        "max_drawdown_pct": stats['max_drawdown_pct'],
        "exit_reasons": stats.get('exit_reasons', {}),
        "skipped": stats.get('skipped', {}),
    }


async def main():
    print("=" * 80)
    print("🚀 V2 STRATEGY - REAL DATA BACKTEST COMPARISON")
    print("=" * 80)

    # Test 1: Last 30 days (recent market)
    end_recent = datetime.utcnow().strftime("%Y-%m-%d")
    results1 = await run_period("آخر 30 يوم (سوق متذبذب/هابط)", end_recent, days=45, equity=20.0)

    # Test 2: Bull market period (Nov 4 - Dec 4, 2024)
    results2 = await run_period("سوق صاعد (نوفمبر 2024)", "2024-12-04", days=45, equity=20.0)

    # Compare
    print(f"\n{'='*80}")
    print(f"📊 COMPARISON: V2 vs V1 (old results)")
    print(f"{'='*80}\n")

    print(f"{'Period':<35} | {'V1 Result':<25} | {'V2 Result':<25}")
    print("-" * 90)
    if results1:
        print(f"{'آخر 30 يوم (متذبذب)':<35} | "
              f"$20→$20.04 (+0.2%) WR=71% | "
              f"${results1['initial_equity']:.0f}→${results1['final_equity']:.2f} "
              f"({results1['roi_pct']:+.1f}%) WR={results1['win_rate']*100:.0f}%")
    if results2:
        print(f"{'سوق صاعد (نوف 2024)':<35} | "
              f"$20→$20.02 (+0.1%) WR=100% | "
              f"${results2['initial_equity']:.0f}→${results2['final_equity']:.2f} "
              f"({results2['roi_pct']:+.1f}%) WR={results2['win_rate']*100:.0f}%")

    # Save
    output = {
        "test_date": datetime.now().isoformat(),
        "v2_results": {
            "recent_30_days": results1,
            "bull_market_nov_2024": results2,
        },
    }
    with open("data/v2_comparison_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n💾 Saved to: data/v2_comparison_results.json")


if __name__ == "__main__":
    asyncio.run(main())
