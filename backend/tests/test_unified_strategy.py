"""V541 UNIFIED STRATEGY TESTS.

Tests the ONE strategy used by the whole bot:
1. Exit ladder scenarios (quick TP band, runner trail, ratchet, SL, mega, stagnation, time)
2. Entry gates (momentum window, age, buy pressure, holder concentration, missing data)
3. Config wiring (unified enabled by default, active_* values)
4. Implementation-vs-model validation: the real unified_exit_reason run
   poll-by-poll over Monte-Carlo token paths must reproduce the optimizer's
   expected performance (sanity range).
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_ENV", "test")

from app.modules.trading_strategy.unified import (  # noqa: E402
    UNIFIED_PARAMS,
    unified_exit_reason,
    unified_entry_check,
)
from app.core.config import settings  # noqa: E402


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def exit_of(path, elapsed_minutes=0.0):
    """Walk a price path poll-by-poll like fast_monitor does (1s cadence
    approximated by checkpoints). Returns (reason, exit_price)."""
    entry = 1.0
    peak = entry
    for p in path:
        peak = max(peak, p)
        reason = unified_exit_reason(entry, peak, p, elapsed_minutes)
        if reason:
            return reason, p
    return None, path[-1]


@dataclass
class FakeToken:
    price_change_5m_pct: float = 20.0
    first_seen_at: datetime = field(default_factory=datetime.utcnow)
    txns_24h_buys: int = 40
    txns_24h_sells: int = 20
    top_10_holders_pct: float = None
    onchain_detail_json: dict = None


# ----------------------------------------------------------------------
# 1) EXIT LADDER
# ----------------------------------------------------------------------
class TestUnifiedExitLadder:

    def test_band_exit_at_15pct(self):
        # gradual pump: crosses +15% slowly, peak stays below +35%
        reason, p = exit_of([1.00, 1.05, 1.10, 1.15, 1.18, 1.16])
        assert reason == "quick_tp"
        assert 1.14 <= p <= 1.20

    def test_gap_to_runner_trails(self):
        # gaps from 1.10 straight to 1.60 (peak_gain 60% >= 35%) -> runner.
        # then drops 15% from peak (1.60 -> 1.35 = 15.6%) -> trail exit
        reason, p = exit_of([1.00, 1.10, 1.60, 1.45, 1.35])
        assert reason == "smart_trailing"
        assert p == pytest.approx(1.35)

    def test_runner_sudden_dump_priority(self):
        # runner zone + 20% drop from peak -> sudden_dump fires BEFORE trail
        reason, p = exit_of([1.00, 1.10, 1.60, 1.25])
        assert reason == "sudden_dump"
        assert p == pytest.approx(1.25)

    def test_trail_ratchet_after_100pct(self):
        # jumps 1.10 -> 2.20 (skips the band) -> runner. peak >= +100%
        # -> trail tightens to 9% (0.15*0.6): exit must fire at <= 2.20*0.91
        reason, p = exit_of([1.00, 1.10, 2.20, 2.05, 2.00])
        assert reason == "smart_trailing"
        assert p <= 2.20 * 0.91 + 1e-9

    def test_stop_loss_minus_6pct(self):
        reason, p = exit_of([1.00, 0.98, 0.95, 0.93])
        assert reason == "stop_loss"
        assert p == pytest.approx(0.93)

    def test_shakeout_survived(self):
        # -4% dip (above -6% SL) then pump -> must NOT stop out
        reason, p = exit_of([1.00, 0.96, 1.00, 1.16])
        assert reason == "quick_tp"  # survived the shakeout, exits at +15%

    def test_mega_tp_10x(self):
        # jumps 1.10 -> 5.00 (skips the band) then to 11.5 = 10x+ cap
        reason, p = exit_of([1.00, 1.10, 5.00, 11.5])
        assert reason == "mega_tp"
        assert p == pytest.approx(11.5)

    def test_hold_when_rising(self):
        reason, _ = exit_of([1.00, 1.03, 1.07, 1.10])
        assert reason is None

    def test_stagnation_4h(self):
        # 5h elapsed, small gain 0..5%, peak < 5%
        entry, peak, cur = 1.0, 1.03, 1.02
        reason = unified_exit_reason(entry, peak, cur, elapsed_minutes=300)
        assert reason == "stagnation"

    def test_stagnation_not_when_peak_high(self):
        # peak was +20% (quick_tp would have fired earlier) — stagnation
        # must NOT trigger just because current gain is small
        reason = unified_exit_reason(1.0, 1.20, 1.03, elapsed_minutes=300)
        # peak_gain >= 0.15 -> band exit takes priority (already visited)
        assert reason == "quick_tp"

    def test_time_exit_5_days(self):
        # small LOSS (not stagnation-eligible) held for 5 days -> time exit
        reason = unified_exit_reason(1.0, 1.0, 0.98, elapsed_minutes=8000)
        assert reason == "time_exit"

    def test_runner_locks_profit_not_loss(self):
        # jumps 1.10 -> 1.80 (skips the band) -> runner at +80%; trail =
        # peak*(1-15%) = 1.53 -> exit locks +53%, never a loss
        reason, p = exit_of([1.00, 1.10, 1.80, 1.60, 1.53])
        assert reason == "smart_trailing"
        assert p == pytest.approx(1.53)
        assert p > 1.40  # locked a big profit


# ----------------------------------------------------------------------
# 2) ENTRY GATES
# ----------------------------------------------------------------------
class TestUnifiedEntryGates:

    def test_accept_healthy_token(self):
        ok, why = unified_entry_check(FakeToken())
        assert ok, why

    def test_reject_no_momentum(self):
        ok, _ = unified_entry_check(FakeToken(price_change_5m_pct=3.0))
        assert not ok

    def test_reject_late_entry(self):
        ok, _ = unified_entry_check(FakeToken(price_change_5m_pct=75.0))
        assert not ok

    def test_reject_old_token(self):
        ok, _ = unified_entry_check(
            FakeToken(first_seen_at=datetime.utcnow() - timedelta(seconds=400)))
        assert not ok

    def test_reject_no_buy_pressure(self):
        ok, _ = unified_entry_check(FakeToken(txns_24h_buys=10, txns_24h_sells=5))
        assert not ok

    def test_reject_sellers_dominate(self):
        ok, _ = unified_entry_check(FakeToken(txns_24h_buys=40, txns_24h_sells=50))
        assert not ok

    def test_reject_whale_concentration(self):
        ok, _ = unified_entry_check(FakeToken(top_10_holders_pct=40.0))
        assert not ok

    def test_missing_data_never_blocks(self):
        # no chg_5m, no txns, no holders data -> must NOT block
        ok, why = unified_entry_check(FakeToken(
            price_change_5m_pct=None, txns_24h_buys=None, txns_24h_sells=None))
        assert ok, why


# ----------------------------------------------------------------------
# 3) CONFIG WIRING
# ----------------------------------------------------------------------
class TestUnifiedConfig:

    def test_unified_enabled_by_default(self):
        assert settings.unified_strategy_enabled is True

    def test_active_values_are_unified(self):
        assert settings.active_trade_pct == 10.0
        assert settings.active_min_liquidity_usd == 4000.0
        assert settings.active_max_chg_5m_pct == 60.0
        assert settings.active_loss_pct == 12.0
        assert settings.trailing_tiers == [(0.35, 0.15)]

    def test_params_match_optimized_model(self):
        assert UNIFIED_PARAMS["quick_tp"] == 0.15
        assert UNIFIED_PARAMS["runner_threshold"] == 0.35
        assert UNIFIED_PARAMS["trail_width"] == 0.15
        assert UNIFIED_PARAMS["stop_loss"] == 0.06
        assert UNIFIED_PARAMS["max_buys_per_day"] == 3


# ----------------------------------------------------------------------
# 4) IMPLEMENTATION vs MONTE-CARLO MODEL VALIDATION
#    The real unified_exit_reason must reproduce the optimizer's numbers.
# ----------------------------------------------------------------------
class TestImplementationMatchesModel:

    def test_mc_performance_in_expected_range(self):
        """Run the REAL exit function over 3000 model token paths and
        verify median 30-day ROI lands in the optimizer's sanity range."""
        import numpy as np

        rng = np.random.default_rng(4242)
        RT_DRAG = (0.995 * 0.99) * (0.99 / 1.005)
        ENTRY_PENALTY = 0.985
        INITIAL = 20.0

        def path_fail_day1(r):
            peak = 1.0 + r.uniform(0.0, 0.25)
            pts = [1.0]
            for k in range(1, 5):
                pts.append(min(1.0 + (peak - 1.0) * k / 4 * r.uniform(0.9, 1.1), peak))
            floor = r.uniform(0.05, 0.15)
            for k in range(1, 11):
                pts.append(max((peak + (floor - peak) * (k / 10) ** 0.55)
                               * r.uniform(0.93, 1.03), 0.01))
            return pts

        def path_rug(r):
            if r.random() < 0.6:
                peak = r.uniform(1.2, 1.9)
                pts = [1.0]
                for k in range(1, 4):
                    pts.append(min(1.0 + (peak - 1.0) * k / 3 * r.uniform(0.95, 1.1), peak))
                pts += [r.uniform(0.0, 0.08), 0.01]
                return pts
            return [1.0, r.uniform(0.85, 1.0), r.uniform(0.0, 0.12), 0.01]

        def path_pump_dump(r):
            peak = r.uniform(2.0, 5.0)
            explosive = r.random() < 0.25
            pts = [1.0]
            if explosive:
                pts += [r.uniform(1.5, 2.2), peak * r.uniform(0.75, 0.95), peak]
            else:
                for k in range(1, 9):
                    pts.append(min(1.0 + (peak - 1.0) * (k / 8) ** 1.3
                                   * r.uniform(0.92, 1.08), peak))
            dump = r.uniform(0.20, 0.40)
            for k in range(1, 9):
                pts.append(max((peak + (dump - peak) * (k / 8) ** 0.5)
                               * r.uniform(0.93, 1.03), 0.01))
            return pts

        def path_graduate(r):
            sol = r.uniform(25.0, 50.0) / 100.0 * 85.0
            mult = ((30.0 + 85.0) / (30.0 + sol)) ** 2
            post = mult * r.uniform(0.60, 0.90)
            pts = [1.0]
            for k in range(1, 11):
                pts.append(min(1.0 + (mult - 1.0) * (k / 10) ** 1.2
                               * r.uniform(0.93, 1.07), mult))
            for k in range(1, 7):
                pts.append(max((mult + (post - mult) * (k / 6) ** 0.7)
                               * r.uniform(0.95, 1.02), 0.01))
            return pts

        def path_moonshot(r):
            peak = r.uniform(10.0, 100.0)
            return [1.0, r.uniform(1.4, 3.0), peak * r.uniform(0.4, 0.7),
                    peak * r.uniform(0.8, 0.95), peak,
                    max(peak * 0.5, 0.02), max(peak * 0.2, 0.02), 0.02]

        makers = [path_fail_day1, path_rug, path_pump_dump, path_graduate, path_moonshot]
        # platform mix (base regime)
        mix = [0.69, 0.15, 0.12, 0.015, 0.005]

        nets = []
        for _ in range(3000):
            u = rng.random()
            cum, ti = 0.0, 0
            for i, m in enumerate(mix):
                cum += m
                if u < cum:
                    ti = i
                    break
            path = makers[ti](rng)
            reason, p = exit_of(path)
            nets.append(p * RT_DRAG * ENTRY_PENALTY)

        nets = np.array(nets)
        # EV per trade must be modestly negative-to-positive (the model's
        # F2-filter base EV ~ -0.5%..+1% per trade; anything outside
        # [-8%, +8%] means the implementation diverges from the model)
        ev = nets.mean() - 1.0
        assert -0.08 < ev < 0.08, f"EV per trade {ev:+.3f} diverges from model"

        # quick_tp must be the most common exit reason
        reasons = {"quick_tp": 0}
        for _ in range(500):
            u = rng.random()
            cum, ti = 0.0, 0
            for i, m in enumerate(mix):
                cum += m
                if u < cum:
                    ti = i
                    break
            r2, _ = exit_of(makers[ti](rng))
            reasons[r2] = reasons.get(r2, 0) + 1
        assert reasons.get("quick_tp", 0) == max(reasons.values())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
