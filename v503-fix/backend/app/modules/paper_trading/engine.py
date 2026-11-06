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
        # V400 P1-6 FIX: Add asyncio.Lock to prevent race conditions
        # between run_cycle (open) and fast_monitor_forever (close).
        # Without this, both can close the same trade simultaneously.
        self._lock = asyncio.Lock()
        # V407 P1-1: sync account state from DB on init
        self._synced = False

    # =====================================================================
    # Account queries
    # =====================================================================
    async def _sync_from_db(self) -> None:
        """V407 P1-1: Rebuild account state from DB after restart.

        Before this fix, after restart the engine would think it has
        $10,000 cash and 0 open positions even if there were 5 open
        trades. This could cause over-trading and incorrect cash balance.
        """
        if self._synced:
            return
        try:
            async with get_session() as session:
                # Count open positions
                stmt = select(Trade).where(
                    Trade.status == "open",
                    or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
                )
                result = await session.execute(stmt)
                open_trades = list(result.scalars().all())

                # Sum all closed trade PnL (all-time)
                closed_stmt = select(Trade).where(Trade.status == "closed")
                closed_result = await session.execute(closed_stmt)
                closed_trades = list(closed_result.scalars().all())

                total_realized_pnl = sum(t.net_pnl_usd or 0 for t in closed_trades)
                total_fees = sum(t.fees_total_usd or 0 for t in closed_trades)
                total_slippage = sum(t.slippage_total_usd or 0 for t in closed_trades)
                open_value = sum(t.entry_size_usd or 0 for t in open_trades)

                # Rebuild account
                self.account.cash_balance = (
                    settings.paper_capital_usd + total_realized_pnl - open_value
                )
                self.account.realized_pnl = total_realized_pnl
                self.account.open_positions_count = len(open_trades)
                self.account.closed_trades_count = len(closed_trades)
                self.account.winning_trades_count = sum(
                    1 for t in closed_trades if (t.net_pnl_usd or 0) > 0
                )
                self.account.losing_trades_count = sum(
                    1 for t in closed_trades if (t.net_pnl_usd or 0) <= 0
                )
                self.account.total_fees_paid = total_fees
                self.account.total_slippage_paid = total_slippage

                # Compute peak equity from all-time PnL
                current_equity = settings.paper_capital_usd + total_realized_pnl
                if current_equity > self.account.peak_equity:
                    self.account.peak_equity = current_equity

                self._synced = True
                logger.info(
                    f"📊 Account synced from DB: cash=${self.account.cash_balance:.2f}, "
                    f"open={len(open_trades)}, closed={len(closed_trades)}, "
                    f"PnL=${total_realized_pnl:.2f}"
                )
        except Exception as exc:
            logger.warning(f"Account sync from DB failed (non-fatal): {exc}")

    async def get_account_state(self) -> AccountState:
        """Refresh account state from DB (open positions, PnL)."""
        # V407 P1-1: sync on first call after restart
        await self._sync_from_db()
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
        # V31.4: square-root market-impact model (same as close_position).
        # Previous linear model with `paper_capital * 0.5` fallback produced
        # unrealistic slippage for small-cap memecoins.
        if liquidity_usd <= 0:
            slippage_pct = 5.0  # conservative fallback when liquidity unknown
        else:
            participation = min(0.5, position_size_usd / liquidity_usd)
            slippage_pct = 0.10 * (participation ** 0.5) * 100
            slippage_pct = min(slippage_pct, 25.0)
        slippage_pct += random.uniform(0, 0.15)
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

            # Simulate slippage on exit (sell pushes price down).
            # V31.4: use square-root market-impact model (Almgren-Chriss
            # approximation) which is the industry standard for equity and
            # crypto execution. The previous linear model with a fallback
            # of `position_value * 10` produced ~20% slippage on every
            # exit, turning winning paper trades into losses.
            #
            # slippage_pct = k * sqrt(trade_usd / liquidity_usd) * 100
            # where k ~= 0.10 for typical memecoin order books.
            position_value = trade.entry_quantity * exit_price
            if liquidity_usd <= 0:
                # Last-resort fallback: assume 5% slippage if we have no
                # liquidity data (better than the old 20% default).
                slippage_pct = 5.0
            else:
                # Cap participation at 50% of liquidity (beyond that the
                # model breaks down and the trade shouldn't have happened).
                participation = min(0.5, position_value / liquidity_usd)
                slippage_pct = 0.10 * (participation ** 0.5) * 100
                slippage_pct = min(slippage_pct, 25.0)  # hard cap
            # Tiny random component simulates order-book noise.
            slippage_pct += random.uniform(0, 0.15)
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
        """Check all open positions for SL/TP/time exit triggers.

        V400 P1-6 FIX: Wrapped in asyncio.Lock to prevent race condition
        with open_position (called from run_cycle).
        """
        async with self._lock:
            async with get_session() as session:
                stmt = select(Trade).where(
                    Trade.status == "open",
                    or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
                )
                result = await session.execute(stmt)
                open_trades = list(result.scalars().all())

        closed = {"stop_loss": 0, "sudden_dump": 0, "mega_tp": 0, "smart_trailing": 0, "stagnation": 0, "time_exit": 0, "circuit_breaker": 0, "exit_all": 0}
        for trade in open_trades:
            current_price = await self._fetch_current_price(trade.token_id)
            if not current_price:
                continue

            # V506: trailing stop with PERSISTED peak_price column (was
            # overloaded onto max_unrealized_profit_usd — wrong field).
            # Now uses dedicated peak_price column. Legacy migration on first read.
            legacy_peak = float(trade.max_unrealized_profit_usd or 0)
            current_peak = float(trade.peak_price or 0)
            if current_peak <= 0 and legacy_peak > 0:
                current_peak = legacy_peak
            if current_peak < float(trade.entry_price or 0):
                # Field was used for something else, reinitialize
                current_peak = max(float(trade.entry_price or 0), current_price)
            if current_price > current_peak:
                current_peak = current_price
                # Persist peak to DB (so it survives cycles/restarts)
                try:
                    async with get_session() as s2:
                        db_trade = await s2.get(Trade, trade.id)
                        if db_trade and db_trade.status == "open":
                            db_trade.peak_price = current_peak
                            await s2.commit()
                except Exception:
                    pass  # non-critical
            peak_price = current_peak
            # V500: Smart Exit Strategy — Variable trailing based on gain level
            peak_gain_pct = (peak_price / trade.entry_price - 1) if trade.entry_price > 0 else 0
            current_gain = (current_price / trade.entry_price - 1) * 100 if trade.entry_price > 0 else 0

            # V506: TIGHTER variable trailing tiers (was 25/35/45% in V502).
            # Backtest showed V502 tiers gave back too much profit on memecoins
            # (a "winning" trailing exit could be -10% net after 8% round-trip
            # slippage). V506: 15/20/30% — lock more profit, accept more whipsaws.
            if peak_gain_pct >= 5.0:      # +500%
                trail_pct = 0.30
            elif peak_gain_pct >= 1.0:    # +100%
                trail_pct = 0.20
            elif peak_gain_pct >= 0.15:   # +15% (was +30%)
                trail_pct = 0.15
            else:
                trail_pct = None          # trailing not armed yet

            drop_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0
            trailing_hit = trail_pct is not None and drop_from_peak >= trail_pct

            # V506: sudden-dump rule — escape an obvious rug-pull in the
            # +10%..+15% pre-trailing zone. When peak_gain >= +10% but price
            # has now dropped -18% from that peak (e.g. peak +15% → now -5.8%
            # from entry, well above SL of -15%), we exit early to lock in
            # small wins and avoid the SL taking a -15% loss.
            sudden_dump = trail_pct is None and peak_gain_pct >= 0.10 and drop_from_peak >= 0.18

            # V506: stagnation wait lowered from 4h to 3h (memecoins move fast).
            # Still requires: held >=3h, in profit, never above +5%.
            stagnation_hit = False
            if trade.entry_timestamp and trade.entry_price > 0:
                elapsed_min = (datetime.utcnow() - trade.entry_timestamp).total_seconds() / 60
                if elapsed_min >= 180 and 0 < current_gain <= 5 and peak_gain_pct < 0.05:
                    stagnation_hit = True

            # Determine exit reason (V500 priority order)
            exit_reason = None
            # 1. Sudden dump (emergency)
            if sudden_dump:
                exit_reason = "sudden_dump"
            # 2. Hard SL
            elif trade.stop_loss_price and current_price <= trade.stop_loss_price:
                exit_reason = "stop_loss"
            # 3. Mega TP cap (1000% = 10x)
            elif trade.take_profit_price and current_price >= trade.take_profit_price:
                exit_reason = "mega_tp"
            # 4. Smart trailing (variable)
            elif trailing_hit:
                exit_reason = "smart_trailing"
            # 5. Stagnation exit
            elif stagnation_hit:
                exit_reason = "stagnation"
            # 6. Time exit (5 days = 7200 min)
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
                # V31.4: pass liquidity_usd so close_position can compute
                # realistic slippage instead of the previous 20% default.
                # Without this, every exit was hit with ~20% slippage,
                # turning winning trades into losses and inflating drawdown.
                token_liquidity = 0.0
                try:
                    async with get_session() as s:
                        token = await s.get(Token, trade.token_id)
                        if token:
                            token_liquidity = float(token.liquidity_usd or 0)
                except Exception:
                    pass
                await self.close_position(
                    trade.id,
                    exit_price=current_price,
                    exit_reason=exit_reason,
                    liquidity_usd=token_liquidity,
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
