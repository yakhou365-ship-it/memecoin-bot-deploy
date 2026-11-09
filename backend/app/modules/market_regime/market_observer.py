"""
V533 — Market Observer & Learning System
يراقب السوق، يتعلم من كل عملة، ويحفظ ما تعلمه في ملف.

الميزات:
1. يراقب كل عملة جديدة على pump.fun
2. يسجل: السعر الأولي، الذروة، النهاية، المدة، الحجم، السيولة، المحافظ
3. يحلل الأنماط: ما الذي يجعل العملة تنجح؟ ما الذي يجعلها تفشل؟
4. يحفظ كل ما تعلمه في ملف JSON في data/learning/
5. كل يوم يولّد تقرير ملخص
6. يمكن استخراج رؤى من البيانات لتطوير البوت لاحقاً
"""
from __future__ import annotations

import os, sys, json, asyncio, statistics
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
from pathlib import Path

from sqlalchemy import select, desc, func

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, Trade, SystemState


@dataclass
class TokenObservation:
    """ملاحظة على عملة واحدة."""
    address: str
    symbol: str
    first_seen: str
    last_seen: str
    initial_price: float
    peak_price: float
    final_price: float
    peak_gain_pct: float
    final_gain_pct: float
    duration_minutes: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_5m_usd: float
    txns_buys: int
    txns_sells: int
    top_holder_pct: float
    holders_over_30: int
    chg_5m: float
    chg_1h: float
    chg_24h: float
    dex: str
    fate: str  # rug | fail | lateral | graduate | moonshot | unknown
    learned_patterns: List[str] = field(default_factory=list)


@dataclass
class DailyLearningReport:
    """تقرير تعلم يومي."""
    date: str
    tokens_observed: int
    rugs_detected: int
    moonshots_detected: int
    graduates_detected: int
    avg_liquidity: float
    avg_peak_gain: float
    avg_duration_minutes: float
    best_performers: List[Dict]
    worst_performers: List[Dict]
    patterns_learned: List[str]
    market_regime: str
    recommendations: List[str]


class MarketObserver:
    """يراقب السوق ويتعلم من كل عملة."""

    LEARNING_KEY = "market_learning_data"
    DAILY_REPORT_KEY = "daily_learning_report"

    async def observe_and_learn(self) -> Dict:
        """يراقب كل التوكنات في الـ DB، يحللها، ويحفظ ما تعلمه."""
        observations = []
        learnings = []
        patterns = []

        try:
            async with get_session() as s:
                # جلب كل التوكنات التي شوهدت في آخر 24h
                cutoff = datetime.utcnow() - timedelta(hours=24)
                stmt = select(Token).where(
                    Token.chain == "solana",
                    Token.last_seen_at >= cutoff,
                ).order_by(desc(Token.last_seen_at)).limit(500)
                tokens = (await s.execute(stmt)).scalars().all()

                for token in tokens:
                    obs = self._analyze_token(token)
                    if obs:
                        observations.append(obs)
                        # تعلم الأنماط
                        learned = self._extract_patterns(obs)
                        learnings.extend(learned)
                        patterns.extend([p for p in learned if "pattern" in p.lower()])

            if not observations:
                return {"status": "no_tokens", "learned": 0}

            # تحليل إجمالي
            analysis = self._analyze_batch(observations)

            # حفظ في الـ DB
            await self._save_learning(observations, analysis)

            # حفظ في ملف (لـ GitHub)
            self._save_to_file(observations, analysis)

            # إنشاء تقرير يومي
            report = self._generate_daily_report(observations, analysis)

            logger.info(
                f"📊 MarketObserver: observed {len(observations)} tokens, "
                f"learned {len(learnings)} patterns, regime={analysis['market_regime']}"
            )

            return {
                "status": "ok",
                "tokens_observed": len(observations),
                "patterns_learned": len(learned),
                "market_regime": analysis["market_regime"],
                "report": asdict(report) if report else None,
            }

        except Exception as exc:
            logger.error(f"MarketObserver failed: {exc}")
            return {"status": "error", "error": str(exc)[:200]}

    def _analyze_token(self, token: Token) -> Optional[TokenObservation]:
        """حلل عملة واحدة وحدد مصيرها."""
        try:
            initial_price = float(token.price_usd or 0)
            if initial_price <= 0:
                return None

            # حساب الذروة والنهاية
            peak_price = float(getattr(token, "max_unrealized_profit_usd", 0) or initial_price)
            if peak_price <= 0:
                peak_price = initial_price

            # تحديد المصير
            chg_24h = float(getattr(token, "price_change_24h_pct", 0) or 0)
            chg_5m = float(getattr(token, "price_change_5m_pct", 0) or 0)
            top_holder = float(getattr(token, "top_holder_pct", 0) or 0)
            liquidity = float(token.liquidity_usd or 0)

            # تصنيف المصير
            if top_holder >= 30:
                fate = "rug"
            elif chg_24h < -50:
                fate = "fail"
            elif chg_24h > 500:
                fate = "moonshot"
            elif chg_24h > 50:
                fate = "graduate"
            elif abs(chg_24h) < 20:
                fate = "lateral"
            else:
                fate = "unknown"

            # المدة
            first_seen = getattr(token, "first_seen_at", None)
            last_seen = getattr(token, "last_seen_at", None)
            duration_min = 0
            if first_seen and last_seen:
                if isinstance(first_seen, str):
                    first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                if isinstance(last_seen, str):
                    last_seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                duration_min = (last_seen.replace(tzinfo=None) - first_seen.replace(tzinfo=None)).total_seconds() / 60

            return TokenObservation(
                address=token.address,
                symbol=token.symbol or "UNKNOWN",
                first_seen=str(first_seen) if first_seen else "",
                last_seen=str(last_seen) if last_seen else "",
                initial_price=initial_price,
                peak_price=peak_price,
                final_price=initial_price,
                peak_gain_pct=((peak_price / initial_price - 1) * 100) if initial_price > 0 else 0,
                final_gain_pct=chg_24h,
                duration_minutes=duration_min,
                liquidity_usd=liquidity,
                volume_24h_usd=float(token.volume_24h_usd or 0),
                volume_5m_usd=float(getattr(token, "volume_5m_usd", 0) or 0),
                txns_buys=int(getattr(token, "txns_24h_buys", 0) or 0),
                txns_sells=int(getattr(token, "txns_24h_sells", 0) or 0),
                top_holder_pct=top_holder,
                holders_over_30=1 if top_holder >= 30 else 0,
                chg_5m=chg_5m,
                chg_1h=float(getattr(token, "price_change_1h_pct", 0) or 0),
                chg_24h=chg_24h,
                dex=token.dex or "unknown",
                fate=fate,
            )
        except Exception:
            return None

    def _extract_patterns(self, obs: TokenObservation) -> List[str]:
        """استخرج الأنماط المتعلمة من ملاحظة واحدة."""
        patterns = []

        # نمط 1: العملات الناجحة لها سيولة عالية
        if obs.fate in ("graduate", "moonshot") and obs.liquidity_usd > 20000:
            patterns.append(f"pattern_success_high_liquidity: {obs.symbol} liq=${obs.liquidity_usd:.0f}")

        # نمط 2: العملات الفاشلة لها محفظة كبيرة
        if obs.fate == "rug" and obs.top_holder_pct >= 30:
            patterns.append(f"pattern_rug_big_holder: {obs.symbol} holder={obs.top_holder_pct:.1f}%")

        # نمط 3: pump سريع = dump سريع
        if obs.chg_5m > 100 and obs.fate == "rug":
            patterns.append(f"pattern_fast_pump_fast_dump: {obs.symbol} chg_5m={obs.chg_5m:.0f}%")

        # نمط 4: عملات مع LP locked تنجح أكثر
        if obs.fate in ("graduate", "moonshot") and obs.dex and "pumpswap" in str(obs.dex).lower():
            patterns.append(f"pattern_graduated_success: {obs.symbol} dex={obs.dex}")

        # نمط 5: حجم تداول عالي = اهتمام حقيقي
        if obs.volume_24h_usd > 100000 and obs.fate in ("graduate", "moonshot"):
            patterns.append(f"pattern_high_volume_success: {obs.symbol} vol=${obs.volume_24h_usd:.0f}")

        # نمط 6: buy/sell ratio
        if obs.txns_buys > 0 and obs.txns_sells > 0:
            ratio = obs.txns_buys / max(obs.txns_sells, 1)
            if ratio > 3 and obs.fate in ("graduate", "moonshot"):
                patterns.append(f"pattern_high_buy_ratio_success: {obs.symbol} ratio={ratio:.1f}")
            elif ratio < 0.5 and obs.fate == "rug":
                patterns.append(f"pattern_low_buy_ratio_rug: {obs.symbol} ratio={ratio:.1f}")

        return patterns

    def _analyze_batch(self, observations: List[TokenObservation]) -> Dict:
        """حلل مجموعة الملاحظات واستخرج رؤى."""
        if not observations:
            return {"market_regime": "unknown", "patterns": []}

        total = len(observations)
        rugs = [o for o in observations if o.fate == "rug"]
        fails = [o for o in observations if o.fate == "fail"]
        laterals = [o for o in observations if o.fate == "lateral"]
        graduates = [o for o in observations if o.fate == "graduate"]
        moonshots = [o for o in observations if o.fate == "moonshot"]

        rug_rate = len(rugs) / total * 100
        moon_rate = len(moonshots) / total * 100
        grad_rate = len(graduates) / total * 100

        avg_liq = statistics.mean([o.liquidity_usd for o in observations if o.liquidity_usd > 0]) if any(o.liquidity_usd > 0 for o in observations) else 0
        avg_peak = statistics.mean([o.peak_gain_pct for o in observations if o.peak_gain_pct != 0]) if any(o.peak_gain_pct != 0 for o in observations) else 0

        # تصنيف السوق
        if rug_rate > 50 and moon_rate < 1:
            regime = "brutal"
        elif rug_rate > 30:
            regime = "poor"
        elif rug_rate > 15:
            regime = "average"
        else:
            regime = "good"

        # أنماط شاملة
        all_patterns = []
        # العملات الناجحة لها سيولة أعلى من الفاشلة؟
        success_liq = [o.liquidity_usd for o in graduates + moonshots if o.liquidity_usd > 0]
        fail_liq = [o.liquidity_usd for o in rugs + fails if o.liquidity_usd > 0]
        if success_liq and fail_liq:
            avg_success_liq = statistics.mean(success_liq)
            avg_fail_liq = statistics.mean(fail_liq)
            if avg_success_liq > avg_fail_liq * 1.5:
                all_patterns.append(f"insight: العملات الناجحة لها سيولة أعلى (${avg_success_liq:.0f} vs ${avg_fail_liq:.0f})")

        # العملات الناجحة لها محافظ أصغر؟
        success_holders = [o.top_holder_pct for o in graduates + moonshots if o.top_holder_pct > 0]
        fail_holders = [o.top_holder_pct for o in rugs + fails if o.top_holder_pct > 0]
        if success_holders and fail_holders:
            avg_success_holder = statistics.mean(success_holders)
            avg_fail_holder = statistics.mean(fail_holders)
            if avg_success_holder < avg_fail_holder:
                all_patterns.append(f"insight: العملات الناجحة لها محافظ أصغر ({avg_success_holder:.1f}% vs {avg_fail_holder:.1f}%)")

        # حجم التداول
        success_vol = [o.volume_24h_usd for o in graduates + moonshots if o.volume_24h_usd > 0]
        fail_vol = [o.volume_24h_usd for o in rugs + fails if o.volume_24h_usd > 0]
        if success_vol and fail_vol:
            avg_success_vol = statistics.mean(success_vol)
            avg_fail_vol = statistics.mean(fail_vol)
            if avg_success_vol > avg_fail_vol:
                all_patterns.append(f"insight: العملات الناجحة لها حجم تداول أعلى (${avg_success_vol:.0f} vs ${avg_fail_vol:.0f})")

        return {
            "market_regime": regime,
            "rug_rate": rug_rate,
            "moonshot_rate": moon_rate,
            "graduation_rate": grad_rate,
            "avg_liquidity": avg_liq,
            "avg_peak_gain": avg_peak,
            "total_tokens": total,
            "rugs": len(rugs),
            "fails": len(fails),
            "laterals": len(laterals),
            "graduates": len(graduates),
            "moonshots": len(moonshots),
            "patterns": all_patterns,
        }

    def _generate_daily_report(self, observations: List[TokenObservation], analysis: Dict) -> DailyLearningReport:
        """أنشئ تقرير يومي."""
        # أفضل وأسوأ العملات
        sorted_obs = sorted(observations, key=lambda o: o.peak_gain_pct, reverse=True)
        best = sorted_obs[:5]
        worst = sorted_obs[-5:]

        recommendations = []
        if analysis["rug_rate"] > 50:
            recommendations.append("السوق قاسي جداً — استخدم SURVIVOR (حماية رأس المال)")
        elif analysis["rug_rate"] > 30:
            recommendations.append("السوق سيء — استخدم RUG-SURFER (اركب الـ pumps القصيرة)")
        else:
            recommendations.append("السوق جيد — استخدم ADAPTIVE-PRO (استغل كل فرصة)")

        for p in analysis.get("patterns", []):
            recommendations.append(f"تعلم: {p}")

        return DailyLearningReport(
            date=datetime.utcnow().strftime("%Y-%m-%d"),
            tokens_observed=len(observations),
            rugs_detected=analysis.get("rugs", 0),
            moonshots_detected=analysis.get("moonshots", 0),
            graduates_detected=analysis.get("graduates", 0),
            avg_liquidity=analysis.get("avg_liquidity", 0),
            avg_peak_gain=analysis.get("avg_peak_gain", 0),
            avg_duration_minutes=statistics.mean([o.duration_minutes for o in observations if o.duration_minutes > 0]) if any(o.duration_minutes > 0 for o in observations) else 0,
            best_performers=[{"symbol": o.symbol, "peak_gain": o.peak_gain_pct, "liq": o.liquidity_usd, "fate": o.fate} for o in best],
            worst_performers=[{"symbol": o.symbol, "peak_gain": o.peak_gain_pct, "liq": o.liquidity_usd, "fate": o.fate} for o in worst],
            patterns_learned=analysis.get("patterns", []),
            market_regime=analysis.get("market_regime", "unknown"),
            recommendations=recommendations,
        )

    async def _save_learning(self, observations: List[TokenObservation], analysis: Dict):
        """احفظ في الـ DB."""
        from sqlalchemy.orm.attributes import flag_modified
        try:
            async with get_session() as s:
                row = await s.get(SystemState, self.LEARNING_KEY)
                data = {
                    "last_updated": datetime.utcnow().isoformat(),
                    "analysis": analysis,
                    "observations_count": len(observations),
                }
                if row is None:
                    row = SystemState(key=self.LEARNING_KEY, value_json=data)
                    s.add(row)
                else:
                    existing = dict(row.value_json or {})
                    existing.update(data)
                    # احتفظ بآخر 30 يوم من التحليلات
                    history = existing.get("history", [])
                    history.append({
                        "date": data["last_updated"],
                        "analysis": analysis,
                    })
                    existing["history"] = history[-30:]  # آخر 30 يوم
                    row.value_json = existing
                    flag_modified(row, "value_json")
        except Exception as exc:
            logger.warning(f"Failed to save learning to DB: {exc}")

    def _save_to_file(self, observations: List[TokenObservation], analysis: Dict):
        """احفظ في ملف في GitHub."""
        try:
            # مسار مجلد التعلم
            learning_dir = Path(settings.__dict__.get("data_dir", "data")) / "learning"
            learning_dir.mkdir(parents=True, exist_ok=True)

            # ملف اليوم
            today = datetime.utcnow().strftime("%Y-%m-%d")
            daily_file = learning_dir / f"learning_{today}.json"

            data = {
                "date": today,
                "generated_at": datetime.utcnow().isoformat(),
                "analysis": analysis,
                "observations": [asdict(o) for o in observations],
            }
            with open(daily_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)

            # ملف ملخص تراكمي
            summary_file = learning_dir / "learning_summary.json"
            summary = {
                "last_updated": datetime.utcnow().isoformat(),
                "total_days_observed": 0,
                "total_tokens_observed": 0,
                "latest_analysis": analysis,
                "history": [],
            }
            # اقرأ الملخص الموجود
            if summary_file.exists():
                with open(summary_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                summary["total_days_observed"] = existing.get("total_days_observed", 0) + 1
                summary["total_tokens_observed"] = existing.get("total_tokens_observed", 0) + len(observations)
                summary["history"] = existing.get("history", [])
            else:
                summary["total_days_observed"] = 1
                summary["total_tokens_observed"] = len(observations)

            summary["history"].append({
                "date": today,
                "tokens": len(observations),
                "regime": analysis.get("market_regime"),
                "rug_rate": analysis.get("rug_rate"),
                "moonshot_rate": analysis.get("moonshot_rate"),
                "patterns": analysis.get("patterns", []),
            })
            # احتفظ بآخر 90 يوم
            summary["history"] = summary["history"][-90:]

            with open(summary_file, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str, ensure_ascii=False)

        except Exception as exc:
            logger.warning(f"Failed to save learning to file: {exc}")

    async def get_learning_summary(self) -> Dict:
        """استرجع ملخص ما تعلمه البوت."""
        try:
            summary_file = Path("data/learning/learning_summary.json")
            if summary_file.exists():
                with open(summary_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {"status": "no_learning_yet"}
        except Exception:
            return {"status": "error"}
