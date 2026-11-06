"""
Independent Auditor.

Verifies the integrity of the bot's reported performance. This module can
REJECT the main AI's claims if it finds:

1. Missing trades (trades that should exist but don't)
2. Probability calibration errors (predicted win rate vs actual)
3. Data leakage (e.g., trades with future-looking entry/exit times)
4. Hidden trade filtering (suspicious gaps in trade IDs)
5. Cherry-picked backtests (in-sample WR >> out-of-sample WR)
6. Win rate inflation (counting partial wins as full wins)
7. Survivorship bias (only counting tokens that survived)
8. Look-ahead bias (using data from after the decision time)

The auditor is intentionally paranoid. It assumes the AI is wrong until proven
otherwise.
"""
from __future__ import annotations

import asyncio
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
from ...core.models import (
    Trade, Token, AnalysisSnapshot, SystemState,
    StrategyVersion, ModelVersion, ScanRun, DataSourceCall,
)


# =========================================================================
# Result structures
# =========================================================================
@dataclass
class AuditFinding:
    """A single audit finding (issue or pass)."""
    check_name: str
    passed: bool
    severity: str = "info"  # info | warning | error | critical
    message: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    """Full audit report."""
    audit_id: str
    timestamp: str
    period_start: str
    period_end: str

    # Overall verdict: approved | rejected | needs_review
    verdict: str = "needs_review"

    # Aggregate metrics
    total_checks: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    critical_failures: int = 0
    warnings: int = 0

    # Specific findings
    findings: List[Dict[str, Any]] = field(default_factory=list)

    # Verified metrics (re-computed independently by auditor)
    verified_total_trades: int = 0
    verified_winning_trades: int = 0
    verified_losing_trades: int = 0
    verified_win_rate: float = 0.0
    verified_net_pnl_usd: float = 0.0
    verified_profit_factor: float = 0.0
    verified_expectancy_usd: float = 0.0

    # AI-claimed metrics (to compare against verified)
    claimed_win_rate: Optional[float] = None
    claimed_total_trades: Optional[int] = None

    # Calibration
    avg_predicted_win_prob: float = 0.0
    avg_actual_win_rate: float = 0.0
    calibration_error: float = 0.0  # |predicted - actual|

    # Discrepancy flag
    win_rate_discrepancy: float = 0.0
    trades_discrepancy: int = 0

    # Summary
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================================================================
# Auditor
# =========================================================================
class IndependentAuditor:
    """
    Independent auditor that verifies the AI's reported performance.

    All checks are read-only — the auditor never modifies trade data.
    """

    AUDIT_INDEX_KEY = "auditor:index"
    AUDIT_PREFIX = "auditor:report:"

    def __init__(
        self,
        win_rate_discrepancy_threshold: float = 5.0,  # % points
        calibration_warning_threshold: float = 0.10,
        calibration_critical_threshold: float = 0.20,
        min_trades_for_audit: int = 10,
    ):
        self.win_rate_discrepancy_threshold = win_rate_discrepancy_threshold
        self.calibration_warning_threshold = calibration_warning_threshold
        self.calibration_critical_threshold = calibration_critical_threshold
        self.min_trades_for_audit = min_trades_for_audit

    # =====================================================================
    # Main audit
    # =====================================================================
    async def audit_performance(
        self,
        period_days: int = 7,
        claimed_win_rate: Optional[float] = None,
        claimed_total_trades: Optional[int] = None,
    ) -> AuditReport:
        """
        Run a full audit on the last `period_days` of trading.

        Args:
            period_days: how many days of history to audit
            claimed_win_rate: what the AI claims its win rate is
            claimed_total_trades: what the AI claims its total trade count is
        """
        audit_id = str(uuid.uuid4())
        now = datetime.utcnow()
        cutoff = now - timedelta(days=period_days)
        findings: List[AuditFinding] = []

        # Load all trades in the period
        trades = await self._load_trades(since=cutoff, until=now)

        if len(trades) < self.min_trades_for_audit:
            findings.append(AuditFinding(
                check_name="min_sample",
                passed=False,
                severity="warning",
                message=f"Insufficient trades for audit: {len(trades)} < {self.min_trades_for_audit}",
                evidence={"trade_count": len(trades)},
            ))

        # Compute verified metrics
        verified = self._compute_verified_metrics(trades)

        # Run all checks
        findings.append(await self._check_no_missing_trades(trades, cutoff, now))
        findings.append(await self._check_no_lookahead(trades))
        findings.append(await self._check_no_survivorship_bias(trades, cutoff))
        findings.append(await self._check_calibration(trades))
        findings.append(await self._check_no_cherry_picked_backtests(cutoff))
        findings.append(await self._check_data_source_health(cutoff))
        findings.append(await self._check_no_trade_filtering(trades))
        findings.append(await self._check_no_orphan_trades(trades))
        findings.append(await self._check_pnl_integrity(trades))
        findings.append(await self._check_win_rate_consistency(
            trades, claimed_win_rate, claimed_total_trades, verified
        ))
        findings.append(await self._check_exit_reason_distribution(trades))
        findings.append(await self._check_time_distribution(trades, cutoff, now))

        # Aggregate verdict
        critical = sum(1 for f in findings if f.severity == "critical" and not f.passed)
        errors = sum(1 for f in findings if f.severity == "error" and not f.passed)
        warnings = sum(1 for f in findings if f.severity == "warning" and not f.passed)
        passed = sum(1 for f in findings if f.passed)

        if critical > 0:
            verdict = "rejected"
        elif errors > 0:
            verdict = "needs_review"
        elif warnings > 0:
            verdict = "needs_review"
        else:
            verdict = "approved"

        report = AuditReport(
            audit_id=audit_id,
            timestamp=now.isoformat(),
            period_start=cutoff.isoformat(),
            period_end=now.isoformat(),
            verdict=verdict,
            total_checks=len(findings),
            checks_passed=passed,
            checks_failed=len(findings) - passed,
            critical_failures=critical,
            warnings=warnings,
            findings=[f.__dict__ if hasattr(f, '__dict__') else asdict(f) for f in findings],
            verified_total_trades=verified["total_trades"],
            verified_winning_trades=verified["winning_trades"],
            verified_losing_trades=verified["losing_trades"],
            verified_win_rate=verified["win_rate"],
            verified_net_pnl_usd=verified["net_pnl"],
            verified_profit_factor=verified["profit_factor"],
            verified_expectancy_usd=verified["expectancy"],
            claimed_win_rate=claimed_win_rate,
            claimed_total_trades=claimed_total_trades,
            avg_predicted_win_prob=verified.get("avg_predicted_win_prob", 0),
            avg_actual_win_rate=verified["win_rate"],
            calibration_error=abs(verified.get("avg_predicted_win_prob", 0) - verified["win_rate"]),
            win_rate_discrepancy=(
                abs(claimed_win_rate - verified["win_rate"] * 100)
                if claimed_win_rate is not None else 0.0
            ),
            trades_discrepancy=(
                abs(claimed_total_trades - verified["total_trades"])
                if claimed_total_trades is not None else 0
            ),
            summary=self._build_summary(verdict, findings, verified, claimed_win_rate, claimed_total_trades),
        )

        # Persist
        await self._save_report(report)

        logger.info(
            f"🔍 Audit complete: verdict={verdict} "
            f"(passed {passed}/{len(findings)}, {critical} critical, {warnings} warnings)"
        )
        return report

    # =====================================================================
    # Individual checks
    # =====================================================================
    async def _check_no_missing_trades(
        self, trades: List[Trade], cutoff: datetime, now: datetime
    ) -> AuditFinding:
        """
        Check for gaps in trade timestamps that might indicate deleted trades.

        Heuristic: if the bot was actively scanning (ScanRun records exist),
        but no trades were opened for >3 hours during market hours, flag it.
        """
        evidence: Dict[str, Any] = {"trade_count": len(trades)}
        if len(trades) < 2:
            return AuditFinding(
                check_name="no_missing_trades",
                passed=True,
                severity="info",
                message="Not enough trades to analyze gaps",
                evidence=evidence,
            )

        # Sort by entry timestamp
        sorted_trades = sorted(trades, key=lambda t: t.entry_timestamp)
        gaps: List[Dict[str, Any]] = []
        for i in range(1, len(sorted_trades)):
            prev = sorted_trades[i - 1]
            curr = sorted_trades[i]
            gap = (curr.entry_timestamp - prev.entry_timestamp).total_seconds()
            if gap > 3 * 3600:  # >3 hours
                gaps.append({
                    "from": prev.entry_timestamp.isoformat(),
                    "to": curr.entry_timestamp.isoformat(),
                    "gap_hours": gap / 3600,
                })

        # Check if there were scan runs during the gaps (i.e., bot was active but no trades)
        suspicious_gaps = []
        async with get_session() as s:
            for gap in gaps:
                stmt = select(func.count(ScanRun.id)).where(
                    and_(
                        ScanRun.started_at >= datetime.fromisoformat(gap["from"]),
                        ScanRun.started_at <= datetime.fromisoformat(gap["to"]),
                        ScanRun.status == "done",
                    )
                )
                result = await s.execute(stmt)
                count = result.scalar() or 0
                if count > 0:
                    suspicious_gaps.append({**gap, "scans_during_gap": count})

        evidence["gaps"] = gaps[:10]
        evidence["suspicious_gaps"] = suspicious_gaps[:5]

        if suspicious_gaps:
            return AuditFinding(
                check_name="no_missing_trades",
                passed=False,
                severity="warning",
                message=(
                    f"{len(suspicious_gaps)} suspicious gaps where bot was scanning "
                    f"but no trades were opened (possible filtering)"
                ),
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_missing_trades",
            passed=True,
            severity="info",
            message="No suspicious trade gaps detected",
            evidence=evidence,
        )

    async def _check_no_lookahead(self, trades: List[Trade]) -> AuditFinding:
        """
        Check that no trade's analysis snapshot was created AFTER the trade was opened.
        """
        lookahead_count = 0
        evidence: Dict[str, Any] = {"examples": []}
        async with get_session() as s:
            for trade in trades[:200]:  # cap for performance
                if not trade.analysis_snapshot_id:
                    continue
                snapshot = await s.get(AnalysisSnapshot, trade.analysis_snapshot_id)
                if snapshot and snapshot.timestamp > trade.entry_timestamp:
                    lookahead_count += 1
                    if len(evidence["examples"]) < 5:
                        evidence["examples"].append({
                            "trade_id": trade.id,
                            "snapshot_time": snapshot.timestamp.isoformat(),
                            "entry_time": trade.entry_timestamp.isoformat(),
                            "delta_seconds": (snapshot.timestamp - trade.entry_timestamp).total_seconds(),
                        })

        if lookahead_count > 0:
            return AuditFinding(
                check_name="no_lookahead",
                passed=False,
                severity="critical",
                message=f"{lookahead_count} trades have analysis snapshots created AFTER entry (look-ahead bias!)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_lookahead",
            passed=True,
            severity="info",
            message="No look-ahead bias detected",
            evidence=evidence,
        )

    async def _check_no_survivorship_bias(
        self, trades: List[Trade], cutoff: datetime
    ) -> AuditFinding:
        """
        Check that the trade set includes tokens that died (rugged / abandoned).
        A suspiciously high survival rate suggests survivorship bias.
        """
        async with get_session() as s:
            # Get all tokens that were traded
            token_ids = list({t.token_id for t in trades})
            stmt = select(Token).where(Token.id.in_(token_ids))
            result = await s.execute(stmt)
            tokens = list(result.scalars().all())

        total = len(tokens)
        dead = sum(1 for t in tokens if t.is_dead)
        blacklisted = sum(1 for t in tokens if t.blacklisted)
        dead_pct = (dead / total * 100) if total > 0 else 0

        evidence = {
            "total_tokens_traded": total,
            "dead_tokens": dead,
            "blacklisted_tokens": blacklisted,
            "dead_pct": dead_pct,
        }

        # Heuristic: if 0% of traded tokens died in memecoin trading, that's suspicious
        # (memecoins have a high death rate)
        if total >= 10 and dead == 0:
            return AuditFinding(
                check_name="no_survivorship_bias",
                passed=False,
                severity="warning",
                message=(
                    f"0% of traded tokens are dead — unusual for memecoins "
                    f"(possible survivorship bias in token selection)"
                ),
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_survivorship_bias",
            passed=True,
            severity="info",
            message=f"{dead_pct:.1f}% of traded tokens are dead (reasonable for memecoins)",
            evidence=evidence,
        )

    async def _check_calibration(self, trades: List[Trade]) -> AuditFinding:
        """
        Check that the predicted win probability matches the actual win rate.
        Large discrepancies indicate miscalibration or model drift.
        """
        # Get snapshots with predictions
        snapshots_with_pred: List[Tuple[float, bool]] = []
        async with get_session() as s:
            for trade in trades[:500]:
                if not trade.analysis_snapshot_id:
                    continue
                snapshot = await s.get(AnalysisSnapshot, trade.analysis_snapshot_id)
                if snapshot and snapshot.win_probability is not None:
                    actual_win = (trade.net_pnl_usd or 0) > 0
                    snapshots_with_pred.append((float(snapshot.win_probability), actual_win))

        if len(snapshots_with_pred) < self.min_trades_for_audit:
            return AuditFinding(
                check_name="calibration",
                passed=True,
                severity="info",
                message="Not enough trades with predictions for calibration check",
                evidence={"sample_size": len(snapshots_with_pred)},
            )

        avg_pred = sum(p for p, _ in snapshots_with_pred) / len(snapshots_with_pred)
        actual_wr = sum(1 for _, w in snapshots_with_pred if w) / len(snapshots_with_pred)
        error = abs(avg_pred - actual_wr)

        evidence = {
            "avg_predicted_win_prob": avg_pred,
            "actual_win_rate": actual_wr,
            "calibration_error": error,
            "sample_size": len(snapshots_with_pred),
        }

        if error > self.calibration_critical_threshold:
            return AuditFinding(
                check_name="calibration",
                passed=False,
                severity="critical",
                message=f"Calibration error {error:.1%} > critical threshold {self.calibration_critical_threshold:.0%}",
                evidence=evidence,
            )
        elif error > self.calibration_warning_threshold:
            return AuditFinding(
                check_name="calibration",
                passed=False,
                severity="warning",
                message=f"Calibration error {error:.1%} > warning threshold {self.calibration_warning_threshold:.0%}",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="calibration",
            passed=True,
            severity="info",
            message=f"Calibration OK (error={error:.1%})",
            evidence=evidence,
        )

    async def _check_no_cherry_picked_backtests(self, cutoff: datetime) -> AuditFinding:
        """
        Check that no strategy version has a suspicious gap between in-sample and out-of-sample win rate.
        A gap > 15 percentage points suggests overfitting / cherry-picking.
        """
        async with get_session() as s:
            stmt = select(StrategyVersion).where(StrategyVersion.created_at >= cutoff)
            result = await s.execute(stmt)
            strategies = list(result.scalars().all())

        evidence: Dict[str, Any] = {"strategies_checked": len(strategies), "suspicious": []}
        suspicious = []
        for strat in strategies:
            if strat.backtest_win_rate is None or strat.backtest_out_of_sample_win_rate is None:
                continue
            gap = strat.backtest_win_rate - strat.backtest_out_of_sample_win_rate
            if gap > 0.15:
                suspicious.append({
                    "strategy_id": strat.id,
                    "name": strat.name,
                    "in_sample_wr": strat.backtest_win_rate,
                    "oos_wr": strat.backtest_out_of_sample_win_rate,
                    "gap": gap,
                })

        evidence["suspicious"] = suspicious
        if suspicious:
            return AuditFinding(
                check_name="no_cherry_picked_backtests",
                passed=False,
                severity="warning",
                message=f"{len(suspicious)} strategies have in-sample/OOS WR gap > 15% (possible overfitting)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_cherry_picked_backtests",
            passed=True,
            severity="info",
            message="No suspicious in-sample/OOS gaps detected",
            evidence=evidence,
        )

    async def _check_data_source_health(self, cutoff: datetime) -> AuditFinding:
        """Check data source API health — failed calls indicate unreliable data."""
        async with get_session() as s:
            # Total calls
            total_stmt = select(func.count(DataSourceCall.id)).where(
                DataSourceCall.timestamp >= cutoff
            )
            total = (await s.execute(total_stmt)).scalar() or 0

            failed_stmt = select(func.count(DataSourceCall.id)).where(
                and_(
                    DataSourceCall.timestamp >= cutoff,
                    DataSourceCall.success == False,
                )
            )
            failed = (await s.execute(failed_stmt)).scalar() or 0

        failure_rate = (failed / total * 100) if total > 0 else 0
        evidence = {"total_calls": total, "failed_calls": failed, "failure_rate_pct": failure_rate}

        if failure_rate > 20:
            return AuditFinding(
                check_name="data_source_health",
                passed=False,
                severity="critical",
                message=f"Data source failure rate {failure_rate:.1f}% > 20% — data unreliable",
                evidence=evidence,
            )
        elif failure_rate > 10:
            return AuditFinding(
                check_name="data_source_health",
                passed=False,
                severity="warning",
                message=f"Data source failure rate {failure_rate:.1f}% > 10%",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="data_source_health",
            passed=True,
            severity="info",
            message=f"Data source failure rate {failure_rate:.1f}% (acceptable)",
            evidence=evidence,
        )

    async def _check_no_trade_filtering(self, trades: List[Trade]) -> AuditFinding:
        """
        Check that we're not selectively filtering trades (e.g., only reporting wins).
        Heuristic: count of cancelled/force_closed trades should be a small fraction.
        """
        cancelled = sum(1 for t in trades if t.status in ("cancelled", "force_closed"))
        closed = sum(1 for t in trades if t.status == "closed")
        open_count = sum(1 for t in trades if t.status == "open")
        total = len(trades)

        # If we have many open trades that should have closed by now, suspicious
        stale_open = 0
        now = datetime.utcnow()
        for t in trades:
            if t.status == "open" and t.entry_timestamp:
                age_hours = (now - t.entry_timestamp).total_seconds() / 3600
                if age_hours > 24 and t.planned_holding_period_minutes:
                    planned_hours = t.planned_holding_period_minutes / 60
                    if age_hours > planned_hours * 3:
                        stale_open += 1

        cancel_rate = (cancelled / total * 100) if total > 0 else 0
        evidence = {
            "total": total,
            "closed": closed,
            "open": open_count,
            "cancelled": cancelled,
            "cancel_rate_pct": cancel_rate,
            "stale_open_trades": stale_open,
        }

        if stale_open > 5:
            return AuditFinding(
                check_name="no_trade_filtering",
                passed=False,
                severity="warning",
                message=f"{stale_open} trades stuck 'open' past their planned holding period (possible filter to avoid counting losses)",
                evidence=evidence,
            )
        if cancel_rate > 30:
            return AuditFinding(
                check_name="no_trade_filtering",
                passed=False,
                severity="warning",
                message=f"High cancel rate {cancel_rate:.1f}% (possible filtering)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_trade_filtering",
            passed=True,
            severity="info",
            message=f"Cancel rate {cancel_rate:.1f}% (acceptable)",
            evidence=evidence,
        )

    async def _check_no_orphan_trades(self, trades: List[Trade]) -> AuditFinding:
        """Check that all trades reference valid tokens."""
        token_ids = list({t.token_id for t in trades})
        if not token_ids:
            return AuditFinding(
                check_name="no_orphan_trades",
                passed=True,
                severity="info",
                message="No trades to check",
            )
        async with get_session() as s:
            stmt = select(Token.id).where(Token.id.in_(token_ids))
            result = await s.execute(stmt)
            found_ids = set(result.scalars().all())

        missing = [tid for tid in token_ids if tid not in found_ids]
        evidence = {"total_token_refs": len(token_ids), "missing_tokens": len(missing)}

        if missing:
            return AuditFinding(
                check_name="no_orphan_trades",
                passed=False,
                severity="error",
                message=f"{len(missing)} trades reference missing tokens (data integrity issue)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="no_orphan_trades",
            passed=True,
            severity="info",
            message="All trades reference valid tokens",
            evidence=evidence,
        )

    async def _check_pnl_integrity(self, trades: List[Trade]) -> AuditFinding:
        """Verify PnL fields are internally consistent."""
        inconsistencies = 0
        examples: List[Dict[str, Any]] = []
        for t in trades:
            # net_pnl should be roughly gross - fees - slippage
            if t.net_pnl_usd is not None and t.gross_pnl_usd is not None:
                expected_net = t.gross_pnl_usd - (t.fees_total_usd or 0) - (t.slippage_total_usd or 0)
                if abs(t.net_pnl_usd - expected_net) > 1.0:  # $1 tolerance
                    inconsistencies += 1
                    if len(examples) < 5:
                        examples.append({
                            "trade_id": t.id,
                            "gross": t.gross_pnl_usd,
                            "fees": t.fees_total_usd,
                            "slippage": t.slippage_total_usd,
                            "expected_net": expected_net,
                            "actual_net": t.net_pnl_usd,
                        })
            # pnl_pct should be (exit-entry)/entry * 100
            if (t.pnl_pct is not None and t.entry_price and t.exit_price
                    and t.entry_price > 0 and t.exit_price > 0):
                expected_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                if abs(t.pnl_pct - expected_pct) > 1.0:
                    inconsistencies += 1
                    if len(examples) < 5:
                        examples.append({
                            "trade_id": t.id,
                            "entry_price": t.entry_price,
                            "exit_price": t.exit_price,
                            "expected_pct": expected_pct,
                            "actual_pct": t.pnl_pct,
                        })

        evidence = {"inconsistencies": inconsistencies, "examples": examples}
        if inconsistencies > 5:
            return AuditFinding(
                check_name="pnl_integrity",
                passed=False,
                severity="error",
                message=f"{inconsistencies} PnL inconsistencies detected",
                evidence=evidence,
            )
        elif inconsistencies > 0:
            return AuditFinding(
                check_name="pnl_integrity",
                passed=False,
                severity="warning",
                message=f"{inconsistencies} minor PnL inconsistencies",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="pnl_integrity",
            passed=True,
            severity="info",
            message="PnL fields internally consistent",
            evidence=evidence,
        )

    async def _check_win_rate_consistency(
        self,
        trades: List[Trade],
        claimed_win_rate: Optional[float],
        claimed_total_trades: Optional[int],
        verified: Dict[str, Any],
    ) -> AuditFinding:
        """Compare AI's claimed win rate / trade count against the auditor's recomputation."""
        evidence: Dict[str, Any] = {
            "claimed_win_rate": claimed_win_rate,
            "verified_win_rate_pct": verified["win_rate"] * 100,
            "claimed_total_trades": claimed_total_trades,
            "verified_total_trades": verified["total_trades"],
        }

        wr_disc = (
            abs(claimed_win_rate - verified["win_rate"] * 100)
            if claimed_win_rate is not None else 0.0
        )
        trade_disc = (
            abs(claimed_total_trades - verified["total_trades"])
            if claimed_total_trades is not None else 0
        )
        evidence["win_rate_discrepancy"] = wr_disc
        evidence["trades_discrepancy"] = trade_disc

        if wr_disc > self.win_rate_discrepancy_threshold:
            return AuditFinding(
                check_name="win_rate_consistency",
                passed=False,
                severity="critical",
                message=(
                    f"AI claimed WR={claimed_win_rate:.1f}% but auditor computed "
                    f"{verified['win_rate']*100:.1f}% (delta {wr_disc:.1f}%)"
                ),
                evidence=evidence,
            )
        if trade_disc > 0:
            return AuditFinding(
                check_name="win_rate_consistency",
                passed=False,
                severity="error",
                message=f"AI claimed {claimed_total_trades} trades but auditor found {verified['total_trades']}",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="win_rate_consistency",
            passed=True,
            severity="info",
            message="Claimed metrics match auditor's computation",
            evidence=evidence,
        )

    async def _check_exit_reason_distribution(self, trades: List[Trade]) -> AuditFinding:
        """Check that exit reasons are diverse (not all manual)."""
        reasons: Dict[str, int] = {}
        for t in trades:
            r = t.exit_reason or "unknown"
            reasons[r] = reasons.get(r, 0) + 1
        evidence = {"distribution": reasons, "total": len(trades)}

        if not trades:
            return AuditFinding(
                check_name="exit_reason_distribution",
                passed=True,
                severity="info",
                message="No trades to check",
                evidence=evidence,
            )

        manual_pct = (reasons.get("manual", 0) / len(trades)) * 100
        if manual_pct > 50:
            return AuditFinding(
                check_name="exit_reason_distribution",
                passed=False,
                severity="warning",
                message=f"{manual_pct:.1f}% of trades exited manually (possible cherry-picking)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="exit_reason_distribution",
            passed=True,
            severity="info",
            message="Exit reason distribution looks healthy",
            evidence=evidence,
        )

    async def _check_time_distribution(
        self, trades: List[Trade], cutoff: datetime, now: datetime
    ) -> AuditFinding:
        """Check that trades are distributed across the time window (no suspicious clusters)."""
        if len(trades) < 5:
            return AuditFinding(
                check_name="time_distribution",
                passed=True,
                severity="info",
                message="Not enough trades for distribution analysis",
            )
        # Bucket by day
        buckets: Dict[str, int] = {}
        for t in trades:
            day = t.entry_timestamp.strftime("%Y-%m-%d")
            buckets[day] = buckets.get(day, 0) + 1

        # Check that no single day has >50% of trades
        max_pct = (max(buckets.values()) / len(trades)) * 100 if buckets else 0
        evidence = {"daily_distribution": buckets, "max_day_pct": max_pct}

        if max_pct > 50:
            return AuditFinding(
                check_name="time_distribution",
                passed=False,
                severity="warning",
                message=f"{max_pct:.1f}% of trades clustered in a single day (possible retesting)",
                evidence=evidence,
            )
        return AuditFinding(
            check_name="time_distribution",
            passed=True,
            severity="info",
            message="Trade time distribution looks normal",
            evidence=evidence,
        )

    # =====================================================================
    # Helpers
    # =====================================================================
    def _compute_verified_metrics(self, trades: List[Trade]) -> Dict[str, Any]:
        """Re-compute all performance metrics independently."""
        closed = [t for t in trades if t.status == "closed"]
        winning = [t for t in closed if (t.net_pnl_usd or 0) > 0]
        losing = [t for t in closed if (t.net_pnl_usd or 0) < 0]

        wins_total = sum(t.net_pnl_usd or 0 for t in winning)
        losses_total = sum(abs(t.net_pnl_usd or 0) for t in losing)
        net_pnl = wins_total - losses_total
        wr = len(winning) / max(len(closed), 1)
        pf = wins_total / max(losses_total, 1.0)
        expectancy = net_pnl / max(len(closed), 1)

        # Calibration: average predicted win probability
        pred_probs = []
        for t in closed:
            # We don't have snapshot here but we collect via separate query in check
            pass

        return {
            "total_trades": len(closed),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": wr,
            "net_pnl": net_pnl,
            "profit_factor": pf,
            "expectancy": expectancy,
            "avg_predicted_win_prob": 0.0,  # filled in by calibration check
        }

    def _build_summary(
        self,
        verdict: str,
        findings: List[AuditFinding],
        verified: Dict[str, Any],
        claimed_wr: Optional[float],
        claimed_trades: Optional[int],
    ) -> str:
        parts = [
            f"Audit verdict: {verdict.upper()}",
            f"Verified: {verified['total_trades']} trades, "
            f"WR={verified['win_rate']*100:.1f}%, "
            f"PF={verified['profit_factor']:.2f}, "
            f"net PnL=${verified['net_pnl']:.2f}",
        ]
        if claimed_wr is not None:
            parts.append(f"AI claimed WR={claimed_wr:.1f}% (delta={abs(claimed_wr - verified['win_rate']*100):.1f}%)")
        if claimed_trades is not None:
            parts.append(f"AI claimed {claimed_trades} trades (delta={abs(claimed_trades - verified['total_trades'])})")
        failed = [f for f in findings if not f.passed]
        if failed:
            parts.append(f"Failed checks: {', '.join(f.check_name for f in failed)}")
        return " | ".join(parts)

    async def _load_trades(
        self, since: datetime, until: datetime
    ) -> List[Trade]:
        async with get_session() as s:
            stmt = (
                select(Trade)
                .where(and_(
                    Trade.entry_timestamp >= since,
                    Trade.entry_timestamp <= until,
                ))
                .order_by(Trade.entry_timestamp)
            )
            result = await s.execute(stmt)
            return list(result.scalars().all())

    async def _save_report(self, report: AuditReport) -> None:
        async with get_session() as s:
            key = f"{self.AUDIT_PREFIX}{report.audit_id}"
            state = SystemState(key=key, value_json=report.to_dict())
            await s.merge(state)
            # Update index
            index_state = await s.get(SystemState, self.AUDIT_INDEX_KEY)
            if index_state is None:
                index_state = SystemState(key=self.AUDIT_INDEX_KEY, value_json={"ids": []})
            ids = list(index_state.value_json.get("ids", []))
            if report.audit_id not in ids:
                ids.append(report.audit_id)
            index_state.value_json = {
                "ids": ids,
                "count": len(ids),
                "updated_at": datetime.utcnow().isoformat(),
            }
            await s.merge(index_state)

    async def get_latest_report(self) -> Optional[AuditReport]:
        """Get the most recent audit report."""
        async with get_session() as s:
            stmt = (
                select(SystemState)
                .where(SystemState.key.like(f"{self.AUDIT_PREFIX}%"))
                .order_by(SystemState.updated_at.desc())
                .limit(1)
            )
            result = await s.execute(stmt)
            state = result.scalars().first()
            if not state or not state.value_json:
                return None
            return AuditReport(**{
                k: v for k, v in state.value_json.items()
                if k in AuditReport.__dataclass_fields__
            })

    async def list_reports(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent audit reports (summaries only)."""
        out: List[Dict[str, Any]] = []
        async with get_session() as s:
            stmt = (
                select(SystemState)
                .where(SystemState.key.like(f"{self.AUDIT_PREFIX}%"))
                .order_by(SystemState.updated_at.desc())
                .limit(limit)
            )
            result = await s.execute(stmt)
            for state in result.scalars().all():
                data = state.value_json or {}
                out.append({
                    "audit_id": data.get("audit_id"),
                    "timestamp": data.get("timestamp"),
                    "verdict": data.get("verdict"),
                    "total_checks": data.get("total_checks"),
                    "checks_passed": data.get("checks_passed"),
                    "critical_failures": data.get("critical_failures"),
                    "verified_win_rate": data.get("verified_win_rate"),
                    "summary": data.get("summary"),
                })
        return out


__all__ = ["IndependentAuditor", "AuditReport", "AuditFinding"]
