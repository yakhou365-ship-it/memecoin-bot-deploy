"""
AI Research & Reasoning layer.

Translates raw analysis outputs into human-readable insights.
Strictly separates: confirmed facts / probable inferences / hypotheses / missing data.

Does NOT use an LLM by default (to avoid hallucination). Instead, it uses
rule-based natural language generation from the analysis result fields.

Optional: can be extended to call an external LLM with strict prompts
that forbid fabrication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...core.models import AnalysisSnapshot, Trade


@dataclass
class ResearchReport:
    confirmed_facts: List[str] = field(default_factory=list)
    probable_inferences: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    recommendation: str = ""
    confidence_level: str = "low"  # low | medium | high
    summary_ar: str = ""


class AIResearcher:
    """Generates research reports from analysis snapshots."""

    def generate_token_report(
        self,
        token,
        snapshot: Optional[AnalysisSnapshot] = None,
        decision_result: Optional[Any] = None,
    ) -> ResearchReport:
        report = ResearchReport()

        # === Confirmed facts (from market data) ===
        if token.price_usd:
            report.confirmed_facts.append(f"السعر الحالي: ${token.price_usd:.8f}")
        if token.liquidity_usd:
            report.confirmed_facts.append(f"السيولة: ${token.liquidity_usd:,.0f}")
        if token.volume_24h_usd:
            report.confirmed_facts.append(f"حجم التداول 24h: ${token.volume_24h_usd:,.0f}")
        if token.price_change_24h_pct is not None:
            direction = "ارتفاع" if token.price_change_24h_pct > 0 else "انخفاض"
            report.confirmed_facts.append(
                f"التغير خلال 24h: {direction} {abs(token.price_change_24h_pct):.1f}%"
            )
        if token.txns_24h_buys and token.txns_24h_sells:
            report.confirmed_facts.append(
                f"صفقات شراء 24h: {token.txns_24h_buys} | صفقات بيع: {token.txns_24h_sells}"
            )

        # === Probable inferences (from analysis) ===
        if snapshot:
            if snapshot.technical_score > 70:
                report.probable_inferences.append("المؤشرات الفنية تشير إلى فرصة شراء جيدة")
            elif snapshot.technical_score < 40:
                report.probable_inferences.append("المؤشرات الفنية تشير إلى ضعف")

            if snapshot.security_score > 70:
                report.probable_inferences.append("العملة تبدو آمنة نسبياً")
            elif snapshot.security_score < 40:
                report.probable_inferences.append("هناك مخاوف أمنية ملحوظة")

            if snapshot.market_regime:
                report.probable_inferences.append(
                    f"حالة السوق الحالية: {snapshot.market_regime}"
                )

        # === Hypotheses (need testing) ===
        if snapshot and snapshot.win_probability > 0.7:
            report.hypotheses.append(
                "احتمال النجاح يتجاوز 70% - يحتاج التحقق عبر样本 أكبر"
            )
        if decision_result and decision_result.technical_detail:
            pa = decision_result.technical_detail.get("price_action", {})
            if pa.get("trend_reversal"):
                report.hypotheses.append("قد يكون هناك انعكاس اتجاه - يحتاج تأكيد")

        # === Missing data ===
        if snapshot and not snapshot.data_sufficient:
            report.missing_data.append("البيانات غير كافية لتحليل موثوق")
        if snapshot and snapshot.data_age_seconds > 120:
            report.missing_data.append(f"البيانات عمرها {snapshot.data_age_seconds}s - تحتاج تحديث")
        if snapshot and snapshot.sentiment_score == 50:
            report.missing_data.append("البيانات الاجتماعية غير متوفرة - التحليل مبني على السوق فقط")
        if not token.pair_address:
            report.missing_data.append("عنوان زوج التداول غير معروف - لا يمكن جلب بيانات OHLCV")

        # === Confidence level ===
        if snapshot:
            if snapshot.data_sufficient and snapshot.win_probability_method == "ml_calibrated":
                report.confidence_level = "high"
            elif snapshot.data_sufficient:
                report.confidence_level = "medium"
            else:
                report.confidence_level = "low"

        # === Recommendation ===
        if decision_result:
            if decision_result.recommendation == "buy":
                report.recommendation = (
                    f"يُنصح بالشراء الافتراضي بحجم {decision_result.suggested_position_size_pct:.2f}% "
                    f"من رأس المال، Stop Loss عند ${decision_result.suggested_stop_loss:.8f}, "
                    f"Take Profit عند ${decision_result.suggested_take_profit:.8f}"
                )
            elif decision_result.recommendation == "wait":
                report.recommendation = "يُنصح بالانتظار حتى تتحسن الظروف"
            elif decision_result.recommendation == "reject":
                report.recommendation = f"يُرفض التداول: {decision_result.rejection_reason}"
            else:
                report.recommendation = "يُنصح بالمراقبة دون فتح صفقة الآن"
        else:
            report.recommendation = "لا توصية - البيانات غير كافية"

        # === Arabic summary ===
        parts = [
            f"📊 تقرير تحليل العملة: {token.symbol} ({token.chain})",
            "",
            "✅ حقائق مؤكدة:",
            *[f"  • {f}" for f in report.confirmed_facts],
            "",
            "🤔 استنتاجات محتملة:",
            *[f"  • {i}" for i in report.probable_inferences],
        ]
        if report.hypotheses:
            parts.extend(["", "🔍 فرضيات تحتاج اختبار:", *[f"  • {h}" for h in report.hypotheses]])
        if report.missing_data:
            parts.extend(["", "⚠️ بيانات غير متوفرة:", *[f"  • {m}" for m in report.missing_data]])
        parts.extend(["", f"🎯 التوصية: {report.recommendation}", f"📊 مستوى الثقة: {report.confidence_level}"])
        report.summary_ar = "\n".join(parts)

        return report

    def generate_trade_review(self, trade: Trade) -> str:
        """Generate a post-trade review in Arabic."""
        parts = [
            f"📝 مراجعة الصفقة",
            f"العملة: {trade.token_id}",
            f"سعر الدخول: ${trade.entry_price:.8f}",
            f"سعر الخروج: ${trade.exit_price:.8f}" if trade.exit_price else "الصفقة لا تزال مفتوحة",
            f"الحجم: ${trade.entry_size_usd:.2f}",
        ]
        if trade.net_pnl_usd is not None:
            result_ar = "ربح" if trade.net_pnl_usd > 0 else "خسارة"
            parts.append(f"النتيجة: {result_ar} ${abs(trade.net_pnl_usd):.2f} ({trade.pnl_pct:.2f}%)")
        if trade.exit_reason:
            reason_ar = {
                "take_profit": "تحقيق الربح المستهدف",
                "stop_loss": "تفعيل وقف الخسارة",
                "time_exit": "انتهاء المدة الزمنية",
                "circuit_breaker": "تفعيل قاطع الدائرة",
                "manual": "إغلاق يدوي",
            }.get(trade.exit_reason, trade.exit_reason)
            parts.append(f"سبب الإغلاق: {reason_ar}")
        parts.append(f"الرسوم: ${trade.fees_total_usd:.2f}")
        parts.append(f"الانزلاق السعري: ${trade.slippage_total_usd:.2f}")
        if trade.max_unrealized_profit_usd:
            parts.append(f"أعلى ربح غير محقق: ${trade.max_unrealized_profit_usd:.2f}")
        if trade.max_unrealized_loss_usd:
            parts.append(f"أعلى خسارة غير محققة: ${trade.max_unrealized_loss_usd:.2f}")
        return "\n".join(parts)
