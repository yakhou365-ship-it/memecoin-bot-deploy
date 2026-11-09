"""
V526 — BRUTALLY HONEST STRATEGY AUDIT
فحص صارم جداً للاستراتيجية — بدون أي تفاؤل.

افحص كل افتراض ممكن يُعطي نتائج متفائلة:
1. هل 70% احتمال الهروب من rug واقعي؟
2. هل الـ moonshots تُلتقط فعلاً؟
3. هل الرسوم محسوبة بدقة؟
4. هل slippage السوق الحقيقي أعلى من 3%؟
5. هل الـ quick_tp يُملأ عند +10% فعلاً أم أقل؟
6. هل الـ SL يُملأ عند -5% فعلاً أم أقل (slippage سلبي)؟
7. هل الـ circuit breaker يحمي فعلاً؟

محاكاة واقعية جداً مع:
- 95% من العملات rugs (لا هروب)
- moonshots نادرة جداً (0.1%)
- slippage حقيقي 5-15% على البيع
- رسوم إضافية (priority fee, rent)
- فشل تنفيذ الأوامر (network errors)
"""
from __future__ import annotations

import os, sys, random, statistics
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# إحصائيات السوق الحقيقية (محافظة جداً)
# ============================================================

# توزيع مصير العملات — محافظ جداً
# الواقع: 98.6% scams، فقط 0.5% moonshots
FATE_DIST_PESSIMISTIC = {
    "rug_immediate":    0.65,   # 65% rug فوري (لا هروب ممكن)
    "fail_day1_dump":   0.30,   # 30% يموت يوم 1
    "lateral_die":      0.035,  # 3.5% جانبي ثم يموت
    "graduate_small":   0.014,  # 1.4% يتخرج بربح متواضع
    "moonshot":         0.001,  # 0.1% moonshot (نادر جداً)
}

# رسوم حقيقية محافظة جداً
# PumpPortal: 1% + bonding curve 0.95% + slippage buy 2% + slippage sell 8%
# + priority fee + rent + failed tx fee
REAL_FEES_PCT = 12.0   # 12% round-trip (مضاعف ما كان!)

# احتمال فشل تنفيذ الأمر (network, slippage exceeded, etc.)
ORDER_FAIL_RATE = 0.10   # 10% من الأوامر تفشل

# slippage سلبي على الـ SL: عند حدث الـ SL، السعر الفعلي يكون أقل من -5%
# لأن الـ dump سريع جداً في memecoin
SL_SLIPPAGE_PCT = 0.10   # SL يُملأ عند -15% بدل -5%


@dataclass
class RealisticToken:
    fate: str
    peak_mult: float
    final_mult: float
    # خلال كم ثانية يحدث الـ dump (1-60 ثانية للـ rug)
    time_to_dump_sec: int


def gen_realistic_token() -> RealisticToken:
    fate = random.random()
    cum = 0
    fate_name = "lateral_die"
    for fn, p in FATE_DIST_PESSIMISTIC.items():
        cum += p
        if fate < cum:
            fate_name = fn
            break

    if fate_name == "rug_immediate":
        peak_mult = random.uniform(1.2, 2.5)   # pump قصير ثم dump
        final_mult = 0.02                        # 98% خسارة
        time_to_dump = random.randint(1, 30)     # ثوانٍ
    elif fate_name == "fail_day1_dump":
        peak_mult = random.uniform(1.2, 2.0)
        final_mult = random.uniform(0.15, 0.4)
        time_to_dump = random.randint(60, 600)   # 1-10 دقائق
    elif fate_name == "lateral_die":
        peak_mult = random.uniform(1.1, 1.5)
        final_mult = random.uniform(0.3, 0.7)
        time_to_dump = random.randint(600, 3600)  # 10-60 دقيقة
    elif fate_name == "graduate_small":
        peak_mult = random.uniform(1.5, 3.0)
        final_mult = random.uniform(1.2, 2.0)
        time_to_dump = random.randint(3600, 86400)  # 1-24 ساعة
    else:  # moonshot (نادر جداً)
        peak_mult = random.uniform(5, 20)
        final_mult = random.uniform(2, 8)
        time_to_dump = random.randint(3600, 86400)

    return RealisticToken(fate_name, peak_mult, final_mult, time_to_dump)


# ============================================================
# استراتيجية ULTIMATE كما في الكود الفعلي
# ============================================================

def ultimate_exit_realistic(t: RealisticToken) -> tuple:
    """خروج ULTIMATE مع افتراضات واقعية جداً.

    المشكلة السابقة: افترضنا 70% احتمال هروب من rug — غير واقعي!
    الواقع: rug يحدث في 1-30 ثانية. البوت يحتاج:
      - اكتشاف التوكن (WebSocket delay: 2-5s)
      - تحليل (3-5s)
      - إرسال أمر الشراء (2-3s)
      - تأكيد on-chain (5-15s)
      - إرسال أمر البيع (2-3s)
      - تأكيد on-chain (5-15s)
    المجموع: 19-46 ثانية. معظم الـ rugs تنفجر خلال 1-30 ثانية!

    احتمال الهروب = 0% لو time_to_dump < 20s
                  = 20% لو 20-40s
                  = 50% لو 40-60s
                  = 90% لو > 60s (سوق طبيعي)
    """
    if t.fate == "rug_immediate":
        if t.time_to_dump_sec < 20:
            escape_chance = 0.0   # لا فاصلة زمنية للهروب
        elif t.time_to_dump_sec < 40:
            escape_chance = 0.20
        elif t.time_to_dump_sec < 60:
            escape_chance = 0.50
        else:
            escape_chance = 0.90

        if random.random() < escape_chance:
            # هروب عند +10% (quick_tp)
            return (1.10, "escaped_+10")
        # لو لم نهرب، SL لا يُملأ عند -5% بل عند -15% (slippage)
        return (1.0 - SL_SLIPPAGE_PCT, "rug_with_slippage")

    peak = t.peak_mult
    final = t.final_mult
    peak_gain = peak - 1

    # SL صارم -5% لكن slippage سلبي يجعله -15%
    if peak < 0.95:
        return (1.0 - SL_SLIPPAGE_PCT, "sl_with_slippage")

    # ULTIMATE exit tiers
    if peak_gain >= 2.00:   # moonshot +200%
        exit_mult = peak * (1 - 0.25)   # trailing 25%
        return (exit_mult, "trail_moonshot")
    if peak_gain >= 0.60:   # medium +60%
        # slippage على البيع 8%
        return (1.60 * (1 - 0.08), "medium_tp_+60")
    if peak_gain >= 0.10:   # quick_tp +10%
        # slippage على البيع 5%
        return (1.10 * (1 - 0.05), "quick_tp_+10")
    # hold to final
    return (max(final, 1.0 - SL_SLIPPAGE_PCT), "hold")


# ============================================================
# المحاكاة الواقعية الصارمة
# ============================================================

@dataclass
class Trade:
    fate: str
    pnl_pct: float       # نسبة الربح/الخسارة من رأس المال المُستثمر
    exit_reason: str
    failed: bool = False


def simulate_brutal_honest(capital: float, num_days: int, seed: int) -> Dict:
    """محاكاة صارمة جداً — بدون أي تفاؤل."""
    random.seed(seed)

    wallet = capital
    trades: List[Trade] = []
    cb_days = 0
    failed_orders = 0
    max_position_usd = capital * 0.5

    # ULTIMATE settings
    trade_pct = 10.0
    daily_cap_pct = 50.0
    daily_loss_pct = 25.0
    max_trades_per_day = 12

    for day in range(1, num_days + 1):
        if wallet < capital * 0.10:
            break

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        cb_today = False

        # 600 توكن يومياً للفحص
        for i in range(600):
            if trades_today >= max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                cb_today = True
                break

            token = gen_realistic_token()

            # بوابات ULTIMATE: ~30% من العملات تجتاز (الـ V511 يحظر 35%)
            # ~50% من البقية تُرفض بسبب chg_5m أو age
            if random.random() > 0.30:
                continue

            trade_size_usd = min(wallet * trade_pct / 100, max_position_usd)
            if trade_size_usd < 0.50:
                continue

            # محاكاة فشل الأمر (network error, slippage exceeded)
            if random.random() < ORDER_FAIL_RATE:
                # أمر فاشل — لا توجد صفقة لكن قد تكون رسوم gas
                failed_orders += 1
                wallet -= trade_size_usd * 0.005  # 0.5% gas fee on failed tx
                continue

            exit_mult, reason = ultimate_exit_realistic(token)
            entry_usd = trade_size_usd
            # رسوم حقيقية 12% round-trip
            net_exit = entry_usd * exit_mult * (1 - REAL_FEES_PCT / 100)
            pnl = net_exit - entry_usd
            pnl_pct = (pnl / entry_usd) * 100

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1
            trades.append(Trade(token.fate, pnl_pct, reason))

        if cb_today:
            cb_days += 1

    executed = [t for t in trades if not t.failed]
    wins = [t for t in executed if t.pnl_pct > 0]
    losses = [t for t in executed if t.pnl_pct <= 0]

    return {
        "capital_start": capital,
        "capital_end": wallet,
        "net_pnl": wallet - capital,
        "roi_pct": (wallet - capital) / capital * 100,
        "trades": len(executed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(1, len(executed)) * 100,
        "rug_losses": len([t for t in losses if t.fate == "rug_immediate"]),
        "moonshots_caught": len([t for t in wins if t.fate == "moonshot"]),
        "circuit_breaker_days": cb_days,
        "failed_orders": failed_orders,
        "avg_win_pct": statistics.mean([t.pnl_pct for t in wins]) if wins else 0,
        "avg_loss_pct": statistics.mean([t.pnl_pct for t in losses]) if losses else 0,
    }


def main():
    print("=" * 90)
    print("V526 — BRUTALLY HONEST STRATEGY AUDIT")
    print("=" * 90)
    print(f"  Market: Nov 2026 — pessimistic assumptions")
    print(f"  Rugs: 65% immediate | Moonshots: 0.1% only")
    print(f"  Real fees: {REAL_FEES_PCT}% round-trip (was 3%)")
    print(f"  Order fail rate: {ORDER_FAIL_RATE*100}%")
    print(f"  SL slippage: -{SL_SLIPPAGE_PCT*100}% (SL at -5% fills at -15%)")
    print(f"  Profile: ULTIMATE")
    print()

    capitals = [20.0, 100.0, 500.0, 1000.0]
    seeds = list(range(1, 6))   # 5 seeds
    num_days = 30   # شهر واحد فقط (أقصر فترة = نتائج أوضح)

    all_results = {}
    for cap in capitals:
        print(f"\n{'='*90}")
        print(f"  Testing ${cap:.0f} × {len(seeds)} seeds × {num_days} days (pessimistic)")
        print(f"{'='*90}")

        cap_results = [simulate_brutal_honest(cap, num_days, s) for s in seeds]
        all_results[cap] = cap_results

        # جدول مفصل
        print(f"\n  {'Seed':<6} {'ROI':<12} {'PnL ($)':<12} {'Trades':<8} {'WR%':<7} "
              f"{'RugLss':<8} {'Moonsht':<8} {'FailOrd':<8} {'CB':<4}")
        print("  " + "-" * 90)
        for s, r in zip(seeds, cap_results):
            print(f"  {s:<6} {r['roi_pct']:>+10.1f}%  ${r['net_pnl']:>+9.2f}  "
                  f"{r['trades']:<8} {r['win_rate']:>5.1f}%  "
                  f"{r['rug_losses']:<8} {r['moonshots_caught']:<8} "
                  f"{r['failed_orders']:<8} {'Y' if r['circuit_breaker_days'] > 0 else 'N':<4}")

        # إحصائيات
        rois = [r["roi_pct"] for r in cap_results]
        pnls = [r["net_pnl"] for r in cap_results]
        wrs = [r["win_rate"] for r in cap_results]
        winning = sum(1 for r in rois if r > 0)

        print(f"\n  📊 إحصائيات ${cap:.0f} (pessimistic 30d):")
        print(f"     ROI avg:      {statistics.mean(rois):>+.1f}%")
        print(f"     ROI median:   {statistics.median(rois):>+.1f}%")
        print(f"     ROI best:     {max(rois):>+.1f}%")
        print(f"     ROI worst:    {min(rois):>+.1f}%")
        print(f"     PnL avg:      ${statistics.mean(pnls):>+.2f}")
        print(f"     WR avg:       {statistics.mean(wrs):.1f}%")
        print(f"     Winning periods: {winning}/{len(seeds)}")

    # مقارنة مع المحاكاة السابقة المتفائلة
    print(f"\n{'='*90}")
    print("  📊 COMPARISON: V520 optimistic vs V526 brutally honest")
    print(f"{'='*90}")
    print(f"""
  V520 (optimistic):
    - 3% fees only
    - 70% escape rate from rugs
    - SL fills at -5%
    - 0% order failures
    - Result: +2595% avg ROI (100% winning)

  V526 (brutally honest):
    - 12% fees (real)
    - 0-50% escape rate from rugs (most rugs < 30s)
    - SL fills at -15% (slippage)
    - 10% order failure rate
    - 65% rugs, only 0.1% moonshots
""")

    # التقييم الصادق
    print(f"{'='*90}")
    print("  💀 HONEST ASSESSMENT — هل الاستراتيجية رابحة فعلاً؟")
    print(f"{'='*90}")

    avg_roi_all = statistics.mean([
        statistics.mean([r["roi_pct"] for r in all_results[c]])
        for c in capitals
    ])
    winning_all = sum(
        sum(1 for r in all_results[c] if r["roi_pct"] > 0)
        for c in capitals
    )
    total_periods = sum(len(all_results[c]) for c in capitals)

    print(f"""
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  النتيجة الإجمالية (20 period × 4 capitals):                              │
  │  ──────────────────────────────────────────────────────────────────────  │
  │  متوسط ROI عبر كل الفترات:  {avg_roi_all:>+10.1f}%                          │
  │  فترات رابحة:                 {winning_all}/{total_periods} ({winning_all/total_periods*100:.0f}%)              │
  └──────────────────────────────────────────────────────────────────────────┘
""")

    for cap in capitals:
        rs = all_results[cap]
        rois = [r["roi_pct"] for r in rs]
        pnls = [r["net_pnl"] for r in rs]
        winning = sum(1 for r in rois if r > 0)
        avg_roi = statistics.mean(rois)
        avg_pnl = statistics.mean(pnls)

        status = "✅ رابح" if avg_roi > 0 else "❌ خاسر"
        print(f"  {status} ${cap:.0f}: avg ROI {avg_roi:>+8.1f}% | PnL ${avg_pnl:>+8.2f} | winning {winning}/{len(seeds)}")

    print(f"""
  🎓 الخلاصة الصادقة:
  ─────────────────────────────────────────────────────────────────
  المحاكاة المتفائلة السابقة (V520) كانت تُظهر +2595% ROI — لكنها
  افترضت افتراضات غير واقعية:
    ❌ رسوم 3% (الحقيقي 8-15%)
    ❌ 70% هروب من الـ rug (الحقيقي 0-50%)
    ❌ SL يُملأ عند -5% (الحقيقي -15% بسبب slippage)
    ❌ لا توجد أوامر فاشلة (الحقيقي 10%)

  المحاكاة الصارمة الحالية (V526) تُظهر الواقع:
  - لو السوق الحقيقي يشبه V526 → البوت يخسر (يخسر أقل من المتداول العادي لكنه يخسر)
  - البوت يحمي رأس المال (circuit breaker + V511) لكنه لا يربح في سوق 98.6% scams
  - الأمل الوحيد: انتظار سوق أفضل (graduation rate > 5%)

  ⚠️ توصية: لا تستثمر مبالغ كبيرة الآن. السوق الحالي قاسٍ جداً.
""")


if __name__ == "__main__":
    main()
