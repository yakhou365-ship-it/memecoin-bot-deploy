"""
Market Regime Memory.

Stores a historical database of which trading strategies worked best in each
market regime. Used by the StrategyResearchAgent and ChiefIntelligenceAgent
to pick the right strategy for the current market.

Regimes tracked:
- bull_market, bear_market, sideways_market
- high_volatility, low_volatility
- panic_selling, strong_momentum
- low_liquidity, accumulation, distribution

For each regime, we track:
- Number of trades attempted
- Win rate per strategy
- Profit factor per strategy
- Average PnL per strategy
- Recommended strategies (top-N by performance)

Storage:
- SystemState table with keys like "regime_memory:{regime_name}"
- A summary index at "regime_memory:index"
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import SystemState, Trade, AnalysisSnapshot


# =========================================================================
# Known regimes
# =========================================================================
KNOWN_REGIMES = [
    "bull_market",
    "bear_market",
    "sideways_market",
    "high_volatility",
    "low_volatility",
    "panic_selling",
    "strong_momentum",
    "low_liquidity",
    "accumulation",
    "distribution",
    "unknown",
]


# =========================================================================
# Data structures
# =========================================================================
@dataclass
class RegimePerformanceRecord:
    """Performance record for a single regime."""
    regime: str
    last_updated: str = ""

    # Per-strategy stats
    strategy_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # strategy_stats[strategy_id] = {
    #   "trades": N, "wins": N, "losses": N, "win_rate": 0.X,
    #   "avg_pnl_pct": X, "total_pnl_usd": X, "profit_factor": X,
    #   "avg_holding_minutes": X,
    #   "last_used": ISO timestamp,
    # }

    # Overall regime stats
    total_trades: int = 0
    overall_win_rate: float = 0.0
    overall_avg_pnl_pct: float = 0.0
    overall_profit_factor: float = 0.0

    # Top strategies by win rate (min 5 trades)
    recommended_strategies: List[str] = field(default_factory=list)

    # Regime characteristics (typical values when this regime is active)
    typical_volatility_pct: float = 0.0
    typical_trend_slope: float = 0.0
    typical_rsi: float = 0.0

    # Sample trade IDs (for audit)
    sample_trade_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RegimePerformanceRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# =========================================================================
# Memory
# =========================================================================
class MarketRegimeMemory:
    """
    Stores and queries market regime performance data.

    Usage:
        memory = MarketRegimeMemory()
        # After a trade closes
        await memory.record_trade_outcome(regime="bull_market", strategy_id="trend_follow",
                                          pnl_usd=50.0, pnl_pct=5.0, holding_minutes=120)
        # Before opening a new trade
        best = await memory.get_best_strategies_for_regime("bull_market", top_n=3)
        recommended = await memory.get_recommended_strategy("bull_market")
    """

    REGIME_PREFIX = "regime_memory:"
    INDEX_KEY = "regime_memory:index"
    MIN_TRADES_FOR_RECOMMENDATION = 5
    DECAY_DAYS = 90  # older trades count less

    def __init__(self, min_trades: int = 5, decay_days: int = 90):
        self.min_trades = min_trades
        self.decay_days = decay_days

    # =====================================================================
    # Recording
    # =====================================================================
    async def record_trade_outcome(
        self,
        regime: str,
        strategy_id: str,
        pnl_usd: float,
        pnl_pct: float,
        holding_minutes: int = 0,
        trade_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record the outcome of a closed trade under a specific regime/strategy."""
        regime = regime or "unknown"
        strategy_id = strategy_id or "default"
        ts = timestamp or datetime.utcnow()

        async with get_session() as s:
            record = await self._load_regime_record(s, regime)
            if record is None:
                record = RegimePerformanceRecord(regime=regime)

            # Update strategy stats
            if strategy_id not in record.strategy_stats:
                record.strategy_stats[strategy_id] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "win_rate": 0.0, "avg_pnl_pct": 0.0,
                    "total_pnl_usd": 0.0, "profit_factor": 0.0,
                    "avg_holding_minutes": 0.0,
                    "last_used": ts.isoformat(),
                }

            stats = record.strategy_stats[strategy_id]
            stats["trades"] += 1
            stats["last_used"] = ts.isoformat()
            if pnl_usd > 0:
                stats["wins"] += 1
            elif pnl_usd < 0:
                stats["losses"] += 1
            stats["total_pnl_usd"] += pnl_usd
            stats["win_rate"] = stats["wins"] / max(stats["trades"], 1)
            # Running average PnL pct
            stats["avg_pnl_pct"] = (
                (stats["avg_pnl_pct"] * (stats["trades"] - 1) + pnl_pct) / max(stats["trades"], 1)
            )
            stats["avg_holding_minutes"] = (
                (stats["avg_holding_minutes"] * (stats["trades"] - 1) + holding_minutes) / max(stats["trades"], 1)
            )

            # Recompute profit factor (need gross win/loss)
            # Approximate: if pnl>0 it's a win, if pnl<0 it's a loss
            # We track this incrementally via totals
            if stats["trades"] > 0:
                gross_wins = sum(
                    s["total_pnl_usd"] for s in record.strategy_stats.values()
                    if s.get("total_pnl_usd", 0) > 0
                )
                gross_losses = abs(sum(
                    s["total_pnl_usd"] for s in record.strategy_stats.values()
                    if s.get("total_pnl_usd", 0) < 0
                ))
                stats["profit_factor"] = (
                    gross_wins / max(gross_losses, 1.0) if gross_losses > 0 else 99.0
                )

            # Update overall regime stats
            record.total_trades += 1
            wins = sum(s["wins"] for s in record.strategy_stats.values())
            record.overall_win_rate = wins / max(record.total_trades, 1)
            record.overall_avg_pnl_pct = (
                (record.overall_avg_pnl_pct * (record.total_trades - 1) + pnl_pct) /
                max(record.total_trades, 1)
            )
            total_pnl = sum(s["total_pnl_usd"] for s in record.strategy_stats.values())
            gross_wins = sum(
                s["total_pnl_usd"] for s in record.strategy_stats.values()
                if s.get("total_pnl_usd", 0) > 0
            )
            gross_losses = abs(sum(
                s["total_pnl_usd"] for s in record.strategy_stats.values()
                if s.get("total_pnl_usd", 0) < 0
            ))
            record.overall_profit_factor = (
                gross_wins / max(gross_losses, 1.0) if gross_losses > 0 else 99.0
            )

            # Sample trade IDs (cap at 50)
            if trade_id:
                record.sample_trade_ids.append(trade_id)
                if len(record.sample_trade_ids) > 50:
                    record.sample_trade_ids = record.sample_trade_ids[-50:]

            # Update recommendations
            record.recommended_strategies = self._compute_recommended_strategies(record)
            record.last_updated = ts.isoformat()

            await self._save_regime_record(s, record)

        logger.debug(
            f"🧠 Recorded regime memory: {regime}/{strategy_id} "
            f"pnl=${pnl_usd:.2f} ({pnl_pct:+.2f}%)"
        )

    async def bulk_update_from_trades(
        self, lookback_days: int = 30
    ) -> Dict[str, Any]:
        """
        Re-compute all regime performance stats from historical trades.
        Useful for backfilling when the memory system is first deployed.
        """
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        async with get_session() as s:
            # Join Trade with AnalysisSnapshot to get the regime at trade time
            stmt = (
                select(Trade, AnalysisSnapshot.market_regime)
                .join(AnalysisSnapshot, Trade.analysis_snapshot_id == AnalysisSnapshot.id, isouter=True)
                .where(and_(
                    Trade.status == "closed",
                    Trade.entry_timestamp >= cutoff,
                ))
                .order_by(Trade.entry_timestamp)
            )
            result = await s.execute(stmt)
            rows = list(result.all())

        # Group by regime -> strategy
        grouped: Dict[Tuple[str, str], List[Trade]] = defaultdict(list)
        for trade, regime in rows:
            r = regime or "unknown"
            strat = trade.strategy_id or "default"
            grouped[(r, strat)].append(trade)

        # Reset all records first
        await self._reset_all_records()

        # Re-record everything
        count = 0
        for (regime, strategy), trades in grouped.items():
            for t in trades:
                pnl_usd = float(t.net_pnl_usd or 0)
                pnl_pct = float(t.pnl_pct or 0)
                holding = int(t.planned_holding_period_minutes or 0)
                await self.record_trade_outcome(
                    regime=regime,
                    strategy_id=strategy,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    holding_minutes=holding,
                    trade_id=t.id,
                    timestamp=t.entry_timestamp,
                )
                count += 1

        logger.info(
            f"🧠 Bulk-updated regime memory: {count} trades across "
            f"{len(grouped)} regime/strategy combos"
        )
        return {
            "trades_processed": count,
            "regime_strategy_combos": len(grouped),
            "lookback_days": lookback_days,
            "timestamp": datetime.utcnow().isoformat(),
        }

    # =====================================================================
    # Queries
    # =====================================================================
    async def get_best_strategies_for_regime(
        self, regime: str, top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """Get the top-N strategies by win rate for a regime."""
        async with get_session() as s:
            record = await self._load_regime_record(s, regime or "unknown")
            if not record:
                return []

        candidates = [
            {"strategy_id": sid, **stats}
            for sid, stats in record.strategy_stats.items()
            if stats.get("trades", 0) >= self.min_trades
        ]
        candidates.sort(
            key=lambda x: (x.get("win_rate", 0), x.get("profit_factor", 0)),
            reverse=True,
        )
        return candidates[:top_n]

    async def get_recommended_strategy(self, regime: str) -> Optional[str]:
        """Get the single best recommended strategy for a regime."""
        top = await self.get_best_strategies_for_regime(regime, top_n=1)
        if top:
            return top[0]["strategy_id"]
        # Fallback: use typical mapping
        return self._default_strategy_for_regime(regime)

    async def get_regime_stats(self, regime: str) -> Optional[RegimePerformanceRecord]:
        """Get the full performance record for a regime."""
        async with get_session() as s:
            return await self._load_regime_record(s, regime)

    async def get_all_regimes(self) -> Dict[str, RegimePerformanceRecord]:
        """Get performance records for all known regimes."""
        out: Dict[str, RegimePerformanceRecord] = {}
        async with get_session() as s:
            for regime in KNOWN_REGIMES:
                record = await self._load_regime_record(s, regime)
                if record:
                    out[regime] = record
        return out

    async def get_regime_summary(self) -> Dict[str, Any]:
        """Get a compact summary of all regimes for dashboard display."""
        all_records = await self.get_all_regimes()
        summary: Dict[str, Any] = {}
        for regime, record in all_records.items():
            summary[regime] = {
                "total_trades": record.total_trades,
                "win_rate": record.overall_win_rate,
                "avg_pnl_pct": record.overall_avg_pnl_pct,
                "profit_factor": record.overall_profit_factor,
                "recommended": record.recommended_strategies[:3],
                "last_updated": record.last_updated,
            }
        return summary

    async def compare_regimes(self) -> Dict[str, Dict[str, Any]]:
        """Compare performance across regimes — useful for strategy allocation."""
        all_records = await self.get_all_regimes()
        comparison: Dict[str, Dict[str, Any]] = {}
        for regime, record in all_records.items():
            if record.total_trades == 0:
                continue
            comparison[regime] = {
                "total_trades": record.total_trades,
                "win_rate": record.overall_win_rate,
                "avg_pnl_pct": record.overall_avg_pnl_pct,
                "profit_factor": record.overall_profit_factor,
                "best_strategy": (
                    record.recommended_strategies[0] if record.recommended_strategies else None
                ),
                "strategy_count": len(record.strategy_stats),
            }
        return comparison

    # =====================================================================
    # Helpers
    # =====================================================================
    def _compute_recommended_strategies(
        self, record: RegimePerformanceRecord
    ) -> List[str]:
        """Pick top strategies by win_rate * profit_factor."""
        candidates = [
            (sid, stats)
            for sid, stats in record.strategy_stats.items()
            if stats.get("trades", 0) >= self.min_trades
        ]
        # Score = win_rate * profit_factor (higher is better)
        candidates.sort(
            key=lambda x: x[1].get("win_rate", 0) * max(x[1].get("profit_factor", 0), 0.1),
            reverse=True,
        )
        return [sid for sid, _ in candidates[:5]]

    def _default_strategy_for_regime(self, regime: str) -> str:
        """Fallback strategy mapping when no historical data exists."""
        defaults = {
            "bull_market": "trend_follow",
            "bear_market": "wait",
            "sideways_market": "range_trading",
            "high_volatility": "wait",
            "low_volatility": "mean_reversion",
            "panic_selling": "wait",
            "strong_momentum": "momentum_breakout",
            "low_liquidity": "wait",
            "accumulation": "breakout",
            "distribution": "wait",
            "unknown": "default",
        }
        return defaults.get(regime, "default")

    async def _load_regime_record(
        self, session: AsyncSession, regime: str
    ) -> Optional[RegimePerformanceRecord]:
        key = f"{self.REGIME_PREFIX}{regime}"
        state = await session.get(SystemState, key)
        if not state or not state.value_json:
            return None
        try:
            return RegimePerformanceRecord.from_dict(state.value_json)
        except Exception as exc:
            logger.debug(f"Failed to load regime record {regime}: {exc}")
            return None

    async def _save_regime_record(
        self, session: AsyncSession, record: RegimePerformanceRecord
    ) -> None:
        key = f"{self.REGIME_PREFIX}{record.regime}"
        state = SystemState(key=key, value_json=record.to_dict())
        await session.merge(state)
        # Update index
        index_state = await session.get(SystemState, self.INDEX_KEY)
        if index_state is None:
            index_state = SystemState(key=self.INDEX_KEY, value_json={"regimes": []})
        regimes = list(index_state.value_json.get("regimes", []))
        if record.regime not in regimes:
            regimes.append(record.regime)
        index_state.value_json = {
            "regimes": regimes,
            "count": len(regimes),
            "updated_at": datetime.utcnow().isoformat(),
        }
        await session.merge(index_state)

    async def _reset_all_records(self) -> None:
        """Clear all regime records (used before bulk re-load)."""
        async with get_session() as s:
            for regime in KNOWN_REGIMES:
                key = f"{self.REGIME_PREFIX}{regime}"
                state = SystemState(
                    key=key,
                    value_json=asdict(RegimePerformanceRecord(regime=regime)),
                )
                await s.merge(state)
            # Reset index
            index_state = SystemState(
                key=self.INDEX_KEY,
                value_json={"regimes": list(KNOWN_REGIMES), "count": len(KNOWN_REGIMES)},
            )
            await s.merge(index_state)


__all__ = ["MarketRegimeMemory", "RegimePerformanceRecord", "KNOWN_REGIMES"]
