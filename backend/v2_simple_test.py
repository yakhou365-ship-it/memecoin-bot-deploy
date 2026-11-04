"""
SIMPLIFIED V2 BACKTEST - 5 pairs, 2 periods, $20 each
"""
import os, sys, json, asyncio, time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{os.path.abspath("data/v2_simple.db")}'
os.environ['APP_ENV'] = 'test'
os.environ['APP_LOG_LEVEL'] = 'WARNING'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.strategy_v2.config import StrategyConfig
from app.modules.strategy_v2.backtester import run_backtest


async def fetch_klines(symbol, days, end_date=None):
    import httpx
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000) if end_date else int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        cur_start = start_ms
        while cur_start < end_ms:
            params = {"symbol": symbol, "interval": "15m", "startTime": cur_start, "endTime": end_ms, "limit": 1000}
            for attempt in range(3):
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        break
                    elif resp.status_code in (418, 429):
                        print(f"  ⏳ {symbol}: rate limited, waiting 60s...")
                        await asyncio.sleep(60)
                    else:
                        return pd.DataFrame()
                except Exception:
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
            await asyncio.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["ts", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    df = df.astype(float)
    df = df[~df.index.duplicated()].iloc[:-1]
    return df


async def run_test(period_name, end_date, days, equity=20.0):
    print(f"\n{'='*70}")
    print(f"📊 {period_name}")
    print(f"📅 end_date={end_date} | days={days} | equity=${equity}")
    print(f"{'='*70}")

    # Use top 5 high-volume pairs
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    data = {}
    for s in symbols:
        df = await fetch_klines(s, days, end_date)
        if len(df) >= 1500:
            data[s] = df
            print(f"  ✅ {s}: {len(df)} bars")
        else:
            print(f"  ❌ {s}: {len(df)} bars")
        await asyncio.sleep(0.4)
    if not data:
        return None

    cfg = StrategyConfig().with_(min_notional_usd=0.5)  # lower min for $20 test
    print(f"\nV2 config: SL={cfg.sl_atr}×ATR TP1={cfg.tp1_atr}×ATR TP2={cfg.tp2_atr}×ATR trail={cfg.trail_atr}×ATR")
    print(f"Cost RT: {cfg.costs.round_trip*100:.3f}%")

    result = run_backtest(data, cfg, 'BTCUSDT', equity)
    stats = result['stats']

    print(f"\n📊 RESULTS:")
    print(f"  Initial: ${equity:.2f}")
    print(f"  Final:   ${result['portfolio']['equity']:.2f}")
    print(f"  PnL:     ${result['portfolio']['equity']-equity:+.2f}")
    print(f"  ROI:     {stats['return_pct']:+.2f}%")
    print(f"  Trades:  {stats['n']}")
    print(f"  WR:      {stats['win_rate']*100:.1f}%")
    print(f"  E[R]:    {stats['expectancy_r']:+.3f}")
    print(f"  PF:      {stats['profit_factor']:.2f}")
    print(f"  DD:      {stats['max_drawdown_pct']:.1f}%")
    print(f"\n  Exit reasons:")
    for r, info in stats.get('exit_reasons', {}).items():
        wr = info['wins']/info['n']*100 if info['n']>0 else 0
        print(f"    {r:<15}: {info['n']:>3} trades | {wr:>5.1f}% WR | R={info['avg_r']:+.3f}")

    return {
        'period': period_name,
        'initial': equity,
        'final': result['portfolio']['equity'],
        'pnl': result['portfolio']['equity']-equity,
        'roi': stats['return_pct'],
        'trades': stats['n'],
        'win_rate': stats['win_rate'],
        'expectancy_r': stats['expectancy_r'],
        'profit_factor': stats['profit_factor'],
        'max_dd': stats['max_drawdown_pct'],
    }


async def main():
    print("="*70)
    print("🚀 V2 IMPROVED STRATEGY - REAL DATA TEST ($20 CAPITAL)")
    print("="*70)

    # Test 1: Recent 30 days (mixed/bearish market)
    end_recent = datetime.utcnow().strftime("%Y-%m-%d")
    r1 = await run_test("RECENT 30 DAYS (mixed/bearish market)", end_recent, days=45, equity=20.0)

    # Test 2: Bull market (Nov 4 - Dec 4, 2024)
    r2 = await run_test("BULL MARKET (Nov 4 - Dec 4, 2024)", "2024-12-04", days=45, equity=20.0)

    # Comparison
    print(f"\n{'='*70}")
    print(f"📊 V2 vs V1 COMPARISON")
    print(f"{'='*70}\n")
    print(f"{'Period':<35} | {'V1 (old)':<25} | {'V2 (improved)':<25}")
    print("-" * 90)
    if r1:
        print(f"{'Recent 30 days (bear/mixed)':<35} | "
              f"$20→$20.04 (+0.2%) WR=71% | "
              f"$20→${r1['final']:.2f} ({r1['roi']:+.1f}%) WR={r1['win_rate']*100:.0f}%")
    if r2:
        print(f"{'Bull market (Nov 2024)':<35} | "
              f"$20→$20.02 (+0.1%) WR=100% | "
              f"$20→${r2['final']:.2f} ({r2['roi']:+.1f}%) WR={r2['win_rate']*100:.0f}%")

    with open('data/v2_vs_v1_comparison.json', 'w') as f:
        json.dump({'recent': r1, 'bull_market': r2}, f, indent=2, default=str)
    print(f"\n💾 Saved: data/v2_vs_v1_comparison.json")


if __name__ == "__main__":
    asyncio.run(main())
