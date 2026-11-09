"""
V511 — REALISTIC PUMP.FUN SIMULATION WITH FULL BOT STRATEGY
============================================================

يحاكي استراتيجية البوت الفعلية مع كل بوابات الأمان:
1. V511 — حظر محفظة واحدة >30%
2. anti-chase — لا يدخل عند +500% 24h و 5m ≤ 0
3. anti-pump — لا يدخل عند +100% في 5 دقائق
4. V500 Smart Exit — trailing stop متغير + sudden_dump + stagnation
5. Auto-sizing — 5% من المحفظة لكل صفقة، حد خسارة يومي 15%
6. Circuit breaker — يوقف عند 3 خسائر متتالية

مقارنة بالنسبة للاستراتيجية الساذجة (بدون بوابات).
"""
from __future__ import annotations

import os
import sys
import random
import math
from dataclasses import dataclass, field
from typing import List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import numpy as np


# ============================================================
# Real Pump.fun statistics (from research)
# ============================================================
PUMP_FUN_STATS = {
    "tokens_fail_day1_pct": 69.0,        # 69% stop trading on launch day
    "tokens_rug_pct": 15.0,               # 15% are outright rugs
    "tokens_graduate_pct": 1.5,            # 1.5% reach graduation
    "tokens_pump_then_dump_pct": 12.0,    # pump 2-5x then dump
    "tokens_moonshot_pct": 0.5,            # 0.5% do 10x+
    "bonding_curve_fee_pct": 0.95,        # 0.95% per side
    "pumportal_lightning_fee_pct": 1.0,   # 1% per side
    "slippage_pct": 0.5,                  # 0.5% per side
}

ROUND_TRIP_FEE_PCT = (
    PUMP_FUN_STATS["pumportal_lightning_fee_pct"] * 2  # buy + sell
    + PUMP_FUN_STATS["slippage_pct"] * 2                # slippage both ways
)  # = 3% round trip


# ============================================================
# Token simulator
# ============================================================
@dataclass
class SimulatedToken:
    """رمز pump.fun محاكى مع metadata للفلترة."""
    symbol: str
    age_minutes: float
    bonding_curve_pct: float         # 0-100
    chg_24h_pct: float                # price change 24h
    chg_5m_pct: float                 # price change 5m
    liquidity_usd: float
    volume_24h_usd: float
    top_holder_pct: float             # نسبة أكبر محفظة
    mint_authority: bool
    freeze_authority: bool
    lp_locked: bool
    fate: str                         # "rug" | "fail_day1" | "pump_dump" | "graduate" | "moonshot" | "stable"
    peak_multiplier: float            # أعلى سعر يصل إليه (x من entry)
    final_multiplier: float           # السعر النهائي بعد 4h
    time_to_peak_minutes: float       # دقائق حتى القمة


def _classify_fate() -> str:
    """اختيار مصير العملة بناءً على توزيع pump.fun الحقيقي."""
    r = random.random() * 100
    cum = 0
    for fate, pct in [
        ("rug", PUMP_FUN_STATS["tokens_rug_pct"]),
        ("fail_day1", PUMP_FUN_STATS["tokens_fail_day1_pct"] - PUMP_FUN_STATS["tokens_rug_pct"]),
        ("pump_dump", PUMP_FUN_STATS["tokens_pump_then_dump_pct"]),
        ("graduate", PUMP_FUN_STATS["tokens_graduate_pct"]),
        ("moonshot", PUMP_FUN_STATS["tokens_moonshot_pct"]),
        ("stable", 100.0 - sum([PUMP_FUN_STATS["tokens_rug_pct"],
                                  PUMP_FUN_STATS["tokens_fail_day1_pct"] - PUMP_FUN_STATS["tokens_rug_pct"],
                                  PUMP_FUN_STATS["tokens_pump_then_dump_pct"],
                                  PUMP_FUN_STATS["tokens_graduate_pct"],
                                  PUMP_FUN_STATS["tokens_moonshot_pct"]])),
    ]:
        cum += pct
        if r < cum:
            return fate
    return "stable"


def _gen_token(symbol: str) -> SimulatedToken:
    """توليد توكن عشوائي بمصير محدد."""
    fate = _classify_fate()

    # Bonding curve progress (newer tokens = lower)
    age_min = random.uniform(2, 120)
    bonding = max(5, min(95, random.gauss(40, 20)))

    # Price changes — تعتمد على المصير
    if fate == "rug":
        # Rug pull: عادةً يكون الـ dev wallet كبير، توكن حديث
        chg_24h = random.uniform(50, 800)
        chg_5m = random.uniform(20, 200)  # pump نشط
        top_holder = random.uniform(25, 60)  # 🔴 محفظة واحدة كبيرة
        peak_mult = random.uniform(2, 5)
        final_mult = 0.0
        time_to_peak = random.uniform(10, 60)
    elif fate == "fail_day1":
        chg_24h = random.uniform(-30, 100)
        chg_5m = random.uniform(-20, 30)
        top_holder = random.uniform(5, 25)
        peak_mult = random.uniform(1.0, 1.8)
        final_mult = random.uniform(0.1, 0.7)
        time_to_peak = random.uniform(5, 30)
    elif fate == "pump_dump":
        chg_24h = random.uniform(100, 500)
        chg_5m = random.uniform(30, 150)  # 🔴 pump مفاجئ
        top_holder = random.uniform(8, 20)
        peak_mult = random.uniform(2.5, 5.0)
        final_mult = random.uniform(0.2, 0.6)
        time_to_peak = random.uniform(15, 90)
    elif fate == "graduate":
        chg_24h = random.uniform(50, 300)
        chg_5m = random.uniform(0, 30)
        top_holder = random.uniform(3, 12)
        peak_mult = random.uniform(3, 8)
        final_mult = random.uniform(1.5, 4.0)
        time_to_peak = random.uniform(60, 240)
    elif fate == "moonshot":
        chg_24h = random.uniform(200, 1000)
        chg_5m = random.uniform(10, 80)
        top_holder = random.uniform(3, 10)
        peak_mult = random.uniform(10, 50)
        final_mult = random.uniform(3, 15)
        time_to_peak = random.uniform(120, 480)
    else:  # stable
        chg_24h = random.uniform(-10, 30)
        chg_5m = random.uniform(-5, 10)
        top_holder = random.uniform(3, 15)
        peak_mult = random.uniform(1.2, 2.5)
        final_mult = random.uniform(0.6, 1.5)
        time_to_peak = random.uniform(30, 120)

    return SimulatedToken(
        symbol=symbol,
        age_minutes=age_min,
        bonding_curve_pct=bonding,
        chg_24h_pct=chg_24h,
        chg_5m_pct=chg_5m,
        liquidity_usd=random.uniform(5_000, 80_000),
        volume_24h_usd=random.uniform(10_000, 200_000),
        top_holder_pct=top_holder,
        mint_authority=random.random() < 0.05,  # 5% have mint auth
        freeze_authority=random.random() < 0.03,  # 3% have freeze
        lp_locked=random.random() < 0.40,  # 40% locked
        fate=fate,
        peak_multiplier=peak_mult,
        final_multiplier=final_mult,
        time_to_peak_minutes=time_to_peak,
    )


# ============================================================
# Entry filters (بوابات الأمان)
# ============================================================
def passes_v511_concentration(t: SimulatedToken) -> bool:
    """V511: حظر إذا محفظة واحدة >30%."""
    return t.top_holder_pct < 30.0


def passes_security_critical(t: SimulatedToken) -> bool:
    """حظر mint/freeze authority + minimum liquidity."""
    if t.mint_authority:
        return False
    if t.freeze_authority:
        return False
    if t.liquidity_usd < 5_000:
        return False
    return True


def passes_anti_chase(t: SimulatedToken) -> bool:
    """لا تطارد السعر: لا تدخل عند +500% 24h و 5m ≤ 0."""
    if t.chg_24h_pct > 500 and t.chg_5m_pct <= 0:
        return False
    return True


def passes_anti_pump(t: SimulatedToken) -> bool:
    """لا تدخل عند +100% في 5 دقائق."""
    return t.chg_5m_pct <= 100


def passes_all_bot_gates(t: SimulatedToken) -> bool:
    """كل بوابات البوت الفعلية."""
    return (
        passes_v511_concentration(t)
        and passes_security_critical(t)
        and passes_anti_chase(t)
        and passes_anti_pump(t)
    )


# ============================================================
# V500 Smart Exit Strategy (يستخدمه البوت فقط)
# ============================================================
def smart_exit(t: SimulatedToken) -> tuple[float, str]:
    """يحاكي V500 Smart Exit. يُرجع (multiplier_at_exit, exit_reason).

    المنطق:
    - trailing يبدأ بعد +30% gain
    - sudden_dump: 25% drop من peak بعد +10% gain
    - stagnation: 4h+, 0-5% gain, peak <5%
    - SL: -8% من entry
    - لأخذ الـ rug في الحسبان: rug = خروج عند ~0.05x (95% خسارة)
    """
    peak = t.peak_multiplier
    final = t.final_multiplier

    # rug pull = كارثة كاملة (لا يمكن الخروج عند -8% لأن الـ dump مفاجئ)
    if t.fate == "rug":
        # الـ rug يحدث خلال ثوانٍ — لا يمكن للبوت الخروج قبل الخسارة
        # الـ final_mult للـ rug = 0.0 (محاكاة واقعية)
        return (0.05, "rug_pull_loss")  # 95% خسارة

    # SL: -8% — يُفعّل إذا وصل السعر إلى 0.92× الـ entry قبل القمة
    if peak < 1.08:
        # لم نربح أبداً، SL يُفعّل
        return (0.92, "stop_loss")

    # peak_gain = (peak - 1) → نسبة الربح من entry
    peak_gain = peak - 1

    # Trailing_pct متغير
    if peak_gain >= 5.0:    # +500%
        trail_pct = 0.45
    elif peak_gain >= 1.0:   # +100%
        trail_pct = 0.35
    elif peak_gain >= 0.30:  # +30%
        trail_pct = 0.25
    else:
        trail_pct = None  # لم يبدأ الـ trailing

    # إذا الـ trailing بدأ: نخرج عند peak * (1 - trail_pct)
    if trail_pct is not None:
        exit_mult = peak * (1 - trail_pct)
        return (exit_mult, "smart_trailing")

    # Sudden dump قبل أن يبدأ الـ trailing (لكن بعد +10% gain)
    if peak_gain >= 0.10 and (peak - final) / peak >= 0.25:
        # حدث sudden dump — نخرج عند 75% من peak
        return (peak * 0.75, "sudden_dump")

    # Stagnation: 4h+, 0-5% gain, peak <5%
    if peak_gain < 0.05 and final < 1.05:
        return (final, "stagnation")

    # خروج افتراضي: نأخذ الـ final
    return (final, "time_exit")


def naive_exit(t: SimulatedToken) -> tuple[float, str]:
    """خروج naive: SL بسيط -8% فقط، بدون trailing/dump/stagnation.
    يأخذ الـ final أو الـ SL أيهما يأتي أولاً.
    """
    peak = t.peak_multiplier
    final = t.final_multiplier

    # rug = كارثة كاملة
    if t.fate == "rug":
        return (0.05, "rug_pull_loss")

    # SL بسيط -8%: إذا السعر هبط إلى 0.92× entry قبل الـ peak
    if final <= 0.92:
        # السعر هبط أكثر من 8% — نخرج عند SL
        return (0.92, "simple_sl")

    # خروج افتراضي: نأخذ الـ final
    return (final, "hold_to_end")


# ============================================================
# Simulation
# ============================================================
@dataclass
class TradeResult:
    symbol: str
    fate: str
    entry_usd: float
    exit_usd: float
    pnl_usd: float
    multiplier: float
    exit_reason: str
    blocked: bool = False
    block_reason: str = ""


def simulate_30_days(
    capital: float = 20.0,
    apply_gates: bool = True,
    seed: int = 42,
    max_trades_per_day: int = 2,
) -> dict:
    """يحاكي 30 يوم من التداول. يُرجع dict بالنتائج."""
    random.seed(seed)
    np.random.seed(seed)

    wallet = capital
    daily_cap_pct = 30.0  # 30% of wallet per day
    trade_size_pct = 5.0   # 5% of wallet per trade
    daily_loss_pct = 15.0  # 15% daily loss limit

    start_wallet = wallet
    trades: List[TradeResult] = []
    days_active = 0
    circuit_breaker = False

    for day in range(1, 31):
        if circuit_breaker or wallet < 0.50:
            continue

        days_active += 1
        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0

        # محاكاة 5-15 توكن يومياً — نأخذ أول ones التي تجتاز البوابات
        candidates = [_gen_token(f"TKN{day}-{i}") for i in range(random.randint(8, 15))]

        for token in candidates:
            if trades_today >= max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                # circuit breaker اليومي
                circuit_breaker = True
                break

            # فلترة البوابات
            if apply_gates:
                if not passes_all_bot_gates(token):
                    trades.append(TradeResult(
                        symbol=token.symbol, fate=token.fate,
                        entry_usd=0, exit_usd=0, pnl_usd=0, multiplier=1,
                        exit_reason="blocked",
                        blocked=True,
                        block_reason=(
                            "30%+ wallet" if token.top_holder_pct >= 30
                            else "mint/freeze" if (token.mint_authority or token.freeze_authority)
                            else "anti-chase" if (token.chg_24h_pct > 500 and token.chg_5m_pct <= 0)
                            else "anti-pump" if token.chg_5m_pct > 100
                            else "low-liq"
                        ),
                    ))
                    continue

            # حساب حجم الصفقة (auto-sizing)
            trade_size_usd = wallet * trade_size_pct / 100
            if trade_size_usd < 0.50:
                continue  # محفظة صغيرة جداً

            # تنفيذ الصفقة: استخدم smart_exit للـ bot, naive_exit للـ naive
            exit_fn = smart_exit if apply_gates else naive_exit
            exit_mult, reason = exit_fn(token)
            entry_usd = trade_size_usd
            gross_exit = entry_usd * exit_mult
            # خصم الرسوم (3% round trip)
            net_exit = gross_exit * (1 - ROUND_TRIP_FEE_PCT / 100)
            pnl = net_exit - entry_usd

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1

            trades.append(TradeResult(
                symbol=token.symbol, fate=token.fate,
                entry_usd=entry_usd, exit_usd=net_exit,
                pnl_usd=pnl, multiplier=exit_mult,
                exit_reason=reason, blocked=False,
            ))

    # إحصائيات
    executed = [t for t in trades if not t.blocked]
    blocked = [t for t in trades if t.blocked]
    wins = [t for t in executed if t.pnl_usd > 0]
    losses = [t for t in executed if t.pnl_usd <= 0]
    rug_losses = [t for t in losses if t.fate == "rug"]
    moonshot_wins = [t for t in wins if t.fate == "moonshot"]
    graduate_wins = [t for t in wins if t.fate == "graduate"]

    return {
        "capital_start": capital,
        "capital_end": wallet,
        "net_pnl_usd": wallet - capital,
        "roi_pct": (wallet - capital) / capital * 100,
        "total_trades": len(executed),
        "blocked_count": len(blocked),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(1, len(executed)) * 100,
        "rug_losses_avoided": len([t for t in blocked if t.block_reason == "30%+ wallet"]),
        "rug_losses_taken": len(rug_losses),
        "moonshot_wins": len(moonshot_wins),
        "graduate_wins": len(graduate_wins),
        "avg_win_usd": sum(t.pnl_usd for t in wins) / max(1, len(wins)),
        "avg_loss_usd": sum(t.pnl_usd for t in losses) / max(1, len(losses)),
        "circuit_breaker_hit": circuit_breaker,
        "trades": trades,
    }


def print_summary(label: str, result: dict):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  💵 رأس المال الابتدائي:    ${result['capital_start']:.2f}")
    print(f"  💰 رأس المال النهائي:      ${result['capital_end']:.2f}")
    print(f"  📈 صافي الربح/الخسارة:     ${result['net_pnl_usd']:.2f}")
    print(f"  📊 العائد (ROI):            {result['roi_pct']:+.1f}%")
    print(f"  🎯 إجمالي الصفقات:         {result['total_trades']}")
    print(f"  🚫 الصفقات المحظورة:        {result['blocked_count']}")
    print(f"  ✅ صفقات رابحة:             {result['wins']}")
    print(f"  ❌ صفقات خاسرة:            {result['losses']}")
    print(f"  📏 نسبة النجاح (WR):        {result['win_rate']:.1f}%")
    print(f"  🛡️ عمليات rug محظورة:      {result['rug_losses_avoided']}")
    print(f"  💔 عمليات rug تم تنفيذها:   {result['rug_losses_taken']}")
    print(f"  🚀 صفقات moonshot رابحة:   {result['moonshot_wins']}")
    print(f"  🎓 صفقات graduate رابحة:   {result['graduate_wins']}")
    print(f"  💵 متوسط الربح:           ${result['avg_win_usd']:.3f}")
    print(f"  💸 متوسط الخسارة:          ${result['avg_loss_usd']:.3f}")
    print(f"  ⛔ circuit breaker:         {'نعم' if result['circuit_breaker_hit'] else 'لا'}")


def main():
    print("=" * 70)
    print("V511 — REALISTIC PUMP.FUN SIMULATION: BOT STRATEGY vs NAIVE")
    print("=" * 70)
    print(f"رأس المال: $20 | 30 يوم | رسوم: {ROUND_TRIP_FEE_PCT:.1f}% round-trip")
    print()

    # 3 seeds لكل سيناريو
    seeds = [42, 123, 789]
    results_naive = []
    results_bot = []

    for seed in seeds:
        r_naive = simulate_30_days(capital=20.0, apply_gates=False, seed=seed)
        r_bot = simulate_30_days(capital=20.0, apply_gates=True, seed=seed)
        results_naive.append(r_naive)
        results_bot.append(r_bot)

    # طباعة كل سيناريو
    for seed, r_n, r_b in zip(seeds, results_naive, results_bot):
        print_summary(f"Seed {seed} — NAIVE (no gates)", r_n)
        print_summary(f"Seed {seed} — BOT (full V511 strategy)", r_b)

    # متوسط
    avg_naive_roi = sum(r["roi_pct"] for r in results_naive) / len(results_naive)
    avg_bot_roi = sum(r["roi_pct"] for r in results_bot) / len(results_bot)
    avg_naive_wr = sum(r["win_rate"] for r in results_naive) / len(results_naive)
    avg_bot_wr = sum(r["win_rate"] for r in results_bot) / len(results_bot)
    avg_naive_blocked = sum(r["blocked_count"] for r in results_naive) / len(results_naive)
    avg_bot_blocked = sum(r["blocked_count"] for r in results_bot) / len(results_bot)
    avg_naive_rugs = sum(r["rug_losses_taken"] for r in results_naive) / len(results_naive)
    avg_bot_rugs = sum(r["rug_losses_taken"] for r in results_bot) / len(results_bot)
    avg_bot_rugs_avoided = sum(r["rug_losses_avoided"] for r in results_bot) / len(results_bot)

    print(f"\n{'='*70}")
    print(f"  📊 SUMMARY — AVERAGE OF 3 SEEDS")
    print(f"{'='*70}")
    print(f"  {'Metric':<30} {'NAIVE':<15} {'BOT (V511)':<15}")
    print(f"  {'-'*60}")
    print(f"  {'ROI':<30} {avg_naive_roi:>+10.1f}%   {avg_bot_roi:>+10.1f}%")
    print(f"  {'Win Rate':<30} {avg_naive_wr:>10.1f}%   {avg_bot_wr:>10.1f}%")
    print(f"  {'Blocked trades':<30} {avg_naive_blocked:>10.1f}    {avg_bot_blocked:>10.1f}")
    print(f"  {'Rug losses (executed)':<30} {avg_naive_rugs:>10.1f}    {avg_bot_rugs:>10.1f}")
    print(f"  {'Rug losses (avoided by 30% block)':<30} {'N/A':>10}    {avg_bot_rugs_avoided:>10.1f}")

    print(f"\n{'='*70}")
    print(f"  🏆 VERDICT")
    print(f"{'='*70}")
    if avg_bot_roi > avg_naive_roi:
        improvement = avg_bot_roi - avg_naive_roi
        print(f"  ✅ BOT strategy OUTPERFORMS naive by {improvement:+.1f}% ROI")
        print(f"  ✅ Bot avoided {avg_bot_rugs_avoided:.0f} rug losses via 30% gate")
        if avg_bot_roi > 0:
            print(f"  ✅ Bot is PROFITABLE: {avg_bot_roi:+.1f}% ROI on $20 over 30 days")
        else:
            print(f"  ⚠️ Bot is still losing (but losing LESS than naive)")
            print(f"     This is realistic — pump.fun is brutal even with all gates")
    else:
        print(f"  ⚠️ Naive outperformed bot (unusual — check seed)")


if __name__ == "__main__":
    main()
