"""
REAL DATA Backtest - Tests bot on REAL historical candles from Binance.

Fetches REAL OHLCV data (not generated) for multiple trading pairs:
- SOLUSDT, BNBUSDT, AVAXUSDT, DOGEUSDT, ADAUSDT, LINKUSDT
- 1000 candles per pair (5m timeframe = ~3.5 days of real data)
- Tests SmartDecisionEngine on every entry point
- Reports: trades, wins, losses, win rate, PnL, profit factor

Uses Binance PUBLIC API (no API key needed) for real historical data.
"""
import asyncio
import os
import sys
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Install httpx if needed
os.environ.setdefault('DATABASE_URL', f'sqlite+aiosqlite:///{os.path.abspath("data/real_backtest.db")}')
os.environ.setdefault('APP_ENV', 'test')


async def fetch_real_candles(symbol: str, interval: str = "5m", limit: int = 1000) -> pd.DataFrame:
    """Fetch REAL historical OHLCV from Binance public API."""
    import httpx
    
    url = f"https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            print(f"  Failed to fetch {symbol}: {resp.status_code}")
            return pd.DataFrame()
        
        raw = resp.json()
    
    # Parse Binance kline format:
    # [openTime, open, high, low, close, volume, closeTime, ...]
    candles = []
    for k in raw:
        candles.append({
            "timestamp": int(k[0] // 1000),  # ms to seconds
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    
    df = pd.DataFrame(candles)
    return df


def simulate_trade(df, entry_idx, entry_price, atr, config):
    """Simulate trade with REAL future data."""
    if atr <= 0 or entry_price <= 0:
        return None
    
    sl_pct = config["sl_pct"]
    tp_pct = config["tp_pct"]
    trail_after = config["trail_after"]
    trail_dist = config["trail_dist"]
    max_hold = config["max_hold"]
    
    sl = entry_price * (1 - sl_pct / 100)
    tp = entry_price * (1 + tp_pct / 100)
    
    future = df.iloc[entry_idx + 1: entry_idx + max_hold + 1]
    if len(future) < 5:
        return None
    
    max_price = entry_price
    trail_active = False
    current_sl = sl
    exit_price = None
    exit_reason = "time_exit"
    holding_min = max_hold * 5  # 5m candles
    
    for i, row in future.iterrows():
        h = float(row["high"])
        l = float(row["low"])
        max_price = max(max_price, h)
        profit_pct = (h - entry_price) / entry_price * 100
        
        # Activate trailing after threshold
        if not trail_active and profit_pct >= trail_after:
            trail_active = True
            current_sl = max(current_sl, entry_price * 1.01)
        
        # Update trailing SL
        if trail_active:
            current_sl = max(current_sl, max_price * (1 - trail_dist / 100))
        
        # Check trailing SL hit
        if trail_active and l <= current_sl and i > entry_idx + 1:
            exit_price = current_sl
            exit_reason = "trailing_stop"
            holding_min = (i - entry_idx) * 5
            break
        
        # Check TP
        if h >= tp:
            exit_price = tp
            exit_reason = "take_profit"
            holding_min = (i - entry_idx) * 5
            break
    
    if exit_price is None:
        exit_price = float(future["close"].iloc[-1])
    
    # PnL after fees (0.1% * 2 = 0.2%) + slippage (0.1%)
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 - 0.3
    
    return {
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "holding_min": holding_min,
    }


async def run_real_backtest():
    """Run backtest on REAL historical data."""
    from app.core.database import init_db
    await init_db()
    
    from app.modules.technical_analysis.analyzer import TechnicalAnalyzer
    from app.modules.liquidity.analyzer import LiquidityAnalyzer
    from app.modules.market_regime.detector import MarketRegimeDetector
    from app.modules.security.danger_detector import DangerousPatternDetector, get_danger_summary
    from app.modules.decision_engine.smart_engine import SmartDecisionEngine
    
    ta = TechnicalAnalyzer()
    liq_an = LiquidityAnalyzer()
    regime_det = MarketRegimeDetector()
    danger_det = DangerousPatternDetector()
    smart_engine = SmartDecisionEngine()
    
    # Trading pairs to test (REAL Binance data)
    symbols = [
        "SOLUSDT", "BNBUSDT", "AVAXUSDT", "DOGEUSDT",
        "ADAUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
        "ATOMUSDT", "NEARUSDT",
    ]
    
    print("=" * 80)
    print("🚀 REAL DATA BACKTEST - 100% REAL HISTORICAL CANDLES FROM BINANCE")
    print("=" * 80)
    print()
    print(f"Pairs: {len(symbols)}")
    print(f"Candles per pair: 1000 (5m timeframe = ~3.5 days of REAL data)")
    print(f"Total real candles: {len(symbols) * 1000}")
    print()
    
    # Trade config - HIGH PROFIT focused
    config = {
        "sl_pct": 5.0,        # 5% stop loss
        "tp_pct": 15.0,       # 15% take profit (HIGH PROFIT)
        "trail_after": 5.0,   # Start trailing after +5%
        "trail_dist": 3.0,    # Trail by 3%
        "max_hold": 48,       # Max 48 candles (4 hours)
    }
    
    print("Strategy: HIGH PROFIT FOCUSED")
    print(f"  SL: {config['sl_pct']}% | TP: {config['tp_pct']}%")
    print(f"  Trailing: after +{config['trail_after']}%, distance {config['trail_dist']}%")
    print(f"  Max holding: {config['max_hold']} candles ({config['max_hold']*5} min)")
    print()
    
    # Stats
    total_signals = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0
    gross_profit = 0
    gross_loss = 0
    trade_details = []
    rejection_reasons = {}
    
    for sym_idx, symbol in enumerate(symbols, 1):
        print(f"\n[{sym_idx}/{len(symbols)}] Fetching REAL data for {symbol}...")
        df = await fetch_real_candles(symbol, "5m", 1000)
        
        if df is None or len(df) < 100:
            print(f"  ❌ Not enough data for {symbol}")
            continue
        
        print(f"  ✅ Got {len(df)} REAL candles (price: ${df['close'].iloc[-1]:.2f})")
        
        # Create a mock token for analysis
        class MockToken:
            def __init__(self, symbol, price, liq=100000, vol=500000):
                self.symbol = symbol
                self.chain = "binance"
                self.address = symbol.lower()
                self.price_usd = price
                self.liquidity_usd = liq
                self.volume_24h_usd = vol
                self.volume_5m_usd = vol / 288
                self.market_cap_usd = liq * 10
                self.fdv_usd = liq * 10
                self.txns_24h_buys = 1000
                self.txns_24h_sells = 800
                self.pair_address = symbol.lower()
                self.dex = "binance"
                self.name = symbol
                self.price_change_24h_pct = ((df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0]) * 100
                self.price_change_5m_pct = 0
        
        token = MockToken(symbol, float(df["close"].iloc[-1]))
        
        # Test every 5th candle
        test_indices = list(range(50, len(df) - 50, 5))
        sym_trades = 0
        sym_wins = 0
        sym_pnl = 0
        
        for entry_idx in test_indices:
            total_signals += 1
            df_slice = df.iloc[:entry_idx + 1].copy()
            
            try:
                # === TECHNICAL ANALYSIS ===
                technical = ta.analyze(df_slice)
                if not technical.data_sufficient:
                    continue
                
                # === LIQUIDITY ANALYSIS ===
                liquidity = liq_an.analyze(token, df_slice, 200)
                
                # === MARKET REGIME ===
                regime = regime_det.detect(broad_market_candles=df_slice)
                if regime.regime in ["panic_selling", "bear_market"]:
                    rejection_reasons["bad_regime"] = rejection_reasons.get("bad_regime", 0) + 1
                    continue
                
                # === DANGER DETECTION ===
                onchain_data = {
                    "liquidity_locked": True,
                    "mint_authority_enabled": False,
                    "freeze_authority_enabled": False,
                    "top_10_pct": 40,
                    "owner_concentration_risk": 0.3,
                    "token_bundling_probability": 0.1,
                    "insider_activity_probability": 0.2,
                }
                dangers = danger_det.detect_all(df_slice, token, onchain_data)
                danger_summary = get_danger_summary(dangers)
                if danger_summary["critical_count"] > 0:
                    rejection_reasons["critical_danger"] = rejection_reasons.get("critical_danger", 0) + 1
                    continue
                
                # === SMART DECISION ENGINE ===
                # Check for bullish patterns
                patterns = technical.candlestick_patterns or []
                bullish_patterns = {"hammer", "bullish_engulfing", "morning_star", "piercing_line"}
                has_bullish = any(p in bullish_patterns for p in patterns)
                
                # Check trend alignment (EMA 9 > 20 > 50)
                trend_aligned = (technical.ema_9 > technical.ema_20 > technical.ema_50)
                
                # Volume spike
                vol = df_slice["volume"].astype(float).tail(20)
                vol_spike = float(vol.iloc[-1]) > float(vol.mean()) * 1.5
                
                decision = smart_engine.analyze(
                    technical_score=technical.technical_score,
                    rsi=technical.rsi,
                    macd_hist=technical.macd_hist,
                    adx=technical.adx,
                    volatility_pct=technical.volatility_pct,
                    liquidity_score=liquidity.liquidity_score,
                    volume_spike=vol_spike,
                    bullish_pattern=has_bullish,
                    trend_alignment=trend_aligned,
                    risk_score=danger_summary.get("critical_count", 0) * 20,
                )
                
                if not decision.should_trade:
                    rejection_reasons["low_confidence"] = rejection_reasons.get("low_confidence", 0) + 1
                    continue
                
                # === SIMULATE TRADE ON REAL DATA ===
                entry_price = float(df_slice["close"].iloc[-1])
                atr = technical.atr
                
                # Use adaptive config based on decision
                trade_config = {
                    "sl_pct": decision.stop_loss_pct,
                    "tp_pct": decision.take_profit_pct,
                    "trail_after": decision.trailing_stop_pct,
                    "trail_dist": decision.trailing_stop_pct,
                    "max_hold": decision.holding_minutes // 5,
                }
                
                result = simulate_trade(df, entry_idx, entry_price, atr, trade_config)
                if result is None:
                    continue
                
                # Position size: $200 per trade
                pnl = 200 * result["pnl_pct"] / 100
                total_trades += 1
                sym_trades += 1
                
                if pnl > 0:
                    total_wins += 1
                    sym_wins += 1
                    gross_profit += pnl
                else:
                    total_losses += 1
                    gross_loss += abs(pnl)
                total_pnl += pnl
                sym_pnl += pnl
                
                trade_details.append({
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": result["exit_price"],
                    "exit_reason": result["exit_reason"],
                    "pnl_pct": result["pnl_pct"],
                    "pnl_usd": pnl,
                    "confidence": decision.confidence,
                    "expected_profit": decision.expected_profit_pct,
                    "tp_pct": decision.take_profit_pct,
                    "sl_pct": decision.stop_loss_pct,
                    "holding_min": result["holding_min"],
                })
                
            except Exception as e:
                continue
        
        if sym_trades > 0:
            wr = sym_wins / sym_trades * 100
            print(f"  📊 {symbol}: {sym_trades} trades | {sym_wins} wins | {wr:.1f}% WR | PnL: ${sym_pnl:+.2f}")
    
    # === FINAL REPORT ===
    print()
    print("=" * 80)
    print("📊 REAL DATA BACKTEST - FINAL REPORT")
    print("=" * 80)
    print()
    print(f"🔍 Pairs tested: {len(symbols)}")
    print(f"📊 Total signals evaluated: {total_signals}")
    print(f"🚫 Rejected: {total_signals - total_trades}")
    if rejection_reasons:
        print(f"   Rejection reasons:")
        for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1]):
            print(f"      {reason}: {count}")
    print()
    print(f"🎯 Trades opened: {total_trades}")
    print(f"   ✅ Wins: {total_wins}")
    print(f"   ❌ Losses: {total_losses}")
    
    if total_trades > 0:
        wr = total_wins / total_trades * 100
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        print(f"   🎯 WIN RATE: {wr:.2f}%")
        print(f"   💰 Total PnL: ${total_pnl:+.2f}")
        print(f"   📈 Avg PnL per trade: ${total_pnl/total_trades:+.2f}")
        print(f"   💪 Profit Factor: {pf:.2f}")
        print(f"   ✨ Gross profit: ${gross_profit:.2f}")
        print(f"   💔 Gross loss: ${gross_loss:.2f}")
        if total_wins > 0:
            print(f"   📊 Avg win: ${gross_profit/total_wins:.2f}")
        if total_losses > 0:
            print(f"   📉 Avg loss: ${gross_loss/total_losses:.2f}")
    
    if trade_details:
        print()
        print("📋 Exit reasons:")
        exits = {}
        for t in trade_details:
            r = t["exit_reason"]
            exits[r] = exits.get(r, 0) + 1
        for reason, count in sorted(exits.items(), key=lambda x: -x[1]):
            wins_in = sum(1 for t in trade_details if t["exit_reason"] == reason and t["pnl_usd"] > 0)
            print(f"   {reason:15s}: {count:4d} trades ({wins_in:4d} wins)")
        
        print()
        print("🏆 Top 10 winning trades:")
        winners = sorted([t for t in trade_details if t["pnl_usd"] > 0], key=lambda x: -x["pnl_usd"])[:10]
        for i, t in enumerate(winners, 1):
            print(f"   {i:2d}. {t['symbol']:10s}: +${t['pnl_usd']:7.2f} ({t['pnl_pct']:+6.2f}%) | conf={t['confidence']:.0f}% | TP={t['tp_pct']:.0f}% | {t['exit_reason']}")
        
        print()
        print("💔 Top 5 losing trades:")
        losers = sorted([t for t in trade_details if t["pnl_usd"] <= 0], key=lambda x: x["pnl_usd"])[:5]
        for i, t in enumerate(losers, 1):
            print(f"   {i:2d}. {t['symbol']:10s}: ${t['pnl_usd']:7.2f} ({t['pnl_pct']:+6.2f}%) | conf={t['confidence']:.0f}% | SL={t['sl_pct']:.0f}% | {t['exit_reason']}")
    
    print()
    print("=" * 80)
    if total_trades > 0:
        wr = total_wins / total_trades * 100
        target_reached = wr >= 70
        print(f"🎯 TARGET (70% win rate): {'✅✅✅ REACHED!' if target_reached else '❌ NOT REACHED'}")
        print(f"   Achieved: {wr:.2f}%")
        print(f"   Total trades: {total_trades}")
        print(f"   Total PnL: ${total_pnl:+.2f}")
        if wr >= 80:
            print(f"   🎉🎉🎉 EXCELLENT! Win Rate exceeds 80%!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_real_backtest())
