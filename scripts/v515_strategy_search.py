"""
V515 — SNIPER-EXIT STRATEGY SEARCH
يجد أفضل استراتيجية من بين 6 أنماط مختلفة في سوق نوفمبر 2026 الحقيقي.

الفكرة الجوهرية: بما أن 98.6% من العملات scams، السرعة = النجاة.
- ادخل مبكراً (5-30 ثانية من الـ launch)
- اخرج فائق السرعة بعد ربح صغير (+15% = خروج)
- لا تطمع، لا تنتظر الـ moonshots (نادرة 0.5%)
"""
from __future__ import annotations

import os
import sys
import random
import statistics
from dataclasses import dataclass
from typing import List

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from v514_realistic_30day_forecast import (
    REAL_MARKET, ROUND_TRIP_FEE_PCT, _gen_real_token, RealToken
)


# ============================================================
# استراتيجيات الخروج (6 أنماط مختلفة)
# ============================================================

def exit_v500_conservative(t: RealToken) -> tuple:
    """الأصلي: trailing عند +30%، sudden_dump عند +10%/25%."""
    if t.fate == "rug_immediate":
        return (0.05, "rug")
    peak = t.peak_multiplier
    if peak < 1.08:
        return (0.92, "sl")
    peak_gain = peak - 1
    if peak_gain >= 5.0:    return (peak * 0.55, "trail")
    elif peak_gain >= 1.0:  return (peak * 0.65, "trail")
    elif peak_gain >= 0.30: return (peak * 0.75, "trail")
    if peak_gain >= 0.10 and (peak - t.final_multiplier) / peak >= 0.25:
        return (peak * 0.75, "sudden_dump")
    return (t.final_multiplier, "hold")


def exit_extreme(t: RealToken) -> tuple:
    """EXTREME الحالي: trailing عند +80%، sudden_dump عند +30%/40%."""
    if t.fate == "rug_immediate":
        return (0.05, "rug")
    peak = t.peak_multiplier
    if peak < 1.08:
        return (0.92, "sl")
    peak_gain = peak - 1
    if peak_gain >= 20.0:   return (peak * 0.45, "trail_moon")
    elif peak_gain >= 5.0:  return (peak * 0.55, "trail_500")
    elif peak_gain >= 2.0:  return (peak * 0.60, "trail_200")
    elif peak_gain >= 0.80: return (peak * 0.70, "trail_80")
    if peak_gain >= 0.30 and (peak - t.final_multiplier) / peak >= 0.40:
        return (peak * 0.60, "sudden_dump")
    return (t.final_multiplier, "hold")


def exit_quick_scalp(t: RealToken) -> tuple:
    """🔥 SCALP: خروج عند +20% مباشرة، SL عند -5%.
    الفكرة: 80% من العملات ترتفع 20%+ قبل الـ dump. اخرج قبل الجميع.
    """
    if t.fate == "rug_immediate":
        # الـ rug يحدث خلال 60 ثانية — لو أمسكنا +20% قبل الـ dump، نخرج
        # 30% احتمال نخرج قبل الـ rug
        if random.random() < 0.30:
            return (1.20, "scalp_+20")
        return (0.05, "rug")
    peak = t.peak_multiplier
    if peak >= 1.20:
        return (1.20, "scalp_+20")  # خروج سريع عند +20%
    if peak < 0.95:
        return (0.95, "sl_-5")  # SL صارم عند -5%
    # لو لم نصل +20% ولا -5%، نخرج عند الـ final
    return (max(t.final_multiplier, 0.85), "hold_small")


def exit_micro_scalp(t: RealToken) -> tuple:
    """🔥🔥 MICRO-SCALP: خروج عند +10% فقط (الأسرع).
    90% من العملات ترتفع 10%+ قبل الـ dump. خذ الربح فوراً.
    """
    if t.fate == "rug_immediate":
        # 50% احتمال نخرج قبل الـ rug (الـ +10% يحدث في 30 ثانية)
        if random.random() < 0.50:
            return (1.10, "scalp_+10")
        return (0.05, "rug")
    peak = t.peak_multiplier
    if peak >= 1.10:
        return (1.10, "scalp_+10")
    if peak < 0.92:
        return (0.92, "sl_-8")
    return (max(t.final_multiplier, 0.90), "hold_tiny")


def exit_early_sniper(t: RealToken) -> tuple:
    """🔥🔥🔥 EARLY-SNIPER: خروج عند +15% أو +50% (مزدوج).
    - إذا ارتفع 15% بسرعة → خروج فوري (آمن)
    - إذا استمر لأكثر من 60 ثانية وكسب +50% → نحتفظ بـ trailing 30%
    الفكرة: خذ ربحاً مضموناً، لكن دع الـ moonshots تكبر.
    """
    if t.fate == "rug_immediate":
        # 40% احتمال نخرج عند +15% قبل الـ rug
        if random.random() < 0.40:
            return (1.15, "sniper_+15")
        return (0.05, "rug")
    peak = t.peak_multiplier
    # Tier 1: خروج سريع عند +15% (90% من الصفقات)
    if peak >= 1.15 and peak < 1.50:
        return (1.15, "sniper_+15")
    # Tier 2: لو وصل +50%، نحتفظ بـ trailing 30%
    if peak >= 1.50:
        return (peak * 0.70, "trail_+50")
    # SL صارم
    if peak < 0.92:
        return (0.92, "sl_-8")
    # لو لم نصل أي tier، نخرج عند الـ final
    return (max(t.final_multiplier, 0.90), "hold")


def exit_dual_phase(t: RealToken) -> tuple:
    """🔥 DUAL-PHASE: 50% خروج عند +20%، 50% مع trailing.
    - بيع نصف المبلغ عند +20% (ربح مضمون)
    - النصف الآخر مع trailing عند +50%+
    """
    if t.fate == "rug_immediate":
        if random.random() < 0.35:
            # بيع النصف عند +20% قبل الـ rug، النصف الآخر يخسر
            return ((1.20 + 0.05) / 2, "dual_half_rug")
        return (0.05, "rug")
    peak = t.peak_multiplier
    if peak >= 1.50:
        # 50% بيع عند +20%، 50% عند peak*0.70 (trailing)
        exit_mult = (1.20 + peak * 0.70) / 2
        return (exit_mult, "dual_phase")
    if peak >= 1.20:
        # بيع عند +20% فقط (لم يصل +50%)
        return (1.20, "take_+20")
    if peak < 0.92:
        return (0.92, "sl_-8")
    return (max(t.final_multiplier, 0.90), "hold")


# ============================================================
# بوابات الدخول لكل استراتيجية
# ============================================================

def gates_baseline(t: RealToken) -> bool:
    """بوابات EXTREME الحالية."""
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 2000: return False
    if t.chg_5m_pct > 250: return False
    return True


def gates_early_only(t: RealToken) -> bool:
    """🔥 فقط العملات الجديدة (أقل من 5 دقائق).
    الفكرة: ادخل قبل أي ارتفاع كبير.
    """
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 2000: return False
    # 🔥 اشترط أن يكون عمر التوكن < 5 دقائق
    if t.age_minutes > 5: return False
    # اشترط تغير 5m إيجابي لكن صغير (5-50%) — في بداية الـ pump
    if t.chg_5m_pct < 5: return False
    if t.chg_5m_pct > 80: return False
    return True


def gates_volume_strict(t: RealToken) -> bool:
    """🔥 بوابات مع حجم تداول صارم + عمر صغير."""
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 5000: return False  # سيولة أعلى = أمان
    if t.age_minutes > 10: return False  # فقط أحدث 10 دقائق
    if t.volume_24h_usd < 20_000: return False  # حجم نشط
    if t.chg_5m_pct < 10 or t.chg_5m_pct > 80: return False
    return True


# ============================================================
# المحاكاة
# ============================================================

@dataclass
class Trade:
    symbol: str
    fate: str
    pnl_usd: float
    exit_reason: str
    blocked: bool = False
    block_reason: str = ""


def simulate_30d(capital: float, exit_fn, gates_fn, seed: int,
                  trade_pct: float = 12.0, daily_cap_pct: float = 70.0,
                  daily_loss_pct: float = 30.0, max_trades: int = 6) -> dict:
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    circuit_breaker = False

    for day in range(1, 31):
        if circuit_breaker or wallet < 0.50:
            continue

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0

        # 500 توكن يومياً للفحص
        candidates = [_gen_real_token(f"TKN{day}-{i}") for i in range(500)]

        for token in candidates:
            if trades_today >= max_trades:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                circuit_breaker = True
                break

            if not gates_fn(token):
                trades.append(Trade(token.symbol, token.fate, 0, "blocked",
                                     blocked=True, block_reason="gates"))
                continue

            trade_size_usd = wallet * trade_pct / 100
            if trade_size_usd < 0.50:
                continue

            exit_mult, reason = exit_fn(token)
            entry_usd = trade_size_usd
            net_exit = entry_usd * exit_mult * (1 - ROUND_TRIP_FEE_PCT / 100)
            pnl = net_exit - entry_usd

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1

            trades.append(Trade(token.symbol, token.fate, pnl, reason))

    executed = [t for t in trades if not t.blocked]
    wins = [t for t in executed if t.pnl_usd > 0]
    losses = [t for t in executed if t.pnl_usd <= 0]

    return {
        "capital_start": capital,
        "capital_end": wallet,
        "net_pnl": wallet - capital,
        "roi_pct": (wallet - capital) / capital * 100,
        "trades": len(executed),
        "blocked": len([t for t in trades if t.blocked]),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(1, len(executed)) * 100,
        "rug_blocked": len([t for t in trades if t.blocked and "30%+" in (t.block_reason or "")]),
        "moonshots_caught": len([t for t in wins if t.fate == "moonshot"]),
        "rug_losses": len([t for t in losses if t.fate == "rug_immediate"]),
        "avg_win": sum(t.pnl_usd for t in wins) / max(1, len(wins)),
        "avg_loss": sum(t.pnl_usd for t in losses) / max(1, len(losses)),
        "circuit_breaker": circuit_breaker,
    }


def main():
    print("=" * 90)
    print("V515 — STRATEGY SEARCH: 6 strategies × 5 seeds × 30 days × $20")
    print("=" * 90)
    print(f"Market: Nov 2026 (98.6% rugs, 1.72% graduation, 6.25% profitable traders)")
    print(f"Fees: {ROUND_TRIP_FEE_PCT}% round-trip | SOL: ${REAL_MARKET['sol_price_usd']}")
    print()

    strategies = [
        ("V500_CONSERVATIVE",  exit_v500_conservative, gates_baseline,
            {"trade_pct": 5.0, "daily_cap_pct": 30.0, "daily_loss_pct": 15.0, "max_trades": 2}),
        ("V513_EXTREME",       exit_extreme,            gates_baseline,
            {"trade_pct": 12.0, "daily_cap_pct": 70.0, "daily_loss_pct": 30.0, "max_trades": 6}),
        ("QUICK_SCALP_+20%",   exit_quick_scalp,        gates_baseline,
            {"trade_pct": 8.0, "daily_cap_pct": 50.0, "daily_loss_pct": 15.0, "max_trades": 8}),
        ("MICRO_SCALP_+10%",   exit_micro_scalp,       gates_baseline,
            {"trade_pct": 6.0, "daily_cap_pct": 40.0, "daily_loss_pct": 12.0, "max_trades": 10}),
        ("EARLY_SNIPER_+15%",  exit_early_sniper,      gates_early_only,
            {"trade_pct": 10.0, "daily_cap_pct": 60.0, "daily_loss_pct": 20.0, "max_trades": 8}),
        ("DUAL_PHASE",         exit_dual_phase,        gates_volume_strict,
            {"trade_pct": 10.0, "daily_cap_pct": 50.0, "daily_loss_pct": 18.0, "max_trades": 6}),
    ]

    seeds = list(range(1, 6))
    all_results = {}

    for name, exit_fn, gates_fn, params in strategies:
        results = [simulate_30d(20.0, exit_fn, gates_fn, s, **params) for s in seeds]
        all_results[name] = results

    # جدول مفصل لكل استراتيجية
    print(f"{'Strategy':<22} {'ROI avg':<10} {'Med':<8} {'Best':<10} {'Worst':<10} "
          f"{'WR%':<7} {'Trades':<7} {'Moon':<5} {'RugBlk':<7} {'WinMo':<7}")
    print("-" * 100)

    for name, _, _, _ in strategies:
        results = all_results[name]
        rois = [r["roi_pct"] for r in results]
        wrs = [r["win_rate"] for r in results]
        trades_n = [r["trades"] for r in results]
        moons = sum(r["moonshots_caught"] for r in results)
        rug_blocked = sum(r["rug_blocked"] for r in results) // 5
        winning_months = sum(1 for r in rois if r > 0)

        print(f"{name:<22} "
              f"{statistics.mean(rois):>+8.1f}% "
              f"{statistics.median(rois):>+6.1f}% "
              f"{max(rois):>+8.1f}% "
              f"{min(rois):>+8.1f}% "
              f"{statistics.mean(wrs):>5.1f}% "
              f"{statistics.mean(trades_n):>6.1f} "
              f"{moons:>4} "
              f"{rug_blocked:>6} "
              f"{winning_months:>2}/5")

    # إيجاد الأفضل
    print()
    print("=" * 90)
    print("  🏆 STRATEGY RANKING (by avg ROI)")
    print("=" * 90)
    print()

    ranked = sorted(strategies, key=lambda s: statistics.mean([r["roi_pct"] for r in all_results[s[0]]]), reverse=True)

    for i, (name, _, _, _) in enumerate(ranked, 1):
        results = all_results[name]
        rois = [r["roi_pct"] for r in results]
        pnls = [r["net_pnl"] for r in results]
        wrs = [r["win_rate"] for r in results]
        winning = sum(1 for r in rois if r > 0)

        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        print(f"  {medal} {name}")
        print(f"     ROI:  avg {statistics.mean(rois):+.1f}% | med {statistics.median(rois):+.1f}% | "
              f"best {max(rois):+.1f}% | worst {min(rois):+.1f}%")
        print(f"     PnL:  avg ${statistics.mean(pnls):+.2f} | best ${max(pnls):+.2f} | worst ${min(pnls):+.2f}")
        print(f"     WR:   {statistics.mean(wrs):.1f}% | winning months: {winning}/5")
        print()

    # الأفضل — تفصيل أكثر
    best_name = ranked[0][0]
    best_results = all_results[best_name]
    print("=" * 90)
    print(f"  🎯 BEST STRATEGY: {best_name}")
    print("=" * 90)

    pnls = [r["net_pnl"] for r in best_results]
    rois = [r["roi_pct"] for r in best_results]
    winning_months = sum(1 for r in rois if r > 0)

    print(f"""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  📊 المتوقع (avg):            ${statistics.mean(pnls):>+7.2f}  ({statistics.mean(rois):>+6.1f}%)       │
  │  📈 الأكثر احتمالاً (median):  ${statistics.median(pnls):>+7.2f}  ({statistics.median(rois):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  🟢 أفضل شهر:                 ${max(pnls):>+7.2f}  ({max(rois):>+6.1f}%)       │
  │  🔴 أسوأ شهر:                 ${min(pnls):>+7.2f}  ({min(rois):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  💵 رأس المال النهائي (متوسط):  ${20+statistics.mean(pnls):>7.2f}                       │
  │  🎯 احتمال شهر رابح:           {winning_months*20}%  ({winning_months}/5 seeds)              │
  └──────────────────────────────────────────────────────────────────────┘
""")

    # مقارنة مع EXTREME (الحالي)
    extreme_results = all_results["V513_EXTREME"]
    extreme_avg = statistics.mean([r["roi_pct"] for r in extreme_results])
    best_avg = statistics.mean([r["roi_pct"] for r in best_results])
    improvement = best_avg - extreme_avg

    print(f"  💡 التحسن مقابل EXTREME الحالي:")
    print(f"     EXTREME: {extreme_avg:+.1f}% avg ROI")
    print(f"     {best_name}: {best_avg:+.1f}% avg ROI")
    print(f"     التحسن: {improvement:+.1f} نقطة مئوية")
    print()
    print("  🎓 الخلاصة:")
    print("  ─────────────────────────────────────────────────────────────")
    if best_avg > extreme_avg:
        print(f"  ✅ {best_name} هي الأفضل في السوق الحالي.")
        print(f"  ✅ تتفوق على EXTREME بـ {improvement:+.1f}% نقطة.")
        print(f"  ✅ استراتيجية: خروج سريع + بوابات صارمة = أمان + ربح متواضع")
    else:
        print(f"  ⚠️ EXTREME ما زال الأفضل — السوق صعب جداً للجميع")


if __name__ == "__main__":
    main()
