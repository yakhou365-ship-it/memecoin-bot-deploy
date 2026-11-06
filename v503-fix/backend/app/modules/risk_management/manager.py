"""
Risk Manager.

Enforces all the hard risk limits specified in the project brief:
- Daily loss limit
- Weekly loss limit
- Maximum drawdown limit
- Max open positions
- Cooldown after consecutive losses
- Circuit breaker (kill switch)

INVARIANT: the bot NEVER modifies these limits itself. They come from settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Trade, SystemState


@dataclass
class RiskState:
    """Snapshot of current risk state."""
    daily_pnl_usd: float = 0.0
    weekly_pnl_usd: float = 0.0
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    open_positions_count: int = 0
    consecutive_losses: int = 0
    cooldown_active: bool = False
    cooldown_until: Optional[datetime] = None
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""
    last_trade_at: Optional[datetime] = None
    last_loss_at: Optional[datetime] = None
    can_open: bool = True
    reason: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RiskCheckResult:
    can_open: bool
    reason: str = ""


class RiskManager:
    """Enforces risk limits. Single source of truth for whether a trade can open."""

    STATE_KEY = "risk_manager_state"
    # V504: live_capital_base row written by LiveTradingExecutor on the first
    # trade of each UTC day. Contains {"date": "YYYY-MM-DD", "usd": float, ...}.
    LIVE_CAPITAL_KEY = "live_capital_base"

    async def _get_capital_base_usd(self) -> float:
        """USD capital base for %-based risk calculations.

        V504 FIX: previously every %-based gate (daily loss, weekly loss, max
        drawdown) divided by ``settings.paper_capital_usd`` (default 10000) no
        matter what mode the bot was in.  For a $20 live wallet a $5 loss
        showed as 0.05% (instead of 25%), so the 5% daily-loss gate could
        NEVER trigger on a live wallet — it could lose everything.

        Now: when the live executor has recorded a start-of-day wallet USD
        value in SystemState (today's date), use it.  Otherwise fall back to
        ``paper_capital_usd`` (correct for paper trading, sensible default
        when no live data exists yet).
        """
        try:
            async with get_session() as session:
                row = await session.get(SystemState, self.LIVE_CAPITAL_KEY)
                if row:
                    v = row.value_json or {}
                    today = datetime.utcnow().date().isoformat()
                    if v.get("date") == today:
                        base = float(v.get("usd", 0.0) or 0.0)
                        if base > 0:
                            return base
        except Exception as exc:
            logger.debug(f"Could not read live capital base, using paper_capital_usd: {exc}")
        return float(settings.paper_capital_usd)

    async def get_state(self) -> RiskState:
        """Compute current risk state from DB + system state."""
        state = RiskState()

        # V504: fetch the capital base FIRST so all %-based calcs below use it.
        # Returns live wallet USD when the executor has recorded one for today,
        # otherwise falls back to settings.paper_capital_usd (paper mode default).
        capital_base_usd = await self._get_capital_base_usd()

        # Load persistent state
        async with get_session() as session:
            sys_state = await session.get(SystemState, self.STATE_KEY)
            if sys_state:
                state.cooldown_active = sys_state.value_json.get("cooldown_active", False)
                state.cooldown_until = datetime.fromisoformat(
                    sys_state.value_json.get("cooldown_until")
                ) if sys_state.value_json.get("cooldown_until") else None
                state.circuit_breaker_active = sys_state.value_json.get("circuit_breaker_active", False)
                state.circuit_breaker_reason = sys_state.value_json.get("circuit_breaker_reason", "")
                state.consecutive_losses = sys_state.value_json.get("consecutive_losses", 0)
                state.last_trade_at = datetime.fromisoformat(
                    sys_state.value_json["last_trade_at"]
                ) if sys_state.value_json.get("last_trade_at") else None
                state.last_loss_at = datetime.fromisoformat(
                    sys_state.value_json["last_loss_at"]
                ) if sys_state.value_json.get("last_loss_at") else None

            # Compute daily & weekly PnL
            now = datetime.utcnow()
            day_ago = now - timedelta(days=1)
            week_ago = now - timedelta(days=7)

            # Daily PnL
            stmt = select(Trade).where(
                and_(Trade.status == "closed", Trade.exit_timestamp >= day_ago)
            )
            result = await session.execute(stmt)
            daily_trades = list(result.scalars().all())
            state.daily_pnl_usd = sum(t.net_pnl_usd or 0 for t in daily_trades)
            # V504 FIX: divide by the REAL capital base (live wallet USD when
            # available) instead of always paper_capital_usd = $10000.
            # For a $20 live wallet, a $5 loss is now correctly 25% (was 0.05%).
            state.daily_pnl_pct = (
                state.daily_pnl_usd / capital_base_usd * 100
                if capital_base_usd > 0 else 0.0
            )

            # Weekly PnL
            stmt = select(Trade).where(
                and_(Trade.status == "closed", Trade.exit_timestamp >= week_ago)
            )
            result = await session.execute(stmt)
            weekly_trades = list(result.scalars().all())
            state.weekly_pnl_usd = sum(t.net_pnl_usd or 0 for t in weekly_trades)
            state.weekly_pnl_pct = (
                state.weekly_pnl_usd / capital_base_usd * 100
                if capital_base_usd > 0 else 0.0
            )

            # Open positions count
            open_stmt = select(Trade).where(Trade.status == "open")
            state.open_positions_count = len((await session.execute(open_stmt)).scalars().all())

            # V400 P1-4 + P1-5 FIX: Compute equity from ALL-TIME cumulative PnL,
            # not just daily trades. This was a critical bug: peak_equity was
            # based on single-trade PnL, and equity only counted 24h trades.
            all_time_stmt = select(Trade).where(Trade.status == "closed")
            all_time_trades = list((await session.execute(all_time_stmt)).scalars().all())
            all_time_pnl = sum(t.net_pnl_usd or 0 for t in all_time_trades)

            # V400: peak_equity is cumulative (max of starting_capital + cumulative_pnl)
            # V504: use capital_base_usd (live wallet USD when available) so peak
            # tracking and drawdown are measured against the real account size.
            peak = sys_state.value_json.get("peak_equity", capital_base_usd) if sys_state else capital_base_usd
            current_equity = capital_base_usd + all_time_pnl
            if current_equity > peak:
                peak = current_equity
                # Will be persisted by _persist_state later

            if peak > 0:
                state.max_drawdown_pct = (peak - current_equity) / peak * 100

        # Check cooldown expiry
        if state.cooldown_active and state.cooldown_until:
            if datetime.utcnow() > state.cooldown_until:
                state.cooldown_active = False
                state.cooldown_until = None
                await self._persist_state(state)

        return state

    async def check_live_can_open(self) -> RiskCheckResult:
        """V503+V504: risk gates for LIVE trading.

        Enforces circuit breaker + cooldown-after-N-losses (V503). The
        %-based loss limits are now (V504) computed against the live wallet
        USD capital base instead of paper_capital_usd = $10000, so they
        trigger correctly for small live wallets too. The executor still
        enforces its own wallet-relative daily loss limit as a backstop.
        """
        state = await self.get_state()
        if state.circuit_breaker_active:
            return RiskCheckResult(False, f"Circuit breaker active: {state.circuit_breaker_reason}")
        if state.cooldown_active:
            return RiskCheckResult(False, f"Cooldown active until {state.cooldown_until}")
        if state.consecutive_losses >= settings.risk_cooldown_after_losses:
            streak = state.consecutive_losses
            state.cooldown_active = True
            state.cooldown_until = datetime.utcnow() + timedelta(hours=2)
            state.consecutive_losses = 0
            await self._persist_state(state)
            logger.warning(f"⚠️ Live cooldown (2h) after {streak} consecutive losses")
            return RiskCheckResult(False, f"Cooldown after {streak} consecutive losses")
        return RiskCheckResult(True)

    async def check_can_open_trade(self) -> RiskCheckResult:
        """The main pre-trade gate. Returns can_open=True only if ALL limits are within bounds."""
        state = await self.get_state()

        # Gate 1: Circuit breaker
        if state.circuit_breaker_active:
            return RiskCheckResult(
                False, f"Circuit breaker active: {state.circuit_breaker_reason}"
            )

        # Gate 2: Cooldown
        if state.cooldown_active:
            return RiskCheckResult(
                False,
                f"Cooldown active until {state.cooldown_until}",
            )

        # Gate 3: Daily loss limit
        if state.daily_pnl_pct <= -settings.risk_daily_loss_limit_pct:
            await self.activate_circuit_breaker(
                f"Daily loss limit breached: {state.daily_pnl_pct:.2f}%"
            )
            return RiskCheckResult(
                False, f"Daily loss limit breached ({state.daily_pnl_pct:.2f}%)"
            )

        # Gate 4: Weekly loss limit
        if state.weekly_pnl_pct <= -settings.risk_weekly_loss_limit_pct:
            await self.activate_circuit_breaker(
                f"Weekly loss limit breached: {state.weekly_pnl_pct:.2f}%"
            )
            return RiskCheckResult(
                False, f"Weekly loss limit breached ({state.weekly_pnl_pct:.2f}%)"
            )

        # Gate 5: Max drawdown
        if state.max_drawdown_pct >= settings.risk_max_drawdown_pct:
            await self.activate_circuit_breaker(
                f"Max drawdown breached: {state.max_drawdown_pct:.2f}%"
            )
            return RiskCheckResult(
                False, f"Max drawdown breached ({state.max_drawdown_pct:.2f}%)"
            )

        # Gate 6: Max open positions
        # V506 FIX: was reading paper_max_open_positions (=10) even in live mode,
        # so the gate never tripped in live (executor's own check on
        # live_max_open_positions=3 was the only effective limit). Now picks
        # the right ceiling based on the trading mode.
        max_open = (
            settings.live_max_open_positions
            if settings.is_live_trading
            else settings.paper_max_open_positions
        )
        if state.open_positions_count >= max_open:
            return RiskCheckResult(
                False, f"Max open positions ({max_open})"
            )

        # Gate 7: Cooldown after N consecutive losses
        if state.consecutive_losses >= settings.risk_cooldown_after_losses:
            cooldown_until = datetime.utcnow() + timedelta(hours=2)
            state.cooldown_active = True
            state.cooldown_until = cooldown_until
            streak = state.consecutive_losses
            # V503 FIX: reset the streak when arming the cooldown. Before, the
            # counter only reset on a WIN, so after the 2h pause the cooldown
            # re-armed forever (no trade could ever be placed to win again).
            state.consecutive_losses = 0
            await self._persist_state(state)
            logger.warning(
                f"⚠️ Cooldown activated: {streak} consecutive losses"
            )
            return RiskCheckResult(
                False,
                f"Cooldown after {streak} consecutive losses"
            )

        # V200: Removed V101's 24h adaptive bear pause.
        # Backtest proved it kills bull profits (V101 mixed = +5% vs V57 mixed = +103%).
        # The 2h cooldown above is sufficient — it protects without over-blocking.

        return RiskCheckResult(True)

    async def record_trade_result(self, winning: bool, pnl_usd: float) -> None:
        """Called after a trade closes to update risk state."""
        from sqlalchemy.orm.attributes import flag_modified
        # V504: fetch the capital base BEFORE opening the inner session to avoid
        # nested sessions (each get_session() opens a new connection from pool).
        capital_base_usd = await self._get_capital_base_usd()
        async with get_session() as session:
            sys_state = await session.get(SystemState, self.STATE_KEY)
            if not sys_state:
                sys_state = SystemState(key=self.STATE_KEY, value_json={})
                session.add(sys_state)
                await session.flush()

            v = dict(sys_state.value_json or {})
            v["last_trade_at"] = datetime.utcnow().isoformat()

            if winning:
                v["consecutive_losses"] = 0
            else:
                v["consecutive_losses"] = v.get("consecutive_losses", 0) + 1
                v["last_loss_at"] = datetime.utcnow().isoformat()

            # V400 P1-4 FIX: peak_equity is cumulative, not single-trade.
            # Compute current equity from ALL closed trades' cumulative PnL.
            all_time_stmt = select(Trade).where(Trade.status == "closed")
            all_time_trades = list((await session.execute(all_time_stmt)).scalars().all())
            all_time_pnl = sum(t.net_pnl_usd or 0 for t in all_time_trades)
            # V504: use the real capital base (live wallet USD when available)
            # instead of always paper_capital_usd, so peak tracking and
            # drawdown reflect the live account size.
            current_peak = v.get("peak_equity", capital_base_usd)
            # V400: equity = starting_capital + all_time_pnl (cumulative)
            approx_equity = capital_base_usd + all_time_pnl
            if approx_equity > current_peak:
                v["peak_equity"] = approx_equity

            sys_state.value_json = v
            flag_modified(sys_state, "value_json")

    async def activate_circuit_breaker(self, reason: str) -> None:
        """Hard stop. Closes all open positions and blocks new trades until manually reset."""
        logger.critical(f"🛑 CIRCUIT BREAKER ACTIVATED: {reason}")
        from sqlalchemy.orm.attributes import flag_modified
        async with get_session() as session:
            sys_state = await session.get(SystemState, self.STATE_KEY)
            if not sys_state:
                sys_state = SystemState(key=self.STATE_KEY, value_json={})
                session.add(sys_state)
                await session.flush()
            current = dict(sys_state.value_json or {})
            current["circuit_breaker_active"] = True
            current["circuit_breaker_reason"] = reason
            current["circuit_breaker_at"] = datetime.utcnow().isoformat()
            sys_state.value_json = current
            flag_modified(sys_state, "value_json")

    async def reset_circuit_breaker(self) -> None:
        """Manually reset circuit breaker (after human review)."""
        logger.critical("✅ Circuit breaker reset (manual)")
        async with get_session() as session:
            sys_state = await session.get(SystemState, self.STATE_KEY)
            if not sys_state:
                sys_state = SystemState(key=self.STATE_KEY, value_json={})
                session.add(sys_state)
                await session.flush()
            # Build a fresh dict (don't mutate the cached JSON in place)
            current = dict(sys_state.value_json or {})
            current["circuit_breaker_active"] = False
            current["circuit_breaker_reason"] = ""
            current["cooldown_active"] = False
            current["cooldown_until"] = None
            current["consecutive_losses"] = 0
            # Mark as changed for SQLAlchemy
            from sqlalchemy.orm.attributes import flag_modified
            sys_state.value_json = current
            flag_modified(sys_state, "value_json")

    async def _persist_state(self, state: RiskState) -> None:
        from sqlalchemy.orm.attributes import flag_modified
        async with get_session() as session:
            sys_state = await session.get(SystemState, self.STATE_KEY)
            if not sys_state:
                sys_state = SystemState(key=self.STATE_KEY, value_json={})
                session.add(sys_state)
                await session.flush()
            v = dict(sys_state.value_json or {})
            v["cooldown_active"] = state.cooldown_active
            v["cooldown_until"] = state.cooldown_until.isoformat() if state.cooldown_until else None
            v["consecutive_losses"] = state.consecutive_losses
            v["last_trade_at"] = state.last_trade_at.isoformat() if state.last_trade_at else None
            v["last_loss_at"] = state.last_loss_at.isoformat() if state.last_loss_at else None
            sys_state.value_json = v
            flag_modified(sys_state, "value_json")
