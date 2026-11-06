"""
Counterfactual Tracker.

Records every rejected trade opportunity and later evaluates what WOULD have
happened if we had taken it. This is critical for:

1. Catching over-rejection (we're too cautious and missing winners)
2. Catching under-rejection (we're correctly rejecting losers, validating the model)
3. Calibrating the multi-agent system's veto thresholds
4. Providing feedback to the HistoricalLearningAgent

Storage strategy:
- We use the SystemState table (key/value JSON) so we don't require migrations.
- Keys follow the pattern: "counterfactual:{token_id}:{record_id}"
- A registry key "counterfactual:index" tracks all open (unevaluated) record IDs
  so the evaluator can quickly find pending records.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, desc, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import SystemState, Token, AnalysisSnapshot, Trade


# =========================================================================
# Data structures
# =========================================================================
@dataclass
class CounterfactualRecord:
    """A single rejected opportunity tracked over time."""
    record_id: str
    token_id: str
    token_symbol: str = ""
    token_address: str = ""
    chain: str = ""

    rejection_time: str = ""  # ISO datetime
    rejection_reason: str = ""
    rejecting_agents: List[str] = field(default_factory=list)
    vetoed: bool = False
    veto_reasons: List[str] = field(default_factory=list)

    # State of the token at rejection (entry price reference)
    price_at_rejection: float = 0.0
    liquidity_usd_at_rejection: float = 0.0
    market_regime: str = "unknown"
    win_probability_estimate: float = 0.0

    # Simulated trade params (what we WOULD have done)
    simulated_entry_size_usd: float = 0.0
    simulated_take_profit_pct: float = 0.0
    simulated_stop_loss_pct: float = 0.0
    simulated_holding_minutes: int = 0

    # Price trajectory (sampled periodically until evaluation)
    price_history: List[Dict[str, Any]] = field(default_factory=list)  # [{ts, price}]

    # Final evaluation (filled in once the holding window elapses)
    evaluated: bool = False
    evaluation_time: str = ""
    final_price: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    would_have_pnl_usd: float = 0.0
    would_have_pnl_pct: float = 0.0
    would_have_hit_tp: bool = False
    would_have_hit_sl: bool = False
    rejection_was_correct: bool = False
    evaluation_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CounterfactualRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# =========================================================================
# Tracker
# =========================================================================
class CounterfactualTracker:
    """
    Records rejections and evaluates outcomes.

    Usage:
        tracker = CounterfactualTracker()
        await tracker.record_rejection(token, snapshot, "Low win probability", ["prediction"])
        # ... later ...
        await tracker.update_price(token_id, current_price)
        # ... after holding window ...
        report = await tracker.evaluate_pending()
        missed = await tracker.generate_missed_opportunities_report(days=7)
    """

    INDEX_KEY = "counterfactual:index"
    RECORD_PREFIX = "counterfactual:"
    DEFAULT_HOLDING_MINUTES = 120  # default simulated holding period
    DEFAULT_TP_PCT = 15.0
    DEFAULT_SL_PCT = 5.0
    DEFAULT_ENTRY_SIZE_USD = 100.0

    def __init__(
        self,
        holding_minutes: int = DEFAULT_HOLDING_MINUTES,
        tp_pct: float = DEFAULT_TP_PCT,
        sl_pct: float = DEFAULT_SL_PCT,
        entry_size_usd: float = DEFAULT_ENTRY_SIZE_USD,
    ):
        self.holding_minutes = holding_minutes
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.entry_size_usd = entry_size_usd

    # =====================================================================
    # Recording
    # =====================================================================
    async def record_rejection(
        self,
        token: Token,
        snapshot: Optional[AnalysisSnapshot] = None,
        rejection_reason: str = "",
        rejecting_agents: Optional[List[str]] = None,
        vetoed: bool = False,
        veto_reasons: Optional[List[str]] = None,
        agent_results: Optional[List[Dict[str, Any]]] = None,
        market_regime: str = "unknown",
    ) -> CounterfactualRecord:
        """Record a rejected trade opportunity."""
        record_id = str(uuid.uuid4())
        now = datetime.utcnow()

        # Derive simulated entry params (use snapshot's TP/SL if available, else defaults)
        tp_pct = self.tp_pct
        sl_pct = self.sl_pct
        holding_min = self.holding_minutes
        win_prob = 0.0
        if snapshot:
            win_prob = float(snapshot.win_probability or 0.0)
            # If snapshot had a strategy embedded, we'd parse TP/SL here

        # Pull rejecting agents from agent_results if not provided
        if rejecting_agents is None and agent_results:
            rejecting_agents = [
                a.get("agent_name", "?")
                for a in agent_results
                if a.get("verdict") in ("reject", "strong_reject", "veto")
            ]
        if veto_reasons is None:
            veto_reasons = []

        record = CounterfactualRecord(
            record_id=record_id,
            token_id=str(token.id),
            token_symbol=str(getattr(token, "symbol", "") or ""),
            token_address=str(getattr(token, "address", "") or ""),
            chain=str(getattr(token, "chain", "") or ""),
            rejection_time=now.isoformat(),
            rejection_reason=rejection_reason or "unspecified",
            rejecting_agents=rejecting_agents or [],
            vetoed=vetoed,
            veto_reasons=veto_reasons,
            price_at_rejection=float(getattr(token, "price_usd", 0) or 0),
            liquidity_usd_at_rejection=float(getattr(token, "liquidity_usd", 0) or 0),
            market_regime=market_regime,
            win_probability_estimate=win_prob,
            simulated_entry_size_usd=self.entry_size_usd,
            simulated_take_profit_pct=tp_pct,
            simulated_stop_loss_pct=sl_pct,
            simulated_holding_minutes=holding_min,
            price_history=[
                {"ts": now.isoformat(), "price": float(getattr(token, "price_usd", 0) or 0)}
            ],
        )

        # Persist
        async with get_session() as s:
            # Save the record
            state = SystemState(
                key=f"{self.RECORD_PREFIX}{token.id}:{record_id}",
                value_json=record.to_dict(),
            )
            await s.merge(state)
            # Update index
            index = await self._load_index(s)
            if record_id not in index:
                index.append(record_id)
            await self._save_index(s, index)

        logger.info(
            f"📋 Recorded counterfactual for {record.token_symbol} "
            f"(reason={rejection_reason}, vetoed={vetoed})"
        )
        return record

    # =====================================================================
    # Price updates
    # =====================================================================
    async def update_price(
        self,
        token_id: str,
        current_price: float,
        timestamp: Optional[datetime] = None,
    ) -> int:
        """
        Update all open counterfactuals for a token with the latest price.
        Returns the number of records updated.
        """
        if current_price <= 0:
            return 0

        ts = (timestamp or datetime.utcnow()).isoformat()
        updated = 0
        async with get_session() as s:
            index = await self._load_index(s)
            for record_id in list(index):
                # We need to load each record and check if it matches the token
                # (the index is global, but records store token_id)
                record_key = await self._find_record_key(s, record_id)
                if not record_key:
                    continue
                stmt = select(SystemState).where(SystemState.key == record_key)
                result = await s.execute(stmt)
                state = result.scalars().first()
                if not state:
                    continue
                record = CounterfactualRecord.from_dict(state.value_json or {})
                if record.token_id != token_id or record.evaluated:
                    continue

                # Append to price history (cap at 200 points to avoid bloat)
                record.price_history.append({"ts": ts, "price": float(current_price)})
                if len(record.price_history) > 200:
                    record.price_history = record.price_history[-200:]

                state.value_json = record.to_dict()
                updated += 1

        if updated:
            logger.debug(f"📈 Updated {updated} counterfactuals for token {token_id}")
        return updated

    # =====================================================================
    # Evaluation
    # =====================================================================
    async def evaluate_pending(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Evaluate all counterfactuals whose holding window has elapsed.
        Returns a summary dict.
        """
        now = now or datetime.utcnow()
        evaluated = 0
        correct_rejections = 0
        missed_opportunities = 0
        avg_missed_pnl_pct = 0.0
        missed_pnl_list: List[float] = []

        async with get_session() as s:
            index = await self._load_index(s)
            new_index = list(index)

            for record_id in list(index):
                record_key = await self._find_record_key(s, record_id)
                if not record_key:
                    new_index.remove(record_id)
                    continue

                stmt = select(SystemState).where(SystemState.key == record_key)
                result = await s.execute(stmt)
                state = result.scalars().first()
                if not state:
                    if record_id in new_index:
                        new_index.remove(record_id)
                    continue

                record = CounterfactualRecord.from_dict(state.value_json or {})
                if record.evaluated:
                    continue

                rejection_dt = datetime.fromisoformat(record.rejection_time) if record.rejection_time else now
                if (now - rejection_dt).total_seconds() < record.simulated_holding_minutes * 60:
                    continue  # not enough time yet

                # Evaluate
                outcome = self._evaluate_record(record, now)
                record.evaluated = True
                record.evaluation_time = now.isoformat()
                record.final_price = outcome["final_price"]
                record.peak_price = outcome["peak_price"]
                record.trough_price = outcome["trough_price"]
                record.would_have_pnl_usd = outcome["pnl_usd"]
                record.would_have_pnl_pct = outcome["pnl_pct"]
                record.would_have_hit_tp = outcome["hit_tp"]
                record.would_have_hit_sl = outcome["hit_sl"]
                record.rejection_was_correct = outcome["rejection_was_correct"]
                record.evaluation_notes = outcome["notes"]

                state.value_json = record.to_dict()
                evaluated += 1

                if record.rejection_was_correct:
                    correct_rejections += 1
                else:
                    missed_opportunities += 1
                    missed_pnl_list.append(record.would_have_pnl_pct)

            # Persist updated index (in case we removed orphans)
            await self._save_index(s, new_index)

        avg_missed_pnl_pct = (
            sum(missed_pnl_list) / len(missed_pnl_list) if missed_pnl_list else 0.0
        )

        logger.info(
            f"🔍 Evaluated {evaluated} counterfactuals: "
            f"{correct_rejections} correct rejections, "
            f"{missed_opportunities} missed opportunities "
            f"(avg would-have PnL: {avg_missed_pnl_pct:+.2f}%)"
        )

        return {
            "evaluated": evaluated,
            "correct_rejections": correct_rejections,
            "missed_opportunities": missed_opportunities,
            "avg_missed_pnl_pct": avg_missed_pnl_pct,
            "missed_opportunity_pnls": missed_pnl_list,
            "evaluation_time": now.isoformat(),
        }

    def _evaluate_record(
        self, record: CounterfactualRecord, now: datetime
    ) -> Dict[str, Any]:
        """Compute the counterfactual outcome for a single record."""
        prices = [p.get("price", 0) for p in record.price_history if p.get("price", 0) > 0]
        entry = record.price_at_rejection
        if entry <= 0 or not prices:
            return {
                "final_price": 0.0,
                "peak_price": 0.0,
                "trough_price": 0.0,
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
                "hit_tp": False,
                "hit_sl": False,
                "rejection_was_correct": True,
                "notes": "No price data available",
            }

        peak = max(prices)
        trough = min(prices)
        final = prices[-1]

        tp_price = entry * (1 + record.simulated_take_profit_pct / 100.0)
        sl_price = entry * (1 - record.simulated_stop_loss_pct / 100.0)

        hit_tp = peak >= tp_price
        hit_sl = trough <= sl_price

        # Determine exit price (whichever hits first chronologically)
        exit_price = final
        exit_reason = "time_exit"
        for p in prices[1:]:
            if p <= sl_price:
                exit_price = sl_price
                exit_reason = "stop_loss"
                break
            if p >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                break

        pnl_pct = (exit_price - entry) / entry * 100.0
        # Subtract fees+slippage (rough estimate)
        cost_pct = settings.paper_fee_pct + settings.paper_slippage_pct
        pnl_pct -= cost_pct
        pnl_usd = (pnl_pct / 100.0) * record.simulated_entry_size_usd

        # Decision: was rejection correct?
        # Correct if we would have lost money (or barely broken even)
        rejection_was_correct = pnl_usd <= 0

        notes = (
            f"Exit reason: {exit_reason}. "
            f"Peak: {(peak/entry-1)*100:+.2f}%, Trough: {(trough/entry-1)*100:+.2f}%, "
            f"Final: {(final/entry-1)*100:+.2f}%"
        )

        return {
            "final_price": final,
            "peak_price": peak,
            "trough_price": trough,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "hit_tp": hit_tp,
            "hit_sl": hit_sl,
            "rejection_was_correct": rejection_was_correct,
            "notes": notes,
        }

    # =====================================================================
    # Reports
    # =====================================================================
    async def generate_missed_opportunities_report(
        self, days: int = 7
    ) -> Dict[str, Any]:
        """Report on rejected tokens that WOULD have been profitable."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        records = await self._load_evaluated_records(since=cutoff)
        missed = [r for r in records if not r.rejection_was_correct and r.would_have_pnl_usd > 0]
        missed.sort(key=lambda r: r.would_have_pnl_pct, reverse=True)

        total_missed_pnl = sum(r.would_have_pnl_usd for r in missed)
        by_reason: Dict[str, int] = {}
        for r in missed:
            by_reason[r.rejection_reason] = by_reason.get(r.rejection_reason, 0) + 1

        return {
            "report_type": "missed_opportunities",
            "window_days": days,
            "total_missed": len(missed),
            "total_missed_pnl_usd": total_missed_pnl,
            "avg_missed_pnl_pct": sum(r.would_have_pnl_pct for r in missed) / max(len(missed), 1),
            "top_missed": [r.to_dict() for r in missed[:20]],
            "by_reason": by_reason,
            "generated_at": datetime.utcnow().isoformat(),
        }

    async def generate_correct_rejections_report(
        self, days: int = 7
    ) -> Dict[str, Any]:
        """Report on rejections that were correct (would have lost money)."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        records = await self._load_evaluated_records(since=cutoff)
        correct = [r for r in records if r.rejection_was_correct]
        correct.sort(key=lambda r: r.would_have_pnl_pct)  # most-saved first

        total_saved = sum(abs(r.would_have_pnl_usd) for r in correct if r.would_have_pnl_usd < 0)
        by_reason: Dict[str, int] = {}
        for r in correct:
            by_reason[r.rejection_reason] = by_reason.get(r.rejection_reason, 0) + 1

        return {
            "report_type": "correct_rejections",
            "window_days": days,
            "total_correct": len(correct),
            "total_saved_usd": total_saved,
            "avg_avoided_loss_pct": sum(r.would_have_pnl_pct for r in correct) / max(len(correct), 1),
            "top_correct": [r.to_dict() for r in correct[:20]],
            "by_reason": by_reason,
            "generated_at": datetime.utcnow().isoformat(),
        }

    async def get_overall_stats(self) -> Dict[str, Any]:
        """Overall counterfactual statistics."""
        records = await self._load_all_records()
        evaluated = [r for r in records if r.evaluated]
        pending = [r for r in records if not r.evaluated]

        correct = [r for r in evaluated if r.rejection_was_correct]
        missed = [r for r in evaluated if not r.rejection_was_correct]

        veto_records = [r for r in records if r.vetoed]
        veto_correct = [r for r in veto_records if r.evaluated and r.rejection_was_correct]
        veto_missed = [r for r in veto_records if r.evaluated and not r.rejection_was_correct]

        return {
            "total_records": len(records),
            "evaluated": len(evaluated),
            "pending": len(pending),
            "correct_rejections": len(correct),
            "missed_opportunities": len(missed),
            "rejection_accuracy": len(correct) / max(len(evaluated), 1),
            "veto_records": len(veto_records),
            "veto_correct": len(veto_correct),
            "veto_missed": len(veto_missed),
            "veto_accuracy": len(veto_correct) / max(len(veto_records), 1),
            "total_missed_pnl_usd": sum(r.would_have_pnl_usd for r in missed),
            "total_saved_usd": sum(abs(r.would_have_pnl_usd) for r in correct if r.would_have_pnl_usd < 0),
            "generated_at": datetime.utcnow().isoformat(),
        }

    async def get_token_records(self, token_id: str) -> List[CounterfactualRecord]:
        """Get all counterfactual records for a token."""
        all_records = await self._load_all_records()
        return [r for r in all_records if r.token_id == token_id]

    # =====================================================================
    # Index management
    # =====================================================================
    async def _load_index(self, session: AsyncSession) -> List[str]:
        stmt = select(SystemState).where(SystemState.key == self.INDEX_KEY)
        result = await session.execute(stmt)
        state = result.scalars().first()
        if not state or not state.value_json:
            return []
        return list(state.value_json.get("record_ids", []) or [])

    async def _save_index(self, session: AsyncSession, record_ids: List[str]) -> None:
        state = SystemState(
            key=self.INDEX_KEY,
            value_json={"record_ids": list(record_ids), "updated_at": datetime.utcnow().isoformat()},
        )
        await session.merge(state)

    async def _find_record_key(self, session: AsyncSession, record_id: str) -> Optional[str]:
        """Find the SystemState key for a given record_id."""
        # Records are stored as "counterfactual:{token_id}:{record_id}"
        # We don't know the token_id without loading the index, so we look up by pattern
        # Since SQLAlchemy doesn't have a clean LIKE for JSON, we use raw SQL.
        if settings.database_url.startswith("sqlite"):
            stmt = text(
                "SELECT key FROM system_state WHERE key LIKE :pattern AND value_json LIKE :rid"
            )
            result = await session.execute(stmt, {"pattern": f"{self.RECORD_PREFIX}%", "rid": f'%"{record_id}"%'})
        else:
            # PostgreSQL
            stmt = text(
                "SELECT key FROM system_state WHERE key LIKE :pattern AND value_json::text LIKE :rid"
            )
            result = await session.execute(stmt, {"pattern": f"{self.RECORD_PREFIX}%", "rid": f'%"{record_id}"%'})
        row = result.first()
        return row[0] if row else None

    async def _load_all_records(self) -> List[CounterfactualRecord]:
        """Load all counterfactual records (limited to last 1000 for safety)."""
        records: List[CounterfactualRecord] = []
        async with get_session() as s:
            stmt = select(SystemState).where(SystemState.key.like(f"{self.RECORD_PREFIX}%"))
            result = await s.execute(stmt)
            for state in result.scalars().all():
                if state.key == self.INDEX_KEY:
                    continue
                try:
                    records.append(CounterfactualRecord.from_dict(state.value_json or {}))
                except Exception as exc:
                    logger.debug(f"Failed to load counterfactual record: {exc}")
        return records

    async def _load_evaluated_records(
        self, since: Optional[datetime] = None
    ) -> List[CounterfactualRecord]:
        """Load all evaluated counterfactual records."""
        all_records = await self._load_all_records()
        evaluated = [r for r in all_records if r.evaluated]
        if since:
            evaluated = [
                r for r in evaluated
                if r.evaluation_time and datetime.fromisoformat(r.evaluation_time) >= since
            ]
        return evaluated


__all__ = ["CounterfactualTracker", "CounterfactualRecord"]
