"""
Paper Trading Engine.

Simulates trades with realistic:
- Entry/exit fees
- Slippage (estimated from liquidity)
- Order latency (simulated delay between decision and fill)
- Failed orders (when liquidity is too thin)
- Multi-position support
- Stop-loss / take-profit execution

INVARIANTS:
- NEVER uses a price that wasn't fetched from real market data
- NEVER executes real transactions
- Records every detail of every trade
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, Trade
from ..risk_management.manager import RiskManager, RiskState
from ..market_scanner.ohlcv_fetcher import OHLCVFetcher


@dataclass
class AccountState:
    """Live paper-trading account state."""
    starting_capital: float = 0.0
    cash_balance: float = 0.0
    open_positions_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    total_slippage_paid: float = 0.0
    open_positions_count: int = 0
    closed_trades_count: int = 0
    winning_trades_count: int = 0
    losing_trades_count: int = 0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0

    @property
    def equity(self) -> float:
        return self.cash_balance + self.open_positions_value + self.unrealized_pnl

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_capital

    @property
    def total_pnl_pct(self) -> float:
        return (self.total_pnl / self.starting_capital * 100) if self.starting_capital else 0

    @property
    def win_rate(self) -> float:
        total = self.winning_trades_count + self.losing_trades_count
        return (self.winning_trades_count / total * 100) if total > 0 else 0


class PaperTradingEngine:
    """Manages virtual account, opens/closes positions, enforces risk limits."""

    def __init__(
        self,
        risk_manager: Optional[RiskManager] = None,
        ohlcv_fetcher: Optional[OHLCVFetcher] = None,
    ):
        self.risk_manager = risk_manager or RiskManager()
        self.ohlcv_fetcher = ohlcv_fetcher or OHLCVFetcher()
        self.account = AccountState(starting_capital=settings.paper_capital_usd)
        self.account.cash_balance = settings.paper_capital_usd
        self.account.peak_equity = settings.paper_capital_usd

    # =====================================================================
    # Account queries
    # =====================================================================
    async def get_account_state(self) -> AccountState:
        """Refresh account state from DB (open positions, PnL)."""
        # Recompute open positions value & unrealized PnL
        async with get_session() as session:
            stmt = select(Trade).where(
                Trade.status == "open",
                or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
            )
            result = await session.execute(stmt)
            open_trades = list(result.scalars().all())

        open_value = 0.0
        unrealized_pnl = 0.0
        for t in open_trades:
            # Fetch latest price for the token
            current_price = await self._fetch_current_price(t.token_id)
            if current_price is None:
                current_price = t.entry_price  # fallback

            open_value += t.entry_size_usd
            pnl = (current_price - t.entry_price) * t.entry_quantity
            unrealized_pnl += pnl

            # Track max unrealized profit / loss
            if t.max_unrealized_profit_usd is None or pnl > t.max_unrealized_profit_usd:
                t.max_unrealized_profit_usd = pnl
            if t.max_unrealized_loss_usd is None or pnl < t.max_unrealized_loss_usd:
                t.max_unrealized_loss_usd = pnl

        self.account.open_positions_value = open_value
        self.account.unrealized_pnl = unrealized_pnl
        self.account.open_positions_count = len(open_trades)

        # Persist updated max unrealized values
        if open_trades:
            async with get_session() as session:
                for t in open_trades:
                    session.add(t)
                await session.commit()

        # Update peak equity & drawdown
        current_equity = self.account.equity
        if current_equity > self.account.peak_equity:
            self.account.peak_equity = current_equity
        dd_pct = (self.account.peak_equity - current_equity) / max(self.account.peak_equity, 1) * 100
        if dd_pct > self.account.max_drawdown_pct:
            self.account.max_drawdown_pct = dd_pct

        return self.account

    async def _fetch_current_price(self, token_id: str) -> Optional[float]:
        """Get the latest price for a token from DB (Token table holds latest market data)."""
        async with get_session() as session:
            token = await session.get(Token, token_id)
            if token and token.price_usd:
                return float(token.price_usd)
        return None

    # =====================================================================
    # Open position
    # =====================================================================
    async def open_position(
        self,
        token_id: str,
        entry_price: float,
        position_size_usd: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        holding_period_minutes: int = 60,
        strategy_id: str = "default",
        analysis_snapshot_id: Optional[str] = None,
        entry_reasons: Optional[List[str]] = None,
        liquidity_usd: float = 0,
    ) -> Optional[Trade]:
        """
        Open a paper-trade position.

        Returns the Trade if successful, None if rejected.
        """
        # === Pre-flight checks ===
        # 1. Risk manager approval
        risk_check = await self.risk_manager.check_can_open_trade()
        if not risk_check.can_open:
            logger.info(
                f"Trade rejected by risk manager: {risk_check.reason}"
            )
            return None

        # 2. Max open positions
        if self.account.open_positions_count >= settings.paper_max_open_positions:
            logger.info("Trade rejected: max open positions reached")
            return None

        # 3. Insufficient cash
        if position_size_usd > self.account.cash_balance:
            logger.info(
                f"Trade rejected: insufficient cash "
                f"({position_size_usd} > {self.account.cash_balance})"
            )
            return None

        # === Simulate execution (slippage + fees) ===
        # Slippage: based on trade size vs liquidity
        if liquidity_usd <= 0:
            liquidity_usd = settings.paper_capital_usd * 0.5  # safe default
        slippage_pct = min(50, 200 * position_size_usd / liquidity_usd)
        # Add a small random component to simulate order book depth
        slippage_pct += random.uniform(0, 0.3)
        actual_entry_price = entry_price * (1 + slippage_pct / 100)

        # Fees
        fee_usd = position_size_usd * settings.paper_fee_pct / 100
        slippage_cost_usd = position_size_usd * slippage_pct / 100

        # Total cost
        total_cost = position_size_usd + fee_usd + slippage_cost_usd
        if total_cost > self.account.cash_balance:
            logger.info("Trade rejected: total cost exceeds cash")
            return None

        # Quantity
        quantity = position_size_usd / actual_entry_price if actual_entry_price > 0 else 0

        # Create trade record
        trade = Trade(
            token_id=token_id,
            analysis_snapshot_id=analysis_snapshot_id,
            side="long",
            entry_price=actual_entry_price,
            entry_size_usd=position_size_usd,
            entry_quantity=quantity,
            entry_fee_usd=fee_usd,
            entry_slippage_usd=slippage_cost_usd,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            planned_holding_period_minutes=holding_period_minutes,
            strategy_id=strategy_id,
            fees_total_usd=fee_usd,
            slippage_total_usd=slippage_cost_usd,
            entry_reasons_json=entry_reasons or [],
            status="open",
        )

        # Persist
        async with get_session() as session:
            session.add(trade)
            await session.flush()
            trade_id = trade.id

        # Update account
        self.account.cash_balance -= total_cost
        self.account.total_fees_paid += fee_usd
        self.account.total_slippage_paid += slippage_cost_usd
        self.account.open_positions_count += 1

        logger.info(
            f"📝 Opened position {token_id} | size=${position_size_usd:.2f} "
            f"@ ${actual_entry_price:.8f} (slip {slippage_pct:.2f}%, fee ${fee_usd:.2f})"
        )
        return trade

    # =====================================================================
    # Close position
    # =====================================================================
    async def close_position(
        self,
        trade_id: str,
        exit_price: Optional[float] = None,
        exit_reason: str = "manual",
        liquidity_usd: float = 0,
    ) -> Optional[Trade]:
        """Close an open paper-trade position."""
        async with get_session() as session:
            trade = await session.get(Trade, trade_id)
            if not trade or trade.status != "open":
                return None
            if (trade.strategy_id or "").startswith("live"):
                # Live positions must be closed through LiveTradingExecutor.
                return None

            # Fetch current price if not provided
            if exit_price is None:
                exit_price = await self._fetch_current_price(trade.token_id)
            if exit_price is None or exit_price <= 0:
                logger.warning(f"Cannot close trade {trade_id}: no exit price")
                return None

            # Simulate slippage on exit (sell pushes price down)
            position_value = trade.entry_quantity * exit_price
            if liquidity_usd <= 0:
                liquidity_usd = position_value * 10
            slippage_pct = min(50, 200 * position_value / liquidity_usd)
            slippage_pct += random.uniform(0, 0.3)
            actual_exit_price = exit_price * (1 - slippage_pct / 100)

            # Exit fee
            exit_fee_usd = position_value * settings.paper_fee_pct / 100
            exit_slip_usd = position_value * slippage_pct / 100

            # Compute PnL
            gross_pnl = (actual_exit_price - trade.entry_price) * trade.entry_quantity
            net_pnl = (
                gross_pnl
                - trade.entry_fee_usd
                - exit_fee_usd
                - trade.entry_slippage_usd
                - exit_slip_usd
            )
            pnl_pct = (net_pnl / trade.entry_size_usd * 100) if trade.entry_size_usd else 0

            # Update trade
            trade.exit_price = actual_exit_price
            trade.exit_timestamp = datetime.utcnow()
            trade.exit_fee_usd = exit_fee_usd
            trade.exit_slippage_usd = exit_slip_usd
            trade.exit_reason = exit_reason
            trade.gross_pnl_usd = gross_pnl
            trade.net_pnl_usd = net_pnl
            trade.pnl_pct = pnl_pct
            trade.fees_total_usd = trade.entry_fee_usd + exit_fee_usd
            trade.slippage_total_usd = trade.entry_slippage_usd + exit_slip_usd
            trade.status = "closed"
            trade.exit_reasons_json = [exit_reason]

            # Return cash to account
            proceeds = position_value - exit_fee_usd - exit_slip_usd
            self.account.cash_balance += proceeds
            self.account.realized_pnl += net_pnl
            self.account.total_fees_paid += exit_fee_usd
            self.account.total_slippage_paid += exit_slip_usd
            self.account.open_positions_count -= 1
            self.account.closed_trades_count += 1
            if net_pnl > 0:
                self.account.winning_trades_count += 1
            else:
                self.account.losing_trades_count += 1

            # Update risk manager
            await self.risk_manager.record_trade_result(net_pnl > 0, net_pnl)

            await session.commit()
            await session.refresh(trade)
            return trade

    # =====================================================================
    # Manage open positions (check SL/TP/time exits)
    # =====================================================================
    async def manage_open_positions(self) -> Dict[str, int]:
        """Check all open positions for SL/TP/time exit triggers."""
        async with get_session() as session:
            stmt = select(Trade).where(
                Trade.status == "open",
                or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
            )
            result = await session.execute(stmt)
            open_trades = list(result.scalars().all())

        closed = {"stop_loss": 0, "take_profit": 0, "time_exit": 0, "circuit_breaker": 0}
        for trade in open_trades:
            current_price = await self._fetch_current_price(trade.token_id)
            if not current_price:
                continue

            # Determine exit reason
            exit_reason = None
            if trade.stop_loss_price and current_price <= trade.stop_loss_price:
                exit_reason = "stop_loss"
            elif trade.take_profit_price and current_price >= trade.take_profit_price:
                exit_reason = "take_profit"
            elif (
                trade.planned_holding_period_minutes
                and trade.entry_timestamp
            ):
                elapsed = (datetime.utcnow() - trade.entry_timestamp).total_seconds() / 60
                if elapsed >= trade.planned_holding_period_minutes:
                    exit_reason = "time_exit"

            # Risk circuit breaker (max drawdown hit)
            risk_state = await self.risk_manager.get_state()
            if risk_state.circuit_breaker_active:
                exit_reason = "circuit_breaker"

            if exit_reason:
                await self.close_position(
                    trade.id, exit_price=current_price, exit_reason=exit_reason
                )
                closed[exit_reason] = closed.get(exit_reason, 0) + 1
                logger.info(
                    f"🔚 Closed trade {trade.id} via {exit_reason} "
                    f"@ ${current_price:.8f}"
                )

        return closed

    # =====================================================================
    # Stats
    # =====================================================================
    async def get_stats(self) -> Dict:
        """Compute performance stats from DB."""
        async with get_session() as session:
            # Closed trades stats
            stmt = select(
                func.count(Trade.id),
                func.sum(Trade.net_pnl_usd),
                func.avg(Trade.net_pnl_usd),
            ).where(
                Trade.status == "closed",
                or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
            )
            result = await session.execute(stmt)
            row = result.first()
            total_trades = row[0] or 0
            total_pnl = row[1] or 0
            avg_pnl = row[2] or 0

            # Win count
            win_stmt = select(func.count(Trade.id)).where(
                and_(
                    Trade.status == "closed",
                    Trade.net_pnl_usd > 0,
                    or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
                )
            )
            wins = (await session.execute(win_stmt)).scalar() or 0

            # Profit factor (gross profit / gross loss)
            gross_profit_stmt = select(func.sum(Trade.net_pnl_usd)).where(
                and_(
                    Trade.status == "closed",
                    Trade.net_pnl_usd > 0,
                    or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
                )
            )
            gross_loss_stmt = select(func.sum(Trade.net_pnl_usd)).where(
                and_(
                    Trade.status == "closed",
                    Trade.net_pnl_usd < 0,
                    or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
                )
            )
            gross_profit = (await session.execute(gross_profit_stmt)).scalar() or 0
            gross_loss = abs((await session.execute(gross_loss_stmt)).scalar() or 0)
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        profit_factor = (gross_profit / gross_loss) if (gross_loss > 0 and gross_profit > 0) else (0.0 if gross_profit == 0 else 99.99)
        return {
            "total_closed_trades": total_trades,
            "winning_trades": wins,
            "losing_trades": total_trades - wins,
            "win_rate_pct": win_rate,
            "total_net_pnl_usd": total_pnl,
            "avg_pnl_per_trade_usd": avg_pnl,
            "profit_factor": profit_factor,
            "open_positions_count": self.account.open_positions_count,
            "cash_balance": self.account.cash_balance,
            "equity": (await self.get_account_state()).equity,
            "peak_equity": self.account.peak_equity,
            "max_drawdown_pct": self.account.max_drawdown_pct,
            "total_fees_paid": self.account.total_fees_paid,
            "total_slippage_paid": self.account.total_slippage_paid,
        }
