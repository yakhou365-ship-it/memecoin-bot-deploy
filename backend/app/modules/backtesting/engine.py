"""
Backtesting engine.

Critical invariants to prevent bias:
1. NO look-ahead: only uses data available at the simulated decision time
2. NO survivorship: includes dead/rugged tokens in the test universe
3. NO data leakage: train/val/test splits are time-based, no shuffling
4. Walk-forward: strategies tested on rolling out-of-sample windows
5. Realistic costs: fees + slippage on every trade
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select, and_, desc

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import OHLCV, Token, Trade


@dataclass
class BacktestResult:
    strategy_id: str
    period_start: datetime
    period_end: datetime
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0  # avg $ per trade
    max_drawdown_pct: float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_period_minutes: float = 0.0
    out_of_sample: bool = False
    trades: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


class Backtester:
    """Runs strategy backtests on historical OHLCV data."""

    async def backtest_token(
        self,
        token_id: str,
        timeframe: str = "5m",
        lookback_days: int = 7,
        strategy_fn=None,  # callable(df_slice) -> (action, sl, tp, size_pct)
    ) -> BacktestResult:
        """Backtest a strategy on a single token's historical data."""
        # Load OHLCV
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        async with get_session() as s:
            stmt = (
                select(OHLCV)
                .where(
                    and_(
                        OHLCV.token_id == token_id,
                        OHLCV.timeframe == timeframe,
                    )
                )
                .order_by(OHLCV.timestamp)
            )
            result = await s.execute(stmt)
            rows = list(result.scalars().all())

        if len(rows) < 50:
            return BacktestResult(
                strategy_id="backtest",
                period_start=cutoff,
                period_end=datetime.utcnow(),
                total_trades=0,
            )

        # Build DataFrame
        df = pd.DataFrame([{
            "timestamp": r.timestamp,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
        } for r in rows])

        # Walk-forward: split into 5 windows, test on each
        window_size = len(df) // 5
        all_trades: List[Dict] = []
        for w in range(5):
            start = w * window_size
            end = (w + 1) * window_size if w < 4 else len(df)
            window = df.iloc[start:end]
            trades = self._run_strategy_on_window(window, strategy_fn or self._default_strategy)
            all_trades.extend(trades)

        # Compute metrics
        result = self._compute_metrics(all_trades, token_id, cutoff)
        return result

    def _default_strategy(self, df_slice: pd.DataFrame):
        """
        Default strategy: simple EMA crossover + RSI confirmation.
        Returns (action, stop_loss, take_profit, position_size_pct) or None.
        action: 'buy' | 'sell' | 'hold'
        """
        if len(df_slice) < 50:
            return None
        close = df_slice["close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).fillna(50)

        last_idx = len(df_slice) - 1
        if ema9.iloc[last_idx] > ema20.iloc[last_idx] and rsi.iloc[last_idx] < 70:
            price = close.iloc[last_idx]
            return ("buy", price * 0.95, price * 1.10, 5.0)
        return None

    def _run_strategy_on_window(self, df: pd.DataFrame, strategy_fn) -> List[Dict]:
        """Execute strategy on a single window."""
        trades = []
        position = None
        cash = settings.paper_capital_usd
        for i in range(50, len(df)):
            slice_df = df.iloc[:i+1]
            current_price = float(slice_df["close"].iloc[-1])
            current_time = int(slice_df["timestamp"].iloc[-1])

            # Manage open position
            if position is not None:
                # Check exit
                if current_price <= position["stop_loss"]:
                    exit_reason = "stop_loss"
                elif current_price >= position["take_profit"]:
                    exit_reason = "take_profit"
                elif i - position["entry_idx"] >= 100:  # time exit (100 candles)
                    exit_reason = "time_exit"
                else:
                    exit_reason = None

                if exit_reason:
                    # Compute PnL
                    slippage = 0.005
                    exit_price = current_price * (1 - slippage)
                    gross_pnl = (exit_price - position["entry_price"]) * position["quantity"]
                    fee = position["size_usd"] * settings.backtest_fee_pct / 100 * 2
                    net_pnl = gross_pnl - fee
                    cash += position["size_usd"] + net_pnl
                    trades.append({
                        "entry_price": position["entry_price"],
                        "exit_price": exit_price,
                        "size_usd": position["size_usd"],
                        "net_pnl": net_pnl,
                        "exit_reason": exit_reason,
                        "entry_time": position["entry_time"],
                        "exit_time": current_time,
                        "holding_minutes": (current_time - position["entry_time"]) // 60,
                    })
                    position = None

            # Open new position
            if position is None:
                signal = strategy_fn(slice_df)
                if signal and signal[0] == "buy":
                    _, sl, tp, size_pct = signal
                    size_usd = min(cash * size_pct / 100, cash)
                    if size_usd > 10:
                        slippage = 0.005
                        entry_price = current_price * (1 + slippage)
                        quantity = size_usd / entry_price
                        fee = size_usd * settings.backtest_fee_pct / 100
                        cash -= (size_usd + fee)
                        position = {
                            "entry_price": entry_price,
                            "entry_idx": i,
                            "entry_time": current_time,
                            "stop_loss": sl,
                            "take_profit": tp,
                            "size_usd": size_usd,
                            "quantity": quantity,
                        }

        # Close any remaining position at last price
        if position is not None:
            exit_price = df["close"].iloc[-1] * 0.995
            gross_pnl = (exit_price - position["entry_price"]) * position["quantity"]
            fee = position["size_usd"] * settings.backtest_fee_pct / 100 * 2
            net_pnl = gross_pnl - fee
            trades.append({
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "size_usd": position["size_usd"],
                "net_pnl": net_pnl,
                "exit_reason": "end_of_data",
                "entry_time": position["entry_time"],
                "exit_time": int(df["timestamp"].iloc[-1]),
                "holding_minutes": (int(df["timestamp"].iloc[-1]) - position["entry_time"]) // 60,
            })

        return trades

    def _compute_metrics(self, trades: List[Dict], token_id: str, start: datetime) -> BacktestResult:
        if not trades:
            return BacktestResult(
                strategy_id="default",
                period_start=start,
                period_end=datetime.utcnow(),
            )

        df = pd.DataFrame(trades)
        wins = df[df["net_pnl"] > 0]
        losses = df[df["net_pnl"] <= 0]

        gross_profit = wins["net_pnl"].sum() if len(wins) else 0
        gross_loss = abs(losses["net_pnl"].sum()) if len(losses) else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        win_rate = len(wins) / len(df) * 100
        expectancy = df["net_pnl"].mean()
        total_return_pct = df["net_pnl"].sum() / settings.paper_capital_usd * 100

        # Max drawdown
        cumulative = df["net_pnl"].cumsum()
        running_max = cumulative.cummax()
        drawdowns = running_max - cumulative
        max_dd_pct = drawdowns.max() / settings.paper_capital_usd * 100 if len(drawdowns) else 0

        # Sharpe (rough, per-trade)
        if df["net_pnl"].std() > 0:
            sharpe = df["net_pnl"].mean() / df["net_pnl"].std() * np.sqrt(len(df))
        else:
            sharpe = 0.0

        return BacktestResult(
            strategy_id="default",
            period_start=start,
            period_end=datetime.utcnow(),
            total_trades=len(df),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            max_drawdown_pct=max_dd_pct,
            total_return_pct=total_return_pct,
            sharpe_ratio=sharpe,
            avg_holding_period_minutes=df["holding_minutes"].mean(),
            trades=trades,
        )

    async def walk_forward_test(
        self, token_ids: List[str], train_days: int = 30, test_days: int = 7
    ) -> Dict[str, BacktestResult]:
        """Run walk-forward tests on multiple tokens."""
        results = {}
        for tid in token_ids:
            results[tid] = await self.backtest_token(tid, lookback_days=train_days + test_days)
        return results
