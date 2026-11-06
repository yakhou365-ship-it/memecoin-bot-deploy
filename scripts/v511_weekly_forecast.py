"""
V511 — 7-DAY P&L FORECAST ($20 STARTING CAPITAL)

يشغل 20 seeds مختلفة لـ 7 أيام ويحسب:
- متوسط الربح
- أفضل سيناريو (max)
- أسوأ سيناريو (min)
- median
- نسبة الأسابيع الرابحة

هذا يعطي المستخدم صورة واقعية: ليس "ستربح X%" بل
"متوسط X% لكن قد يكون بين -Y% و +Z%".
"""
from __future__ import annotations

import os
import sys
import statistics

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)

# Import from the 30-day backtest
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
from v511_backtest_realistic import (
    simulate_30_days,
    ROUND_TRIP_FEE_PCT,
)


def simulate_7_days(capital: float = 20.0, apply_gates: bool = True, seed: int = 42) -> dict:
    """مثل simulate_30_days لكن لـ 7 أيام فقط."""
    # نُعدّل المنطق بأن نوقف بعد 7 أيام
    # simulate_30_days تأخذ seed وتُرجع dict لكنها تشغل 30 يوم.
    # الحل: ننسخ المنطق لكن مع 7 أيام فقط.
    import random
    import numpy as np
    from v511_backtest_realistic import (
        _gen_token, passes_all_bot_gates, smart_exit, naive_exit, TradeResult
    )

    random.seed(seed)
    np.random.seed(seed)

    wallet = capital
    daily_cap_pct = 30.0
    trade_size_pct = 5.0
    daily_loss_pct = 15.0

    trades = []
    circuit_breaker = False

    for day in range(1, 8):  # 7 أيام فقط
        if circuit_breaker or wallet < 0.50:
            continue

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        max_trades_per_day = 2

        candidates = [_gen_token(f"TKN{day}-{i}") for i in range(random.randint(8, 15))]

        for token in candidates:
            if trades_today >= max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                circuit_breaker = True
                break

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

            trade_size_usd = wallet * trade_size_pct / 100
            if trade_size_usd < 0.50:
                continue

            exit_fn = smart_exit if apply_gates else naive_exit
            exit_mult, reason = exit_fn(token)
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
    blocked = [t for t in trades if t.blocked]
    rugs_avoided = len([t for t in blocked if t.block_reason == "30%+ wallet"])

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
        "rug_losses_avoided": rugs_avoided,
    }


def main():
    print("=" * 70)
    print("V511 — 7-DAY P&L FORECAST ($20 STARTING CAPITAL, 20 SEEDS)")
    print("=" * 70)
    print(f"رأس المال: $20 | المدة: 7 أيام | رسوم: {ROUND_TRIP_FEE_PCT:.1f}% round-trip")
    print(f"حد يومي: 2 صفقة | حجم الصفقة: 5% من المحفظة | حد خسارة يومي: 15%")
    print()

    seeds = list(range(1, 21))  # 20 seeds
    bot_results = []
    naive_results = []

    print(f"{'Seed':<6} {'BOT (ROI%)':<15} {'BOT ($)':<12} {'NAIVE (ROI%)':<15} {'NAIVE ($)':<12}")
    print("-" * 70)

    for seed in seeds:
        r_bot = simulate_7_days(capital=20.0, apply_gates=True, seed=seed)
        r_naive = simulate_7_days(capital=20.0, apply_gates=False, seed=seed)
        bot_results.append(r_bot)
        naive_results.append(r_naive)

        print(f"{seed:<6} {r_bot['roi_pct']:>+10.1f}%   ${r_bot['net_pnl_usd']:>+8.2f}"
              f"   {r_naive['roi_pct']:>+10.1f}%   ${r_naive['net_pnl_usd']:>+8.2f}")

    # إحصائيات BOT
    bot_rois = [r["roi_pct"] for r in bot_results]
    bot_pnls = [r["net_pnl_usd"] for r in bot_results]
    bot_wrs = [r["win_rate"] for r in bot_results]
    bot_avoided = sum(r["rug_losses_avoided"] for r in bot_results)

    # إحصائيات NAIVE
    naive_rois = [r["roi_pct"] for r in naive_results]
    naive_pnls = [r["net_pnl_usd"] for r in naive_results]

    print()
    print("=" * 70)
    print("  📊 STATISTICAL SUMMARY (20 seeds)")
    print("=" * 70)
    print(f"\n  {'Metric':<35} {'BOT (V511)':<18} {'NAIVE':<18}")
    print(f"  {'-'*70}")

    def stats(vals):
        return {
            "avg": statistics.mean(vals),
            "median": statistics.median(vals),
            "min": min(vals),
            "max": max(vals),
            "stdev": statistics.stdev(vals) if len(vals) > 1 else 0,
        }

    bot_roi_s = stats(bot_rois)
    naive_roi_s = stats(naive_rois)
    bot_pnl_s = stats(bot_pnls)
    naive_pnl_s = stats(naive_pnls)
    bot_wr_s = stats(bot_wrs)

    print(f"  {'ROI avg':<35} {bot_roi_s['avg']:>+10.1f}%     {naive_roi_s['avg']:>+10.1f}%")
    print(f"  {'ROI median':<35} {bot_roi_s['median']:>+10.1f}%     {naive_roi_s['median']:>+10.1f}%")
    print(f"  {'ROI min (worst week)':<35} {bot_roi_s['min']:>+10.1f}%     {naive_roi_s['min']:>+10.1f}%")
    print(f"  {'ROI max (best week)':<35} {bot_roi_s['max']:>+10.1f}%     {naive_roi_s['max']:>+10.1f}%")
    print(f"  {'ROI stdev (volatility)':<35} {bot_roi_s['stdev']:>10.1f}%     {naive_roi_s['stdev']:>10.1f}%")
    print()
    print(f"  {'P&L avg ($20 → ?)':<35} ${bot_pnl_s['avg']:>+10.2f}     ${naive_pnl_s['avg']:>+10.2f}")
    print(f"  {'P&L median':<35} ${bot_pnl_s['median']:>+10.2f}     ${naive_pnl_s['median']:>+10.2f}")
    print(f"  {'P&L worst':<35} ${bot_pnl_s['min']:>+10.2f}     ${naive_pnl_s['min']:>+10.2f}")
    print(f"  {'P&L best':<35} ${bot_pnl_s['max']:>+10.2f}     ${naive_pnl_s['max']:>+10.2f}")
    print()
    print(f"  {'Win Rate avg':<35} {bot_wr_s['avg']:>10.1f}%     {'N/A':>10}")
    print(f"  {'Rug losses avoided (total 20 weeks)':<35} {bot_avoided:>10}     {'N/A':>10}")

    # نسبة الأسابيع الرابحة
    bot_winning_weeks = sum(1 for r in bot_rois if r > 0)
    naive_winning_weeks = sum(1 for r in naive_rois if r > 0)
    print(f"  {'Winning weeks (out of 20)':<35} {bot_winning_weeks:>10}/20     {naive_winning_weeks:>10}/20")
    print(f"  {'%':<35} {bot_winning_weeks/20*100:>10.0f}%     {naive_winning_weeks/20*100:>10.0f}%")

    # التوقعات
    print()
    print("=" * 70)
    print("  🎯 FORECAST — بعد أسبوع واحد بـ $20")
    print("=" * 70)
    print(f"""
  ┌──────────────────────────────────────────────────────────────┐
  │  السيناريو الأكثر احتمالاً (median):  ${bot_pnl_s['median']:>+6.2f}  │
  │  ────────────────────────────────────────────────────────     │
  │  متوسط الربح:                    ${bot_pnl_s['avg']:>+6.2f}  │
  │  ────────────────────────────────────────────────────────     │
  │  🟢 أفضل أسبوع:                   ${bot_pnl_s['max']:>+6.2f}  │
  │  🔴 أسوأ أسبوع:                   ${bot_pnl_s['min']:>+6.2f}  │
  │  ────────────────────────────────────────────────────────     │
  │  رأس المال النهائي (متوسط):       ${20+bot_pnl_s['avg']:>6.2f}  │
  │  رأس المال النهائي (median):      ${20+bot_pnl_s['median']:>6.2f}  │
  │  ────────────────────────────────────────────────────────     │
  │  احتمال أسبوع رابح:               {bot_winning_weeks/20*100:>4.0f}% ({bot_winning_weeks}/20 seeds)│
  └──────────────────────────────────────────────────────────────┘
""")

    print("  ⚠️ تحذيرات مهمة:")
    print("  ─────────────────────────────────────────────────────────────")
    print("  1. هذه محاكاة إحصائية، ليست ضماناً للنتائج المستقبلية.")
    print("  2. نتائج حقيقية قد تختلف حسب حالة السوق (bull/bear/sideways).")
    print("  3. الأسابيع الفردية متذبذبة جداً — قد تخسر في أسبوع وتكثر في آخر.")
    print("  4. النتائج تعتمد على عمل البوت بشكل صحيح 24/7 (Railway يجب أن يعمل).")
    print("  5. الـ auto-sizing يحمي رأس المال: لا تخسر أكثر من 15%/يوم.")
    print("  6. أي توقف للبوت قد يفوت فرص ربح أو يترك صفقات مفتوحة (خطر SL).")
    print()
    print("  💡 نصيحة:")
    print("  ─────────────────────────────────────────────────────────────")
    print("  ابدأ بـ $20 لمدة أسبوع، راقب النتائج الفعلية، ثم قارن بالتنبؤ.")
    print("  إذا كانت النتائج قريبة من المتوقع، ارفع رأس المال تدريجياً.")
    print("  لا تستثمر أبداً مبلغ لا تستطيع تحمل خسارته كاملاً.")


if __name__ == "__main__":
    main()
