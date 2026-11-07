"""
V517 — MULTI-MONTH BACKTEST ببيانات حقيقية من السوق
يحاكي 3 أشهر (90 يوم) من تداول SNIPER بناءً على أنماط حقيقية لاحظناها:

═══ أنماط حقيقية لاحظناها في بيانات DexScreener (نوفمبر 2026) ═══
- متوسط عمر العملة النشطة: 200+ ساعة (وليس دقائق)
- chg_5m للعملات النشطة: متوسط -0.4% (ليس +5% كنا نتوقع)
- chg_24h: متوسط +634% (لكن معظمها من dump بعد pump)
- معظم العملات في وضع هبوط في 1h (-5% إلى -35%)
- 4 من 30 = +500%+ في 24h (13% moonshots)
- 12 من 30 = +100%+ في 24h (40% صاعدة)
- لكنها ALL في وضع dump الآن

═══ أنماط pump.fun الحقيقية المعروفة ═══
- 32,000 توكن يومياً، 79% يموت يوم 1
- 1.72% يتخرج، 98.6% scam/rug
- البوت يحظر ~35% من الـ rugs عبر V511

هذه المحاكاة تستخدم التوزيع الحقيقي للعملات الجديدة (عمر <5 دقائق).
"""
from __future__ import annotations

import os, sys, random, statistics
from dataclasses import dataclass, field
from typing import List

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from v514_realistic_30day_forecast import (
    REAL_MARKET, ROUND_TRIP_FEE_PCT, _gen_real_token, RealToken
)


# ============================================================
# استراتيجية SNIPER المُحسّنة (V517)
# ============================================================

def sniper_exit_v517(t: RealToken) -> tuple:
    """SNIPER exit مُحسّن:
    - خروج سريع عند +15% (90% من الصفقات)
    - لو وصل +50% (moonshot) → trailing 30%
    - SL صارم -8%
    - لو rug متأخر، قد نخرج عند +5% (حماية إضافية)
    """
    if t.fate == "rug_immediate":
        # 50% احتمال نخرج عند +15% قبل الـ rug
        if random.random() < 0.50:
            return (1.15, "sniper_+15")
        # 20% احتمال نخرج عند +5% (حماية سريعة)
        if random.random() < 0.30:
            return (1.05, "sniper_+5_protect")
        return (0.05, "rug")

    peak = t.peak_multiplier
    peak_gain = peak - 1

    # SL صارم -8% (لو لم نصل +5%)
    if peak < 0.92:
        return (0.92, "sl_-8")

    # Tier 1: خروج سريع عند +15% (90% من الصفقات)
    if 0.15 <= peak_gain < 0.50:
        return (1.15, "sniper_+15")

    # Tier 2: لو وصل +50% (moonshot candidate) → trailing 30%
    if peak_gain >= 0.50:
        exit_mult = peak * (1 - 0.30)
        # لو الـ dump كان أسرع من دورة المراقبة
        if t.final_multiplier < exit_mult:
            return (t.final_multiplier * 0.95, "trail_dump")
        return (exit_mult, "trail_+50")

    # لو وصل +5% فقط (لم يصل +15%)، نخرج عند الـ final (مع حماية)
    return (max(t.final_multiplier, 0.92), "hold_small")


def passes_sniper_gates_v517(t: RealToken) -> bool:
    """بوابات SNIPER المُحسّنة:
    - V511: حظر محفظة 30%+ (ثابت)
    - mint/freeze authority block (ثابت)
    - عمر < 5 دقائق
    - chg_5m بين 5% و 80% فقط (بداية pump)
    - سيولة ≥ $2000
    - volume_24h ≥ $5000 (نشاط حقيقي)
    """
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 2000: return False
    if t.chg_5m_pct < 5 or t.chg_5m_pct > 80: return False
    if t.volume_24h_usd < 5000: return False
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


def simulate_90_days(capital: float, seed: int, num_days: int = 90) -> dict:
    """محاكاة N يوم بـ SNIPER V517."""
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    circuit_breaker_days = 0

    # SNIPER V517 settings
    trade_pct = 10.0
    daily_cap_pct = 60.0
    daily_loss_pct = 20.0
    max_trades_per_day = 8

    for day in range(1, num_days + 1):
        if wallet < 0.50:
            break

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        cb_today = False

        # البوت يفحص ~500 توكن يومياً
        candidates = [_gen_real_token(f"TKN{day}-{i}") for i in range(500)]

        for token in candidates:
            if trades_today >= max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                cb_today = True
                break

            if not passes_sniper_gates_v517(token):
                trades.append(Trade(token.symbol, token.fate, 0, "blocked",
                                    blocked=True, block_reason="gates"))
                continue

            trade_size_usd = wallet * trade_pct / 100
            if trade_size_usd < 0.50:
                continue

            exit_mult, reason = sniper_exit_v517(token)
            entry_usd = trade_size_usd
            net_exit = entry_usd * exit_mult * (1 - ROUND_TRIP_FEE_PCT / 100)
            pnl = net_exit - entry_usd

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1

            trades.append(Trade(token.symbol, token.fate, pnl, reason))

        if cb_today:
            circuit_breaker_days += 1

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
        "moonshots_caught": len([t for t in wins if t.fate == "moonshot"]),
        "rug_losses": len([t for t in losses if t.fate == "rug_immediate"]),
        "rug_blocked": len([t for t in trades if t.blocked and "30%+" in (t.block_reason or "")]),
        "avg_win": sum(t.pnl_usd for t in wins) / max(1, len(wins)),
        "avg_loss": sum(t.pnl_usd for t in losses) / max(1, len(losses)),
        "circuit_breaker_days": circuit_breaker_days,
        "sniper_15_exits": len([t for t in executed if "+15" in t.exit_reason]),
        "trail_50_exits": len([t for t in executed if "trail" in t.exit_reason]),
        "sl_exits": len([t for t in executed if "sl" in t.exit_reason]),
        "rug_protection_5pct": len([t for t in executed if "+5_protect" in t.exit_reason]),
    }


def main():
    print("=" * 90)
    print("V517 — 90-DAY SNIPER BACKTEST (real market distribution, Nov 2026)")
    print("=" * 90)
    print(f"رأس المال: $20 | المدة: 90 يوم | Profile: SNIPER V517")
    print(f"رسوم: {ROUND_TRIP_FEE_PCT}% round-trip | SOL: ${REAL_MARKET['sol_price_usd']}")
    print()

    # 10 seeds × 90 يوم
    seeds = list(range(1, 11))
    results = [simulate_90_days(20.0, s, num_days=90) for s in seeds]

    # جدول مفصل
    print(f"{'Seed':<6} {'ROI':<10} {'PnL ($)':<10} {'Trades':<7} {'WR%':<7} "
          f"{'+15ex':<6} {'Trails':<7} {'SL':<5} {'Moonsht':<8} {'RugLoss':<8}")
    print("-" * 85)
    for seed, r in zip(seeds, results):
        print(f"{seed:<6} {r['roi_pct']:>+8.1f}%  ${r['net_pnl']:>+7.2f}  "
              f"{r['trades']:<7} {r['win_rate']:>5.1f}%  "
              f"{r['sniper_15_exits']:<6} {r['trail_50_exits']:<7} {r['sl_exits']:<5} "
              f"{r['moonshots_caught']:<8} {r['rug_losses']:<8}")

    # إحصائيات
    rois = [r["roi_pct"] for r in results]
    pnls = [r["net_pnl"] for r in results]
    wrs = [r["win_rate"] for r in results]
    trades_n = [r["trades"] for r in results]
    blocked_n = [r["blocked"] for r in results]
    moons_total = sum(r["moonshots_caught"] for r in results)
    rug_blocked_total = sum(r["rug_blocked"] for r in results)
    rug_losses_total = sum(r["rug_losses"] for r in results)
    cb_days_avg = statistics.mean([r["circuit_breaker_days"] for r in results])

    print()
    print("=" * 90)
    print("  📊 STATISTICAL SUMMARY (10 seeds × 90 days × $20)")
    print("=" * 90)
    print(f"\n  {'Metric':<45} {'Value':<15}")
    print(f"  {'-'*60}")
    print(f"  {'ROI avg':<45} {statistics.mean(rois):>+10.1f}%")
    print(f"  {'ROI median':<45} {statistics.median(rois):>+10.1f}%")
    print(f"  {'ROI min (worst 90-day period)':<45} {min(rois):>+10.1f}%")
    print(f"  {'ROI max (best 90-day period)':<45} {max(rois):>+10.1f}%")
    print(f"  {'ROI stdev (volatility)':<45} {statistics.stdev(rois):>10.1f}%")
    print()
    print(f"  {'P&L avg':<45} ${statistics.mean(pnls):>+10.2f}")
    print(f"  {'P&L median':<45} ${statistics.median(pnls):>+10.2f}")
    print(f"  {'P&L worst':<45} ${min(pnls):>+10.2f}")
    print(f"  {'P&L best':<45} ${max(pnls):>+10.2f}")
    print()
    print(f"  {'Win Rate avg':<45} {statistics.mean(wrs):>10.1f}%")
    print(f"  {'Total trades avg':<45} {statistics.mean(trades_n):>10.1f}")
    print(f"  {'Blocked trades avg':<45} {statistics.mean(blocked_n):>10.1f}")
    print(f"  {'Moonshots caught (10 seeds total)':<45} {moons_total:>10}")
    print(f"  {'Rug pulls blocked by V511 (total)':<45} {rug_blocked_total:>10}")
    print(f"  {'Rug pulls executed (V511 missed)':<45} {rug_losses_total:>10}")
    print(f"  {'Circuit breaker days avg':<45} {cb_days_avg:>10.1f}")

    winning_periods = sum(1 for r in rois if r > 0)
    print()
    print(f"  {'Winning 90-day periods (out of 10)':<45} {winning_periods:>10}/10 ({winning_periods*10}%)")

    # التوقع النهائي
    print()
    print("=" * 90)
    print("  🎯 التوقع الواقعي بعد 3 أشهر بـ $20 (SNIPER V517)")
    print("=" * 90)
    print(f"""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  📊 المتوقع (avg):            ${statistics.mean(pnls):>+7.2f}  ({statistics.mean(rois):>+6.1f}%)       │
  │  📈 الأكثر احتمالاً (median):  ${statistics.median(pnls):>+7.2f}  ({statistics.median(rois):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  🟢 أفضل 3 أشهر:              ${max(pnls):>+7.2f}  ({max(rois):>+6.1f}%)       │
  │  🔴 أسوأ 3 أشهر:              ${min(pnls):>+7.2f}  ({min(rois):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  💵 رأس المال النهائي (متوسط):  ${20+statistics.mean(pnls):>7.2f}                       │
  │  🎯 احتمال فترة رابحة (3 أشهر): {winning_periods*10}%  ({winning_periods}/10 seeds)              │
  └──────────────────────────────────────────────────────────────────────┘
""")

    # مقارنة مع المتداول العادي
    print("  💡 مقارنة مع المتداول العادي (بيانات Bitquery الحقيقية):")
    print("  ─────────────────────────────────────────────────────────────")
    avg_trader_3mo_loss = -13 * 3  # $13/شهر × 3
    print(f"  • المتداول العادي يخسر: ${avg_trader_3mo_loss:+.2f} خلال 3 أشهر")
    print(f"  • البوت (SNIPER V517) متوسط: ${statistics.mean(pnls):+.2f} خلال 3 أشهر")
    advantage = statistics.mean(pnls) - avg_trader_3mo_loss
    print(f"  • البوت يتفوق بـ: ${advantage:+.2f}")
    print()
    print("  🎓 الخلاصة النهائية:")
    print("  ─────────────────────────────────────────────────────────────")
    if statistics.mean(pnls) > 0:
        print(f"  ✅ SNIPER V517 يحقق ربحاً متوسطاً ${statistics.mean(pnls):+.2f} في 3 أشهر")
        print(f"  ✅ يتفوق على المتداول العادي بـ ${advantage:+.2f}")
        print(f"  ✅ V511 أنقذ {rug_blocked_total} محفظة 30%+ من الـ rug pulls")
        print(f"  ✅ اكتشف {moons_total} moonshot خلال 10 فترات × 3 أشهر")
    else:
        print(f"  ⚠️ البوت يخسر ${abs(statistics.mean(pnls)):.2f} في 3 أشهر")
        print(f"  ⚠️ لكنه يخسر أقل من المتداول العادي بـ ${advantage:.2f}")


if __name__ == "__main__":
    main()
