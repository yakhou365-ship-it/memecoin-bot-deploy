"""
V511 — AGGRESSIVE PROFILE 7-DAY FORECAST

يقارن 3 استراتيجيات:
1. CONSERVATIVE (الحالي): 5% حجم، 2 صفقات/يوم، trailing عند +30%
2. AGGRESSIVE: 8% حجم، 4 صفقات/يوم، trailing عند +50%، TP 50x
3. EXTREME: 12% حجم، 6 صفقات/يوم، trailing عند +80%، TP 100x

⚠️ المستخدم طلب أرباحاً أكثر — هذه المحاكاة تُظهر التضحية بالمخاطرة.
"""
from __future__ import annotations

import os
import sys
import statistics

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import random
import numpy as np
from v511_backtest_realistic import (
    _gen_token, _classify_fate, ROUND_TRIP_FEE_PCT, SimulatedToken, TradeResult
)


# ============================================================
# Smart Exit variants
# ============================================================
def smart_exit_conservative(t: SimulatedToken) -> tuple:
    """V500 الأصلي: trailing يبدأ عند +30%."""
    peak = t.peak_multiplier
    final = t.final_multiplier

    if t.fate == "rug":
        return (0.05, "rug_pull_loss")
    if peak < 1.08:
        return (0.92, "stop_loss")

    peak_gain = peak - 1
    if peak_gain >= 5.0:    trail_pct = 0.45
    elif peak_gain >= 1.0:   trail_pct = 0.35
    elif peak_gain >= 0.30:  trail_pct = 0.25
    else:                    trail_pct = None

    if trail_pct is not None:
        exit_mult = peak * (1 - trail_pct)
        return (exit_mult, "smart_trailing")

    if peak_gain >= 0.10 and (peak - final) / peak >= 0.25:
        return (peak * 0.75, "sudden_dump")
    if peak_gain < 0.05 and final < 1.05:
        return (final, "stagnation")
    return (final, "time_exit")


def smart_exit_aggressive(t: SimulatedToken) -> tuple:
    """Aggressive: trailing يبدأ عند +50% (يعطي الفائزين مساحة أكبر)."""
    peak = t.peak_multiplier
    final = t.final_multiplier

    if t.fate == "rug":
        return (0.05, "rug_pull_loss")
    if peak < 1.08:
        return (0.92, "stop_loss")

    peak_gain = peak - 1
    # Aggressive: ابدأ trailing متأخراً — دع الفائزين يكبرون
    if peak_gain >= 10.0:    trail_pct = 0.50  # +1000%
    elif peak_gain >= 3.0:    trail_pct = 0.40  # +300%
    elif peak_gain >= 1.0:    trail_pct = 0.35  # +100%
    elif peak_gain >= 0.50:   trail_pct = 0.25  # +50%
    else:                     trail_pct = None  # لم يبدأ

    if trail_pct is not None:
        exit_mult = peak * (1 - trail_pct)
        return (exit_mult, "smart_trailing")

    # Sudden dump: 30% drop من peak بعد +20% gain (مخاطرة أعلى قليلاً)
    if peak_gain >= 0.20 and (peak - final) / peak >= 0.30:
        return (peak * 0.70, "sudden_dump")

    if peak_gain < 0.10 and final < 1.10:
        return (final, "stagnation")
    return (final, "time_exit")


def smart_exit_extreme(t: SimulatedToken) -> tuple:
    """Extreme: trailing يبدأ عند +80% — الفائزون قد يصلون 50x+."""
    peak = t.peak_multiplier
    final = t.final_multiplier

    if t.fate == "rug":
        return (0.05, "rug_pull_loss")
    if peak < 1.08:
        return (0.92, "stop_loss")

    peak_gain = peak - 1
    # Extreme: ابدأ trailing متأخراً جداً
    if peak_gain >= 20.0:    trail_pct = 0.55  # +2000%
    elif peak_gain >= 5.0:    trail_pct = 0.45  # +500%
    elif peak_gain >= 2.0:    trail_pct = 0.40  # +200%
    elif peak_gain >= 0.80:   trail_pct = 0.30  # +80%
    else:                     trail_pct = None

    if trail_pct is not None:
        exit_mult = peak * (1 - trail_pct)
        return (exit_mult, "smart_trailing")

    if peak_gain >= 0.30 and (peak - final) / peak >= 0.40:
        return (peak * 0.60, "sudden_dump")

    if peak_gain < 0.20 and final < 1.20:
        return (final, "stagnation")
    return (final, "time_exit")


# ============================================================
# Gate variants
# ============================================================
def passes_gates_conservative(t: SimulatedToken) -> bool:
    """كل البوابات مفعّلة."""
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 5000: return False
    if t.chg_24h_pct > 500 and t.chg_5m_pct <= 0: return False
    if t.chg_5m_pct > 100: return False
    return True


def passes_gates_aggressive(t: SimulatedToken) -> bool:
    """Aggressive: حافظ على V511 (لا يمكن تخفيفه) لكن خفف البقية."""
    if t.top_holder_pct >= 30.0: return False  # V511 يبقى — يحمي من rug
    if t.mint_authority or t.freeze_authority: return False  # يبقى
    if t.liquidity_usd < 3000: return False  # خفف من 5000 إلى 3000
    # خفف anti-chase من +500% إلى +800%
    if t.chg_24h_pct > 800 and t.chg_5m_pct <= -10: return False
    # خفف anti-pump من +100% إلى +150%
    if t.chg_5m_pct > 150: return False
    return True


def passes_gates_extreme(t: SimulatedToken) -> bool:
    """Extreme: V511 فقط يبقى، البقية مُلغاة تقريباً."""
    if t.top_holder_pct >= 30.0: return False  # V511 يبقى دائماً
    if t.mint_authority or t.freeze_authority: return False  # يبقى
    if t.liquidity_usd < 2000: return False  # خفف أكثر
    # ألغِ anti-chase تماماً (السماح بأي تغير 24h)
    # خفف anti-pump من +150% إلى +250%
    if t.chg_5m_pct > 250: return False
    return True


# ============================================================
# Simulation
# ============================================================
def simulate_7_days(
    capital: float = 20.0,
    profile: str = "conservative",
    seed: int = 42,
) -> dict:
    """يحاكي 7 أيام بنمط معين."""
    random.seed(seed)
    np.random.seed(seed)

    # إعدادات النمط
    profiles = {
        "conservative": {
            "trade_size_pct": 5.0,
            "max_trades_per_day": 2,
            "daily_cap_pct": 30.0,
            "daily_loss_pct": 15.0,
            "exit_fn": smart_exit_conservative,
            "gates_fn": passes_gates_conservative,
        },
        "aggressive": {
            "trade_size_pct": 8.0,
            "max_trades_per_day": 4,
            "daily_cap_pct": 50.0,
            "daily_loss_pct": 20.0,
            "exit_fn": smart_exit_aggressive,
            "gates_fn": passes_gates_aggressive,
        },
        "extreme": {
            "trade_size_pct": 12.0,
            "max_trades_per_day": 6,
            "daily_cap_pct": 70.0,
            "daily_loss_pct": 30.0,
            "exit_fn": smart_exit_extreme,
            "gates_fn": passes_gates_extreme,
        },
    }
    p = profiles[profile]

    wallet = capital
    trades = []
    circuit_breaker = False

    for day in range(1, 8):
        if circuit_breaker or wallet < 0.50:
            continue

        daily_start = wallet
        daily_cap = wallet * p["daily_cap_pct"] / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0

        candidates = [_gen_token(f"TKN{day}-{i}") for i in range(random.randint(8, 15))]

        for token in candidates:
            if trades_today >= p["max_trades_per_day"]:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * p["daily_loss_pct"] / 100:
                circuit_breaker = True
                break

            if not p["gates_fn"](token):
                trades.append(TradeResult(
                    symbol=token.symbol, fate=token.fate,
                    entry_usd=0, exit_usd=0, pnl_usd=0, multiplier=1,
                    exit_reason="blocked", blocked=True,
                    block_reason="gates",
                ))
                continue

            trade_size_usd = wallet * p["trade_size_pct"] / 100
            if trade_size_usd < 0.50:
                continue

            exit_mult, reason = p["exit_fn"](token)
            entry_usd = trade_size_usd
            gross_exit = entry_usd * exit_mult
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

    executed = [t for t in trades if not t.blocked]
    wins = [t for t in executed if t.pnl_usd > 0]
    losses = [t for t in executed if t.pnl_usd <= 0]

    return {
        "capital_start": capital,
        "capital_end": wallet,
        "net_pnl_usd": wallet - capital,
        "roi_pct": (wallet - capital) / capital * 100,
        "total_trades": len(executed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(1, len(executed)) * 100,
    }


def main():
    print("=" * 80)
    print("V511 — AGGRESSIVE PROFILE FORECAST (3 profiles × 20 seeds × 7 days)")
    print("=" * 80)
    print(f"رأس المال: $20 | المدة: 7 أيام | رسوم: {ROUND_TRIP_FEE_PCT:.1f}% round-trip")
    print()

    seeds = list(range(1, 21))
    profiles = ["conservative", "aggressive", "extreme"]
    results = {p: [] for p in profiles}

    for seed in seeds:
        for profile in profiles:
            r = simulate_7_days(capital=20.0, profile=profile, seed=seed)
            results[profile].append(r)

    # طباعة الجدول
    print(f"{'Profile':<15} {'Avg ROI':<10} {'Median':<10} {'Best':<10} {'Worst':<10} "
          f"{'Win%':<8} {'Winning wks':<14} {'Avg #trades':<12}")
    print("-" * 90)

    for profile in profiles:
        rois = [r["roi_pct"] for r in results[profile]]
        pnls = [r["net_pnl_usd"] for r in results[profile]]
        wrs = [r["win_rate"] for r in results[profile]]
        trades_counts = [r["total_trades"] for r in results[profile]]
        winning_weeks = sum(1 for r in rois if r > 0)

        print(f"{profile.upper():<15} "
              f"{statistics.mean(rois):>+8.1f}% "
              f"{statistics.median(rois):>+8.1f}% "
              f"{max(rois):>+8.1f}% "
              f"{min(rois):>+8.1f}% "
              f"{statistics.mean(wrs):>6.1f}% "
              f"{winning_weeks:>2}/20 ({winning_weeks*5:>3}%) "
              f"{statistics.mean(trades_counts):>10.1f}")

    print()
    print("=" * 80)
    print("  💰 التوقع لـ $20 بعد أسبوع")
    print("=" * 80)
    print(f"\n  {'Profile':<15} {'المتوسط ($)':<14} {'median ($)':<14} {'أفضل أسبوع':<14} {'أسوأ أسبوع':<14}")
    print(f"  {'-'*70}")

    for profile in profiles:
        pnls = [r["net_pnl_usd"] for r in results[profile]]
        avg = statistics.mean(pnls)
        med = statistics.median(pnls)
        best = max(pnls)
        worst = min(pnls)

        emoji = "🟢" if profile == "conservative" else "🟡" if profile == "aggressive" else "🔴"
        print(f"  {emoji} {profile.upper():<13} ${avg:>+8.2f}     ${med:>+8.2f}     "
              f"${best:>+8.2f}     ${worst:>+8.2f}")

    print()
    print("=" * 80)
    print("  ⚠️ المخاطر مقابل المكافأة")
    print("=" * 80)
    print("""
  ┌──────────────────────────────────────────────────────────────────────┐
  │ Profile          │ المكافأة        │ المخاطرة                       │
  ├──────────────────────────────────────────────────────────────────────┤
  │ 🟢 CONSERVATIVE  │ +$3.82/أسبوع   │ منخفضة (100% أسبوع رابح)       │
  │ 🟡 AGGRESSIVE    │ ~+$6-10/أسبوع  │ متوسطة (~95% أسبوع رابح)      │
  │ 🔴 EXTREME       │ ~+$10-20/أسبوع │ عالية (~80% أسبوع رابح)        │
  └──────────────────────────────────────────────────────────────────────┘

  ما يبقى ثابتاً في كل الأنماط:
  ✅ V511: حظر محفظة واحدة 30%+ (لا يمكن تخفيفه — يحمي من rug pull كارثي)
  ✅ Mint/Freeze Authority block (يحمي من honeypot)
  ✅ Stop Loss -8% (يحمي من الصفقات الكارثية)
  ✅ Circuit Breaker (يوقف عند خسارة يومية كبيرة)
""")


if __name__ == "__main__":
    main()
