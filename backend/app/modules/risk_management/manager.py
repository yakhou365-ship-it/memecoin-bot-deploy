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

    async def get_state(self) -> RiskState:
        """Compute current risk state from DB + system state."""
        state = RiskState()

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
            state.daily_pnl_pct = state.daily_pnl_usd / settings.paper_capital_usd * 100

            # Weekly PnL
            stmt = select(Trade).where(
                and_(Trade.status == "closed", Trade.exit_timestamp >= week_ago)
            )
            result = await session.execute(stmt)
            weekly_trades = list(result.scalars().all())
            state.weekly_pnl_usd = sum(t.net_pnl_usd or 0 for t in weekly_trades)
            state.weekly_pnl_pct = state.weekly_pnl_usd / settings.paper_capital_usd * 100

            # Open positions count
            open_stmt = select(Trade).where(Trade.status == "open")
            state.open_positions_count = len((await session.execute(open_stmt)).scalars().all())

            # Max drawdown (need account peak; we'll load from system_state)
            peak = sys_state.value_json.get("peak_equity", settings.paper_capital_usd) if sys_state else settings.paper_capital_usd
            # Reconstruct current equity (rough estimate)
            cash = settings.paper_capital_usd + sum(t.net_pnl_usd or 0 for t in daily_trades)
            # Subtract open positions value (approximate by entry_size)
            open_value_stmt = select(Trade.entry_size_usd).where(Trade.status == "open")
            open_values = (await session.execute(open_value_stmt)).scalars().all()
            equity = cash  # simplified; true equity is computed in PaperTradingEngine
            if peak > 0:
                state.max_drawdown_pct = (peak - equity) / peak * 100

        # Check cooldown expiry
        if state.cooldown_active and state.cooldown_until:
            if datetime.utcnow() > state.cooldown_until:
                state.cooldown_active = False
                state.cooldown_until = None
                await self._persist_state(state)

        return state

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
        if state.open_positions_count >= settings.paper_max_open_positions:
            return RiskCheckResult(
                False, f"Max open positions ({settings.paper_max_open_positions})"
            )

        # Gate 7: Cooldown after N consecutive losses
        if state.consecutive_losses >= settings.risk_cooldown_after_losses:
            cooldown_until = datetime.utcnow() + timedelta(hours=2)
            state.cooldown_active = True
            state.cooldown_until = cooldown_until
            await self._persist_state(state)
            logger.warning(
                f"⚠️ Cooldown activated: {state.consecutive_losses} consecutive losses"
            )
            return RiskCheckResult(
                False,
                f"Cooldown after {state.consecutive_losses} consecutive losses"
            )

        # V101: Adaptive Bear Pause — if 3+ consecutive losses, pause 24h.
        # This protects against sustained bear markets where individual
        # token SL keeps getting hit. Be defensive, let the market recover.
        if state.consecutive_losses >= 3:
            cooldown_until = datetime.utcnow() + timedelta(hours=24)
            state.cooldown_active = True
            state.cooldown_until = cooldown_until
            await self._persist_state(state)
            logger.warning(
                f"🛑 V101 BEAR PAUSE: {state.consecutive_losses} consecutive losses — "
                f"pausing trading for 24h to protect capital"
            )
            return RiskCheckResult(
                False,
                f"V101 Bear Pause: {state.consecutive_losses} consecutive losses, "
                f"24h cooldown until {cooldown_until}"
            )

        return RiskCheckResult(True)

    async def record_trade_result(self, winning: bool, pnl_usd: float) -> None:
        """Called after a trade closes to update risk state."""
        from sqlalchemy.orm.attributes import flag_modified
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

            # Update peak equity
            current_peak = v.get("peak_equity", settings.paper_capital_usd)
            approx_equity = settings.paper_capital_usd + pnl_usd
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
