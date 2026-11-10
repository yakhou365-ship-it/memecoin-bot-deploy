"""Strategy V2 tests. Run: python -m pytest tests/test_strategy_v2.py -q   (or python tests/test_strategy_v2.py)"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.strategy_v2 import StrategyConfig, breakeven_win_rate
from app.modules.strategy_v2.backtester import run_backtest, simulate_symbol
from app.modules.strategy_v2.data import synthetic_market
from app.modules.strategy_v2.position import Position
from app.modules.strategy_v2.signals import build_features

CFG = StrategyConfig(require_market_ok=False)


def test_breakeven_formula():
    assert abs(breakeven_win_rate(0.005, 0.008, 0.003) - 0.846) < 0.001   # the OLD strategy
    assert abs(breakeven_win_rate(0.009, 0.009, 0.003) - 0.667) < 0.001   # 1R/1R at 0.9% is still costly: runner must pay


def test_initial_stop_is_enforced():
    p = Position(side=1, entry=100.0, atr=1.0, cfg=CFG)      # stop 98.5, tp1 101.5
    p.on_bar(100, 100.4, 98.0, 99)                            # hits stop
    assert p.done and p.fills[-1].reason == "stop" and p.fills[-1].price == 98.5


def test_gap_through_stop_fills_at_open():
    p = Position(side=1, entry=100.0, atr=1.0, cfg=CFG)
    p.on_bar(97.0, 97.5, 96.0, 97.0)
    assert p.fills[-1].price == 97.0


def test_stop_wins_when_bar_touches_both():
    p = Position(side=1, entry=100.0, atr=1.0, cfg=CFG)
    p.on_bar(100, 102, 98, 100)
    assert p.fills[-1].reason == "stop"


def test_partial_tp_then_breakeven_stop_not_a_loss():
    p = Position(side=1, entry=100.0, atr=1.0, cfg=CFG)
    p.on_bar(100, 101.6, 99.9, 101.4)                        # TP1 hit, 40% closed
    assert p.tp1_done and abs(p.remaining - (1 - CFG.partial_fraction)) < 1e-9  # Ultimate-bot: was hardcoded 0.5, but partial_fraction=0.40 → remaining=0.60
    assert p.stop >= 100 * (1 + CFG.costs.round_trip)         # stop at >= breakeven + costs
    # V539: الشمعة تفتح فوق وقف التعادل الجديد (entry×(1+(RT−frac×g₁)/remaining)
    # = 100×1.0408 = 104.08) ثم تلمسه — الوقف يُنفذ عند مستواه وليس بفجوة تحته.
    # الفجوة تحت الوقف تغطيها حالة test_gap_through_stop_fills_at_open.
    p.on_bar(104.5, 105.0, 99.0, 99.5)                        # stopped on runner
    assert p.done
    gross = sum(f.fraction * (f.price - 100) / 100 for f in p.fills)
    assert gross - CFG.costs.round_trip > -0.0006             # ~breakeven after costs


def test_time_stop():
    p = Position(side=1, entry=100.0, atr=1.0, cfg=CFG)
    for _ in range(CFG.time_stop_bars):
        p.on_bar(100, 100.3, 99.7, 100.0)
    assert p.done and p.fills[-1].reason == "time_stop"


def test_short_mirror():
    p = Position(side=-1, entry=100.0, atr=1.0, cfg=CFG)
    assert p.stop == 101.5 and p.tp1 == 98.5
    p.on_bar(100, 101.6, 99.9, 101)
    assert p.done and p.fills[-1].reason == "stop"


def test_no_lookahead_in_signals():
    df = synthetic_market(1, days=40, seed=3)["SYN0"]
    full = build_features(df, CFG)
    for cut in (1500, 2500, 3500):
        part = build_features(df.iloc[:cut], CFG)
        a = full[["long_signal", "regime", "atr"]].iloc[cut - 1]
        b = part[["long_signal", "regime", "atr"]].iloc[-1]
        assert bool(a.long_signal) == bool(b.long_signal) and a.regime == b.regime
        assert abs(a.atr - b.atr) < 1e-9


def test_no_edge_on_random_walk_after_costs():
    """With zero drift there is no edge: net expectancy must NOT be reliably positive."""
    rs = []
    for seed in range(1, 5):
        data = synthetic_market(8, days=120, seed=seed, regime_switching=False)
        res = run_backtest(data, CFG.with_(min_atr_pct=0.0), market_ref=None)
        if res["stats"]["n"] >= 20:
            rs.append(res["stats"]["expectancy_r"])
    assert rs and np.mean(rs) < 0.10, f"suspicious edge on random data: {rs}"


def test_trades_respect_position_lock():
    data = synthetic_market(1, days=60, seed=5)
    tr = simulate_symbol("SYN0", data["SYN0"], CFG.with_(min_atr_pct=0.0))
    for a, b in zip(tr, tr[1:]):
        assert b.entry_time > a.exit_time


def test_cost_gate_blocks_small_tp_on_pumpfun():
    from app.modules.strategy_v2.gate import cost_gate

    class T:
        chain, dex = "solana", "pump.fun"
    assert not cost_gate(T, 1.0, 0.99, 1.01).allowed          # 1% TP vs 3% round trip
    assert cost_gate(T, 1.0, 0.95, 1.15).allowed


def test_engine_rejects_short_history_and_is_fast():
    import time
    from app.modules.strategy_v2 import StrategyV2Engine
    df = synthetic_market(1, days=40, seed=2)["SYN0"]
    e = StrategyV2Engine(CFG)
    assert not e.evaluate(df.iloc[:300]).should_trade
    t = time.time(); e.evaluate(df); assert time.time() - t < 1.0


def test_sizing_never_exceeds_cap_and_min_notional_skips():
    from app.modules.strategy_v2.backtester import Trade, portfolio
    t0 = pd.Timestamp("2025-01-01")
    tr = [Trade("X", 1, t0, t0 + pd.Timedelta(hours=1), 100, 0.001, 0.01, 5.0, 4, "trail_stop")]
    pf = portfolio(tr, CFG, 1000.0)
    assert pf["trades"][0]["notional"] <= 1000 * CFG.max_position_pct + 1e-9
    pf2 = portfolio(tr, CFG, 10.0)                              # $10 account -> below $5 min notional
    assert pf2["skipped"]["min_notional"] == 1 or pf2["trades"][0]["notional"] >= CFG.min_notional_usd


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f(); print("PASS", f.__name__)
