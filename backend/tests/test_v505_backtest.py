#!/usr/bin/env python
"""V505 backtest comparison: V501 strategy (-8% SL, 3% est slippage) vs V505 strategy (-15% SL floor, real-slippage-aware).

Run: cd /home/z/my-project/v503-fix/backend && python tests/test_v505_backtest.py

The backtest:
  - Loads historical OHLCV (15m × 90d) for several memecoin-like tokens
  - Simulates entries when a simple momentum signal triggers (close > MA20 + ATR)
  - Applies each strategy's exit rules to compute the realized PnL per trade
  - Reports: total return, win rate, max drawdown, average trade, # trades
  - Compares V501 (old) vs V505 (new) — shows the impact of the V505 fixes

NOTE: this is a SIMPLIFIED backtest — not a full simulation of the bot's
decision engine + AI agents. The goal is to measure the impact of the V505
exit-rule changes (SL floor/ceiling + slippage accounting), not to predict
real trading performance.
"""
from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data" / "cache"

# Strategies to compare
STRATEGIES = {
    "V501 (-8% SL, 3% est slip, 1.8xATR)": {
        "sl_atr_mult": 1.8,        # tight SL (V57 default before V505)
        "sl_floor_pct": 0.08,      # -8% fallback when no ATR
        "sl_ceiling_pct": 1.0,     # no ceiling
        "entry_slip_pct": 3.0,     # estimated 3% on entry
        "exit_slip_pct": 5.0,      # estimated 5% on exit
        "tp_mult": 11.0,           # 10x cap
        "trail_tiers": [(0.30, 0.25), (1.0, 0.35), (5.0, 0.45)],
        "sudden_dump_pct": 0.25,
        "stagnation_hours": 4,
        "stagnation_max_gain": 0.05,
        "max_hold_hours": 120,     # 5 days
    },
    "V505 (-15% floor, real-slip, 3xATR)": {
        "sl_atr_mult": 3.0,        # wider ATR multiple
        "sl_floor_pct": 0.15,      # -15% fallback
        "sl_ceiling_pct": 0.25,    # never wider than -25%
        "entry_slip_pct": 3.0,     # same estimated entry slip (no real wallet in backtest)
        "exit_slip_pct": 5.0,
        "tp_mult": 11.0,
        "trail_tiers": [(0.30, 0.25), (1.0, 0.35), (5.0, 0.45)],
        "sudden_dump_pct": 0.25,
        "stagnation_hours": 4,
        "stagnation_max_gain": 0.05,
        "max_hold_hours": 120,
    },
}


@dataclass
class Trade:
    entry_idx: int
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_idx: int = 0
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    peak_price: float = 0.0  # max price seen during the trade
    net_pnl_pct: float = 0.0


@dataclass
class BacktestResult:
    strategy_name: str
    symbol: str
    initial_capital: float = 1000.0
    final_equity: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    max_drawdown_pct: float = 0.0

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity - self.initial_capital) / self.initial_capital * 100

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl_pct > 0)
        return wins / len(self.trades) * 100

    @property
    def avg_trade_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.net_pnl_pct for t in self.trades) / len(self.trades)


def compute_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range (same formula as strategy_v2/indicators.py)."""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def find_entry_signals(df: pd.DataFrame, atr: pd.Series) -> List[int]:
    """Simple momentum entry: close > MA20 + 1×ATR (breakout)."""
    ma20 = df["close"].rolling(20).mean()
    signal = (df["close"] > ma20 + atr) & (df["close"].shift() <= ma20.shift() + atr.shift())
    return [i for i, s in enumerate(signal) if s and i > 20 and i < len(df) - 1]


def run_backtest(
    symbol: str, df: pd.DataFrame, strategy: dict, strategy_name: str
) -> BacktestResult:
    """Simulate trades on the OHLCV data with the given strategy."""
    atr = compute_atr(df)
    entry_idxs = find_entry_signals(df, atr)
    result = BacktestResult(strategy_name=strategy_name, symbol=symbol)
    equity = result.initial_capital
    peak_equity = equity

    for entry_idx in entry_idxs:
        # Position size = 5% of current equity (one trade at a time)
        position_usd = equity * 0.05
        entry_price = float(df.iloc[entry_idx]["close"])
        # Apply entry slippage (estimated)
        actual_entry = entry_price * (1 + strategy["entry_slip_pct"] / 100)

        # SL calculation
        atr_at_entry = float(atr.iloc[entry_idx]) if not pd.isna(atr.iloc[entry_idx]) else 0
        if atr_at_entry > 0:
            sl_distance = strategy["sl_atr_mult"] * atr_at_entry
            sl_pct = sl_distance / entry_price
            sl_pct = max(strategy["sl_floor_pct"],
                         min(strategy["sl_ceiling_pct"], sl_pct))
        else:
            sl_pct = strategy["sl_floor_pct"]
        sl_price = actual_entry * (1 - sl_pct)
        tp_price = actual_entry * strategy["tp_mult"]

        # Walk forward to find exit
        trade = Trade(
            entry_idx=entry_idx,
            entry_time=df.iloc[entry_idx]["ts"],
            entry_price=actual_entry,
            stop_loss=sl_price,
            take_profit=tp_price,
            peak_price=actual_entry,
        )

        max_hold_bars = strategy["max_hold_hours"] * 4  # 15m bars
        trail_tiers = strategy["trail_tiers"]
        sudden_dump_pct = strategy["sudden_dump_pct"]
        stagnation_bars = strategy["stagnation_hours"] * 4

        for j in range(entry_idx + 1, min(entry_idx + max_hold_bars, len(df))):
            row = df.iloc[j]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            if high > trade.peak_price:
                trade.peak_price = high

            peak_gain = (trade.peak_price / actual_entry - 1) if actual_entry > 0 else 0
            drop_from_peak = (trade.peak_price - low) / trade.peak_price if trade.peak_price > 0 else 0

            # Determine trailing tier
            trail_pct = None
            for threshold, tp in trail_tiers:
                if peak_gain >= threshold:
                    trail_pct = tp
            trailing_hit = trail_pct is not None and drop_from_peak >= trail_pct
            # Sudden dump only before trailing armed
            sudden_dump = (
                trail_pct is None and peak_gain >= 0.10 and drop_from_peak >= sudden_dump_pct
            )
            # Stagnation
            stagnation_hit = False
            age_bars = j - entry_idx
            if age_bars >= stagnation_bars:
                current_gain = close / actual_entry - 1 if actual_entry > 0 else 0
                if 0 < current_gain <= strategy["stagnation_max_gain"] and peak_gain < strategy["stagnation_max_gain"]:
                    stagnation_hit = True

            # Exit checks (priority order matches executor.py)
            reason = None
            if sudden_dump:
                reason = "sudden_dump"
            elif low <= sl_price:
                reason = "stop_loss"
            elif high >= tp_price:
                reason = "mega_tp"
            elif trailing_hit:
                reason = "smart_trailing"
            elif stagnation_hit:
                reason = "stagnation"
            elif age_bars >= max_hold_bars:
                reason = "time_exit"

            if reason:
                # Determine exit price (worst case for the reason)
                if reason == "stop_loss":
                    exit_price = sl_price
                elif reason == "mega_tp":
                    exit_price = tp_price
                elif reason == "sudden_dump":
                    # Exit at the sudden_dump threshold price
                    exit_price = trade.peak_price * (1 - sudden_dump_pct)
                elif reason == "smart_trailing":
                    exit_price = trade.peak_price * (1 - trail_pct)
                else:  # stagnation, time_exit
                    exit_price = close
                # Apply exit slippage
                actual_exit = exit_price * (1 - strategy["exit_slip_pct"] / 100)
                trade.exit_idx = j
                trade.exit_time = row["ts"]
                trade.exit_price = actual_exit
                trade.exit_reason = reason
                # PnL = (exit - entry) / entry * position_size_pct
                trade.net_pnl_pct = (actual_exit - actual_entry) / actual_entry * 100
                # Apply to equity (5% position)
                equity += position_usd * trade.net_pnl_pct / 100
                break

        result.trades.append(trade)
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        if dd > result.max_drawdown_pct:
            result.max_drawdown_pct = dd
        if equity <= 0:
            break

    result.final_equity = equity
    return result


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def main():
    print("V505 vs V501 backtest comparison")
    print("=" * 80)
    print("Strategy changes tested:")
    print("  V501 → V505:")
    print("    SL: 1.8×ATR (no floor) → 3.0×ATR with floor=-10%, ceiling=-25%")
    print("    SL fallback: -8% → -15%")
    print("    Slippage handling: estimated 3%/5% → real wallet-diff (backtest still uses est)")
    print("    Trailing / sudden_dump / stagnation: unchanged (V502 logic kept)")
    print()

    # Pick a basket of memecoin-like tokens
    tokens = ["BONK", "PEPE", "WIF", "DOGE", "SHIB"]
    all_results = []
    for symbol in tokens:
        path = DATA_DIR / f"{symbol}USDT_15m_90d.csv"
        if not path.exists():
            print(f"⚠️  {path} not found, skipping {symbol}")
            continue
        df = load_csv(path)
        print(f"\n--- {symbol} ({len(df)} 15m bars, {df['ts'].min()} → {df['ts'].max()}) ---")
        for name, strategy in STRATEGIES.items():
            result = run_backtest(symbol, df, strategy, name)
            all_results.append(result)
            print(
                f"  {name[:50]:50s} "
                f"trades={len(result.trades):3d} "
                f"winrate={result.win_rate:5.1f}% "
                f"avg={result.avg_trade_pct:+6.2f}% "
                f"return={result.total_return_pct:+8.2f}% "
                f"maxdd={result.max_drawdown_pct:5.1f}%"
            )
            # Exit reason breakdown
            from collections import Counter
            reasons = Counter(t.exit_reason for t in result.trades)
            reason_str = ", ".join(f"{r}={c}" for r, c in reasons.most_common())
            print(f"    exits: {reason_str}")

    # Aggregate across all tokens
    print(f"\n{'=' * 80}")
    print("--- Aggregate across all tokens ---")
    for name in STRATEGIES:
        agg = [r for r in all_results if r.strategy_name == name]
        if not agg:
            continue
        total_trades = sum(len(r.trades) for r in agg)
        if total_trades == 0:
            continue
        wins = sum(1 for r in agg for t in r.trades if t.net_pnl_pct > 0)
        avg_pnl = sum(t.net_pnl_pct for r in agg for t in r.trades) / total_trades
        # Aggregate return: weight each token equally
        agg_return = sum(r.total_return_pct for r in agg) / len(agg)
        max_dd = max(r.max_drawdown_pct for r in agg)
        print(f"\n{name}")
        print(f"  Total trades across {len(agg)} tokens: {total_trades}")
        print(f"  Win rate: {wins}/{total_trades} = {wins/total_trades*100:.1f}%")
        print(f"  Avg PnL per trade: {avg_pnl:+.2f}%")
        print(f"  Avg total return per token: {agg_return:+.2f}%")
        print(f"  Max drawdown (worst token): {max_dd:.1f}%")
        # Exit reason aggregate
        from collections import Counter
        reasons = Counter(t.exit_reason for r in agg for t in r.trades)
        print(f"  Exit reasons:")
        for r, c in reasons.most_common():
            print(f"    {r:20s} {c:4d} ({c/total_trades*100:.1f}%)")

    # Head-to-head
    print(f"\n{'=' * 80}")
    print("--- V501 → V505 impact (head-to-head) ---")
    v501 = next((r for r in all_results if "V501" in r.strategy_name), None)
    v505 = next((r for r in all_results if "V505" in r.strategy_name), None)
    # Actually need all per-strategy aggregates
    v501_results = [r for r in all_results if "V501" in r.strategy_name]
    v505_results = [r for r in all_results if "V505" in r.strategy_name]
    if v501_results and v505_results:
        v501_total = sum(r.total_return_pct for r in v501_results) / len(v501_results)
        v505_total = sum(r.total_return_pct for r in v505_results) / len(v505_results)
        v501_wr = sum(1 for r in v501_results for t in r.trades if t.net_pnl_pct > 0) / \
                  max(1, sum(len(r.trades) for r in v501_results)) * 100
        v505_wr = sum(1 for r in v505_results for t in r.trades if t.net_pnl_pct > 0) / \
                  max(1, sum(len(r.trades) for r in v505_results)) * 100
        v501_dd = max(r.max_drawdown_pct for r in v501_results)
        v505_dd = max(r.max_drawdown_pct for r in v505_results)
        v501_avg = sum(t.net_pnl_pct for r in v501_results for t in r.trades) / \
                   max(1, sum(len(r.trades) for r in v501_results))
        v505_avg = sum(t.net_pnl_pct for r in v505_results for t in r.trades) / \
                   max(1, sum(len(r.trades) for r in v505_results))

        print(f"\n  {'Metric':30s} {'V501':>12s} {'V505':>12s} {'Δ':>12s}")
        print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*12}")
        print(f"  {'Avg return per token':30s} {v501_total:>+11.2f}% {v505_total:>+11.2f}% {v505_total-v501_total:>+11.2f}%")
        print(f"  {'Win rate':30s} {v501_wr:>11.1f}% {v505_wr:>11.1f}% {v505_wr-v501_wr:>+11.1f}%")
        print(f"  {'Avg trade PnL':30s} {v501_avg:>+11.2f}% {v505_avg:>+11.2f}% {v505_avg-v501_avg:>+11.2f}%")
        print(f"  {'Max drawdown (worst token)':30s} {v501_dd:>11.1f}% {v505_dd:>11.1f}% {v505_dd-v501_dd:>+11.1f}%")

    print(f"\n{'=' * 80}")
    print("Notes:")
    print("  - This is a SIMPLIFIED backtest (single entry signal, no AI agents).")
    print("  - V505's real-slippage measurement can't be tested without real trades.")
    print("  - V505's wider SL (3xATR / -15% floor) reduces stop-out frequency but")
    print("    increases average loss per stop. Net effect depends on the token's volatility.")
    print("  - The trail/sudden_dump/stagnation exit logic is identical (V502 unchanged).")


if __name__ == "__main__":
    main()
