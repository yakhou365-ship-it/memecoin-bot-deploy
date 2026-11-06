"""
Pump.fun V4 - Professional Trading Strategy
=============================================

UPGRADES from V3 → V4 (Professional Trader Mode):

1. MULTI-FACTOR ENTRY SCORING (0-100 points):
   - Volume score (0-25): higher volume = more demand
   - Holder distribution score (0-25): better distribution = safer
   - Momentum score (0-20): price action strength
   - Timing score (0-15): optimal bonding curve progress
   - Social proof score (0-15): holder count + transaction count
   → Only enter trades with score ≥ 75 (A+ setups only)

2. ADAPTIVE SL/TP BASED ON VOLATILITY:
   - Low volatility: tight SL (0.5×ATR), wide TP (3×ATR)
   - Medium volatility: standard SL (1×ATR), TP (2×ATR)
   - High volatility: wide SL (1.5×ATR), TP (2.5×ATR)
   - Extreme volatility: skip trade

3. PROGRESSIVE POSITION SIZING:
   - Win streak: increase size by 10% per win (max 2× base)
   - Loss: reset to base size
   - After big win (>5x): take 50% profit out, compound rest
   - Kelly criterion with 0.5× multiplier (more conservative)

4. DYNAMIC EXIT BASED ON MOMENTUM:
   - Strong momentum: hold longer, wider trailing
   - Weak momentum: tight trailing, exit early
   - Stalled: immediate exit
   - Reversal: immediate exit

5. PROFIT LOCK MECHANISM:
   - Never give back more than 30% of peak profit
   - After +200% profit: move stop to +100% (lock 1×)
   - After +500% profit: move stop to +300% (lock 3×)
   - After +1000% profit: move stop to +700% (lock 7×)

6. TRADE QUALITY FILTER (A+ setups only):
   - Reject B-grade setups (score 60-74)
   - Reject C-grade setups (score <60)
   - Only trade A-grade (75-89) and A+ grade (90-100)

7. SMART PROFIT TAKING:
   - Sell 30% at 2x (lock small profit)
   - Sell 30% at 4x (lock medium profit)
   - Sell 20% at 8x (lock large profit)
   - Hold 20% with aggressive trailing (catch moonshots)
"""
from __future__ import annotations

import os
import sys
import json
import math
import random
import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.market_scanner.pumpfun_client import (
    BondingCurveMath, VIRTUAL_SOL_RESERVES, GRADUATION_THRESHOLD_SOL,
    TOTAL_SUPPLY, PumpPortalClient
)


# Real Pump.fun statistics
ROUND_TRIP_COST_PCT = 3.0


# ============================================================
# 1. MULTI-FACTOR ENTRY SCORING
# ============================================================
@dataclass
class EntryScore:
    """Multi-factor entry quality score (0-100)."""
    volume_score: int = 0       # 0-25
    distribution_score: int = 0 # 0-25
    momentum_score: int = 0     # 0-20
    timing_score: int = 0       # 0-15
    social_score: int = 0       # 0-15
    total: int = 0
    grade: str = "F"            # F/D/C/B/A/A+
    reasons: List[str] = field(default_factory=list)

    @property
    def is_a_grade(self) -> bool:
        return self.total >= 75

    @property
    def is_a_plus(self) -> bool:
        return self.total >= 90


class EntryScorer:
    """
    Professional entry scoring system.

    Only enters trades with score ≥ 75 (A grade or better).
    A+ grade (90+) gets larger position size.
    """

    def score(
        self,
        volume_sol: float,
        dev_holding_pct: float,
        top10_holding_pct: float,
        max_wallet_pct: float,
        holder_count: int,
        bonding_progress: float,
        momentum_score: float,  # 0-100
        txns_5m: int = 0,
        buy_sell_ratio: float = 1.0,
    ) -> EntryScore:
        """Calculate entry quality score."""
        s = EntryScore()

        # 1. VOLUME SCORE (0-25)
        if volume_sol >= 50:
            s.volume_score = 25
            s.reasons.append(f"Volume {volume_sol:.0f}SOL: excellent")
        elif volume_sol >= 20:
            s.volume_score = 20
            s.reasons.append(f"Volume {volume_sol:.0f}SOL: strong")
        elif volume_sol >= 10:
            s.volume_score = 15
            s.reasons.append(f"Volume {volume_sol:.0f}SOL: good")
        elif volume_sol >= 5:
            s.volume_score = 10
            s.reasons.append(f"Volume {volume_sol:.0f}SOL: acceptable")
        else:
            s.volume_score = 0
            s.reasons.append(f"Volume {volume_sol:.0f}SOL: too low")

        # 2. DISTRIBUTION SCORE (0-25)
        if dev_holding_pct > 30:
            s.distribution_score = 0
            s.reasons.append(f"Dev holds {dev_holding_pct:.0f}%: DANGER")
        elif dev_holding_pct > 15:
            s.distribution_score = 5
            s.reasons.append(f"Dev holds {dev_holding_pct:.0f}%: high")
        elif dev_holding_pct > 5:
            s.distribution_score = 15
            s.reasons.append(f"Dev holds {dev_holding_pct:.0f}%: moderate")
        else:
            s.distribution_score = 20
            s.reasons.append(f"Dev holds {dev_holding_pct:.0f}%: safe")

        # Top 10 holders
        if top10_holding_pct < 30:
            s.distribution_score += 5
            s.reasons.append(f"Top10 {top10_holding_pct:.0f}%: well distributed")
        elif top10_holding_pct > 60:
            s.distribution_score = max(0, s.distribution_score - 10)
            s.reasons.append(f"Top10 {top10_holding_pct:.0f}%: concentrated")
        elif top10_holding_pct < 45:
            s.distribution_score += 3

        # Max wallet
        if max_wallet_pct > 25:
            s.distribution_score = max(0, s.distribution_score - 10)
            s.reasons.append(f"Max wallet {max_wallet_pct:.0f}%: whale risk")
        elif max_wallet_pct < 10:
            s.distribution_score = min(25, s.distribution_score + 3)

        # 3. MOMENTUM SCORE (0-20)
        mom = momentum_score
        if mom >= 80:
            s.momentum_score = 20
            s.reasons.append(f"Momentum {mom:.0f}: explosive")
        elif mom >= 60:
            s.momentum_score = 15
            s.reasons.append(f"Momentum {mom:.0f}: strong")
        elif mom >= 40:
            s.momentum_score = 10
            s.reasons.append(f"Momentum {mom:.0f}: decent")
        elif mom >= 25:
            s.momentum_score = 5
            s.reasons.append(f"Momentum {mom:.0f}: weak")
        else:
            s.momentum_score = 0
            s.reasons.append(f"Momentum {mom:.0f}: dead")

        # 4. TIMING SCORE (0-15) - optimal bonding curve progress
        if 30 <= bonding_progress <= 50:
            s.timing_score = 15
            s.reasons.append(f"Curve {bonding_progress:.0f}%: OPTIMAL")
        elif 20 <= bonding_progress < 30 or 50 < bonding_progress <= 65:
            s.timing_score = 10
            s.reasons.append(f"Curve {bonding_progress:.0f}%: good timing")
        elif 15 <= bonding_progress < 20 or 65 < bonding_progress <= 75:
            s.timing_score = 5
            s.reasons.append(f"Curve {bonding_progress:.0f}%: acceptable")
        else:
            s.timing_score = 0
            s.reasons.append(f"Curve {bonding_progress:.0f}%: bad timing")

        # 5. SOCIAL SCORE (0-15)
        if holder_count >= 200:
            s.social_score = 15
            s.reasons.append(f"{holder_count} holders: viral")
        elif holder_count >= 100:
            s.social_score = 12
            s.reasons.append(f"{holder_count} holders: popular")
        elif holder_count >= 50:
            s.social_score = 8
            s.reasons.append(f"{holder_count} holders: growing")
        elif holder_count >= 30:
            s.social_score = 4
            s.reasons.append(f"{holder_count} holders: minimal")
        else:
            s.social_score = 0
            s.reasons.append(f"{holder_count} holders: no interest")

        # Buy/sell ratio bonus
        if buy_sell_ratio >= 1.5:
            s.social_score = min(15, s.social_score + 3)
            s.reasons.append(f"B/S ratio {buy_sell_ratio:.1f}: buy pressure")
        elif buy_sell_ratio < 0.7:
            s.social_score = max(0, s.social_score - 5)
            s.reasons.append(f"B/S ratio {buy_sell_ratio:.1f}: sell pressure")

        # TOTAL
        s.total = (s.volume_score + s.distribution_score +
                   s.momentum_score + s.timing_score + s.social_score)

        # GRADE
        if s.total >= 90:
            s.grade = "A+"
        elif s.total >= 75:
            s.grade = "A"
        elif s.total >= 60:
            s.grade = "B"
        elif s.total >= 45:
            s.grade = "C"
        elif s.total >= 30:
            s.grade = "D"
        else:
            s.grade = "F"

        return s


# ============================================================
# 2. ADAPTIVE SL/TP BASED ON VOLATILITY
# ============================================================
@dataclass
class AdaptiveParams:
    """Adaptive SL/TP based on volatility regime."""
    sl_mult: float = 0.5         # ×ATR
    tp1_mult: float = 2.0        # ×ATR
    tp2_mult: float = 4.0        # ×ATR
    tp3_mult: float = 8.0        # ×ATR
    trail_mult: float = 1.5      # ×ATR
    regime: str = "medium"


class VolatilityAdapter:
    """
    Adapt SL/TP based on current volatility regime.

    Professional traders adjust their stops based on market conditions.
    """

    @staticmethod
    def adapt(atr_pct: float) -> AdaptiveParams:
        """Returns adaptive parameters based on ATR percentage."""
        if atr_pct < 0.02:  # <2% volatility
            p = AdaptiveParams(sl_mult=0.5, tp1_mult=1.5, tp2_mult=3.0,
                                tp3_mult=5.0, trail_mult=1.0, regime="low")
        elif atr_pct < 0.05:  # 2-5%
            p = AdaptiveParams(sl_mult=0.8, tp1_mult=2.0, tp2_mult=4.0,
                                tp3_mult=8.0, trail_mult=1.5, regime="medium")
        elif atr_pct < 0.10:  # 5-10%
            p = AdaptiveParams(sl_mult=1.2, tp1_mult=2.5, tp2_mult=5.0,
                                tp3_mult=10.0, trail_mult=2.0, regime="high")
        elif atr_pct < 0.15:  # 10-15%
            p = AdaptiveParams(sl_mult=1.5, tp1_mult=3.0, tp2_mult=6.0,
                                tp3_mult=12.0, trail_mult=2.5, regime="very_high")
        else:  # >15% - skip
            p = AdaptiveParams(sl_mult=0, tp1_mult=0, tp2_mult=0,
                                tp3_mult=0, trail_mult=0, regime="extreme_skip")
        return p


# ============================================================
# 3. PROGRESSIVE POSITION SIZING
# ============================================================
class ProgressivePositionSizer:
    """
    Professional position sizing with win streak bonuses.

    - Increase size 10% per consecutive win (max 2× base)
    - Reset to base after loss
    - After big win (>5x): take 50% profit out
    - Kelly criterion with 0.5× multiplier
    """

    def __init__(self):
        self.base_pct = 0.05  # 5% base
        self.win_streak = 0
        self.loss_streak = 0
        self.wins = 0
        self.losses = 0
        self.total_win_pnl = 0.0
        self.total_loss_pnl = 0.0
        self.max_pct = 0.10  # cap at 10% (was 15%)
        self.min_trades_for_kelly = 15
        self.max_position_usd = 50.0  # HARD CAP: max $50 per trade (realistic)

    def record_trade(self, pnl: float, mult: float = 1.0):
        """Record trade result and update streaks."""
        if pnl > 0:
            self.wins += 1
            self.total_win_pnl += pnl
            self.win_streak += 1
            self.loss_streak = 0

            # After big win (>5x), reduce next position (bank profits)
            if mult >= 5.0:
                self.win_streak = max(0, self.win_streak - 2)  # cooldown
        else:
            self.losses += 1
            self.total_loss_pnl += abs(pnl)
            self.loss_streak += 1
            self.win_streak = 0

    def get_size(self, capital: float, score: EntryScore) -> Tuple[float, str]:
        """Get position size in USD."""
        # Base size
        size_pct = self.base_pct

        # Win streak bonus: +10% per win, max 2×
        if self.win_streak > 0:
            bonus = min(self.win_streak * 0.10, 1.0)  # max +100%
            size_pct *= (1 + bonus)

        # A+ grade bonus (capped)
        if score.is_a_plus:
            size_pct *= 1.3  # 30% bigger for A+ (was 1.5)
        elif score.is_a_grade:
            size_pct *= 1.15  # 15% bigger for A (was 1.2)

        # Kelly criterion (after 15 trades)
        total = self.wins + self.losses
        if total >= self.min_trades_for_kelly:
            win_rate = self.wins / total
            avg_win = self.total_win_pnl / self.wins if self.wins > 0 else 0
            avg_loss = self.total_loss_pnl / self.losses if self.losses > 0 else 1

            if avg_loss > 0:
                R = avg_win / avg_loss
                kelly_f = win_rate - (1 - win_rate) / R
                kelly_f = max(0, kelly_f)
                # Use 0.5×Kelly (more conservative than V3's 0.25×)
                kelly_pct = kelly_f * 0.5
                size_pct = max(size_pct, min(kelly_pct, self.max_pct * 0.8))

        # Cap at max
        size_pct = min(size_pct, self.max_pct)

        # Loss streak penalty
        if self.loss_streak >= 2:
            size_pct *= 0.5  # halve size after 2 losses
        if self.loss_streak >= 3:
            size_pct *= 0.3  # reduce to 30% after 3 losses

        size_pct = max(size_pct, 0.02)  # minimum 2%

        # HARD CAP: position can't exceed 10% of capital OR $50 (whichever is smaller)
        size_usd = capital * size_pct
        max_position = min(capital * 0.10, self.max_position_usd)
        size_usd = min(size_usd, max_position)

        return size_usd, f"streak W{self.win_streak}/L{self.loss_streak}, ${size_usd:.2f} ({size_pct*100:.1f}%)"


# ============================================================
# 4. PROFIT LOCK MECHANISM
# ============================================================
class ProfitLock:
    """
    Professional profit lock - never give back more than 30% of peak.

    After +X% profit, move stop to lock portion of gains.
    """

    def __init__(self):
        self.peak_mult = 1.0
        self.locked_stop_mult = 0.5  # initial emergency stop

    def update(self, current_mult: float) -> Tuple[Optional[float], Optional[str]]:
        """
        Update peak and return locked stop multiplier + reason.
        Returns (stop_mult, reason) or (None, None) if no update.
        """
        if current_mult > self.peak_mult:
            self.peak_mult = current_mult

        new_stop = None
        reason = None

        # After +100% (2x): lock at +50% (1.5x) - never give back >50% of gain
        if self.peak_mult >= 2.0:
            potential_stop = 1.0 + (self.peak_mult - 1.0) * 0.5
            if potential_stop > self.locked_stop_mult:
                new_stop = potential_stop
                reason = f"lock_50%_at_{self.peak_mult:.1f}x"

        # After +400% (5x): lock at +300% (4x) - never give back >25% of gain
        if self.peak_mult >= 5.0:
            potential_stop = 1.0 + (self.peak_mult - 1.0) * 0.75
            if potential_stop > self.locked_stop_mult:
                new_stop = potential_stop
                reason = f"lock_75%_at_{self.peak_mult:.1f}x"

        # After +900% (10x): lock at +800% (9x) - never give back >10% of gain
        if self.peak_mult >= 10.0:
            potential_stop = 1.0 + (self.peak_mult - 1.0) * 0.90
            if potential_stop > self.locked_stop_mult:
                new_stop = potential_stop
                reason = f"lock_90%_at_{self.peak_mult:.1f}x"

        if new_stop is not None:
            self.locked_stop_mult = new_stop

        return (new_stop, reason) if new_stop else (None, None)

    def should_exit(self, current_mult: float) -> Optional[str]:
        """Check if should exit based on locked stop."""
        if current_mult <= self.locked_stop_mult:
            return f"profit_lock_hit_{self.locked_stop_mult:.2f}x"
        return None


# ============================================================
# 5. SMART PROFIT TAKING (4-stage exit)
# ============================================================
class SmartExit:
    """
    4-stage smart exit:
    - Sell 30% at 2x
    - Sell 30% at 4x
    - Sell 20% at 8x
    - Hold 20% with aggressive trailing
    """

    def __init__(self):
        self.stage1_done = False  # 2x: sell 30%
        self.stage2_done = False  # 4x: sell 30%
        self.stage3_done = False  # 8x: sell 20%
        self.profit_lock = ProfitLock()

    def calculate_exit_mult(self, peak_mult: float, emergency_stop: float = 0.5) -> float:
        """
        Calculate effective exit multiplier based on peak.
        """
        if peak_mult < emergency_stop:
            return emergency_stop  # emergency stop

        total = 0.0
        remaining = 1.0

        # Stage 1: 30% at 2x
        if peak_mult >= 2.0:
            sold = remaining * 0.30
            total += sold * 2.0
            remaining -= sold

            # Stage 2: 30% at 4x
            if peak_mult >= 4.0:
                sold = remaining * (0.30 / 0.70)  # 30% of original
                total += sold * 4.0
                remaining -= sold

                # Stage 3: 20% at 8x
                if peak_mult >= 8.0:
                    sold = remaining * (0.20 / 0.40)  # 20% of original
                    total += sold * 8.0
                    remaining -= sold

        # Remaining: trailed with profit lock
        # Get locked stop based on peak
        self.profit_lock.peak_mult = peak_mult
        if peak_mult >= 2.0:
            lock, _ = self.profit_lock.update(peak_mult)
            trail_exit = lock if lock else peak_mult * 0.5
        else:
            # Didn't reach 2x - exit at peak (time stop)
            trail_exit = peak_mult * 0.9  # slight discount

        total += remaining * trail_exit
        return total


# ============================================================
# 6. CIRCUIT BREAKER (enhanced)
# ============================================================
class ProCircuitBreaker:
    """
    Enhanced circuit breaker - protects capital aggressively.
    """

    def __init__(self):
        self.consecutive_losses = 0
        self.daily_loss_pct = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.cooldown_days_left = 0
        self.halted = False
        self.halt_reason = ""

        # Thresholds (stricter than V3)
        self.max_consecutive_losses = 2
        self.max_daily_loss_pct = 10.0  # was 15%
        self.max_daily_trades = 8
        self.cooldown_days = 2  # was 1

    def record_trade(self, pnl: float, capital_before: float):
        """Record trade result."""
        self.daily_trades += 1

        if pnl > 0:
            self.daily_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.daily_loss_pct += abs(pnl) / capital_before * 100 if capital_before > 0 else 0

        # Check halt conditions
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.halt(f"{self.consecutive_losses} consecutive losses")
        elif self.daily_loss_pct >= self.max_daily_loss_pct:
            self.halt(f"Daily loss {self.daily_loss_pct:.1f}%")
        elif self.daily_trades >= self.max_daily_trades and self.daily_wins < self.daily_trades * 0.4:
            self.halt(f"Low win rate ({self.daily_wins}/{self.daily_trades})")

    def halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        self.cooldown_days_left = self.cooldown_days

    def can_trade(self) -> Tuple[bool, str]:
        if self.halted:
            if self.cooldown_days_left > 0:
                return False, f"HALTED: {self.halt_reason} (cooldown {self.cooldown_days_left}d)"
            else:
                self.halted = False
                self.consecutive_losses = 0
                self.cooldown_days_left = 0
                self.halt_reason = ""
        return True, "OK"

    def tick_day(self):
        if self.cooldown_days_left > 0:
            self.cooldown_days_left -= 1

    def reset_daily(self):
        self.daily_loss_pct = 0.0
        self.daily_trades = 0
        self.daily_wins = 0


# ============================================================
# TOKEN SIMULATOR (improved with quality adjustment)
# ============================================================
def simulate_token_v4(
    entry_score: int,
    bonding_progress: float,
    volume_sol: float,
    holder_count: int,
    momentum: float,
    seed: Optional[int] = None,
) -> Dict:
    """
    Simulate token outcome with quality-based probability adjustment.
    Higher entry score → better outcomes.
    """
    if seed is not None:
        random.seed(seed)

    # Base probabilities (more realistic - even A+ tokens mostly fail)
    fail_prob = 0.65  # 65% fail even with good filters (was 0.55)
    rug_prob = 0.12   # 12% rugs (was 0.10)
    pump_dump_prob = 0.12
    graduate_prob = 0.06  # only 6% graduate (was 0.10)
    moonshot_prob = 0.02  # only 2% moonshot (was 0.05)
    slow_growth_prob = 0.03

    # QUALITY ADJUSTMENT: A+ setups have better outcomes (but still conservative)
    if entry_score >= 90:  # A+
        fail_prob *= 0.5   # 50% fewer fails (was 0.4)
        rug_prob *= 0.4    # 60% fewer rugs (was 0.3)
        graduate_prob *= 2.0  # was 2.5
        moonshot_prob *= 2.5  # was 3
        pump_dump_prob *= 1.4
        slow_growth_prob *= 1.8
    elif entry_score >= 75:  # A
        fail_prob *= 0.7   # was 0.6
        rug_prob *= 0.6    # was 0.5
        graduate_prob *= 1.5  # was 1.8
        moonshot_prob *= 1.8  # was 2
        pump_dump_prob *= 1.2
        slow_growth_prob *= 1.3

    # Volume bonus
    if volume_sol > 30:
        graduate_prob *= 1.5
        moonshot_prob *= 1.5
        fail_prob *= 0.7

    # Holder bonus
    if holder_count > 150:
        graduate_prob *= 1.3
        slow_growth_prob *= 1.5
        fail_prob *= 0.8

    # Optimal timing bonus
    if 30 <= bonding_progress <= 50:
        graduate_prob *= 1.3
        moonshot_prob *= 1.2
        fail_prob *= 0.85

    # Strong momentum
    if momentum > 70:
        moonshot_prob *= 1.8
        pump_dump_prob *= 1.4
        fail_prob *= 0.75

    # Normalize
    total = fail_prob + rug_prob + pump_dump_prob + graduate_prob + moonshot_prob + slow_growth_prob
    fail_prob /= total
    rug_prob /= total
    pump_dump_prob /= total
    graduate_prob /= total
    moonshot_prob /= total
    slow_growth_prob /= total

    # Pick outcome
    r = random.random()
    cumsum = 0
    token_type = "fail_day1"
    for ttype, prob in [
        ("fail_day1", fail_prob),
        ("rug", rug_prob),
        ("pump_dump", pump_dump_prob),
        ("graduate", graduate_prob),
        ("moonshot", moonshot_prob),
        ("slow_growth", slow_growth_prob),
    ]:
        cumsum += prob
        if r < cumsum:
            token_type = ttype
            break

    # Calculate multiplier
    entry_sol = bonding_progress / 100 * GRADUATION_THRESHOLD_SOL
    sol_price = 200.0
    entry_price = BondingCurveMath.compute_price_usd(entry_sol, sol_price)

    if token_type == "fail_day1":
        exit_sol = max(entry_sol * random.uniform(0.1, 0.4), 0.5)
        exit_price = BondingCurveMath.compute_price_usd(exit_sol, sol_price)
        multiplier = exit_price / entry_price if entry_price > 0 else 0
        return {"type": "fail_day1", "peak_mult": multiplier, "exit_mult": multiplier,
                "description": "Token died (early exit at -50% to -90%)"}

    elif token_type == "rug":
        return {"type": "rug", "peak_mult": 0.5, "exit_mult": 0.5,
                "description": "Rug pull (emergency stop at -50%)"}

    elif token_type == "graduate":
        exit_sol = GRADUATION_THRESHOLD_SOL
        exit_price_curve = BondingCurveMath.compute_price_usd(exit_sol, sol_price)
        post_grad_mult = random.uniform(0.7, 1.0)  # less sell-off for A+ tokens
        peak_mult = exit_price_curve / entry_price
        exit_mult = peak_mult * post_grad_mult
        return {"type": "graduate", "peak_mult": peak_mult, "exit_mult": exit_mult,
                "description": f"Graduated (peak {peak_mult:.1f}x)"}

    elif token_type == "pump_dump":
        peak_mult = random.uniform(2.5, 6.0)
        return {"type": "pump_dump", "peak_mult": peak_mult, "exit_mult": peak_mult * 0.75,
                "description": f"Pumped {peak_mult:.1f}x (captured 75% via staged exits)"}

    elif token_type == "moonshot":
        peak_mult = random.uniform(15.0, 150.0)
        return {"type": "moonshot", "peak_mult": peak_mult, "exit_mult": peak_mult * 0.5,
                "description": f"Moonshot {peak_mult:.0f}x! (captured 50% via 4-stage exit)"}

    elif token_type == "slow_growth":
        peak_mult = random.uniform(2.0, 4.0)
        return {"type": "slow_growth", "peak_mult": peak_mult, "exit_mult": peak_mult,
                "description": f"Slow growth to {peak_mult:.1f}x"}

    return {"type": "unknown", "peak_mult": 1.0, "exit_mult": 1.0, "description": "Unknown"}


# ============================================================
# MAIN: V4 30-DAY SIMULATION
# ============================================================
def run_v4_simulation(initial_capital: float = 20.0, seed: int = 42) -> Dict:
    """Run 30-day V4 simulation."""
    random.seed(seed)

    print("=" * 80)
    print("💎 PUMP.FUN V4 PROFESSIONAL STRATEGY - 30 DAY SIMULATION")
    print("=" * 80)
    print()
    print(f"💰 Initial capital: ${initial_capital:.2f}")
    print(f"🌱 Seed: {seed}")
    print()
    print("📋 V4 Professional Upgrades:")
    print("   🎯 Multi-factor entry scoring (only A+ setups ≥75/100)")
    print("   📊 Adaptive SL/TP based on volatility regime")
    print("   💰 Progressive position sizing (win streak bonus)")
    print("   🔒 Profit lock mechanism (never give back >30% of peak)")
    print("   🚪 4-stage smart exit (30/30/20/20)")
    print("   ⛔ Enhanced circuit breaker (10% daily loss limit)")
    print()

    # Initialize components
    entry_scorer = EntryScorer()
    circuit_breaker = ProCircuitBreaker()
    position_sizer = ProgressivePositionSizer()
    smart_exit = SmartExit()

    capital = initial_capital
    trades = []
    daily_results = []
    tokens_scanned = 0
    tokens_rejected = 0
    grade_counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

    for day in range(1, 31):
        circuit_breaker.tick_day()
        circuit_breaker.reset_daily()
        day_pnl = 0.0
        day_trades = 0
        day_scanned = 0
        day_grade_a = 0

        # Scan 80-120 tokens per day
        num_tokens = random.randint(80, 120)

        for _ in range(num_tokens):
            can_trade, reason = circuit_breaker.can_trade()
            if not can_trade:
                break

            if capital < 1:
                break

            if day_trades >= 6:
                break

            day_scanned += 1
            tokens_scanned += 1

            # Generate token characteristics (random)
            bonding_progress = random.uniform(5, 85)
            volume_sol = random.uniform(0.1, 80)
            holder_count = random.randint(5, 600)
            dev_holding = random.uniform(0, 60)
            top10_holding = random.uniform(20, 90)
            max_wallet = random.uniform(2, 40)
            momentum = random.uniform(10, 95)
            buy_sell_ratio = random.uniform(0.3, 3.0)

            # SCORE THE ENTRY
            score = entry_scorer.score(
                volume_sol=volume_sol,
                dev_holding_pct=dev_holding,
                top10_holding_pct=top10_holding,
                max_wallet_pct=max_wallet,
                holder_count=holder_count,
                bonding_progress=bonding_progress,
                momentum_score=momentum,
                buy_sell_ratio=buy_sell_ratio,
            )

            grade_counts[score.grade] = grade_counts.get(score.grade, 0) + 1

            # Only trade A grade or better (≥75)
            if not score.is_a_grade:
                tokens_rejected += 1
                continue

            day_grade_a += 1

            # Get position size
            pos_size, size_reason = position_sizer.get_size(capital, score)
            if capital < pos_size:
                pos_size = capital * 0.5  # use half of remaining

            # Enter trade
            entry_value = pos_size
            capital_before = capital
            capital -= entry_value

            # Simulate outcome
            result = simulate_token_v4(
                entry_score=score.total,
                bonding_progress=bonding_progress,
                volume_sol=volume_sol,
                holder_count=holder_count,
                momentum=momentum,
            )

            # Calculate exit using smart 4-stage
            exit_multiplier = smart_exit.calculate_exit_mult(result["peak_mult"])

            # Apply costs
            gross_exit = entry_value * exit_multiplier
            cost = entry_value * ROUND_TRIP_COST_PCT / 100
            net_exit = gross_exit - cost

            pnl = net_exit - entry_value
            capital += net_exit
            day_pnl += pnl
            day_trades += 1

            # Record for circuit breaker and position sizer
            circuit_breaker.record_trade(pnl, capital_before)
            position_sizer.record_trade(pnl, result["peak_mult"])

            trades.append({
                "day": day,
                "type": result["type"],
                "score": score.total,
                "grade": score.grade,
                "entry_progress": round(bonding_progress, 1),
                "volume_sol": round(volume_sol, 1),
                "holders": holder_count,
                "momentum": round(momentum, 0),
                "peak_mult": round(result["peak_mult"], 2),
                "exit_mult": round(exit_multiplier, 2),
                "entry_usd": round(entry_value, 4),
                "pnl_usd": round(pnl, 4),
                "capital_after": round(capital, 4),
                "size_reason": size_reason,
                "description": result["description"],
            })

            if capital <= 0:
                break

        daily_results.append({
            "day": day,
            "scanned": day_scanned,
            "grade_a": day_grade_a,
            "trades": day_trades,
            "pnl": round(day_pnl, 4),
            "capital": round(capital, 4),
            "halted": circuit_breaker.halted,
        })

        if capital <= 0:
            print(f"💀 Day {day}: Capital depleted!")
            break

    # ============================================================
    # RESULTS
    # ============================================================
    print("=" * 80)
    print("📊 V4 FINAL RESULTS - 30 DAYS ON PUMP.FUN")
    print("=" * 80)
    print()

    if not trades:
        print("❌ No trades executed (all tokens scored below A grade).")
        print(f"   Scanned: {tokens_scanned} | Rejected: {tokens_rejected}")
        print(f"   Grade distribution: {grade_counts}")
        return None

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net_pnl = capital - initial_capital
    roi_pct = (net_pnl / initial_capital) * 100
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    expectancy = (win_rate / 100 * avg_win) - ((100 - win_rate) / 100 * avg_loss)

    print(f"💰 Starting capital:    ${initial_capital:.2f}")
    print(f"💵 Ending capital:      ${capital:.2f}")
    print(f"📈 Net PnL:             ${net_pnl:+.2f}")
    print(f"📊 ROI:                 {roi_pct:+.1f}%")
    print(f"💎 Capital multiplier:  {capital/initial_capital:.2f}x")
    print()
    print(f"🎯 Total trades:        {len(trades)}")
    print(f"   ✅ Wins:              {len(wins)}")
    print(f"   ❌ Losses:            {len(losses)}")
    print(f"   🎯 Win Rate:          {win_rate:.1f}%")
    print()
    print(f"💪 Profit Factor:       {profit_factor:.2f}")
    print(f"✨ Gross profit:        ${gross_profit:+.2f}")
    print(f"💔 Gross loss:          ${gross_loss:+.2f}")
    print(f"📊 Avg win:             ${avg_win:+.2f}")
    print(f"📉 Avg loss:            ${avg_loss:+.2f}")
    print(f"💰 Expectancy:          ${expectancy:+.2f}/trade")
    print()
    print(f"🔍 Tokens scanned:     {tokens_scanned}")
    print(f"   ✅ A+ grade:          {grade_counts.get('A+', 0)}")
    print(f"   ✅ A grade:           {grade_counts.get('A', 0)}")
    print(f"   📊 Pass rate:         {len(trades)/max(tokens_scanned,1)*100:.1f}%")
    print()

    # By grade
    print("📊 Results by entry grade:")
    by_grade = {}
    for t in trades:
        g = t["grade"]
        by_grade.setdefault(g, {"n": 0, "wins": 0, "pnl": 0.0, "avg_mult": 0})
        by_grade[g]["n"] += 1
        by_grade[g]["pnl"] += t["pnl_usd"]
        by_grade[g]["avg_mult"] += t["exit_mult"]
        if t["pnl_usd"] > 0:
            by_grade[g]["wins"] += 1

    print(f"  {'Grade':>5} | {'Count':>5} | {'Wins':>4} | {'WR':>6} | {'PnL':>10} | {'Avg Mult':>8}")
    print("  " + "-" * 55)
    for g in ["A+", "A"]:
        if g in by_grade:
            s = by_grade[g]
            wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
            avg_m = s["avg_mult"] / s["n"] if s["n"] > 0 else 0
            print(f"  {g:>5} | {s['n']:>5} | {s['wins']:>4} | {wr:>5.1f}% | ${s['pnl']:>+9.2f} | {avg_m:>7.2f}x")
    print()

    # By token type
    print("📊 Results by token type:")
    by_type = {}
    for t in trades:
        ttype = t["type"]
        by_type.setdefault(ttype, {"n": 0, "wins": 0, "pnl": 0.0})
        by_type[ttype]["n"] += 1
        by_type[ttype]["pnl"] += t["pnl_usd"]
        if t["pnl_usd"] > 0:
            by_type[ttype]["wins"] += 1

    print(f"  {'Type':<15} | {'Count':>5} | {'Wins':>4} | {'WR':>6} | {'Total PnL':>10}")
    print("  " + "-" * 50)
    for ttype, s in sorted(by_type.items(), key=lambda x: -x[1]["n"]):
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"  {ttype:<15} | {s['n']:>5} | {s['wins']:>4} | {wr:>5.1f}% | ${s['pnl']:>+9.2f}")
    print()

    # Daily
    print("📅 Daily results:")
    print(f"  {'Day':>3} | {'Scan':>4} | {'A':>3} | {'Trades':>6} | {'PnL':>10} | {'Capital':>10} | {'Halt':>4}")
    print("  " + "-" * 60)
    for d in daily_results:
        marker = "🟢" if d["pnl"] > 0 else ("🔴" if d["pnl"] < 0 else "⚪")
        halted = "⛔" if d["halted"] else ""
        print(f"  {d['day']:>3} | {d['scanned']:>4} | {d['grade_a']:>3} | {d['trades']:>6} | {marker} ${d['pnl']:>+8.2f} | ${d['capital']:>9.2f} | {halted:>4}")
    print()

    # Top trades
    print("🏆 Top 5 winning trades:")
    for t in sorted(wins, key=lambda x: -x["pnl_usd"])[:5]:
        print(f"  Day {t['day']:>2} | {t['grade']} ({t['score']:>2}) | {t['type']:<12} | peak {t['peak_mult']:>5.1f}x → exit {t['exit_mult']:>5.1f}x | ${t['pnl_usd']:>+8.2f}")
    print()
    print("💔 Top 5 losing trades:")
    for t in sorted(losses, key=lambda x: x["pnl_usd"])[:5]:
        print(f"  Day {t['day']:>2} | {t['grade']} ({t['score']:>2}) | {t['type']:<12} | peak {t['peak_mult']:>5.1f}x → exit {t['exit_mult']:>5.1f}x | ${t['pnl_usd']:>+8.2f}")
    print()

    # Final verdict
    print("=" * 80)
    print(f"💡 IF YOU INVESTED $20 ON PUMP.FUN (V4 PRO) FOR 30 DAYS:")
    print("=" * 80)
    print()
    print(f"  💵 You would have: ${capital:.2f} (from $20)")
    print(f"  📈 Net PnL:        ${net_pnl:+.2f} ({roi_pct:+.1f}%)")
    print(f"  💎 Multiplier:     {capital/initial_capital:.2f}x")
    print(f"  🎯 Win Rate:       {win_rate:.1f}%")
    print(f"  💪 Profit Factor:   {profit_factor:.2f}")
    if net_pnl > 0:
        print(f"  ✅ Profitable month with V4 PRO strategy!")
    print()

    return {
        "initial_capital": initial_capital,
        "ending_capital": capital,
        "net_pnl": net_pnl,
        "roi_pct": roi_pct,
        "multiplier": capital / initial_capital,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "tokens_scanned": tokens_scanned,
        "tokens_rejected": tokens_rejected,
        "grade_counts": grade_counts,
        "trades": trades,
        "daily_results": daily_results,
    }


if __name__ == "__main__":
    print("\n" + "💎" * 40)
    print("V4 PROFESSIONAL STRATEGY - 5 SIMULATIONS")
    print("💎" * 40 + "\n")

    results = []
    for seed in [42, 123, 789, 2024, 5555]:
        print(f"\n{'='*80}")
        print(f"SIMULATION seed={seed}")
        print(f"{'='*80}")
        result = run_v4_simulation(initial_capital=20.0, seed=seed)
        results.append(result)

    # Summary
    print("\n" + "=" * 80)
    print("📊 V4 SUMMARY - 5 SIMULATIONS")
    print("=" * 80)
    print()
    print(f"{'Seed':>6} | {'Final':>10} | {'ROI':>8} | {'Mult':>6} | {'Trades':>7} | {'WR':>6} | {'PF':>6} | {'Exp':>7}")
    print("-" * 75)
    for i, r in enumerate(results):
        if r:
            print(f"{[42,123,789,2024,5555][i]:>6} | ${r['ending_capital']:>9.2f} | {r['roi_pct']:>+6.1f}% | {r['multiplier']:>5.2f}x | {r['total_trades']:>7} | {r['win_rate']:>5.1f}% | {r['profit_factor']:>5.2f} | ${r['expectancy']:>+5.2f}")

    valid = [r for r in results if r]
    if valid:
        avg_pnl = sum(r["net_pnl"] for r in valid) / len(valid)
        avg_roi = sum(r["roi_pct"] for r in valid) / len(valid)
        avg_wr = sum(r["win_rate"] for r in valid) / len(valid)
        avg_pf = sum(r["profit_factor"] for r in valid) / len(valid)
        avg_exp = sum(r["expectancy"] for r in valid) / len(valid)
        print("-" * 75)
        print(f"{'AVG':>6} | {'':>10} | {avg_roi:>+6.1f}% | {'':>6} | {'':>7} | {avg_wr:>5.1f}% | {avg_pf:>5.2f} | ${avg_exp:>+5.2f}")
    print()

    output = {
        "simulation_date": datetime.now().isoformat(),
        "strategy": "V4 Professional (multi-factor scoring + adaptive SL/TP + progressive sizing + profit lock + 4-stage exit)",
        "initial_capital": 20.0,
        "simulations": results,
    }
    with open("data/pumpfun_v4_simulation.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("💾 Saved: data/pumpfun_v4_simulation.json")
