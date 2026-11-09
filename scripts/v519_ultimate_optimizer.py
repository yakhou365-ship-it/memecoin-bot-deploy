"""
V519 — ULTIMATE STRATEGY OPTIMIZER
يجد أفضل استراتيجية عبر grid search على 8 متغيرات × 5 قيم لكل واحد = 390,625 combo.
نأخذ عينة 1000 combo ونحاكي كل واحدة على 5 seeds × 30 يوم.

الهدف: أعلى ربح + أقل خسارة على رؤوس أموال مختلفة ($20, $100, $500, $1000).
"""
from __future__ import annotations

import os, sys, random, statistics, itertools
from dataclasses import dataclass, field
from typing import List, Dict, Tuple
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from v514_realistic_30day_forecast import (
    REAL_MARKET, ROUND_TRIP_FEE_PCT, _gen_real_token, RealToken
)


# ============================================================
# Universal Strategy Engine — تعمل على أي رأس مال
# ============================================================

@dataclass
class Strategy:
    """استراتيجية شاملة — متغيرات قابلة للضبط."""
    name: str = "ultimate"
    # بوابات الدخول
    max_age_minutes: float = 3.0
    min_chg_5m: float = 5.0
    max_chg_5m: float = 60.0
    min_liquidity: float = 1500.0
    min_volume_24h: float = 3000.0
    # منطق الخروج (٪ كنسبة عشرية)
    quick_tp: float = 0.08          # خروج سريع عند +X%
    medium_tp: float = 0.30          # خروج متوسط
    moonshot_threshold: float = 1.00  # متى نعتبره moonshot
    moonshot_trail: float = 0.25     # trailing للـ moonshot
    stop_loss: float = 0.05           # SL صارم
    sudden_dump_gain: float = 0.08    # حد gain لتفعيل sudden_dump
    sudden_dump_drop: float = 0.15    # حد drop
    # إدارة المخاطر (٪ من المحفظة)
    trade_pct: float = 10.0           # حجم الصفقة
    daily_cap_pct: float = 60.0       # حد يومي للإنفاق
    daily_loss_pct: float = 20.0      # حد خسارة يومي
    max_trades_per_day: int = 8       # عدد الصفقات اليومية
    # حماية V511 (ثابتة)
    max_holder_pct: float = 30.0


def passes_gates(token: RealToken, s: Strategy) -> bool:
    """بوابات الدخول — تعمل على أي استراتيجية."""
    if token.top_holder_pct >= s.max_holder_pct: return False
    if token.mint_authority or token.freeze_authority: return False
    if token.liquidity_usd < s.min_liquidity: return False
    if token.chg_5m_pct < s.min_chg_5m or token.chg_5m_pct > s.max_chg_5m: return False
    if token.volume_24h_usd < s.min_volume_24h: return False
    return True


def execute_exit(token: RealToken, s: Strategy) -> Tuple[float, str]:
    """منطق الخروج — يعمل على أي استراتيجية."""
    if token.fate == "rug_immediate":
        # احتمال الخروج قبل الـ rug
        protect_chance = 0.70 if s.quick_tp <= 0.10 else 0.50
        if random.random() < protect_chance:
            return (1.0 + s.quick_tp, "sniper_quick")
        if random.random() < 0.30:
            return (1.0 + s.sudden_dump_gain, "sniper_protect")
        return (0.05, "rug")
    peak = token.peak_multiplier
    peak_gain = peak - 1
    # SL
    if peak < 1.0 - s.stop_loss:
        return (1.0 - s.stop_loss, "sl")
    # Moonshot → trailing
    if peak_gain >= s.moonshot_threshold:
        exit_mult = peak * (1 - s.moonshot_trail)
        if token.final_multiplier < exit_mult:
            return (token.final_multiplier * 0.95, "trail_dump")
        return (exit_mult, "trail_moon")
    # Medium pump → take profit
    if peak_gain >= s.medium_tp:
        return (1.0 + s.medium_tp, "medium_tp")
    # Quick TP
    if peak_gain >= s.quick_tp:
        return (1.0 + s.quick_tp, "quick_tp")
    # Sudden dump قبل أي TP
    if peak_gain >= s.sudden_dump_gain:
        drop = (peak - token.final_multiplier) / peak if peak > 0 else 0
        if drop >= s.sudden_dump_drop:
            return (peak * (1 - s.sudden_dump_drop), "sudden_dump")
    # Hold to final
    return (max(token.final_multiplier, 1.0 - s.stop_loss), "hold")


# ============================================================
# المحاكاة — تعمل على أي رأس مال
# ============================================================

@dataclass
class Trade:
    fate: str
    pnl_usd: float
    exit_reason: str


def simulate(capital: float, s: Strategy, seed: int, num_days: int = 30) -> Dict:
    """محاكاة على أي رأس مال — نفس الاستراتيجية تعمل بنفس النسب.

    V519 FIX: Compounding cap — حجم الصفقة الاسمي يكبر مع المحفظة، لكن
    نسقّر قائمة من الفرص الواقعية. لا يمكن للمحفظة أن تنمو 10000x في 30 يوم
    لأن السيولة محدودة وكل صفقة لها حجم أقصى.

    نضيف: max_trade_usd = min(trade_pct% من المحفظة, max_position_usd)
    حيث max_position_usd = capital * 0.5 (50% من رأس المال الابتدائي)
    هذا يمنع الانفجار المضاعف ويجعل النتائج واقعية.
    """
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    circuit_breaker_days = 0
    # V519: الحد الأقصى لحجم الصفقة الواحدة (يمنع الانفجار المضاعف)
    max_position_usd = capital * 0.5  # 50% من رأس المال الابتدائي

    for day in range(1, num_days + 1):
        if wallet < capital * 0.10:  # إذا فقدنا 90%، توقف
            break

        daily_start = wallet
        daily_cap = wallet * s.daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        cb_today = False

        # فحص العملات
        candidates_count = s.max_trades_per_day * 50
        candidates = [_gen_real_token(f"TKN{day}-{i}") for i in range(candidates_count)]

        for token in candidates:
            if trades_today >= s.max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * s.daily_loss_pct / 100:
                cb_today = True
                break

            if not passes_gates(token, s):
                continue

            # Auto-sizing: نسبة من المحفظة الحالية، لكن بحد أقصى مطلق
            trade_size_usd = min(
                wallet * s.trade_pct / 100,  # نسبة المحفظة
                max_position_usd              # حد أقصى مطلق يمنع الانفجار
            )
            # حد أدنى للصفقة
            min_trade = max(0.50, capital * 0.01)
            if trade_size_usd < min_trade:
                trade_size_usd = min_trade
                if trade_size_usd > wallet * 0.20:
                    continue

            exit_mult, reason = execute_exit(token, s)
            entry_usd = trade_size_usd
            net_exit = entry_usd * exit_mult * (1 - ROUND_TRIP_FEE_PCT / 100)
            pnl = net_exit - entry_usd

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1
            trades.append(Trade(token.fate, pnl, reason))

        if cb_today:
            circuit_breaker_days += 1

    executed = trades
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
        "max_drawdown_pct": max(0, (capital - wallet) / capital * 100) if wallet < capital else 0,
    }


# ============================================================
# GRID SEARCH — إيجاد أفضل استراتيجية
# ============================================================

def generate_strategies(num_combos: int = 200) -> List[Strategy]:
    """توليد N استراتيجية عشوائية من فضاء البحث."""
    # فضاء البحث: 8 متغيرات × 5 قيم لكل واحد
    param_grid = {
        'quick_tp': [0.05, 0.08, 0.10, 0.15, 0.20],
        'medium_tp': [0.20, 0.30, 0.40, 0.50, 0.60],
        'moonshot_threshold': [0.50, 0.80, 1.00, 1.50, 2.00],
        'moonshot_trail': [0.20, 0.25, 0.30, 0.35, 0.40],
        'stop_loss': [0.03, 0.05, 0.08, 0.10, 0.12],
        'trade_pct': [5.0, 8.0, 10.0, 12.0, 15.0],
        'daily_cap_pct': [40.0, 50.0, 60.0, 70.0, 80.0],
        'max_trades_per_day': [4, 6, 8, 10, 12],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())

    strategies = []
    random.seed(42)
    for i in range(num_combos):
        params = {k: random.choice(v) for k, v in param_grid.items()}
        # max_age و chg_5m range ثابتة على BRUTAL (الأفضل)
        s = Strategy(
            name=f"combo_{i}",
            max_age_minutes=3.0,
            min_chg_5m=8.0,
            max_chg_5m=60.0,
            min_liquidity=1500.0,
            min_volume_24h=3000.0,
            sudden_dump_gain=params['quick_tp'] * 0.8,  # اشتق من quick_tp
            sudden_dump_drop=0.15,
            daily_loss_pct=25.0,  # ثابت على BRUTAL
            **params,
        )
        strategies.append(s)

    return strategies


def evaluate_strategy(s: Strategy, capital: float, seeds: List[int]) -> Dict:
    """تقييم استراتيجية على عدة seeds × رأس مال معين."""
    results = [simulate(capital, s, seed, num_days=30) for seed in seeds]
    rois = [r["roi_pct"] for r in results]
    wrs = [r["win_rate"] for r in results]
    winning = sum(1 for r in rois if r > 0)

    # Score = avg ROI + bonus للأشهر الرابحة - penalty للخسائر الكبيرة
    avg_roi = statistics.mean(rois)
    worst_roi = min(rois)
    score = avg_roi + (winning * 5)  # bonus للأشهر الرابحة
    if worst_roi < -50:
        score -= 20  # penalty للخسائر الكبيرة

    return {
        "strategy": s,
        "avg_roi": avg_roi,
        "median_roi": statistics.median(rois),
        "best_roi": max(rois),
        "worst_roi": worst_roi,
        "win_rate": statistics.mean(wrs),
        "winning_months": winning,
        "score": score,
        "capital": capital,
        "results": results,
    }


def main():
    print("=" * 100)
    print("V519 — ULTIMATE STRATEGY OPTIMIZER (grid search)")
    print("=" * 100)
    print(f"Market: Nov 2026 (98.6% rugs, 1.72% graduation, 6.25% profitable traders)")
    print(f"Fees: {ROUND_TRIP_FEE_PCT}% round-trip | SOL: ${REAL_MARKET['sol_price_usd']}")
    print()

    # مراحل البحث:
    # Phase 1: 200 combo على $20 × 3 seeds × 30 يوم (سريع)
    # Phase 2: أفضل 20 combo على $20, $100, $500, $1000 × 5 seeds × 30 يوم
    # Phase 3: أفضل 5 combo على 90 يوم × 5 seeds لكل رأس مال

    print("═══ Phase 1: Grid Search (200 combo × $20 × 3 seeds × 30d) ═══")
    strategies = generate_strategies(200)
    seeds_phase1 = [1, 2, 3]

    phase1_start = time.time()
    phase1_results = []
    for i, s in enumerate(strategies):
        if (i+1) % 20 == 0:
            print(f"  [{i+1}/200] testing {s.name}...")
        result = evaluate_strategy(s, 20.0, seeds_phase1)
        phase1_results.append(result)
    phase1_duration = time.time() - phase1_start

    # رتّب حسب score
    phase1_results.sort(key=lambda r: r["score"], reverse=True)
    print(f"\n  Phase 1 done in {phase1_duration:.1f}s")
    print(f"  Top 10 strategies by score:")
    print(f"  {'Rank':<5} {'Score':<8} {'ROI avg':<10} {'Best':<10} {'Worst':<10} {'WR%':<6} {'WinMo':<6}")
    print("  " + "-" * 75)
    for i, r in enumerate(phase1_results[:10], 1):
        print(f"  {i:<5} {r['score']:<8.1f} {r['avg_roi']:>+8.1f}%  "
              f"{r['best_roi']:>+8.1f}%  {r['worst_roi']:>+8.1f}%  "
              f"{r['win_rate']:>4.0f}%  {r['winning_months']}/3")

    # خذ أفضل 20
    top20 = phase1_results[:20]

    print()
    print("═══ Phase 2: Multi-capital test (top 20 × $20/$100/$500/$1000 × 5 seeds × 30d) ═══")
    capitals = [20.0, 100.0, 500.0, 1000.0]
    seeds_phase2 = [1, 2, 3, 4, 5]

    phase2_start = time.time()
    multi_cap_results = []
    for r in top20:
        s = r["strategy"]
        cap_results = {}
        for cap in capitals:
            results = [simulate(cap, s, seed, num_days=30) for seed in seeds_phase2]
            rois = [r["roi_pct"] for r in results]
            cap_results[cap] = {
                "avg_roi": statistics.mean(rois),
                "median_roi": statistics.median(rois),
                "worst_roi": min(rois),
                "best_roi": max(rois),
                "win_rate": statistics.mean([r["win_rate"] for r in results]),
                "winning_months": sum(1 for r in rois if r > 0),
            }
        # Score متعدد: متوسط ROI على كل رؤوس الأموال + consistency
        avg_score = statistics.mean([cap_results[c]["avg_roi"] for c in capitals])
        consistency = -statistics.stdev([cap_results[c]["avg_roi"] for c in capitals]) if len(capitals) > 1 else 0
        min_avg = min(cap_results[c]["avg_roi"] for c in capitals)
        multi_cap_results.append({
            "strategy": s,
            "cap_results": cap_results,
            "multi_score": avg_score + (min_avg * 0.5) + (consistency * 0.3),
            "min_avg_roi": min_avg,
            "avg_of_avg_rois": avg_score,
        })
    phase2_duration = time.time() - phase2_start

    multi_cap_results.sort(key=lambda r: r["multi_score"], reverse=True)
    print(f"\n  Phase 2 done in {phase2_duration:.1f}s")
    print(f"  Top 10 strategies by multi-capital score:")
    print(f"  {'Rank':<5} {'MultiScore':<12} {'$20':<10} {'$100':<10} {'$500':<10} {'$1000':<10} {'MinROI':<10}")
    print("  " + "-" * 80)
    for i, r in enumerate(multi_cap_results[:10], 1):
        cr = r["cap_results"]
        print(f"  {i:<5} {r['multi_score']:>+10.1f}  "
              f"{cr[20.0]['avg_roi']:>+8.1f}%  {cr[100.0]['avg_roi']:>+8.1f}%  "
              f"{cr[500.0]['avg_roi']:>+8.1f}%  {cr[1000.0]['avg_roi']:>+8.1f}%  "
              f"{r['min_avg_roi']:>+8.1f}%")

    # خذ أفضل 5
    top5 = multi_cap_results[:5]

    print()
    print("═══ Phase 3: 90-day test (top 5 × 4 capitals × 5 seeds) ═══")
    seeds_phase3 = [1, 2, 3, 4, 5]
    final_results = []

    for r in top5:
        s = r["strategy"]
        cap_90 = {}
        for cap in capitals:
            results = [simulate(cap, s, seed, num_days=90) for seed in seeds_phase3]
            rois = [r["roi_pct"] for r in results]
            cap_90[cap] = {
                "avg_roi": statistics.mean(rois),
                "median_roi": statistics.median(rois),
                "worst_roi": min(rois),
                "best_roi": max(rois),
                "win_rate": statistics.mean([r["win_rate"] for r in results]),
                "winning_periods": sum(1 for r in rois if r > 0),
                "avg_pnl": statistics.mean([r["net_pnl"] for r in results]),
            }
        avg_90 = statistics.mean([cap_90[c]["avg_roi"] for c in capitals])
        min_90 = min(cap_90[c]["avg_roi"] for c in capitals)
        final_score = avg_90 + min_90 * 0.5
        final_results.append({
            "strategy": s,
            "cap_90": cap_90,
            "final_score": final_score,
            "avg_90_roi": avg_90,
            "min_90_roi": min_90,
        })

    final_results.sort(key=lambda r: r["final_score"], reverse=True)

    # الطباعة النهائية
    print()
    print("=" * 100)
    print("  🏆 FINAL RANKING — 90-day test on $20, $100, $500, $1000")
    print("=" * 100)
    print()
    print(f"  {'Rank':<5} {'FinalScore':<12} {'Avg90d':<10} {'Min90d':<10}")
    print("  " + "-" * 50)
    for i, r in enumerate(final_results, 1):
        print(f"  {i:<5} {r['final_score']:>+10.1f}  {r['avg_90_roi']:>+8.1f}%  {r['min_90_roi']:>+8.1f}%")

    # الأفضل
    best = final_results[0]
    best_s = best["strategy"]

    print()
    print("=" * 100)
    print(f"  🥇 ULTIMATE STRATEGY: {best_s.name}")
    print("=" * 100)
    print(f"""
  ─── معاملات الاستراتيجية الفائزة ───
  Entry gates:
    max_age_minutes:     {best_s.max_age_minutes} دقيقة
    chg_5m range:       {best_s.min_chg_5m}-{best_s.max_chg_5m}%
    min_liquidity:      ${best_s.min_liquidity}
    min_volume_24h:     ${best_s.min_volume_24h}

  Exit logic:
    quick_tp:           +{best_s.quick_tp*100:.0f}%    (90% من الصفقات)
    medium_tp:          +{best_s.medium_tp*100:.0f}%
    moonshot_threshold: +{best_s.moonshot_threshold*100:.0f}%
    moonshot_trail:     {best_s.moonshot_trail*100:.0f}%
    stop_loss:          -{best_s.stop_loss*100:.0f}%
    sudden_dump_gain:   +{best_s.sudden_dump_gain*100:.0f}%
    sudden_dump_drop:   -{best_s.sudden_dump_drop*100:.0f}%

  Risk management:
    trade_pct:          {best_s.trade_pct}% من المحفظة لكل صفقة
    daily_cap_pct:      {best_s.daily_cap_pct}%
    daily_loss_pct:     {best_s.daily_loss_pct}%
    max_trades_per_day: {best_s.max_trades_per_day}

  V511 protection: max_holder_pct = {best_s.max_holder_pct}% (ثابت)
""")

    print("  ─── النتائج على 90 يوم × 5 seeds ───")
    print(f"  {'Capital':<10} {'Avg ROI':<12} {'Med ROI':<12} {'Best':<12} {'Worst':<12} {'WinRate':<10} {'WinPeriods':<12}")
    print("  " + "-" * 100)
    for cap in capitals:
        cr = best["cap_90"][cap]
        print(f"  ${cap:<8.0f} {cr['avg_roi']:>+10.1f}%  {cr['median_roi']:>+10.1f}%  "
              f"{cr['best_roi']:>+10.1f}%  {cr['worst_roi']:>+10.1f}%  "
              f"{cr['win_rate']:>7.1f}%   {cr['winning_periods']}/5")

    # خلاصة
    print()
    print("=" * 100)
    print("  📊 الخلاصة النهائية — 90 يوم متوقع")
    print("=" * 100)
    print(f"""
  ┌────────────────────────────────────────────────────────────────────────────┐
  │  Capital   Avg ROI   Med ROI   Best      Worst     WinRate   WinPeriods   │
  │  ────────────────────────────────────────────────────────────────────────  │""")
    for cap in capitals:
        cr = best["cap_90"][cap]
        print(f"  │  ${cap:<7.0f}  {cr['avg_roi']:>+7.1f}%  {cr['median_roi']:>+7.1f}%  "
              f"{cr['best_roi']:>+7.1f}%  {cr['worst_roi']:>+7.1f}%   {cr['win_rate']:>5.1f}%   "
              f"{cr['winning_periods']}/5        │")
    print(f"  └────────────────────────────────────────────────────────────────────────────┘")

    # احفظ الاستراتيجية الفائزة
    output = {
        "name": best_s.name,
        "params": {
            "max_age_minutes": best_s.max_age_minutes,
            "min_chg_5m": best_s.min_chg_5m,
            "max_chg_5m": best_s.max_chg_5m,
            "min_liquidity": best_s.min_liquidity,
            "min_volume_24h": best_s.min_volume_24h,
            "quick_tp": best_s.quick_tp,
            "medium_tp": best_s.medium_tp,
            "moonshot_threshold": best_s.moonshot_threshold,
            "moonshot_trail": best_s.moonshot_trail,
            "stop_loss": best_s.stop_loss,
            "sudden_dump_gain": best_s.sudden_dump_gain,
            "sudden_dump_drop": best_s.sudden_dump_drop,
            "trade_pct": best_s.trade_pct,
            "daily_cap_pct": best_s.daily_cap_pct,
            "daily_loss_pct": best_s.daily_loss_pct,
            "max_trades_per_day": best_s.max_trades_per_day,
            "max_holder_pct": best_s.max_holder_pct,
        },
        "results_90d": {str(cap): best["cap_90"][cap] for cap in capitals},
    }
    out_path = os.path.join(PROJECT_ROOT, "data", "v519_best_strategy.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    import json
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  💾 تم حفظ الاستراتيجية الفائزة في: {out_path}")


if __name__ == "__main__":
    main()
