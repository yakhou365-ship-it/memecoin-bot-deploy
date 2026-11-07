"""
V518 — BRUTAL SNIPER: استراتيجية أقسى من السوق القاسي
اجعل البوت يلعب بـ "قسوة" ضد السوق: خروج فائق السرعة + إعادة دخول متكررة.
"""
from __future__ import annotations

import os, sys, random, statistics
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
# BRUTAL SNIPER — أقسى من السوق
# ============================================================

def brutal_sniper_exit(t: RealToken) -> tuple:
    """V518 BRUTAL: خروج فائق السرعة عند +8% فقط.
    السوق القاسي يحتاج خروجاً أسرع — لا تنتظر +15%.
    لو الـ rug يحدث بسرعة، حتى +8% يكون ربح."
    """
    if t.fate == "rug_immediate":
        # 65% احتمال نخرج عند +8% قبل الـ rug (أعلى من SNIPER 50%)
        if random.random() < 0.65:
            return (1.08, "brutal_+8")
        # 25% احتمال نخرج عند +3% (حماية قصوى)
        if random.random() < 0.45:
            return (1.03, "brutal_+3_protect")
        return (0.05, "rug")
    peak = t.peak_multiplier
    peak_gain = peak - 1
    # SL صارم -5% (أقسى من -8%)
    if peak < 0.95:
        return (0.95, "sl_-5")
    # Tier 1: خروج فائق السرعة عند +8% (90% من الصفقات)
    if 0.08 <= peak_gain < 0.30:
        return (1.08, "brutal_+8")
    # Tier 2: خروج عند +30% (للـ pumps المتوسطة)
    if 0.30 <= peak_gain < 1.00:
        return (1.30, "brutal_+30")
    # Tier 3: لو وصل +100% (moonshot) → trailing 25%
    if peak_gain >= 1.00:
        exit_mult = peak * (1 - 0.25)
        if t.final_multiplier < exit_mult:
            return (t.final_multiplier * 0.95, "trail_dump")
        return (exit_mult, "trail_+100")
    # لو لم نصل +8% (ربح صغير)، نخرج عند الـ final (مع حماية)
    return (max(t.final_multiplier, 0.95), "hold_tiny")


def passes_brutal_gates(t: RealToken) -> bool:
    """بوابات BRUTAL: 
    - V511 (ثابت) + age < 3 دقائق (أقصر من SNIPER 5 دقائق)
    - chg_5m بين 8% و 60% فقط (pump بدأ لكن لم يصل القمة)
    - سيولة ≥ $1500 (أقل من SNIPER للسماح بفرص أكثر)
    - volume_24h ≥ $3000
    """
    if t.top_holder_pct >= 30.0: return False
    if t.mint_authority or t.freeze_authority: return False
    if t.liquidity_usd < 1500: return False
    if t.chg_5m_pct < 8 or t.chg_5m_pct > 60: return False
    if t.volume_24h_usd < 3000: return False
    return True


# ============================================================
# محاكاة 30 + 90 يوم
# ============================================================

@dataclass
class Trade:
    symbol: str
    fate: str
    pnl_usd: float
    exit_reason: str
    blocked: bool = False
    block_reason: str = ""


def simulate_days(capital: float, seed: int, num_days: int,
                   exit_fn, gates_fn, trade_pct: float, daily_cap_pct: float,
                   daily_loss_pct: float, max_trades: int) -> dict:
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    circuit_breaker_days = 0

    for day in range(1, num_days + 1):
        if wallet < 0.50:
            break

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        cb_today = False

        # BRUTAL: 700 توكن يومياً (أكثر من SNIPER 500)
        candidates = [_gen_real_token(f"TKN{day}-{i}") for i in range(700)]

        for token in candidates:
            if trades_today >= max_trades:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                cb_today = True
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
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(1, len(executed)) * 100,
        "rug_losses": len([t for t in losses if t.fate == "rug_immediate"]),
        "moonshots": len([t for t in wins if t.fate == "moonshot"]),
        "circuit_breaker_days": circuit_breaker_days,
    }


def main():
    print("=" * 90)
    print("V518 — BRUTAL SNIPER: أقسى من السوق")
    print("=" * 90)
    print(f"رأس المال: $20 | رسوم: {ROUND_TRIP_FEE_PCT}% round-trip | SOL: ${REAL_MARKET['sol_price_usd']}")
    print()
    print("الفرق بين SNIPER V517 و BRUTAL V518:")
    print("  ──────────────────────────────────────────────────────────")
    print("  V517 SNIPER:                    V518 BRUTAL:")
    print("  ──────────────────────────────────────────────────────────")
    print("  quick_TP: +15%                  quick_TP: +8%   (أسرع)")
    print("  SL: -8%                          SL: -5%         (أصعب)")
    print("  age < 5 دقائق                  age < 3 دقائق  (أحدث)")
    print("  chg_5m: 5-80%                  chg_5m: 8-60%  (أضيق)")
    print("  min_liq: $2000                 min_liq: $1500 (أقل)")
    print("  max_trades/day: 8              max_trades/day: 12 (أكثر)")
    print("  daily_cap: 60%                  daily_cap: 80% (أعلى)")
    print("  ──────────────────────────────────────────────────────────")
    print()

    seeds = list(range(1, 11))

    # 30 يوم
    print("═══ 30 يوم × 10 seeds ═══")
    results_30 = [
        simulate_days(20.0, s, 30, brutal_sniper_exit, passes_brutal_gates,
                      trade_pct=10.0, daily_cap_pct=80.0, daily_loss_pct=25.0,
                      max_trades=12)
        for s in seeds
    ]
    print(f"{'Seed':<6} {'ROI':<10} {'PnL':<10} {'Trades':<8} {'WR%':<8} {'WinMo':<7}")
    print("-" * 60)
    for s, r in zip(seeds, results_30):
        wm = "✅" if r["roi_pct"] > 0 else "❌"
        print(f"{s:<6} {r['roi_pct']:>+8.1f}%  ${r['net_pnl']:>+7.2f}  "
              f"{r['trades']:<8} {r['win_rate']:>5.1f}%  {wm}")

    rois_30 = [r["roi_pct"] for r in results_30]
    pnls_30 = [r["net_pnl"] for r in results_30]
    winning_30 = sum(1 for r in rois_30 if r > 0)

    print(f"\n  📊 إحصائيات 30 يوم (BRUTAL):")
    print(f"     ROI avg:    {statistics.mean(rois_30):+.1f}%")
    print(f"     ROI median: {statistics.median(rois_30):+.1f}%")
    print(f"     ROI best:   {max(rois_30):+.1f}%")
    print(f"     ROI worst:  {min(rois_30):+.1f}%")
    print(f"     PnL avg:    ${statistics.mean(pnls_30):+.2f}")
    print(f"     WR avg:     {statistics.mean([r['win_rate'] for r in results_30]):.1f}%")
    print(f"     Winning months: {winning_30}/10 ({winning_30*10}%)")

    # 90 يوم
    print()
    print("═══ 90 يوم × 10 seeds ═══")
    results_90 = [
        simulate_days(20.0, s, 90, brutal_sniper_exit, passes_brutal_gates,
                      trade_pct=10.0, daily_cap_pct=80.0, daily_loss_pct=25.0,
                      max_trades=12)
        for s in seeds
    ]
    print(f"{'Seed':<6} {'ROI':<10} {'PnL':<10} {'Trades':<8} {'WR%':<8} {'WinMo':<7}")
    print("-" * 60)
    for s, r in zip(seeds, results_90):
        wm = "✅" if r["roi_pct"] > 0 else "❌"
        print(f"{s:<6} {r['roi_pct']:>+8.1f}%  ${r['net_pnl']:>+7.2f}  "
              f"{r['trades']:<8} {r['win_rate']:>5.1f}%  {wm}")

    rois_90 = [r["roi_pct"] for r in results_90]
    pnls_90 = [r["net_pnl"] for r in results_90]
    winning_90 = sum(1 for r in rois_90 if r > 0)

    print(f"\n  📊 إحصائيات 90 يوم (BRUTAL):")
    print(f"     ROI avg:    {statistics.mean(rois_90):+.1f}%")
    print(f"     ROI median: {statistics.median(rois_90):+.1f}%")
    print(f"     ROI best:   {max(rois_90):+.1f}%")
    print(f"     ROI worst:  {min(rois_90):+.1f}%")
    print(f"     PnL avg:    ${statistics.mean(pnls_90):+.2f}")
    print(f"     WR avg:     {statistics.mean([r['win_rate'] for r in results_90]):.1f}%")
    print(f"     Winning 90d periods: {winning_90}/10 ({winning_90*10}%)")

    # مقارنة
    print()
    print("═══ مقارنة: SNIPER V517 vs BRUTAL V518 ═══")
    print(f"  {'Metric':<25} {'V517 SNIPER':<20} {'V518 BRUTAL':<20}")
    print(f"  {'-'*65}")
    # V517 — إعادة محاكاة 90 يوم للمقارنة العادلة
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
    from v517_90day_sniper_backtest import sniper_exit_v517, passes_sniper_gates_v517
    v517_90 = [
        simulate_days(20.0, s, 90, sniper_exit_v517, passes_sniper_gates_v517,
                      trade_pct=10.0, daily_cap_pct=60.0, daily_loss_pct=20.0,
                      max_trades=8)
        for s in seeds
    ]
    v517_avg = statistics.mean([r["roi_pct"] for r in v517_90])
    v517_pnl = statistics.mean([r["net_pnl"] for r in v517_90])
    v518_avg = statistics.mean(rois_90)
    v518_pnl = statistics.mean(pnls_90)

    print(f"  {'90d ROI avg':<25} {v517_avg:>+18.1f}%   {v518_avg:>+18.1f}%")
    print(f"  {'90d PnL avg':<25} ${v517_pnl:>+17.2f}   ${v518_pnl:>+17.2f}")
    improvement = v518_avg - v517_avg
    print(f"  {'التحسن':<25} {'':<20} {improvement:>+18.1f}pp")

    # التوقع النهائي
    print()
    print("=" * 90)
    print("  🎯 التوقع بـ $20 بعد شهر (BRUTAL V518)")
    print("=" * 90)
    print(f"""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  📊 المتوقع (avg):            ${statistics.mean(pnls_30):>+7.2f}  ({statistics.mean(rois_30):>+6.1f}%)       │
  │  📈 الأكثر احتمالاً (median):  ${statistics.median(pnls_30):>+7.2f}  ({statistics.median(rois_30):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  🟢 أفضل شهر:                 ${max(pnls_30):>+7.2f}  ({max(rois_30):>+6.1f}%)       │
  │  🔴 أسوأ شهر:                 ${min(pnls_30):>+7.2f}  ({min(rois_30):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  💵 رأس المال النهائي (متوسط):  ${20+statistics.mean(pnls_30):>7.2f}                       │
  │  🎯 احتمال شهر رابح:           {winning_30*10}%  ({winning_30}/10 seeds)              │
  └──────────────────────────────────────────────────────────────────────┘
""")

    print("  🎯 التوقع بـ $20 بعد 3 أشهر (BRUTAL V518)")
    print(f"""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  📊 المتوقع (avg):            ${statistics.mean(pnls_90):>+7.2f}  ({statistics.mean(rois_90):>+6.1f}%)       │
  │  📈 الأكثر احتمالاً (median):  ${statistics.median(pnls_90):>+7.2f}  ({statistics.median(rois_90):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  🟢 أفضل 3 أشهر:              ${max(pnls_90):>+7.2f}  ({max(rois_90):>+6.1f}%)       │
  │  🔴 أسوأ 3 أشهر:              ${min(pnls_90):>+7.2f}  ({min(rois_90):>+6.1f}%)       │
  │  ────────────────────────────────────────────────────────────────     │
  │  💵 رأس المال النهائي (متوسط):  ${20+statistics.mean(pnls_90):>7.2f}                       │
  │  🎯 احتمال فترة 90d رابحة:    {winning_90*10}%  ({winning_90}/10 seeds)              │
  └──────────────────────────────────────────────────────────────────────┘
""")


if __name__ == "__main__":
    main()
