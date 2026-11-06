"""
Self-Learning System.

A. Trade Memory: persists every trade with full context
B. Loss Analysis: extracts the cause of each loss with evidence
C. Win Analysis: extracts success factors
D. Mistake Memory: pattern DB for repetition prevention
E. Continuous Improvement: analyzes batches of trades and proposes improvements
F. Anti-Repetition: matches new opportunities against past mistakes

ALL improvements are TESTED before being adopted (anti-overfitting).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Trade, Mistake, Learning, Token, AnalysisSnapshot


# =========================================================================
# Loss analysis: classify why a trade lost
# =========================================================================
LOSS_CAUSES = {
    "late_entry": "الدخول متأخر بعد ارتفاع حاد",
    "fake_breakout": "اختراق وهمي",
    "weak_liquidity": "سيولة ضعيفة",
    "ignored_indicator": "تجاهل مؤشر مهم",
    "stale_data": "البيانات قديمة",
    "bad_market_regime": "ظروف السوق غير مناسبة",
    "bad_stop_loss": "Stop Loss غير مناسب",
    "overconfidence": "توقعات مبالغ فيها",
    "suspicious_activity": "نشاط مشبوه (wash trading / insider)",
    "model_overestimate": "النموذج بالغ في تقدير الاحتمال",
    "high_volatility": "تقلب عالٍ غير محسوب",
    "momentum_reversal": "انعكاس الزخم",
}


@dataclass
class LossAnalysisResult:
    causes: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    suggested_fix: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0  # 0-1
    summary: str = ""


class LossAnalyzer:
    """Analyzes a closed losing trade and determines the cause."""

    async def analyze(self, trade: Trade) -> LossAnalysisResult:
        result = LossAnalysisResult()

        # Fetch the analysis snapshot (entry context)
        snapshot = None
        if trade.analysis_snapshot_id:
            async with get_session() as s:
                snapshot = await s.get(AnalysisSnapshot, trade.analysis_snapshot_id)

        # Gather evidence
        evidence: List[str] = []

        # 1. Late entry?
        if snapshot:
            indicators = snapshot.indicators_json or {}
            tech = indicators.get("technical", {}) or {}
            pa = tech.get("price_action", {}) or {}
            chg_5m = tech.get("price_change_5m_pct", 0) or 0
            # If price was already up >20% in 5m before entry, likely late
            if chg_5m > 20:
                result.causes.append("late_entry")
                evidence.append(f"السعر صعد {chg_5m:.1f}% خلال 5 دقائق قبل الدخول")

        # 2. Fake breakout?
        if snapshot:
            pa = (snapshot.indicators_json or {}).get("technical", {}).get("price_action", {})
            if pa.get("fake_breakout_up"):
                result.causes.append("fake_breakout")
                evidence.append("المؤشرات الفنية أظهرت اختراقاً وهمياً قبل الدخول")

        # 3. Weak liquidity?
        if snapshot and snapshot.liquidity_score < 50:
            result.causes.append("weak_liquidity")
            evidence.append(f"درجة السيولة كانت {snapshot.liquidity_score:.1f}/100")

        # 4. Bad market regime?
        if snapshot and snapshot.market_regime in ["high_volatility", "panic_selling", "bear_market"]:
            result.causes.append("bad_market_regime")
            evidence.append(f"حالة السوق كانت {snapshot.market_regime}")

        # 5. Stop loss too tight?
        if trade.stop_loss_price and trade.entry_price:
            sl_pct = abs(trade.entry_price - trade.stop_loss_price) / trade.entry_price * 100
            if sl_pct < 2:
                result.causes.append("bad_stop_loss")
                evidence.append(f"Stop Loss ضيق جداً ({sl_pct:.2f}%)")
            elif sl_pct > 15:
                result.causes.append("bad_stop_loss")
                evidence.append(f"Stop Loss واسع جداً ({sl_pct:.2f}%)")

        # 6. Suspicious activity?
        if snapshot and (snapshot.flags_json or {}).get("critical_flags"):
            result.causes.append("suspicious_activity")
            evidence.append(f"مؤشرات خطر حرج: {(snapshot.flags_json or {}).get('critical_flags')}")

        # 7. High volatility?
        if snapshot:
            tech = (snapshot.indicators_json or {}).get("technical", {})
            vol_pct = tech.get("volatility_pct", 0)
            if vol_pct > 15:
                result.causes.append("high_volatility")
                evidence.append(f"التقلب {vol_pct:.1f}%")

        # 8. Model overestimate?
        if snapshot and snapshot.win_probability:
            if snapshot.win_probability > 0.85:
                result.causes.append("model_overestimate")
                evidence.append(
                    f"النموذج قدر احتمال نجاح {snapshot.win_probability:.0%} - أعلى من المتوقع"
                )

        # 9. Stale data?
        if snapshot and snapshot.data_age_seconds > 300:
            result.causes.append("stale_data")
            evidence.append(f"عمر البيانات {snapshot.data_age_seconds}s عند الدخول")

        # 10. Momentum reversal?
        if trade.max_unrealized_profit_usd and trade.max_unrealized_profit_usd > 0:
            # If we had profit but lost, momentum reversed
            if trade.net_pnl_usd and trade.net_pnl_usd < 0:
                result.causes.append("momentum_reversal")
                evidence.append(
                    f"تحقق ربح غير محقق ${trade.max_unrealized_profit_usd:.2f} ثم انعكس"
                )

        # Default: if no cause detected
        if not result.causes:
            result.causes.append("unknown")
            evidence.append("لا يوجد سبب واضح - قد يكون صدفة أو عامل لم نرصده")

        result.evidence = evidence
        result.confidence = min(len(evidence) / 3.0, 1.0)
        result.summary = self._build_summary(trade, result)
        return result

    def _build_summary(self, trade: Trade, r: LossAnalysisResult) -> str:
        causes_ar = [LOSS_CAUSES.get(c, c) for c in r.causes]
        return (
            f"خسارة في {trade.token_id}: "
            f"صافي الخسارة ${trade.net_pnl_usd:.2f} ({trade.pnl_pct:.2f}%). "
            f"الأسباب المحتملة: {', '.join(causes_ar)}. "
            f"الأدلة: {' | '.join(r.evidence)}"
        )


# =========================================================================
# Win analysis
# =========================================================================
@dataclass
class WinAnalysisResult:
    success_factors: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    repeatable: bool = False
    summary: str = ""


class WinAnalyzer:
    async def analyze(self, trade: Trade) -> WinAnalysisResult:
        result = WinAnalysisResult()
        snapshot = None
        if trade.analysis_snapshot_id:
            async with get_session() as s:
                snapshot = await s.get(AnalysisSnapshot, trade.analysis_snapshot_id)

        if snapshot:
            if snapshot.technical_score > 70:
                result.success_factors.append("مؤشرات فنية قوية")
                result.evidence.append(f"Technical Score = {snapshot.technical_score:.1f}")
            if snapshot.liquidity_score > 70:
                result.success_factors.append("سيولة جيدة")
                result.evidence.append(f"Liquidity Score = {snapshot.liquidity_score:.1f}")
            if snapshot.momentum_score > 70:
                result.success_factors.append("زخم قوي")
                result.evidence.append(f"Momentum Score = {snapshot.momentum_score:.1f}")
            if snapshot.security_score > 70:
                result.success_factors.append("ملف أمان جيد")
                result.evidence.append(f"Security Score = {snapshot.security_score:.1f}")
            if snapshot.market_regime in ["bull_market", "strong_momentum"]:
                result.success_factors.append(f"حالة سوق مواتية ({snapshot.market_regime})")
            # Was it a clean exit at TP?
            if trade.exit_reason == "take_profit":
                result.success_factors.append("تنفيذ خطة TP بنجاح")
                result.evidence.append(f"Exit reason = {trade.exit_reason}")

        # Repeatable if multiple factors aligned
        result.repeatable = len(result.success_factors) >= 3
        result.summary = (
            f"ربح في {trade.token_id}: "
            f"صافي الربح ${trade.net_pnl_usd:.2f} ({trade.pnl_pct:.2f}%). "
            f"عوامل النجاح: {', '.join(result.success_factors)}. "
            f"القابلية للتكرار: {'عالية' if result.repeatable else 'منخفضة'}."
        )
        return result


# =========================================================================
# Mistake memory
# =========================================================================
class MistakeMemory:
    """Stores and retrieves mistakes for anti-repetition."""

    async def record_mistake(
        self,
        trade: Trade,
        analysis: LossAnalysisResult,
    ) -> Mistake:
        """Record a mistake from a losing trade."""
        # Build conditions (the context under which this mistake tends to happen)
        conditions: Dict[str, Any] = {}
        snapshot = None
        if trade.analysis_snapshot_id:
            async with get_session() as s:
                snapshot = await s.get(AnalysisSnapshot, trade.analysis_snapshot_id)

        if snapshot:
            conditions["market_regime"] = snapshot.market_regime
            conditions["technical_score"] = snapshot.technical_score
            conditions["liquidity_score"] = snapshot.liquidity_score
            conditions["risk_score"] = snapshot.risk_score
            conditions["causes"] = analysis.causes

        mistake = Mistake(
            trade_id=trade.id,
            mistake_type=analysis.causes[0] if analysis.causes else "unknown",
            description=analysis.summary,
            conditions_json=conditions,
            evidence_json=analysis.evidence,
            suggested_fix_json=self._suggest_fix(analysis.causes),
            occurrence_count=1,
            last_occurred_at=datetime.utcnow(),
        )

        async with get_session() as s:
            s.add(mistake)
        return mistake

    def _suggest_fix(self, causes: List[str]) -> Dict[str, Any]:
        """Suggest a fix based on identified causes."""
        fixes: Dict[str, Any] = {}
        if "late_entry" in causes:
            fixes["late_entry"] = "إضافة شرط: عدم الدخول بعد ارتفاع >15% خلال 5 دقائق"
        if "fake_breakout" in causes:
            fixes["fake_breakout"] = "زيادة فترة الانتظار لتأكيد الاختراق (3 شموع إغلاق)"
        if "weak_liquidity" in causes:
            fixes["weak_liquidity"] = "رفع الحد الأدنى للسيولة من 50k إلى 100k"
        if "bad_stop_loss" in causes:
            fixes["bad_stop_loss"] = "استخدام ATR-based stop loss بدلاً من نسب ثابتة"
        if "model_overestimate" in causes:
            fixes["model_overestimate"] = "إعادة معايرة النموذج على بيانات أحدث"
        if "bad_market_regime" in causes:
            fixes["bad_market_regime"] = "تشديد قيود ظروف السوق المسموح بها"
        if "high_volatility" in causes:
            fixes["high_volatility"] = "رفع حد التقلب الأقصى المسموح من 10% إلى 7%"
        if "suspicious_activity" in causes:
            fixes["suspicious_activity"] = "إضافة فلاتر إضافية لـ wash trading و bundling"
        return fixes

    async def find_similar_mistakes(
        self,
        conditions: Dict[str, Any],
        max_results: int = 5,
    ) -> List[Mistake]:
        """
        Find past mistakes with similar conditions.
        Used to PREVENT repeating mistakes before opening a new trade.
        """
        async with get_session() as s:
            stmt = (
                select(Mistake)
                .where(Mistake.active_block == True)
                .order_by(desc(Mistake.last_occurred_at))
                .limit(50)
            )
            result = await s.execute(stmt)
            all_mistakes = list(result.scalars().all())

        # Score similarity (simple matching)
        scored: List[Tuple[float, Mistake]] = []
        for m in all_mistakes:
            sim = self._similarity_score(conditions, m.conditions_json or {})
            if sim > 0.3:
                scored.append((sim, m))
        scored.sort(reverse=True, key=lambda x: x[0])
        return [m for _, m in scored[:max_results]]

    def _similarity_score(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        """Simple similarity: count matching keys."""
        if not a or not b:
            return 0.0
        matches = 0
        total = 0
        for key in ["market_regime", "causes"]:
            if key in a and key in b:
                total += 1
                av = a[key]
                bv = b[key]
                if isinstance(av, list) and isinstance(bv, list):
                    if set(av) & set(bv):
                        matches += 1
                elif av == bv:
                    matches += 1
        # Score range overlap
        for key in ["technical_score", "liquidity_score", "risk_score"]:
            if key in a and key in b:
                total += 1
                try:
                    av = float(a[key])
                    bv = float(b[key])
                    if abs(av - bv) < 20:
                        matches += 1
                except (TypeError, ValueError):
                    pass
        return matches / max(total, 1)

    async def increment_occurrence(self, mistake_id: str) -> None:
        async with get_session() as s:
            m = await s.get(Mistake, mistake_id)
            if m:
                m.occurrence_count += 1
                m.last_occurred_at = datetime.utcnow()


# =========================================================================
# Continuous improvement
# =========================================================================
class ContinuousImprover:
    """
    Analyzes batches of trades to discover patterns and propose strategy improvements.
    IMPORTANT: All proposals are TESTED before being adopted.
    """

    async def analyze_recent_trades(self, lookback_days: int = 7) -> Dict[str, Any]:
        """Discover patterns from recent closed trades."""
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        async with get_session() as s:
            stmt = select(Trade).where(
                and_(Trade.status == "closed", Trade.exit_timestamp >= cutoff)
            )
            result = await s.execute(stmt)
            trades = list(result.scalars().all())

        if len(trades) < 10:
            return {
                "status": "insufficient_data",
                "trades_analyzed": len(trades),
                "message": "عدد الصفقات غير كافٍ لتحليل الأنماط (الحد الأدنى 10)",
            }

        wins = [t for t in trades if (t.net_pnl_usd or 0) > 0]
        losses = [t for t in trades if (t.net_pnl_usd or 0) <= 0]
        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(t.net_pnl_usd or 0 for t in wins) / max(len(wins), 1)
        avg_loss = sum(t.net_pnl_usd or 0 for t in losses) / max(len(losses), 1)
        profit_factor = abs(sum(t.net_pnl_usd or 0 for t in wins)) / max(abs(sum(t.net_pnl_usd or 0 for t in losses)), 1)

        # Pattern discovery: which exit reasons work best?
        exit_reasons: Dict[str, Dict] = {}
        for t in trades:
            reason = t.exit_reason or "unknown"
            if reason not in exit_reasons:
                exit_reasons[reason] = {"count": 0, "wins": 0, "total_pnl": 0}
            exit_reasons[reason]["count"] += 1
            if (t.net_pnl_usd or 0) > 0:
                exit_reasons[reason]["wins"] += 1
            exit_reasons[reason]["total_pnl"] += t.net_pnl_usd or 0

        # Strategy performance
        strategy_perf: Dict[str, Dict] = {}
        for t in trades:
            sid = t.strategy_id or "default"
            if sid not in strategy_perf:
                strategy_perf[sid] = {"count": 0, "wins": 0, "total_pnl": 0}
            strategy_perf[sid]["count"] += 1
            if (t.net_pnl_usd or 0) > 0:
                strategy_perf[sid]["wins"] += 1
            strategy_perf[sid]["total_pnl"] += t.net_pnl_usd or 0

        return {
            "status": "ok",
            "trades_analyzed": len(trades),
            "win_rate_pct": win_rate,
            "avg_win_usd": avg_win,
            "avg_loss_usd": avg_loss,
            "profit_factor": profit_factor,
            "exit_reason_breakdown": exit_reasons,
            "strategy_performance": strategy_perf,
            "recommendations": self._recommend(exit_reasons, strategy_perf, win_rate),
        }

    def _recommend(
        self,
        exit_reasons: Dict[str, Dict],
        strategy_perf: Dict[str, Dict],
        win_rate: float,
    ) -> List[str]:
        recs: List[str] = []
        # If win rate is below 50%, suggest pausing
        if win_rate < 50:
            recs.append(
                "Win Rate أقل من 50% - يُنصح بمراجعة شروط الدخول وإيقاف الاستراتيجيات الضعيفة"
            )

        # Find underperforming exit reasons
        for reason, stats in exit_reasons.items():
            if stats["count"] >= 5:
                wr = stats["wins"] / stats["count"] * 100
                if wr < 30:
                    recs.append(f"خروج '{reason}' لديه Win Rate منخفض ({wr:.0f}%) - مراجعة القاعدة")

        # Strategy comparison
        if len(strategy_perf) > 1:
            sorted_strats = sorted(
                strategy_perf.items(),
                key=lambda x: x[1]["total_pnl"],
                reverse=True,
            )
            best, worst = sorted_strats[0], sorted_strats[-1]
            if best[1]["total_pnl"] > 0 and worst[1]["total_pnl"] < 0:
                recs.append(
                    f"الاستراتيجية '{best[0]}' تتفوق - يُنصح بزيادة وزنها. "
                    f"الاستراتيجية '{worst[0]}' ضعيفة - مراجعة أو إيقاف."
                )

        if not recs:
            recs.append("الأداء ضمن الحدود المقبولة - لا تغييرات ضرورية حالياً")

        return recs


# =========================================================================
# Anti-repetition: check new opportunity against past mistakes
# =========================================================================
class AntiRepetitionChecker:
    """Called BEFORE opening a new trade to check for repeat-mistake conditions."""

    def __init__(self):
        self.memory = MistakeMemory()

    async def check(self, decision_result, snapshot: Optional[AnalysisSnapshot]) -> Dict[str, Any]:
        """
        Returns:
        - blocked: True if we should block the trade
        - reason: explanation
        - matched_mistakes: list of past mistakes that match
        """
        if not snapshot:
            return {"blocked": False, "reason": "", "matched_mistakes": []}

        conditions = {
            "market_regime": snapshot.market_regime,
            "technical_score": snapshot.technical_score,
            "liquidity_score": snapshot.liquidity_score,
            "risk_score": snapshot.risk_score,
            "causes": [],  # we're checking pre-trade, so no causes yet
        }

        # Add indicators that match common mistake triggers
        indicators = snapshot.indicators_json or {}
        tech = indicators.get("technical", {})
        pa = tech.get("price_action", {})
        if pa.get("fake_breakout_up"):
            conditions["causes"].append("fake_breakout")
        if tech.get("volatility_pct", 0) > 15:
            conditions["causes"].append("high_volatility")
        if snapshot.liquidity_score < 50:
            conditions["causes"].append("weak_liquidity")

        similar = await self.memory.find_similar_mistakes(conditions, max_results=3)
        if similar:
            # Block if any recent (last 7 days) similar mistake
            recent = [
                m for m in similar
                if (datetime.utcnow() - m.last_occurred_at).days <= 7
            ]
            if recent:
                return {
                    "blocked": True,
                    "reason": (
                        f"ظروف مشابهة لأخطاء سابقة: "
                        f"{', '.join(m.mistake_type for m in recent)}"
                    ),
                    "matched_mistakes": [m.id for m in recent],
                }

        return {"blocked": False, "reason": "", "matched_mistakes": [m.id for m in similar]}
