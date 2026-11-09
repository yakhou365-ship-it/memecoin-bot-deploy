"""
V527 — SURVIVOR STRATEGY (Asta never gives up, but never charges blindly)

فلسفة جديدة: بدل محاولة الربح من سوق 98.6% scams، نُركز على:
1. **العيش (Survival)** — لا نخسر رأس المال
2. **الانتقائية الشديدة** — لا ندخل إلا لو كل الشروط محققة
3. **الهروب فوري** — أول إشارة خطر = خروج

الحل الجوهري: المشكلة ليست في منطق الخروج، بل في **منطق الدخول**.
كان البوت يدخل 30% من الفرص → 65% منها rugs → خسارة حتمية.

SURVIVOR: يدخل < 5% من الفرص، لكن كل صفقة تُدخلها يكون لها:
- محفظة dev < 15% (ليس فقط < 30%)
- سيولة ≥ $20,000 (وليس $1500)
- حجم تداول 5m ≥ $5,000 (نشاط حقيقي)
- عمر التوكن > 30 ثانية لكن < 2 دقيقة (تجاوز rug المباشر)
- chg_5m بين 10% و 40% (pump صحي، ليس مضاربة)
- LP locked أو منصات موثوقة

+ خرج فوري عند +5% (لا طمع) و SL عند -3% (أصعب)
"""
from __future__ import annotations

import os, sys, random, statistics
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)


# إحصائيات السوق (نفس V526 — محافظة جداً)
FATE_DIST = {
    "rug_immediate":    0.65,
    "fail_day1_dump":   0.30,
    "lateral_die":      0.035,
    "graduate_small":   0.014,
    "moonshot":         0.001,
}
REAL_FEES_PCT = 12.0
ORDER_FAIL_RATE = 0.10
SL_SLIPPAGE_PCT = 0.10


@dataclass
class RealisticToken:
    fate: str
    peak_mult: float
    final_mult: float
    time_to_dump_sec: int
    top_holder_pct: float
    liquidity_usd: float
    volume_5m_usd: float
    age_sec: int
    chg_5m_pct: float
    lp_locked: bool
    mint_authority: bool
    freeze_authority: bool


def gen_token() -> RealisticToken:
    fate = random.random()
    cum = 0
    fate_name = "lateral_die"
    for fn, p in FATE_DIST.items():
        cum += p
        if fate < cum:
            fate_name = fn
            break

    if fate_name == "rug_immediate":
        peak_mult = random.uniform(1.2, 2.5)
        final_mult = 0.02
        time_to_dump = random.randint(1, 30)
        top_holder = random.uniform(25, 60)
        liquidity = random.uniform(500, 5000)
        volume_5m = random.uniform(100, 2000)
    elif fate_name == "fail_day1_dump":
        peak_mult = random.uniform(1.2, 2.0)
        final_mult = random.uniform(0.15, 0.4)
        time_to_dump = random.randint(60, 600)
        top_holder = random.uniform(10, 25)
        liquidity = random.uniform(2000, 15000)
        volume_5m = random.uniform(500, 5000)
    elif fate_name == "lateral_die":
        peak_mult = random.uniform(1.1, 1.5)
        final_mult = random.uniform(0.3, 0.7)
        time_to_dump = random.randint(600, 3600)
        top_holder = random.uniform(5, 15)
        liquidity = random.uniform(5000, 25000)
        volume_5m = random.uniform(1000, 10000)
    elif fate_name == "graduate_small":
        peak_mult = random.uniform(1.5, 3.0)
        final_mult = random.uniform(1.2, 2.0)
        time_to_dump = random.randint(3600, 86400)
        top_holder = random.uniform(3, 10)
        liquidity = random.uniform(15000, 80000)
        volume_5m = random.uniform(3000, 30000)
    else:  # moonshot
        peak_mult = random.uniform(5, 20)
        final_mult = random.uniform(2, 8)
        time_to_dump = random.randint(3600, 86400)
        top_holder = random.uniform(3, 10)
        liquidity = random.uniform(20000, 200000)
        volume_5m = random.uniform(5000, 50000)

    return RealisticToken(
        fate=fate_name, peak_mult=peak_mult, final_mult=final_mult,
        time_to_dump_sec=time_to_dump,
        top_holder_pct=top_holder,
        liquidity_usd=liquidity,
        volume_5m_usd=volume_5m,
        age_sec=random.randint(5, 300),
        chg_5m_pct=random.uniform(-20, 200),
        lp_locked=random.random() < 0.40,
        mint_authority=random.random() < 0.05,
        freeze_authority=random.random() < 0.03,
    )


# ============================================================
# SURVIVOR gates — صارمة جداً
# ============================================================

def passes_survivor_gates(t: RealisticToken) -> bool:
    """بوابات SURVIVOR — أقل من 5% من الفرص تُقبل.

    الفلسفة: لا ندخل إلا لو كل المؤشرات إيجابية:
    1. V511: محفظة < 15% (ليس فقط < 30%)
    2. لا mint/freeze authority
    3. سيولة ≥ $20,000 (نشاط حقيقي)
    4. حجم 5m ≥ $5,000 (تداول نشط)
    5. عمر 30-120 ثانية (تجاوز rug المباشر، قبل pump الرئيسي)
    6. chg_5m بين 10% و 40% (pump صحي، ليس مضاربة)
    7. LP locked (إثبات نية حسنة)
    """
    if t.top_holder_pct >= 15.0: return False   # أصعب من V511 (30%)
    if t.mint_authority: return False
    if t.freeze_authority: return False
    if t.liquidity_usd < 20000: return False    # أصعب من ultimate (1500)
    if t.volume_5m_usd < 5000: return False      # جديد!
    if t.age_sec < 30 or t.age_sec > 120: return False  # نافذة ضيقة جداً
    if t.chg_5m_pct < 10 or t.chg_5m_pct > 40: return False  # نطاق ضيق
    if not t.lp_locked: return False              # LP locked إجباري
    return True


def survivor_exit(t: RealisticToken) -> tuple:
    """خروج SURVIVOR — حذر جداً، لا طمع.

    - quick_tp عند +5% (بدل +10%) — ربح مضمون
    - SL عند -3% (بدل -5%) — أصعب
    - sudden_dump عند +3% gain و 10% drop — حساسية قصوى
    - لا moonshot trailing — نأخذ الربح ونخرج
    """
    if t.fate == "rug_immediate":
        # SURVIVOR: التجاوز الـ rug أسوأ لأننا ندخل متأخرين
        if t.time_to_dump_sec < 30:
            escape_chance = 0.0   # لا فاصلة زمنية
        elif t.time_to_dump_sec < 60:
            escape_chance = 0.30
        else:
            escape_chance = 0.70

        if random.random() < escape_chance:
            return (1.05, "survived_+5")
        return (1.0 - SL_SLIPPAGE_PCT, "rug_with_slippage")

    peak = t.peak_mult
    final = t.final_mult
    peak_gain = peak - 1

    # SL صارم -3% لكن slippage يجعله -13%
    if peak < 0.97:
        return (1.0 - 0.13, "sl_with_slippage")

    # quick_tp عند +5%
    if peak_gain >= 0.05:
        # slippage على البيع 5%
        return (1.05 * (1 - 0.05), "quick_tp_+5")

    # sudden_dump عند +3% gain و 10% drop
    if peak_gain >= 0.03:
        drop = (peak - final) / peak if peak > 0 else 0
        if drop >= 0.10:
            return (peak * 0.90, "sudden_dump")

    # hold to final
    return (max(final, 1.0 - 0.13), "hold")


# ============================================================
# المحاكاة (نفس V526 لكن مع SURVIVOR)
# ============================================================

@dataclass
class Trade:
    fate: str
    pnl_pct: float
    exit_reason: str


def simulate_survivor(capital: float, num_days: int, seed: int) -> Dict:
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    cb_days = 0
    failed_orders = 0
    max_position_usd = capital * 0.5

    # SURVIVOR settings — أصغر من ULTIMATE
    trade_pct = 5.0           # 5% فقط لكل صفقة (حذر)
    daily_cap_pct = 30.0      # 30% حد يومي (حذر)
    daily_loss_pct = 10.0     # 10% حد خسارة يومي (صارم جداً)
    max_trades_per_day = 3   # 3 صفقات فقط يومياً (انتقائية)

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

            token = gen_token()

            # بوابات SURVIVOR الصارمة
            if not passes_survivor_gates(token):
                continue

            trade_size_usd = min(wallet * trade_pct / 100, max_position_usd)
            if trade_size_usd < 0.50:
                continue

            # فشل الأمر
            if random.random() < ORDER_FAIL_RATE:
                failed_orders += 1
                wallet -= trade_size_usd * 0.005
                continue

            exit_mult, reason = survivor_exit(token)
            entry_usd = trade_size_usd
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

    executed = trades
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
    print("V527 — SURVIVOR STRATEGY (Asta never gives up, never charges blindly)")
    print("=" * 90)
    print(f"  Market: Nov 2026 pessimistic (98.6% scams, 12% fees, 10% order fails)")
    print(f"  Strategy: SURVIVOR (strict gates, quick exit, no greed)")
    print()

    capitals = [20.0, 100.0, 500.0, 1000.0]
    seeds = list(range(1, 6))
    num_days = 30

    print(f"  SURVIVOR vs ULTIMATE comparison:")
    print(f"  {'Metric':<30} {'ULTIMATE':<15} {'SURVIVOR':<15}")
    print(f"  {'-'*60}")
    print(f"  {'Max holder pct':<30} {'30%':<15} {'15%':<15}")
    print(f"  {'Min liquidity':<30} {'$1500':<15} {'$20000':<15}")
    print(f"  {'Min volume 5m':<30} {'$3000':<15} {'$5000':<15}")
    print(f"  {'Age window':<30} {'<3min':<15} {'30-120s':<15}")
    print(f"  {'chg_5m range':<30} {'8-60%':<15} {'10-40%':<15}")
    print(f"  {'LP locked required':<30} {'No':<15} {'Yes':<15}")
    print(f"  {'Quick TP':<30} {'+10%':<15} {'+5%':<15}")
    print(f"  {'Stop Loss':<30} {'-5%':<15} {'-3%':<15}")
    print(f"  {'Trade %':<30} {'10%':<15} {'5%':<15}")
    print(f"  {'Daily cap':<30} {'50%':<15} {'30%':<15}")
    print(f"  {'Daily loss limit':<30} {'25%':<15} {'10%':<15}")
    print(f"  {'Max trades/day':<30} {'12':<15} {'3':<15}")

    all_results = {}
    for cap in capitals:
        print(f"\n{'='*90}")
        print(f"  Testing ${cap:.0f} × {len(seeds)} seeds × {num_days} days (SURVIVOR)")
        print(f"{'='*90}")

        cap_results = [simulate_survivor(cap, num_days, s) for s in seeds]
        all_results[cap] = cap_results

        print(f"\n  {'Seed':<6} {'ROI':<12} {'PnL ($)':<12} {'Trades':<8} {'WR%':<7} "
              f"{'RugLss':<8} {'Moonsht':<8} {'CB':<4}")
        print("  " + "-" * 80)
        for s, r in zip(seeds, cap_results):
            print(f"  {s:<6} {r['roi_pct']:>+10.1f}%  ${r['net_pnl']:>+9.2f}  "
                  f"{r['trades']:<8} {r['win_rate']:>5.1f}%  "
                  f"{r['rug_losses']:<8} {r['moonshots_caught']:<8} "
                  f"{'Y' if r['circuit_breaker_days'] > 0 else 'N':<4}")

        rois = [r["roi_pct"] for r in cap_results]
        pnls = [r["net_pnl"] for r in cap_results]
        wrs = [r["win_rate"] for r in cap_results]
        winning = sum(1 for r in rois if r > 0)

        print(f"\n  📊 إحصائيات ${cap:.0f} (SURVIVOR 30d):")
        print(f"     ROI avg:      {statistics.mean(rois):>+.1f}%")
        print(f"     ROI median:   {statistics.median(rois):>+.1f}%")
        print(f"     ROI best:     {max(rois):>+.1f}%")
        print(f"     ROI worst:    {min(rois):>+.1f}%")
        print(f"     PnL avg:      ${statistics.mean(pnls):>+.2f}")
        print(f"     WR avg:       {statistics.mean(wrs):.1f}%")
        print(f"     Trades avg:   {statistics.mean([r['trades'] for r in cap_results]):.1f}")
        print(f"     Winning periods: {winning}/{len(seeds)}")

    # مقارنة V526 vs V527
    print(f"\n{'='*90}")
    print("  📊 COMPARISON: ULTIMATE (V526) vs SURVIVOR (V527)")
    print(f"{'='*90}")
    print(f"\n  {'Capital':<10} {'ULTIMATE ROI':<18} {'SURVIVOR ROI':<18} {'Improvement':<15}")
    print("  " + "-" * 65)

    # V526 نتائج (من المحاكاة السابقة)
    v526_rois = {
        20.0: -75.2,
        100.0: -81.1,
        500.0: -81.1,
        1000.0: -81.1,
    }

    for cap in capitals:
        rs = all_results[cap]
        survivor_avg = statistics.mean([r["roi_pct"] for r in rs])
        ultimate_avg = v526_rois[cap]
        improvement = survivor_avg - ultimate_avg
        print(f"  ${cap:<8.0f} {ultimate_avg:>+15.1f}%  {survivor_avg:>+15.1f}%  "
              f"{improvement:>+12.1f}pp")

    # التقييم
    print(f"\n{'='*90}")
    print("  💀 HONEST ASSESSMENT — SURVIVOR في السوق القاسي")
    print(f"{'='*90}")

    avg_survivor = statistics.mean([
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
  │  SURVIVOR النتيجة الإجمالية:                                              │
  │  ──────────────────────────────────────────────────────────────────────  │
  │  متوسط ROI:          {avg_survivor:>+10.1f}%                          │
  │  فترات رابحة:         {winning_all}/{total_periods} ({winning_all/total_periods*100:.0f}%)              │
  └──────────────────────────────────────────────────────────────────────────┘
""")

    if avg_survivor > 0:
        print("  ✅ SURVIVOR يربح حتى في السوق القاسي!")
    elif avg_survivor > -50:
        print(f"  ⚠️ SURVIVOR يخسر أقل من ULTIMATE لكنه لا يزال خاسراً")
        print(f"     التحسن: {avg_survivor - (-79.6):+.1f}pp مقابل ULTIMATE")
    else:
        print(f"  ❌ SURVIVOR يخسر أيضاً — السوق قاسٍ جداً")


if __name__ == "__main__":
    main()
