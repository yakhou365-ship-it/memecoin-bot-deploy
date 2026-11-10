"""
V529 — SMART PROFILE SWITCHER
يبدل الأنماط تلقائياً حسب حالة السوق + الأداء + العملات المكتشفة.

الفلسفة:
- سوق قاسي + 0 moonshots → SURVIVOR (احمي رأس المال)
- سوق قاسي + moonshots موجودة → RUG-SURFER (اركب الـ pumps القصيرة)
- سوق متوسط → RUG-SURFER (يوازن بين الحماية والربح)
- سوق جيد → ULTIMATE (استغل كل فرصة)
- سوق صاعد → ULTIMATE (أقصى ربح)
- سوق هابط/ذعر → CONSERVATIVE (توقف عن التداول)

يحلل:
1. نسبة التخرج (graduation rate) من آخر 100 توكن
2. نسبة الـ moonshots (+500%) من آخر 100 توكن
3. متوسط السيولة للتوكنات النشطة
4. أداء آخر 10 صفقات (win rate)
5. حالة SOL (bull/bear/sideways)
6. صافي PnL آخر 24h
"""
from __future__ import annotations

import os, sys, statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy import select, func, desc

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROOT)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))


@dataclass
class MarketState:
    """حالة السوق الحالية."""
    regime: str = "unknown"  # brutal | poor | average | good | bull | panic
    graduation_rate_pct: float = 0.0     # نسبة التوكنات التي تخرجت
    moonshot_rate_pct: float = 0.0      # نسبة الـ +500%
    avg_liquidity_usd: float = 0.0
    active_tokens_count: int = 0
    recent_win_rate_pct: float = 0.0     # أداء آخر 10 صفقات
    recent_pnl_24h_usd: float = 0.0
    sol_trend: str = "unknown"          # up | down | sideways
    recommended_profile: str = "survivor"
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)


class SmartProfileSwitcher:
    """يبدل الأنماط تلقائياً حسب حالة السوق + يتعامل مع الصفقات المفتوحة."""

    async def detect_market_state(self) -> MarketState:
        """اكتشف حالة السوق الحالية من الـ DB."""
        from app.core.database import get_session
        from app.core.models import Token, Trade

        state = MarketState()

        try:
            async with get_session() as s:
                # 1. آخر 100 توكن (آخر 24h)
                cutoff_24h = datetime.utcnow() - timedelta(hours=24)
                stmt_tokens = select(Token).where(
                    Token.chain == "solana",
                    Token.first_seen_at >= cutoff_24h,
                ).order_by(desc(Token.first_seen_at)).limit(100)
                recent_tokens = (await s.execute(stmt_tokens)).scalars().all()

                if recent_tokens:
                    state.active_tokens_count = len(recent_tokens)
                    # متوسط السيولة
                    liquidities = [t.liquidity_usd or 0 for t in recent_tokens if t.liquidity_usd]
                    if liquidities:
                        state.avg_liquidity_usd = statistics.mean(liquidities)
                    # نسبة التخرج (graduated tokens)
                    graduated = [t for t in recent_tokens if t.dex and "pump" in str(t.dex).lower() and "swap" in str(t.dex).lower()]
                    state.graduation_rate_pct = len(graduated) / len(recent_tokens) * 100
                    # نسبة الـ moonshots (+500% في 24h)
                    moonshots = [t for t in recent_tokens if (t.price_change_24h_pct or 0) > 500]
                    state.moonshot_rate_pct = len(moonshots) / len(recent_tokens) * 100

                # 2. أداء آخر 10 صفقات
                stmt_trades = select(Trade).where(
                    Trade.status == "closed",
                ).order_by(desc(Trade.exit_timestamp)).limit(10)
                recent_trades = (await s.execute(stmt_trades)).scalars().all()
                if recent_trades:
                    wins = [t for t in recent_trades if (t.net_pnl_usd or 0) > 0]
                    state.recent_win_rate_pct = len(wins) / len(recent_trades) * 100

                # 3. صافي PnL آخر 24h
                cutoff_24h_pnl = datetime.utcnow() - timedelta(hours=24)
                stmt_pnl = select(func.sum(Trade.net_pnl_usd)).where(
                    Trade.status == "closed",
                    Trade.exit_timestamp >= cutoff_24h_pnl,
                )
                pnl_result = (await s.execute(stmt_pnl)).scalar()
                state.recent_pnl_24h_usd = float(pnl_result or 0)

        except Exception as exc:
            state.reasons.append(f"تحليل السوق فشل: {exc}")

        # تحديد النمط الموصى به
        self._classify_market(state)
        return state

    async def auto_switch_if_needed(self) -> Dict:
        """V529: تبديل تلقائي للنمط + تحديث الصفقات المفتوحة.

        1. اكتشف حالة السوق
        2. لو النمط الموصى به ≠ النمط الحالي → بدّل
        3. حدّث SL/TP للصفقات المفتوحة لتطابق النمط الجديد
        """
        from app.core.config import settings
        from app.core.database import get_session
        from app.core.models import Trade, SystemState
        from sqlalchemy.orm.attributes import flag_modified

        state = await self.detect_market_state()
        recommended = state.recommended_profile
        current = settings.trading_profile

        result = {
            "switched": False,
            "old_profile": current,
            "new_profile": current,
            "open_positions_updated": 0,
            "reasons": state.reasons,
            "market_regime": state.regime,
        }

        if recommended != current:
            # بدّل النمط
            settings.trading_profile = recommended
            result["switched"] = True
            result["new_profile"] = recommended

            # احفظ في DB
            try:
                async with get_session() as s:
                    row = await s.get(SystemState, "trading_profile")
                    if row is None:
                        row = SystemState(key="trading_profile",
                                          value_json={"profile": recommended, "auto_switch": True})
                        s.add(row)
                    else:
                        row.value_json = {"profile": recommended, "auto_switch": True}
                        flag_modified(row, "value_json")
            except Exception:
                pass

            # V529: حدّث SL/TP للصفقات المفتوحة
            # V541: تحت الاستراتيجية الموحدة، الخروج يُدار بسُلّم موحد من سعر
            # الدخول مباشرة — لا معنى لإعادة كتابة SL/TP المخزّنة (البروفايل
            # أصبح مجرد قرص مخاطرة للحجم فقط).
            if not settings.unified_strategy_enabled:
                updated = await self._update_open_positions_for_new_profile(recommended)
                result["open_positions_updated"] = updated

        return result

    async def _update_open_positions_for_new_profile(self, new_profile: str) -> int:
        """V529: حدّث SL/TP للصفقات المفتوحة لتطابق النمط الجديد.

        عند تبديل النمط، الصفقات المفتوحة تحتاج تحديث:
        - stop_loss_price = entry * (1 - profile.sl_pct)
        - take_profit_price = entry * (1 + profile.tp_pct)
        """
        from app.core.config import settings
        from app.core.database import get_session
        from app.core.models import Trade

        # احصل على SL/TP للنمط الجديد
        if new_profile == "survivor":
            sl_pct = settings.survivor_stop_loss_pct
            tp_mult = 1.0 + settings.survivor_quick_take_profit_pct
        elif new_profile == "rugsurfer":
            sl_pct = settings.rugsurfer_stop_loss_pct
            tp_mult = 1.0 + settings.rugsurfer_quick_take_profit_pct
        elif new_profile == "ultimate":
            sl_pct = settings.ultimate_stop_loss_pct
            tp_mult = 1.0 + settings.ultimate_medium_take_profit_pct
        elif new_profile == "extreme":
            sl_pct = 0.08  # extreme default
            tp_mult = 11.0  # 10x cap
        elif new_profile == "conservative":
            sl_pct = 0.08
            tp_mult = 11.0
        else:
            sl_pct = 0.08
            tp_mult = 11.0

        updated = 0
        try:
            async with get_session() as s:
                open_trades = (await s.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id.startswith("live"),
                    )
                )).scalars().all()

                for trade in open_trades:
                    if trade.entry_price and trade.entry_price > 0:
                        new_sl = trade.entry_price * (1.0 - sl_pct)
                        new_tp = trade.entry_price * tp_mult
                        # حدّث فقط لو الجديد أكثر صرامة (أصعب SL أو أقرب TP)
                        # هذا يحمي الصفقة فوراً دون إفساد الربح المتراكم
                        if trade.stop_loss_price is None or new_sl > trade.stop_loss_price:
                            trade.stop_loss_price = new_sl
                        if trade.take_profit_price is None or new_tp < trade.take_profit_price:
                            trade.take_profit_price = new_tp
                        updated += 1
                await s.commit()
        except Exception as exc:
            pass

        return updated

    def _classify_market(self, state: MarketState) -> None:
        """صنف السوق واختر النمط المناسب.

        V529 HONEST: لا نوصي بـ EXTREME في السوق القاسي (يكذب المحاكاة).
        الواقع من V526 الصادقة: EXTREME يخسر -90% في سوق قاسي.
        SURVIVOR فقط يحمي رأس المال (-2.2%).
        """
        grad = state.graduation_rate_pct
        moon = state.moonshot_rate_pct
        wr = state.recent_win_rate_pct
        pnl_24h = state.recent_pnl_24h_usd

        # 1. سوق قاسي جداً (مثل Nov 2026): graduation < 2%, moonshots < 0.5%
        # V526 الصادقة: كل الأنماط تخسر هنا ما عدا SURVIVOR (-2.2%)
        if grad < 2.0 and moon < 0.5:
            state.regime = "brutal"
            state.confidence = 0.85
            state.recommended_profile = "survivor"
            state.reasons.append("سوق قاسي (98% scams) → SURVIVOR (الوحيد الذي يحمي رأس المال)")

        # 2. سوق سيء: graduation 2-5%
        # V526: SURVIVOR الأفضل، RUG-SURFER قد يربح لو البوت يلتقط pumps
        elif grad < 5.0:
            state.regime = "poor"
            state.confidence = 0.75
            if wr > 50 and pnl_24h > 0:
                state.recommended_profile = "rugsurfer"
                state.reasons.append("سوق سيء + أداء جيد → RUG-SURFER (اركب الـ pumps)")
            else:
                state.recommended_profile = "survivor"
                state.reasons.append("سوق سيء + أداء ضعيف → SURVIVOR (حماية)")

        # 3. سوق متوسط: graduation 5-10%
        elif grad < 10.0:
            state.regime = "average"
            state.confidence = 0.70
            state.recommended_profile = "rugsurfer"
            state.reasons.append("سوق متوسط → RUG-SURFER (يوازن بين الحماية والربح)")

        # 4. سوق جيد: graduation > 10%
        # هنا فقط نسمح بأنماط أكثر عدوانية
        else:
            state.regime = "good"
            state.confidence = 0.80
            state.recommended_profile = "ultimate"
            state.reasons.append("سوق جيد → ULTIMATE (استغل كل فرصة)")

        # 5. override: لو البوت يخسر كثيراً (PnL 24h < -10%)
        # → SURVIVOR فوراً (توقف الخسائر) بغض النظر عن السوق
        if pnl_24h < -10:
            state.recommended_profile = "survivor"
            state.reasons.append("⚠️ خسارة كبيرة 24h → SURVIVOR (توقف الخسائر فوراً)")

        # 6. override: لو البوت يخسر 3 صفقات متتالية
        # → SURVIVOR (قد يكون السوق تغير)
        if wr < 30 and wr > 0:
            state.recommended_profile = "survivor"
            state.reasons.append("⚠️ win rate < 30% → SURVIVOR (السوق قد يكون تغير)")


# ============================================================
# اختبار
# ============================================================

async def test_switcher():
    """اختبر الـ SmartProfileSwitcher."""
    from app.core.config import settings
    switcher = SmartProfileSwitcher()
    state = await switcher.detect_market_state()
    print(f"📊 حالة السوق:")
    print(f"  Regime: {state.regime}")
    print(f"  Graduation rate: {state.graduation_rate_pct:.1f}%")
    print(f"  Moonshot rate: {state.moonshot_rate_pct:.1f}%")
    print(f"  Active tokens: {state.active_tokens_count}")
    print(f"  Avg liquidity: ${state.avg_liquidity_usd:.0f}")
    print(f"  Win rate (10 trades): {state.recent_win_rate_pct:.1f}%")
    print(f"  PnL 24h: ${state.recent_pnl_24h_usd:.2f}")
    print(f"  Recommended profile: {state.recommended_profile}")
    print(f"  Confidence: {state.confidence:.0%}")
    print(f"  Reasons: {state.reasons}")
    return state


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_switcher())
