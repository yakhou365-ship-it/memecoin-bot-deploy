"""
BULL MARKET BACKTEST - $20 CAPITAL
====================================

Tests the bot's strategy on REAL Binance historical data
during a BULL MARKET period (Nov 4 - Dec 4, 2024, BTC +45.3%).
"""
import asyncio
import os
import sys
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional

os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{os.path.abspath("data/bull_backtest.db")}'
os.environ['APP_ENV'] = 'test'
os.environ['APP_LOG_LEVEL'] = 'WARNING'
os.makedirs('data', exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def fetch_real_candles(symbol: str, interval: str = "1d",
                              start_date: str = None, end_date: str = None,
                              limit: int = 1000) -> pd.DataFrame:
    """Fetch REAL historical OHLCV from Binance."""
    import httpx

    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_date:
        params["startTime"] = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
    if end_date:
        params["endTime"] = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    break
                elif resp.status_code == 418 or resp.status_code == 429:
                    # Banned or rate limited - wait longer
                    wait = 60.0 * (attempt + 1)
                    print(f"  ⏳ {symbol}: rate limited, waiting {wait:.0f}s...")
                    await asyncio.sleep(wait)
                else:
                    print(f"  ❌ {symbol}: HTTP {resp.status_code}")
                    return pd.DataFrame()
            except Exception as e:
                print(f"  ❌ {symbol}: {e}")
                await asyncio.sleep(2.0 * (attempt + 1))
                if attempt == 2:
                    return pd.DataFrame()
        else:
            return pd.DataFrame()

        raw = resp.json()

    candles = []
    for k in raw:
        candles.append({
            "timestamp": int(k[0] // 1000),
            "date": datetime.fromtimestamp(int(k[0] // 1000)).strftime("%Y-%m-%d"),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return pd.DataFrame(candles)


def simulate_trade(df: pd.DataFrame, entry_idx: int, entry_price: float,
                    config: Dict, position_size_usd: float) -> Optional[Dict]:
    """Simulate trade on REAL future data."""
    sl_pct = config["sl_pct"]
    tp_pct = config["tp_pct"]
    trail_after = config["trail_after"]
    trail_dist = config["trail_dist"]
    max_hold = config["max_hold"]

    if entry_price <= 0 or sl_pct <= 0:
        return None

    initial_sl = entry_price * (1 - sl_pct / 100)
    tp = entry_price * (1 + tp_pct / 100)

    future = df.iloc[entry_idx + 1: entry_idx + max_hold + 1]
    if len(future) < 3:
        return None

    max_price = entry_price
    trail_active = False
    current_sl = initial_sl
    exit_price = None
    exit_reason = "time_exit"
    holding_candles = max_hold

    for i, (_, row) in enumerate(future.iterrows()):
        h = float(row["high"])
        l = float(row["low"])
        max_price = max(max_price, h)
        profit_pct = (h - entry_price) / entry_price * 100

        if not trail_active and profit_pct >= trail_after:
            trail_active = True
            current_sl = max(current_sl, entry_price * 1.001)

        if trail_active:
            current_sl = max(current_sl, max_price * (1 - trail_dist / 100))

        if trail_active and l <= current_sl and i > 0:
            exit_price = current_sl
            exit_reason = "trailing_stop"
            holding_candles = i + 1
            break

        if h >= tp:
            exit_price = tp
            exit_reason = "take_profit"
            holding_candles = i + 1
            break

    if exit_price is None:
        exit_price = float(future["close"].iloc[-1])
        exit_reason = "time_exit"

    total_cost_pct = 0.3
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 - total_cost_pct
    pnl_usd = position_size_usd * pnl_pct / 100

    return {
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "holding_candles": holding_candles,
    }


def make_decision(technical, last_close: float, params: Dict) -> Tuple[bool, float]:
    """Decision using SmartDecisionEngine logic."""
    signals = 0
    tech_score = technical.technical_score
    rsi = technical.rsi
    macd_hist = technical.macd_hist
    adx = technical.adx
    vol_pct = technical.volatility_pct

    # EMA200 trend filter
    if technical.ema_200 <= 0:
        return False, 0
    if last_close < technical.ema_200:
        return False, 0
    if not (technical.ema_9 > technical.ema_20 > technical.ema_50):
        return False, 0

    if tech_score >= params["tech_threshold"]:
        signals += 1
    if params["rsi_low"] <= rsi <= params["rsi_high"]:
        signals += 1
    if macd_hist > params["macd_min"]:
        signals += 1
    if adx > params["adx_min"]:
        signals += 1
    if technical.ema_9 > technical.ema_20 > technical.ema_50:
        signals += 1

    base = signals / 8.0 * 50
    tech_bonus = min((tech_score - 50) * 0.3, 15) if tech_score > 50 else 0
    rsi_bonus = 10 if 35 <= rsi <= 55 else (15 if rsi < 35 else 0)
    adx_bonus = min((adx - 18) * 0.5, 10) if adx > 18 else 0
    vol_penalty = -10 if vol_pct > 15 else (-5 if vol_pct > 10 else 0)
    confidence = max(0, min(100, base + tech_bonus + rsi_bonus + adx_bonus + vol_penalty))

    should_trade = signals >= params["min_signals"] and confidence >= params["min_confidence"]
    return should_trade, confidence


async def find_bull_market_period():
    """Find the best bull market period in the last 2 years."""
    print("🔍 Scanning for bull market periods in last 2 years...")

    # Test BTC for bull market detection - fetch 730 days
    btc_df = await fetch_real_candles("BTCUSDT", "1d", limit=730)
    if btc_df.empty:
        print("  ❌ Failed to fetch BTC data")
        return None, None

    print(f"  ✅ Got {len(btc_df)} days of BTC data ({btc_df['date'].iloc[0]} → {btc_df['date'].iloc[-1]})")

    # Find best 30-day rolling return window
    btc_df["return_30d"] = btc_df["close"].pct_change(periods=30) * 100

    # Drop NaN
    btc_clean = btc_df.dropna(subset=["return_30d"])

    # Top 5 best 30-day windows
    best_windows = btc_clean.nlargest(5, "return_30d")[["date", "close", "return_30d"]]

    print("\nTop 5 best 30-day bull market windows (BTC):")
    for _, row in best_windows.iterrows():
        start_date = (datetime.strptime(row["date"], "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        print(f"  {start_date} → {row['date']} | BTC: ${float(row['close']):.0f} | return: {float(row['return_30d']):+.1f}%")

    # Pick the best one
    best = best_windows.iloc[0]
    end_date = best["date"]
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    # Need 100 days before for EMA200 context
    context_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=100)).strftime("%Y-%m-%d")

    print(f"\n✅ Selected bull market window: {start_date} → {end_date}")
    print(f"   (Context data from: {context_start} for EMA200)")
    print(f"   BTC return in window: {float(best['return_30d']):+.1f}%")

    return context_start, end_date


async def run_bull_market_backtest(initial_capital: float = 20.0):
    """Run backtest during a bull market period."""
    print("=" * 80)
    print(f"🐂 BULL MARKET BACKTEST | Initial Capital: ${initial_capital:.2f}")
    print("=" * 80)
    print()

    try:
        from app.core.database import init_db
        await init_db()
    except Exception as e:
        print(f"⚠️  DB warning: {e}")

    # Find bull market period
    context_start, end_date = await find_bull_market_period()
    if not context_start:
        print("❌ Could not find bull market period.")
        return None

    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    symbols = [
        "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
        "ATOMUSDT", "NEARUSDT",
    ]

    print(f"\n📥 Fetching REAL Binance daily candles ({context_start} to {end_date})...")
    symbols_data = []
    for sym in symbols:
        df = await fetch_real_candles(sym, "1d", start_date=context_start, end_date=end_date)
        if df is not None and len(df) >= 100:
            # Filter to only the bull market window (last 30 days + 100 days context)
            symbols_data.append((sym, df))
            bull_window = df[df["date"] >= start_date]
            if not bull_window.empty:
                first_price = float(bull_window["close"].iloc[0])
                last_price = float(bull_window["close"].iloc[-1])
                change_pct = (last_price - first_price) / first_price * 100
                print(f"  ✅ {sym}: {len(df)} days | bull window {start_price_date(start_date, df)} → {last_price:.4f} ({change_pct:+.1f}%)")
            else:
                print(f"  ⚠️  {sym}: {len(df)} days but no bull window data")
        else:
            print(f"  ❌ {sym}: failed (got {len(df) if df is not None else 0} days)")
        await asyncio.sleep(0.4)

    if not symbols_data:
        print("\n❌ No real data fetched.")
        return None

    print(f"\n✅ Got real data for {len(symbols_data)} pairs")

    # Strategy params (verified)
    params = {
        "tech_threshold": 55,
        "rsi_low": 30,
        "rsi_high": 55,
        "macd_min": 0.0,
        "adx_min": 18,
        "min_signals": 3,
        "min_confidence": 50,
        "sl_pct": 0.8,
        "tp_pct": 0.5,
        "trail_after": 0.3,
        "trail_dist": 0.2,
        "max_hold": 8,
    }

    print(f"\n📋 Strategy Parameters (from SmartDecisionEngine):")
    print(f"   SL: {params['sl_pct']}% | TP: {params['tp_pct']}%")
    print(f"   Trailing: after +{params['trail_after']}%, distance {params['trail_dist']}%")
    print(f"   Max hold: {params['max_hold']} days")
    print(f"   Min signals: {params['min_signals']} | Min confidence: {params['min_confidence']}%")
    print(f"   EMA200 filter: ENABLED")
    print()

    # Run backtest
    from app.modules.technical_analysis.analyzer import TechnicalAnalyzer
    ta = TechnicalAnalyzer()

    capital = initial_capital
    position_size_pct = 25.0  # 25% per trade = $5 from $20

    trades = []
    daily_pnl = {}
    per_symbol = {sym: {"trades": 0, "wins": 0, "pnl": 0.0} for sym, _ in symbols_data}

    # Test only the bull market window (last 30 days of the data)
    for symbol, df in symbols_data:
        # Find where bull window starts (date >= start_date)
        bull_start_idx = None
        for i, row in df.iterrows():
            if row["date"] >= start_date:
                bull_start_idx = i
                break

        if bull_start_idx is None:
            continue

        # Test every day in the bull window
        test_indices = list(range(bull_start_idx, len(df) - params["max_hold"] - 1, 1))

        for entry_idx in test_indices:
            df_slice = df.iloc[:entry_idx + 1].copy()

            try:
                technical = ta.analyze(df_slice)
                if not technical.data_sufficient:
                    continue

                last_close = float(df_slice["close"].iloc[-1])
                trade_date = df_slice["date"].iloc[-1]

                # Only test within the bull window
                if trade_date < start_date or trade_date > end_date:
                    continue

                should_trade, confidence = make_decision(technical, last_close, params)
                if not should_trade:
                    continue

                position_size = capital * position_size_pct / 100
                entry_price = last_close

                result = simulate_trade(df, entry_idx, entry_price, params, position_size)
                if result is None:
                    continue

                capital += result["pnl_usd"]

                trades.append({
                    "symbol": symbol,
                    "date": trade_date,
                    "entry_price": entry_price,
                    "exit_price": result["exit_price"],
                    "exit_reason": result["exit_reason"],
                    "pnl_pct": result["pnl_pct"],
                    "pnl_usd": result["pnl_usd"],
                    "capital_after": capital,
                    "confidence": confidence,
                    "holding_days": result["holding_candles"],
                })

                daily_pnl[trade_date] = daily_pnl.get(trade_date, 0) + result["pnl_usd"]

                per_symbol[symbol]["trades"] += 1
                per_symbol[symbol]["pnl"] += result["pnl_usd"]
                if result["pnl_usd"] > 0:
                    per_symbol[symbol]["wins"] += 1

                if capital <= 1:
                    print(f"⚠️  Capital depleted after trade on {trade_date} ({symbol})")
                    break

            except Exception as e:
                continue

        if capital <= 1:
            break

    # Results
    print("=" * 80)
    print(f"📊 BULL MARKET BACKTEST RESULTS - {start_date} → {end_date}")
    print("=" * 80)
    print()

    if not trades:
        print("❌ No trades were executed in the bull market window.")
        print(f"   Ending capital: ${capital:.2f} (no change)")
        return None

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net_pnl = capital - initial_capital
    roi_pct = (net_pnl / initial_capital) * 100

    print(f"📅 الفترة: {start_date} → {end_date} (سوق صاعد حقيقي)")
    print()
    print(f"💰 رأس المال الابتدائي:  ${initial_capital:.2f}")
    print(f"💵 رأس المال النهائي:    ${capital:.2f}")
    print(f"📈 صافي الربح:           ${net_pnl:+.2f}")
    print(f"📊 نسبة الربح (ROI):     {roi_pct:+.2f}%")
    print()
    print(f"🎯 إجمالي الصفقات:       {len(trades)}")
    print(f"   ✅ رابحة:             {len(wins)}")
    print(f"   ❌ خاسرة:             {len(losses)}")
    print(f"   🎯 Win Rate:          {win_rate:.1f}%")
    print()
    print(f"💪 Profit Factor:        {profit_factor:.2f}")
    print(f"✨ إجمالي الأرباح:        ${gross_profit:+.2f}")
    print(f"💔 إجمالي الخسائر:       ${gross_loss:+.2f}")
    if wins:
        print(f"📊 متوسط الربح:          ${gross_profit/len(wins):+.4f}")
    if losses:
        print(f"📉 متوسط الخسارة:        ${gross_loss/len(losses):+.4f}")
    print()

    # Per-symbol
    print("📊 النتائج لكل عملة:")
    print(f"  {'العملة':<12} | {'الصفقات':>8} | {'رابحة':>6} | {'Win Rate':>9} | {'الربح':>10}")
    print("  " + "-" * 60)
    for sym, stats in sorted(per_symbol.items(), key=lambda x: -x[1]["pnl"]):
        if stats["trades"] > 0:
            wr = stats["wins"] / stats["trades"] * 100
            print(f"  {sym:<12} | {stats['trades']:>8} | {stats['wins']:>6} | {wr:>8.1f}% | ${stats['pnl']:>+9.2f}")
    print()

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        r = t["exit_reason"]
        exit_reasons.setdefault(r, {"count": 0, "wins": 0, "pnl": 0.0})
        exit_reasons[r]["count"] += 1
        exit_reasons[r]["pnl"] += t["pnl_usd"]
        if t["pnl_usd"] > 0:
            exit_reasons[r]["wins"] += 1
    print("📋 أسباب الخروج:")
    for reason, stats in sorted(exit_reasons.items(), key=lambda x: -x[1]["count"]):
        wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
        print(f"  {reason:<15}: {stats['count']:>3} صفقات | {wr:>5.1f}% WR | ${stats['pnl']:>+8.2f}")
    print()

    # Daily PnL
    print("📅 الأرباح اليومية:")
    sorted_days = sorted(daily_pnl.items())
    total_positive_days = sum(1 for _, p in sorted_days if p > 0)
    total_negative_days = sum(1 for _, p in sorted_days if p < 0)
    total_neutral_days = sum(1 for _, p in sorted_days if p == 0)
    print(f"  أيام رابحة: {total_positive_days} | أيام خاسرة: {total_negative_days} | محايدة: {total_neutral_days}")
    print()
    for date, pnl in sorted_days:
        marker = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        print(f"  {date} {marker} ${pnl:+.4f}")
    print()

    # Max drawdown
    if trades:
        cumulative = [initial_capital]
        for t in trades:
            cumulative.append(t["capital_after"])
        running_max = pd.Series(cumulative).cummax()
        drawdowns = running_max - pd.Series(cumulative)
        max_dd = drawdowns.max()
        max_dd_pct = max_dd / running_max.max() * 100 if running_max.max() > 0 else 0
        print(f"📉 أقصى خسارة (Drawdown): ${max_dd:.2f} ({max_dd_pct:.1f}%)")
    print()

    # SCENARIO
    print("=" * 80)
    print(f"💡 لو استثمرت $20 في هذه الفترة الصاعدة ({start_date} → {end_date})")
    print("=" * 80)
    print()
    print(f"  💵 كنت ستحصل على: ${capital:.2f} (من $20)")
    print(f"  📈 صافي الربح:     ${net_pnl:+.2f} ({roi_pct:+.1f}%)")
    if net_pnl > 0:
        print(f"  ✅ شهر مربح في سوق صاعد!")
    elif net_pnl < 0:
        print(f"  ❌ شهر خاسر حتى مع السوق الصاعد")
    else:
        print(f"  ➖ تعادل")
    print()

    # Save
    output = {
        "backtest_date": datetime.now().isoformat(),
        "bull_market_period": {"start": start_date, "end": end_date},
        "initial_capital_usd": initial_capital,
        "ending_capital_usd": capital,
        "net_pnl_usd": net_pnl,
        "roi_pct": roi_pct,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "trades_detail": trades,
        "per_symbol": per_symbol,
        "daily_pnl": daily_pnl,
    }

    with open("data/bull_market_backtest_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    return output


def start_price_date(start_date, df):
    """Get the price at the start of the bull window."""
    bull_window = df[df["date"] >= start_date]
    if not bull_window.empty:
        return float(bull_window["close"].iloc[0])
    return 0.0


if __name__ == "__main__":
    asyncio.run(run_bull_market_backtest(initial_capital=20.0))
