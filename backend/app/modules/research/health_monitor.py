"""
System Health Monitor.

Continuously monitors the health of the entire trading system:

1. API failure tracking (per data source)
2. Data freshness checking (tokens/ohlcv not stale)
3. Duplicate record detection (data corruption)
4. Model drift detection (predicted vs actual win rate)
5. Memory consumption / process metrics
6. Database connectivity and latency
7. Queue / job backlogs

If data is unreliable, the health monitor BLOCKS new trades by setting
a system_state flag (`health:block_trading`) that the orchestrator
checks before any new trade.
"""
from __future__ import annotations

import asyncio
import os
import platform
import time
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
    SystemState, Token, AnalysisSnapshot, Trade, DataSourceCall, ScanRun, ModelVersion,
)


# =========================================================================
# Data structures
# =========================================================================
@dataclass
class HealthCheck:
    """A single health check result."""
    name: str
    passed: bool
    severity: str = "info"  # info | warning | error | critical
    message: str = ""
    metric_value: Any = None
    threshold: Any = None
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Full system health status."""
    status_id: str
    timestamp: str
    overall_status: str = "healthy"  # healthy | degraded | unhealthy | critical

    # If True, the orchestrator MUST NOT place new trades
    block_trading: bool = False
    block_reason: str = ""

    # Aggregates
    total_checks: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    critical_count: int = 0
    warning_count: int = 0

    # Specific metrics
    api_failure_rate_pct: float = 0.0
    data_age_seconds: int = 0
    duplicate_records_count: int = 0
    model_drift_score: float = 0.0
    memory_usage_mb: float = 0.0
    memory_usage_pct: float = 0.0
    cpu_load: float = 0.0
    db_latency_ms: int = 0

    # Per-source health
    data_source_health: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Individual checks
    checks: List[Dict[str, Any]] = field(default_factory=list)

    # Recommendations
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================================================================
# Health Monitor
# =========================================================================
class HealthMonitor:
    """
    Monitors system health continuously and can BLOCK trading when data is unreliable.

    Usage:
        monitor = HealthMonitor()
        status = await monitor.check_health()
        if status.block_trading:
            logger.warning(f"Trading blocked: {status.block_reason}")
    """

    BLOCK_KEY = "health:block_trading"
    STATUS_PREFIX = "health:status:"
    LATEST_STATUS_KEY = "health:latest_status"

    # Thresholds
    MAX_DATA_AGE_SECONDS = 300  # 5 minutes
    MAX_API_FAILURE_RATE_PCT = 15.0
    MAX_DUPLICATE_COUNT = 10
    MODEL_DRIFT_CRITICAL = 0.20
    MODEL_DRIFT_WARNING = 0.10
    MAX_DB_LATENCY_MS = 2000
    MAX_MEMORY_PCT = 90.0

    def __init__(
        self,
        max_data_age_seconds: int = 300,
        max_api_failure_rate: float = 15.0,
    ):
        self.max_data_age_seconds = max_data_age_seconds
        self.max_api_failure_rate = max_api_failure_rate

    # =====================================================================
    # Main check
    # =====================================================================
    async def check_health(self) -> HealthStatus:
        """Run all health checks and return aggregated status."""
        status_id = str(uuid.uuid4())
        now = datetime.utcnow()
        checks: List[HealthCheck] = []

        # Run all checks
        checks.append(await self._check_data_freshness())
        checks.append(await self._check_api_health())
        checks.append(await self._check_duplicate_records())
        checks.append(await self._check_model_drift())
        checks.append(await self._check_db_latency())
        checks.append(await self._check_memory_usage())
        checks.append(await self._check_scan_runs_health())
        checks.append(await self._check_token_data_completeness())
        checks.append(await self._check_open_trades_health())

        # Aggregate
        critical = sum(1 for c in checks if c.severity == "critical" and not c.passed)
        errors = sum(1 for c in checks if c.severity == "error" and not c.passed)
        warnings = sum(1 for c in checks if c.severity == "warning" and not c.passed)
        passed = sum(1 for c in checks if c.passed)

        if critical > 0:
            overall = "critical"
            block_trading = True
        elif errors > 0:
            overall = "unhealthy"
            block_trading = True
        elif warnings > 2:
            overall = "degraded"
            block_trading = False  # don't block, just warn
        else:
            overall = "healthy"
            block_trading = False

        # Block reason
        block_reason = ""
        if block_trading:
            failed_critical = [c for c in checks if c.severity in ("critical", "error") and not c.passed]
            block_reason = "; ".join(f"{c.name}: {c.message}" for c in failed_critical[:3])

        # Per-source health
        per_source = await self._get_per_source_health()

        # Memory metrics
        mem_mb, mem_pct = self._get_memory_usage()
        cpu_load = self._get_cpu_load()

        # Recommendations
        recommendations = self._build_recommendations(checks, overall)

        status = HealthStatus(
            status_id=status_id,
            timestamp=now.isoformat(),
            overall_status=overall,
            block_trading=block_trading,
            block_reason=block_reason,
            total_checks=len(checks),
            checks_passed=passed,
            checks_failed=len(checks) - passed,
            critical_count=critical,
            warning_count=warnings,
            api_failure_rate_pct=next(
                (c.metric_value for c in checks if c.name == "api_health" and c.metric_value is not None),
                0.0,
            ),
            data_age_seconds=next(
                (c.metric_value for c in checks if c.name == "data_freshness" and c.metric_value is not None),
                0,
            ),
            duplicate_records_count=next(
                (c.metric_value for c in checks if c.name == "duplicate_records" and c.metric_value is not None),
                0,
            ),
            model_drift_score=next(
                (c.metric_value for c in checks if c.name == "model_drift" and c.metric_value is not None),
                0.0,
            ),
            memory_usage_mb=mem_mb,
            memory_usage_pct=mem_pct,
            cpu_load=cpu_load,
            db_latency_ms=next(
                (c.metric_value for c in checks if c.name == "db_latency" and c.metric_value is not None),
                0,
            ),
            data_source_health=per_source,
            checks=[asdict(c) for c in checks],
            recommendations=recommendations,
        )

        # Persist and set block flag
        await self._save_status(status)
        await self._set_block_flag(block_trading, block_reason)

        logger.info(
            f"❤️  Health check: {overall} "
            f"(passed {passed}/{len(checks)}, {critical} critical, {warnings} warnings) "
            f"block_trading={block_trading}"
        )
        if block_trading:
            logger.warning(f"⛔ TRADING BLOCKED: {block_reason}")

        return status

    async def is_trading_allowed(self) -> Tuple[bool, str]:
        """Quick check: can the bot place new trades right now?"""
        async with get_session() as s:
            state = await s.get(SystemState, self.BLOCK_KEY)
            if state and state.value_json.get("blocked", False):
                return False, state.value_json.get("reason", "unknown")
        return True, ""

    # =====================================================================
    # Individual checks
    # =====================================================================
    async def _check_data_freshness(self) -> HealthCheck:
        """Check that token data is fresh (not stale)."""
        cutoff = datetime.utcnow() - timedelta(seconds=self.max_data_age_seconds)
        async with get_session() as s:
            # Count active tokens not updated recently
            stmt = select(func.count(Token.id)).where(
                and_(
                    Token.active == True,
                    Token.blacklisted == False,
                    Token.is_dead == False,
                    Token.updated_at < cutoff,
                )
            )
            stale_count = (await s.execute(stmt)).scalar() or 0

            # Get max age
            stmt2 = select(func.max(Token.updated_at)).where(Token.active == True)
            latest_update = (await s.execute(stmt2)).scalar()
            age_seconds = 0
            if latest_update:
                age_seconds = int((datetime.utcnow() - latest_update).total_seconds())

        evidence = {"stale_token_count": stale_count, "max_age_seconds": age_seconds}
        if stale_count > 100 or age_seconds > self.max_data_age_seconds * 3:
            return HealthCheck(
                name="data_freshness",
                passed=False,
                severity="critical",
                message=f"Data is stale: {stale_count} tokens not updated, max age {age_seconds}s",
                metric_value=age_seconds,
                threshold=self.max_data_age_seconds,
                evidence=evidence,
            )
        if stale_count > 20 or age_seconds > self.max_data_age_seconds:
            return HealthCheck(
                name="data_freshness",
                passed=False,
                severity="warning",
                message=f"Data aging: {stale_count} tokens stale, max age {age_seconds}s",
                metric_value=age_seconds,
                threshold=self.max_data_age_seconds,
                evidence=evidence,
            )
        return HealthCheck(
            name="data_freshness",
            passed=True,
            severity="info",
            message=f"Data fresh (max age {age_seconds}s)",
            metric_value=age_seconds,
            threshold=self.max_data_age_seconds,
            evidence=evidence,
        )

    async def _check_api_health(self) -> HealthCheck:
        """Check data source API failure rates."""
        cutoff = datetime.utcnow() - timedelta(hours=1)
        async with get_session() as s:
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

        if failure_rate > self.max_api_failure_rate * 2:
            return HealthCheck(
                name="api_health",
                passed=False,
                severity="critical",
                message=f"API failure rate {failure_rate:.1f}% > 2x threshold",
                metric_value=failure_rate,
                threshold=self.max_api_failure_rate,
                evidence=evidence,
            )
        if failure_rate > self.max_api_failure_rate:
            return HealthCheck(
                name="api_health",
                passed=False,
                severity="warning",
                message=f"API failure rate {failure_rate:.1f}% > {self.max_api_failure_rate}%",
                metric_value=failure_rate,
                threshold=self.max_api_failure_rate,
                evidence=evidence,
            )
        return HealthCheck(
            name="api_health",
            passed=True,
            severity="info",
            message=f"API failure rate {failure_rate:.1f}% (acceptable)",
            metric_value=failure_rate,
            threshold=self.max_api_failure_rate,
            evidence=evidence,
        )

    async def _check_duplicate_records(self) -> HealthCheck:
        """Check for duplicate token records (same address+chain)."""
        async with get_session() as s:
            # Use raw SQL for group-by count
            if settings.database_url.startswith("sqlite"):
                stmt = text(
                    "SELECT COUNT(*) as dup_count FROM ("
                    "  SELECT chain, address, COUNT(*) as c FROM tokens "
                    "  GROUP BY chain, address HAVING c > 1"
                    ") as dupes"
                )
            else:
                stmt = text(
                    "SELECT COUNT(*) as dup_count FROM ("
                    "  SELECT chain, address, COUNT(*) as c FROM tokens "
                    "  GROUP BY chain, address HAVING COUNT(*) > 1"
                    ") as dupes"
                )
            try:
                result = await s.execute(stmt)
                dup_count = result.scalar() or 0
            except Exception as exc:
                logger.debug(f"Duplicate check failed: {exc}")
                dup_count = 0

        evidence = {"duplicate_groups": dup_count}
        if dup_count > 50:
            return HealthCheck(
                name="duplicate_records",
                passed=False,
                severity="error",
                message=f"{dup_count} duplicate token groups detected (data integrity issue)",
                metric_value=dup_count,
                threshold=self.MAX_DUPLICATE_COUNT,
                evidence=evidence,
            )
        if dup_count > self.MAX_DUPLICATE_COUNT:
            return HealthCheck(
                name="duplicate_records",
                passed=False,
                severity="warning",
                message=f"{dup_count} duplicate token groups",
                metric_value=dup_count,
                threshold=self.MAX_DUPLICATE_COUNT,
                evidence=evidence,
            )
        return HealthCheck(
            name="duplicate_records",
            passed=True,
            severity="info",
            message=f"{dup_count} duplicate groups (acceptable)",
            metric_value=dup_count,
            threshold=self.MAX_DUPLICATE_COUNT,
            evidence=evidence,
        )

    async def _check_model_drift(self) -> HealthCheck:
        """Check for model drift (predicted win rate vs actual)."""
        cutoff = datetime.utcnow() - timedelta(days=7)
        async with get_session() as s:
            # Get closed trades with their analysis snapshots
            stmt = (
                select(Trade, AnalysisSnapshot.win_probability)
                .join(AnalysisSnapshot, Trade.analysis_snapshot_id == AnalysisSnapshot.id, isouter=True)
                .where(and_(
                    Trade.status == "closed",
                    Trade.entry_timestamp >= cutoff,
                    AnalysisSnapshot.win_probability.isnot(None),
                ))
            )
            result = await s.execute(stmt)
            rows = list(result.all())

        if len(rows) < 20:
            return HealthCheck(
                name="model_drift",
                passed=True,
                severity="info",
                message="Not enough trades for drift analysis",
                metric_value=0.0,
                threshold=self.MODEL_DRIFT_WARNING,
                evidence={"sample_size": len(rows)},
            )

        avg_pred = sum(float(r[1] or 0) for r in rows) / len(rows)
        actual_wins = sum(1 for r in rows if (r[0].net_pnl_usd or 0) > 0)
        actual_wr = actual_wins / len(rows)
        drift = abs(avg_pred - actual_wr)

        evidence = {
            "avg_predicted": avg_pred,
            "actual_win_rate": actual_wr,
            "drift": drift,
            "sample_size": len(rows),
        }

        if drift > self.MODEL_DRIFT_CRITICAL:
            return HealthCheck(
                name="model_drift",
                passed=False,
                severity="critical",
                message=f"Model drift {drift:.1%} > critical threshold",
                metric_value=drift,
                threshold=self.MODEL_DRIFT_CRITICAL,
                evidence=evidence,
            )
        if drift > self.MODEL_DRIFT_WARNING:
            return HealthCheck(
                name="model_drift",
                passed=False,
                severity="warning",
                message=f"Model drift {drift:.1%} > warning threshold",
                metric_value=drift,
                threshold=self.MODEL_DRIFT_WARNING,
                evidence=evidence,
            )
        return HealthCheck(
            name="model_drift",
            passed=True,
            severity="info",
            message=f"Model drift {drift:.1%} (acceptable)",
            metric_value=drift,
            threshold=self.MODEL_DRIFT_WARNING,
            evidence=evidence,
        )

    async def _check_db_latency(self) -> HealthCheck:
        """Check database latency by running a simple query."""
        t0 = time.time()
        try:
            async with get_session() as s:
                await s.execute(text("SELECT 1"))
            latency_ms = int((time.time() - t0) * 1000)
        except Exception as exc:
            return HealthCheck(
                name="db_latency",
                passed=False,
                severity="critical",
                message=f"Database query failed: {str(exc)[:100]}",
                metric_value=99999,
                threshold=self.MAX_DB_LATENCY_MS,
            )

        if latency_ms > self.MAX_DB_LATENCY_MS * 5:
            return HealthCheck(
                name="db_latency",
                passed=False,
                severity="critical",
                message=f"DB latency {latency_ms}ms extremely high",
                metric_value=latency_ms,
                threshold=self.MAX_DB_LATENCY_MS,
            )
        if latency_ms > self.MAX_DB_LATENCY_MS:
            return HealthCheck(
                name="db_latency",
                passed=False,
                severity="warning",
                message=f"DB latency {latency_ms}ms > {self.MAX_DB_LATENCY_MS}ms",
                metric_value=latency_ms,
                threshold=self.MAX_DB_LATENCY_MS,
            )
        return HealthCheck(
            name="db_latency",
            passed=True,
            severity="info",
            message=f"DB latency {latency_ms}ms",
            metric_value=latency_ms,
            threshold=self.MAX_DB_LATENCY_MS,
        )

    async def _check_memory_usage(self) -> HealthCheck:
        """Check memory consumption."""
        mem_mb, mem_pct = self._get_memory_usage()
        evidence = {"memory_mb": mem_mb, "memory_pct": mem_pct}

        if mem_pct > 95:
            return HealthCheck(
                name="memory_usage",
                passed=False,
                severity="critical",
                message=f"Memory {mem_pct:.1f}% critically high",
                metric_value=mem_pct,
                threshold=self.MAX_MEMORY_PCT,
                evidence=evidence,
            )
        if mem_pct > self.MAX_MEMORY_PCT:
            return HealthCheck(
                name="memory_usage",
                passed=False,
                severity="warning",
                message=f"Memory {mem_pct:.1f}% > {self.MAX_MEMORY_PCT}%",
                metric_value=mem_pct,
                threshold=self.MAX_MEMORY_PCT,
                evidence=evidence,
            )
        return HealthCheck(
            name="memory_usage",
            passed=True,
            severity="info",
            message=f"Memory {mem_pct:.1f}% ({mem_mb:.0f}MB)",
            metric_value=mem_pct,
            threshold=self.MAX_MEMORY_PCT,
            evidence=evidence,
        )

    async def _check_scan_runs_health(self) -> HealthCheck:
        """Check that scan runs are happening regularly."""
        cutoff = datetime.utcnow() - timedelta(hours=1)
        async with get_session() as s:
            stmt = select(func.count(ScanRun.id)).where(ScanRun.started_at >= cutoff)
            count = (await s.execute(stmt)).scalar() or 0

            # Check for failed scans
            failed_stmt = select(func.count(ScanRun.id)).where(
                and_(
                    ScanRun.started_at >= cutoff,
                    ScanRun.status == "failed",
                )
            )
            failed = (await s.execute(failed_stmt)).scalar() or 0

        evidence = {"scan_runs_last_hour": count, "failed_scans": failed}
        # Ultimate-bot FIX: a fresh install (zero total scan runs ever) should
        # NOT block trading. Only flag an error if scans WERE happening but
        # stopped (i.e. total scan runs > 0 but none in the last hour).
        total_stmt = select(func.count(ScanRun.id))
        total = (await s.execute(total_stmt)).scalar() or 0

        if count == 0:
            if total == 0:
                # Fresh install — no scans ever ran yet. Don't block trading.
                return HealthCheck(
                    name="scan_runs_health",
                    passed=True,
                    severity="info",
                    message="Fresh install — no scan runs yet. Trading allowed.",
                    metric_value=count,
                    threshold=1,
                    evidence=evidence,
                )
            # Scans existed before but none in the last hour — real problem
            return HealthCheck(
                name="scan_runs_health",
                passed=False,
                severity="warning",
                message="No scan runs in the last hour (scanner may be down)",
                metric_value=count,
                threshold=1,
                evidence=evidence,
            )
        if failed > count * 0.5 and count > 0:
            return HealthCheck(
                name="scan_runs_health",
                passed=False,
                severity="warning",
                message=f"{failed}/{count} scans failed in last hour",
                metric_value=failed,
                threshold=count / 2,
                evidence=evidence,
            )
        return HealthCheck(
            name="scan_runs_health",
            passed=True,
            severity="info",
            message=f"{count} scan runs in last hour ({failed} failed)",
            metric_value=count,
            threshold=1,
            evidence=evidence,
        )

    async def _check_token_data_completeness(self) -> HealthCheck:
        """Check that active tokens have complete market data (price, liquidity, volume)."""
        async with get_session() as s:
            # Count active tokens missing critical fields
            stmt = select(func.count(Token.id)).where(
                and_(
                    Token.active == True,
                    Token.blacklisted == False,
                    or_missing_critical(Token),
                )
            )
            incomplete = (await s.execute(stmt)).scalar() or 0

            total_stmt = select(func.count(Token.id)).where(
                and_(Token.active == True, Token.blacklisted == False)
            )
            total = (await s.execute(total_stmt)).scalar() or 0

        incomplete_pct = (incomplete / total * 100) if total > 0 else 0
        evidence = {"incomplete_tokens": incomplete, "total_tokens": total, "incomplete_pct": incomplete_pct}

        if incomplete_pct > 30:
            return HealthCheck(
                name="token_data_completeness",
                passed=False,
                severity="warning",
                message=f"{incomplete_pct:.1f}% of active tokens missing critical data",
                metric_value=incomplete_pct,
                threshold=30.0,
                evidence=evidence,
            )
        return HealthCheck(
            name="token_data_completeness",
            passed=True,
            severity="info",
            message=f"{incomplete_pct:.1f}% tokens incomplete (acceptable)",
            metric_value=incomplete_pct,
            threshold=30.0,
            evidence=evidence,
        )

    async def _check_open_trades_health(self) -> HealthCheck:
        """Check for stuck open trades."""
        cutoff = datetime.utcnow() - timedelta(hours=24)
        async with get_session() as s:
            stmt = select(func.count(Trade.id)).where(
                and_(
                    Trade.status == "open",
                    Trade.entry_timestamp < cutoff,
                )
            )
            stuck = (await s.execute(stmt)).scalar() or 0

        evidence = {"stuck_open_trades": stuck}
        if stuck > 5:
            return HealthCheck(
                name="open_trades_health",
                passed=False,
                severity="error",
                message=f"{stuck} open trades older than 24h (stuck)",
                metric_value=stuck,
                threshold=5,
                evidence=evidence,
            )
        if stuck > 0:
            return HealthCheck(
                name="open_trades_health",
                passed=False,
                severity="warning",
                message=f"{stuck} stuck open trades",
                metric_value=stuck,
                threshold=5,
                evidence=evidence,
            )
        return HealthCheck(
            name="open_trades_health",
            passed=True,
            severity="info",
            message="No stuck open trades",
            metric_value=stuck,
            threshold=5,
            evidence=evidence,
        )

    # =====================================================================
    # Helpers
    # =====================================================================
    async def _get_per_source_health(self) -> Dict[str, Dict[str, Any]]:
        """Get per-source API health stats."""
        out: Dict[str, Dict[str, Any]] = {}
        cutoff = datetime.utcnow() - timedelta(hours=1)
        async with get_session() as s:
            stmt = (
                select(
                    DataSourceCall.source,
                    func.count(DataSourceCall.id).label("total"),
                    func.sum(func.cast(DataSourceCall.success, Integer())).label("success"),
                    func.avg(DataSourceCall.latency_ms).label("avg_latency"),
                )
                .where(DataSourceCall.timestamp >= cutoff)
                .group_by(DataSourceCall.source)
            )
            result = await s.execute(stmt)
            for row in result.all():
                source = row[0]
                total = row[1] or 0
                success = row[2] or 0
                avg_lat = row[3] or 0
                out[source] = {
                    "total_calls": total,
                    "successful_calls": success,
                    "failure_rate_pct": ((total - success) / total * 100) if total > 0 else 0,
                    "avg_latency_ms": float(avg_lat),
                }
        return out

    def _get_memory_usage(self) -> Tuple[float, float]:
        """Get memory usage in MB and percentage. Uses psutil if available."""
        try:
            import psutil
            proc = psutil.Process()
            mem_info = proc.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            sys_mem = psutil.virtual_memory()
            mem_pct = (mem_info.rss / sys_mem.total) * 100
            return mem_mb, mem_pct
        except ImportError:
            # Fallback: read /proc/self/status (Linux only)
            try:
                with open("/proc/self/status", "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_mb = int(line.split()[1]) / 1024
                            return mem_mb, min(mem_mb / 1024 * 100, 100.0)  # rough
            except Exception:
                pass
        return 0.0, 0.0

    def _get_cpu_load(self) -> float:
        """Get CPU load average. Returns 1-minute load average on Unix."""
        try:
            return float(os.getloadavg()[0])
        except Exception:
            try:
                import psutil
                return psutil.cpu_percent(interval=0.1)
            except ImportError:
                return 0.0

    def _build_recommendations(
        self, checks: List[HealthCheck], overall: str
    ) -> List[str]:
        recs: List[str] = []
        for c in checks:
            if not c.passed:
                if c.name == "data_freshness" and c.severity in ("critical", "error"):
                    recs.append("Restart the market scanner — data is stale.")
                elif c.name == "api_health" and c.severity in ("critical", "error"):
                    recs.append("Check API keys and rate limits — API failure rate is high.")
                elif c.name == "model_drift" and c.severity in ("critical", "error"):
                    recs.append("Retrain the win-probability model — drift detected.")
                elif c.name == "db_latency" and c.severity in ("critical", "error"):
                    recs.append("Investigate DB performance — latency is high.")
                elif c.name == "memory_usage" and c.severity in ("critical", "error"):
                    recs.append("Restart the bot — memory usage is critical.")
                elif c.name == "scan_runs_health":
                    recs.append("Check the scanner orchestrator — no recent scans.")
                elif c.name == "open_trades_health":
                    recs.append("Investigate stuck open trades — they may need force-close.")
                elif c.name == "duplicate_records":
                    recs.append("Run data deduplication — duplicate tokens detected.")
        if overall == "healthy":
            recs.append("All systems healthy.")
        return recs

    async def _save_status(self, status: HealthStatus) -> None:
        async with get_session() as s:
            key = f"{self.STATUS_PREFIX}{status.status_id}"
            state = SystemState(key=key, value_json=status.to_dict())
            await s.merge(state)
            # Save latest status pointer
            latest = SystemState(
                key=self.LATEST_STATUS_KEY,
                value_json={"status_id": status.status_id, "timestamp": status.timestamp},
            )
            await s.merge(latest)

    async def _set_block_flag(self, block: bool, reason: str) -> None:
        async with get_session() as s:
            state = SystemState(
                key=self.BLOCK_KEY,
                value_json={
                    "blocked": block,
                    "reason": reason,
                    "set_at": datetime.utcnow().isoformat(),
                },
            )
            await s.merge(state)

    async def get_latest_status(self) -> Optional[HealthStatus]:
        """Get the most recent health status."""
        async with get_session() as s:
            pointer = await s.get(SystemState, self.LATEST_STATUS_KEY)
            if not pointer:
                return None
            status_id = pointer.value_json.get("status_id")
            if not status_id:
                return None
            state = await s.get(SystemState, f"{self.STATUS_PREFIX}{status_id}")
            if not state:
                return None
            return HealthStatus(**{
                k: v for k, v in (state.value_json or {}).items()
                if k in HealthStatus.__dataclass_fields__
            })


# Helper function (module-level so it can be used in select)
from sqlalchemy import or_, Integer as Integer


def or_missing_critical(token_model):
    """Returns an OR condition for tokens missing critical fields."""
    return or_(
        token_model.price_usd.is_(None),
        token_model.liquidity_usd.is_(None),
        token_model.volume_24h_usd.is_(None),
        token_model.market_cap_usd.is_(None),
    )


__all__ = ["HealthMonitor", "HealthStatus", "HealthCheck"]
