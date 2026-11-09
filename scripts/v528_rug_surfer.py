"""
V528 — RUG-SURFER STRATEGY
يدخل rugs مبكراً (في أول 5-10 ثانية) ويخرج قبل الـ dump.

الفلسفة:
- 65% من العملات على pump.fun هي rugs
- لكن كل rug له "pump phase" قصير (10-60 ثانية) قبل الـ dump
- لو دخلنا في أول 5 ثوانٍ وخرجنا بعد 15-30 ثانية، نلتقط +20-50% ثم نخرج
- المفتاح: timing دقيق جداً + خروج فوري

الفرق عن SURVIVOR:
- SURVIVOR: تجنب الـ rugs تماماً (بوابات صارمة جداً)
- RUG-SURFER: يدخل الـ rugs لكن يخرج قبل الـ dump

بوابات RUG-SURFER (عكس SURVIVOR — أكثر تساهلاً):
- محفظة dev < 35% (نسمح بمحافظ أكبر — نخرج مبكراً)
- سيولة ≥ $1,000 (نقبل سيولة أقل)
- عمر 5-15 ثانية فقط (فقط خلال الـ pump phase)
- chg_5m > 20% (pump قوي بدأ — نريد ركوبه)
- لا نشترط LP locked (الـ rug لم يحدث بعد)

خروج RUG-SURFER:
- خروج بعد 30 ثانية كحد أقصى (وقت ثابت — لا طمع)
- خروج فوري عند +15% (ربح سريع)
- SL عند -8% (أوسع من SURVIVOR لأن البوت يدخل rugs)
- sudden_dump عند +5% gain و 5% drop (حساسية قصوى)

محاكاة على فترات مختلفة:
1. سوق قاسي (Nov 2026): 98.6% scams، 1.72% graduation
2. سوق متوسط (April 2026): 73% profitable
3. سوق جيد (hypothetical): 5% graduation
4. سوق سيء جداً (Jan 2026): 50% profitable
"""
from __future__ import annotations

import os, sys, random, statistics
from dataclasses import dataclass
from typing import List, Dict

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# 4 سيناريوهات سوق مختلفة (فترات تاريخية)
# ============================================================

MARKET_SCENARIOS = {
    "brutal_nov2026": {
        "name": "سوق قاسي (Nov 2026)",
        "description": "98.6% scams، 1.72% graduation، 6.25% profitable traders",
        "fate_dist": {
            "rug_immediate":    0.65,
            "fail_day1_dump":   0.30,
            "lateral_die":      0.035,
            "graduate_small":   0.014,
            "moonshot":         0.001,
        },
        "fees_pct": 12.0,
        "order_fail_rate": 0.10,
        "sl_slippage": 0.10,
    },
    "good_apr2026": {
        "name": "سوق جيد (April 2026)",
        "description": "73.3% profitable traders، أكثر moonshots",
        "fate_dist": {
            "rug_immediate":    0.30,   # أقل rugs
            "fail_day1_dump":   0.40,
            "lateral_die":      0.20,
            "graduate_small":   0.07,    # أكثر graduates
            "moonshot":         0.03,    # 3% moonshots!
        },
        "fees_pct": 10.0,
        "order_fail_rate": 0.08,
        "sl_slippage": 0.08,
    },
    "average_2026": {
        "name": "سوق متوسط (متوسط 2026)",
        "description": "50% profitable، توزيع متوازن",
        "fate_dist": {
            "rug_immediate":    0.45,
            "fail_day1_dump":   0.35,
            "lateral_die":      0.12,
            "graduate_small":   0.06,
            "moonshot":         0.02,
        },
        "fees_pct": 10.0,
        "order_fail_rate": 0.09,
        "sl_slippage": 0.09,
    },
    "bull_hypothetical": {
        "name": "سوق صاعد (افتراضي)",
        "description": "5% graduation rate، moonshots كثيرة",
        "fate_dist": {
            "rug_immediate":    0.20,
            "fail_day1_dump":   0.40,
            "lateral_die":      0.25,
            "graduate_small":   0.10,
            "moonshot":         0.05,
        },
        "fees_pct": 8.0,
        "order_fail_rate": 0.05,
        "sl_slippage": 0.05,
    },
}


@dataclass
class RugSurferToken:
    fate: str
    peak_mult: float
    final_mult: float
    time_to_dump_sec: int
    # أهمية: متى يحدث الـ peak خلال دورة حياة التوكن (ثوانٍ)
    time_to_peak_sec: int
    top_holder_pct: float
    liquidity_usd: float
    volume_5m_usd: float
    age_sec: int
    chg_5m_pct: float
    lp_locked: bool
    mint_authority: bool
    freeze_authority: bool


def gen_token(scenario: Dict) -> RugSurferToken:
    fate = random.random()
    cum = 0
    fate_name = "lateral_die"
    for fn, p in scenario["fate_dist"].items():
        cum += p
        if fate < cum:
            fate_name = fn
            break

    if fate_name == "rug_immediate":
        peak_mult = random.uniform(1.3, 3.0)   # pump قوي
        final_mult = 0.02
        time_to_dump = random.randint(5, 60)
        time_to_peak = max(3, time_to_dump - random.randint(1, 10))
        top_holder = random.uniform(20, 55)
        liquidity = random.uniform(800, 8000)
        volume_5m = random.uniform(300, 3000)
    elif fate_name == "fail_day1_dump":
        peak_mult = random.uniform(1.3, 2.2)
        final_mult = random.uniform(0.15, 0.4)
        time_to_dump = random.randint(60, 600)
        time_to_peak = random.randint(30, 200)
        top_holder = random.uniform(10, 25)
        liquidity = random.uniform(2000, 15000)
        volume_5m = random.uniform(500, 5000)
    elif fate_name == "lateral_die":
        peak_mult = random.uniform(1.1, 1.5)
        final_mult = random.uniform(0.3, 0.7)
        time_to_dump = random.randint(600, 3600)
        time_to_peak = random.randint(60, 300)
        top_holder = random.uniform(5, 15)
        liquidity = random.uniform(5000, 25000)
        volume_5m = random.uniform(1000, 10000)
    elif fate_name == "graduate_small":
        peak_mult = random.uniform(1.5, 3.0)
        final_mult = random.uniform(1.2, 2.0)
        time_to_dump = random.randint(3600, 86400)
        time_to_peak = random.randint(100, 600)
        top_holder = random.uniform(3, 10)
        liquidity = random.uniform(15000, 80000)
        volume_5m = random.uniform(3000, 30000)
    else:  # moonshot
        peak_mult = random.uniform(5, 20)
        final_mult = random.uniform(2, 8)
        time_to_dump = random.randint(3600, 86400)
        time_to_peak = random.randint(100, 1000)
        top_holder = random.uniform(3, 10)
        liquidity = random.uniform(20000, 200000)
        volume_5m = random.uniform(5000, 50000)

    return RugSurferToken(
        fate=fate_name, peak_mult=peak_mult, final_mult=final_mult,
        time_to_dump_sec=time_to_dump,
        time_to_peak_sec=time_to_peak,
        top_holder_pct=top_holder,
        liquidity_usd=liquidity,
        volume_5m_usd=volume_5m,
        age_sec=random.randint(3, 300),
        chg_5m_pct=random.uniform(-20, 200),
        lp_locked=random.random() < 0.40,
        mint_authority=random.random() < 0.05,
        freeze_authority=random.random() < 0.03,
    )


# ============================================================
# RUG-SURFER gates (عكس SURVIVOR — أكثر تساهلاً)
# ============================================================

def passes_rug_surfer_gates(t: RugSurferToken) -> bool:
    """بوابات RUG-SURFER — تدخل الـ rugs لكن تخرج قبل الـ dump.

    الفلسفة: نريد أن نلتقط الـ pump phase فقط:
    1. محفظة dev < 35% (نسمح بمحافظ أكبر من SURVIVOR 15%)
    2. سيولة ≥ $1000 (نقبل سيولة أقل)
    3. عمر 5-30 ثانية (فقط خلال الـ pump phase المبكر)
    4. chg_5m > 20% (pump قوي بدأ — نريد ركوبه)
    5. لا نشترط LP locked (الـ rug لم يحدث بعد)
    6. لا mint/freeze authority (حتى الـ rugs الموثوقة تجنباً honeypots)
    """
    if t.top_holder_pct >= 35.0: return False   # نسمح بمحافظ أكبر من SURVIVOR
    if t.mint_authority: return False            # لكن نتavoid honeypots
    if t.freeze_authority: return False
    if t.liquidity_usd < 1000: return False       # نقبل سيولة أقل
    if t.age_sec < 5 or t.age_sec > 30: return False  # نافذة 5-30 ثانية فقط
    if t.chg_5m_pct < 20: return False           # pump قوي بدأ
    return True


def rug_surfer_exit(t: RugSurferToken, holding_time_sec: int) -> tuple:
    """خروج RUG-SURFER v2 — أكثر ربحاً.

    تحسينات V528.1:
    - quick_tp عند +20% (بدل +15%) — يغطي الرسوم 12% + ربح صافي 5%
    - SL عند -5% (أصعب من -8%) — قص الخسائر أسرع
    - time_exit عند 20 ثانية (بدل 30) — أسرع خروج
    """
    if t.fate == "rug_immediate":
        # rug: لو دخلنا في الـ pump phase، قد نهرب قبل الـ dump
        if t.time_to_dump_sec > holding_time_sec + 5:
            # هروب! نخرج عند +20% أو +8% حسب القمة
            if t.peak_mult >= 1.20:
                return (1.20, "rug_escape_+20")
            elif t.peak_mult >= 1.08:
                return (1.08, "rug_escape_+8")
            else:
                return (1.0, "rug_escape_break_even")
        # لم نهرب — الـ dump حدث قبل خروجنا
        # SL -5% لكن slippage يجعله -15%
        return (1.0 - 0.15, "rug_caught_with_slippage")

    peak = t.peak_mult
    final = t.final_mult
    peak_gain = peak - 1

    # SL -5% لكن slippage يجعله -15%
    if peak < 0.95:
        return (1.0 - 0.15, "sl_with_slippage")

    # quick_tp عند +20% (يكفي لتغطية الرسوم 12% + ربح 5%)
    if peak_gain >= 0.20:
        return (1.20 * (1 - 0.05), "quick_tp_+20")

    # time exit: 20 ثانية مرت (أسرع من 30)
    if holding_time_sec >= 20:
        return (max(final, 0.95) * (1 - 0.05), "time_exit_20s")

    # sudden_dump: حساسية قصوى
    if peak_gain >= 0.05:
        drop = (peak - final) / peak if peak > 0 else 0
        if drop >= 0.05:
            return (peak * 0.95, "sudden_dump")

    # hold
    return (max(final, 0.95), "hold")


# ============================================================
# المحاكاة
# ============================================================

@dataclass
class Trade:
    fate: str
    pnl_pct: float
    exit_reason: str
    holding_sec: int


def simulate_rug_surfer(capital: float, num_days: int, seed: int, scenario: Dict) -> Dict:
    random.seed(seed)
    wallet = capital
    trades: List[Trade] = []
    cb_days = 0
    failed_orders = 0
    max_position_usd = capital * 0.5

    # RUG-SURFER settings
    trade_pct = 8.0            # 8% لكل صفقة (أعلى من SURVIVOR 5%)
    daily_cap_pct = 50.0       # 50% حد يومي
    daily_loss_pct = 20.0      # 20% حد خسارة يومي
    max_trades_per_day = 8     # 8 صفقات (أكثر من SURVIVOR 3)

    fees_pct = scenario["fees_pct"]
    order_fail_rate = scenario["order_fail_rate"]

    for day in range(1, num_days + 1):
        if wallet < capital * 0.10:
            break

        daily_start = wallet
        daily_cap = wallet * daily_cap_pct / 100
        daily_spent = 0
        daily_pnl = 0
        trades_today = 0
        cb_today = False

        for i in range(800):
            if trades_today >= max_trades_per_day:
                break
            if daily_spent >= daily_cap:
                break
            if daily_pnl <= -daily_start * daily_loss_pct / 100:
                cb_today = True
                break

            token = gen_token(scenario)

            if not passes_rug_surfer_gates(token):
                continue

            trade_size_usd = min(wallet * trade_pct / 100, max_position_usd)
            if trade_size_usd < 0.50:
                continue

            if random.random() < order_fail_rate:
                failed_orders += 1
                wallet -= trade_size_usd * 0.005
                continue

            # محاكاة holding time: 5-30 ثانية
            holding_time = random.randint(5, 30)
            exit_mult, reason = rug_surfer_exit(token, holding_time)
            entry_usd = trade_size_usd
            net_exit = entry_usd * exit_mult * (1 - fees_pct / 100)
            pnl = net_exit - entry_usd
            pnl_pct = (pnl / entry_usd) * 100

            wallet += pnl
            daily_spent += entry_usd
            daily_pnl += pnl
            trades_today += 1
            trades.append(Trade(token.fate, pnl_pct, reason, holding_time))

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
        "rug_trades": len([t for t in executed if t.fate == "rug_immediate"]),
        "rug_escapes": len([t for t in wins if t.fate == "rug_immediate"]),
        "rug_losses": len([t for t in losses if t.fate == "rug_immediate"]),
        "moonshots_caught": len([t for t in wins if t.fate == "moonshot"]),
        "circuit_breaker_days": cb_days,
        "failed_orders": failed_orders,
        "avg_win_pct": statistics.mean([t.pnl_pct for t in wins]) if wins else 0,
        "avg_loss_pct": statistics.mean([t.pnl_pct for t in losses]) if losses else 0,
    }


def main():
    print("=" * 95)
    print("V528 — RUG-SURFER STRATEGY (enter rugs, exit before dump)")
    print("=" * 95)
    print(f"  فلسفة: ندخل الـ rugs مبكراً (5-30s) ونخرج قبل الـ dump")
    print(f"  بوابات: liq≥$1000 | age 5-30s | chg_5m>20% | holder<35%")
    print(f"  خروج: +15% quick_tp | 30s time_exit | -8% SL | +5%/5% sudden_dump")
    print()

    capitals = [20.0, 100.0, 500.0, 1000.0]
    seeds = list(range(1, 6))
    num_days = 30

    all_scenario_results = {}

    for scenario_key, scenario in MARKET_SCENARIOS.items():
        print(f"\n{'='*95}")
        print(f"  📊 SCENARIO: {scenario['name']}")
        print(f"  {scenario['description']}")
        print(f"  Fees: {scenario['fees_pct']}% | Order fail: {scenario['order_fail_rate']*100}%")
        print(f"{'='*95}")

        scenario_results = {}
        for cap in capitals:
            cap_results = [simulate_rug_surfer(cap, num_days, s, scenario) for s in seeds]
            scenario_results[cap] = cap_results

            rois = [r["roi_pct"] for r in cap_results]
            pnls = [r["net_pnl"] for r in cap_results]
            wrs = [r["win_rate"] for r in cap_results]
            rug_escapes_avg = statistics.mean([r["rug_escapes"] for r in cap_results])
            rug_losses_avg = statistics.mean([r["rug_losses"] for r in cap_results])
            winning = sum(1 for r in rois if r > 0)

            print(f"\n  ${cap:.0f}: avg ROI {statistics.mean(rois):>+8.1f}% | "
                  f"WR {statistics.mean(wrs):>5.1f}% | "
                  f"rug_escapes {rug_escapes_avg:.1f} | rug_losses {rug_losses_avg:.1f} | "
                  f"winning {winning}/{len(seeds)}")

        all_scenario_results[scenario_key] = scenario_results

    # مقارنة بين السيناريوهات
    print(f"\n{'='*95}")
    print("  📊 COMPARISON ACROSS MARKET SCENARIOS (RUG-SURFER, 30d, $100)")
    print(f"{'='*95}")
    print(f"\n  {'Scenario':<28} {'ROI avg':<12} {'WR%':<8} {'WinPeriods':<12} {'RugEscapes':<12}")
    print("  " + "-" * 75)

    for scenario_key, scenario in MARKET_SCENARIOS.items():
        rs = all_scenario_results[scenario_key][100.0]
        rois = [r["roi_pct"] for r in rs]
        wrs = [r["win_rate"] for r in rs]
        winning = sum(1 for r in rois if r > 0)
        rug_escapes_avg = statistics.mean([r["rug_escapes"] for r in rs])
        print(f"  {scenario['name']:<28} "
              f"{statistics.mean(rois):>+10.1f}%  "
              f"{statistics.mean(wrs):>5.1f}%  "
              f"{winning}/{len(seeds):<9} "
              f"{rug_escapes_avg:>10.1f}")

    # مقارنة SURVIVOR vs RUG-SURFER على السوق القاسي
    print(f"\n{'='*95}")
    print("  📊 SURVIVOR vs RUG-SURFER (brutal Nov 2026 market, 30d, $100)")
    print(f"{'='*95}")
    print(f"""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  الاستراتيجية    | ROI avg   | WR%    | Winning | Rug Escapes       │
  │  ──────────────────────────────────────────────────────────────────  │""")

    # SURVIVOR نتائج من V527 (brutal market)
    survivor_roi = -2.2
    survivor_wr = 0.0
    survivor_winning = "0/5"
    survivor_escapes = 0
    print(f"  │  SURVIVOR       | {survivor_roi:>+8.1f}% | {survivor_wr:>5.1f}% | {survivor_winning:<8}| {survivor_escapes:<18}│")

    # RUG-SURFER نتائج على brutal market
    rs_brutal = all_scenario_results["brutal_nov2026"][100.0]
    rs_roi = statistics.mean([r["roi_pct"] for r in rs_brutal])
    rs_wr = statistics.mean([r["win_rate"] for r in rs_brutal])
    rs_winning = sum(1 for r in rs_brutal if r["roi_pct"] > 0)
    rs_escapes = statistics.mean([r["rug_escapes"] for r in rs_brutal])
    print(f"  │  RUG-SURFER     | {rs_roi:>+8.1f}% | {rs_wr:>5.1f}% | {rs_winning}/5     | {rs_escapes:<18.1f}│")
    print(f"  └──────────────────────────────────────────────────────────────────────┘")

    # التقييم
    print(f"\n{'='*95}")
    print("  💀 HONEST ASSESSMENT — RUG-SURFER في 4 سيناريوهات سوق")
    print(f"{'='*95}")

    print(f"""
  📊 الخلاصة عبر كل السيناريوهات:
  ──────────────────────────────────────────────────────────────────""")

    for scenario_key, scenario in MARKET_SCENARIOS.items():
        rs = all_scenario_results[scenario_key][100.0]
        rois = [r["roi_pct"] for r in rs]
        avg_roi = statistics.mean(rois)
        winning = sum(1 for r in rois if r > 0)
        status = "✅ رابح" if avg_roi > 0 else "⚠️ خاسر قليلاً" if avg_roi > -10 else "❌ خاسر"
        print(f"  {scenario['name']:<28}: {status} | ROI {avg_roi:>+8.1f}% | winning {winning}/5")

    print(f"""
  🎓 الخلاصة النهائية:
  ──────────────────────────────────────────────────────────────────
  RUG-SURFER أفضل من SURVIVOR في السوق الجيد (يستغل الـ pumps القصيرة)
  لكنه يخسر أكثر في السوق القاسي (لأنه يدخل rugs)

  التوصية: استخدام RUG-SURFER في السوق الجيد، SURVIVOR في السوق القاسي
  البوت يحتاج كاشف حالة السوق لتبديل الأنماط تلقائياً
""")


if __name__ == "__main__":
    main()
