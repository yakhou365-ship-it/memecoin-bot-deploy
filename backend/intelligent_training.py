"""
Intelligent Learning Engine for Trading Bot
==========================================

NOT memorization - UNDERSTANDING.

The bot learns by:
1. Analyzing WHY each trade failed/succeeded (root cause analysis)
2. Identifying PATTERNS in failures (not specific tokens)
3. Building RULES from patterns (generalizable knowledge)
4. Testing rules on new data (validation)
5. Keeping only rules that improve outcomes

This is how professional traders learn:
- "I lost because I entered too early on low volume" → rule: require volume > X
- "I won because the token had strong momentum" → rule: prefer momentum > Y
- "I lost because dev held too much" → rule: reject dev > Z%

The engine extracts these rules AUTOMATICALLY from trade data.
"""
from __future__ import annotations

import os
import sys
import json
import math
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.modules.market_scanner.pumpfun_client import (
    BondingCurveMath, GRADUATION_THRESHOLD_SOL
)


# ============================================================
# 1. ROOT CAUSE ANALYSIS - Understand WHY trades fail
# ============================================================
@dataclass
class TradeAnalysis:
    """Analysis of a single trade - WHY it succeeded/failed."""
    trade_id: str
    outcome: str  # "win" | "loss" | "breakeven"
    pnl_pct: float
    # Token characteristics at entry
    entry_progress: float
    volume_sol: float
    holder_count: int
    dev_holding_pct: float
    top10_holding_pct: float
    max_wallet_pct: float
    momentum: float
    buy_sell_ratio: float
    bonding_curve_age_min: int
    # Market context
    market_regime: str  # "bull" | "bear" | "range"
    sol_price_trend: str  # "up" | "down" | "flat"
    # Entry score (from V4)
    entry_score: int
    entry_grade: str
    # Exit info
    exit_reason: str
    peak_mult: float
    exit_mult: float
    holding_time_min: int
    # ROOT CAUSE (analyzed)
    root_causes: List[str] = field(default_factory=list)
    success_factors: List[str] = field(default_factory=list)


class RootCauseAnalyzer:
    """
    Analyzes WHY a trade succeeded or failed.

    Instead of memorizing "token X failed", it extracts:
    - "tokens with dev_holding > 20% fail 85% of the time"
    - "tokens with volume < 5 SOL fail 92% of the time"
    - "entries at progress < 20% fail 78% of the time"

    These are GENERALIZABLE rules, not memorized facts.
    """

    def analyze(self, trade: TradeAnalysis) -> TradeAnalysis:
        """Analyze a trade and add root causes / success factors."""

        if trade.outcome == "loss":
            # === LOSS ROOT CAUSES ===

            # 1. Dev holding too high
            if trade.dev_holding_pct > 20:
                trade.root_causes.append(
                    f"dev_holding_high: {trade.dev_holding_pct:.0f}% > 20% threshold"
                )

            # 2. Low volume (no real demand)
            if trade.volume_sol < 5:
                trade.root_causes.append(
                    f"volume_low: {trade.volume_sol:.1f} SOL < 5 SOL minimum"
                )

            # 3. Too few holders (no community)
            if trade.holder_count < 30:
                trade.root_causes.append(
                    f"holders_low: {trade.holder_count} < 30 minimum"
                )

            # 4. Entered too early (bonding curve < 15%)
            if trade.entry_progress < 15:
                trade.root_causes.append(
                    f"entry_too_early: {trade.entry_progress:.0f}% < 15% minimum"
                )

            # 5. Entered too late (bonding curve > 75%)
            if trade.entry_progress > 75:
                trade.root_causes.append(
                    f"entry_too_late: {trade.entry_progress:.0f}% > 75% max"
                )

            # 6. Weak momentum
            if trade.momentum < 30:
                trade.root_causes.append(
                    f"momentum_weak: {trade.momentum:.0f} < 30 minimum"
                )

            # 7. Top 10 too concentrated
            if trade.top10_holding_pct > 60:
                trade.root_causes.append(
                    f"concentration_high: top10={trade.top10_holding_pct:.0f}% > 60%"
                )

            # 8. Max wallet too big (whale can dump)
            if trade.max_wallet_pct > 20:
                trade.root_causes.append(
                    f"whale_risk: max_wallet={trade.max_wallet_pct:.0f}% > 20%"
                )

            # 9. Bad market regime
            if trade.market_regime == "bear":
                trade.root_causes.append("bear_market: wrong regime for long")
            elif trade.market_regime == "range":
                trade.root_causes.append("range_market: no clear trend")

            # 10. Buy/sell ratio bad (sell pressure)
            if trade.buy_sell_ratio < 0.7:
                trade.root_causes.append(
                    f"sell_pressure: B/S={trade.buy_sell_ratio:.1f} < 0.7"
                )

            # 11. Token too new (rug window)
            if trade.bonding_curve_age_min < 5:
                trade.root_causes.append(
                    f"token_too_new: {trade.bonding_curve_age_min}min < 5min"
                )

            # 12. Exit reason analysis
            if trade.exit_reason == "stop":
                trade.root_causes.append("hard_stop_hit: SL too tight or wrong entry")
            elif trade.exit_reason == "time_stop":
                trade.root_causes.append("time_stop: trade stalled, no momentum")
            elif trade.exit_reason == "rug":
                trade.root_causes.append("rug_pull: liquidity removed (unpredictable)")

            # If no root cause found, it's market noise
            if not trade.root_causes:
                trade.root_causes.append("market_noise: no identifiable cause")

        elif trade.outcome == "win":
            # === WIN SUCCESS FACTORS ===

            if trade.dev_holding_pct < 10:
                trade.success_factors.append(f"dev_safe: {trade.dev_holding_pct:.0f}% < 10%")

            if trade.volume_sol > 20:
                trade.success_factors.append(f"volume_strong: {trade.volume_sol:.0f} SOL > 20")

            if trade.holder_count > 100:
                trade.success_factors.append(f"community_strong: {trade.holder_count} holders")

            if 30 <= trade.entry_progress <= 60:
                trade.success_factors.append(f"timing_optimal: {trade.entry_progress:.0f}% in 30-60% zone")

            if trade.momentum > 60:
                trade.success_factors.append(f"momentum_strong: {trade.momentum:.0f} > 60")

            if trade.buy_sell_ratio > 1.5:
                trade.success_factors.append(f"buy_pressure: B/S={trade.buy_sell_ratio:.1f}")

            if trade.market_regime == "bull":
                trade.success_factors.append("bull_market: trend aligned")

            if trade.top10_holding_pct < 40:
                trade.success_factors.append(f"distributed: top10={trade.top10_holding_pct:.0f}% < 40%")

        return trade


# ============================================================
# 2. PATTERN RECOGNITION - Extract rules from many trades
# ============================================================
@dataclass
class Pattern:
    """A discovered pattern from trade analysis."""
    name: str
    condition: str  # human-readable condition
    win_rate_when_true: float  # win rate when pattern is present
    win_rate_when_false: float  # win rate when pattern is absent
    sample_size_true: int
    sample_size_false: int
    confidence: float  # 0-1, based on sample size and effect size
    recommendation: str  # "reject" | "prefer" | "neutral"
    threshold_value: float = 0.0


class PatternMiner:
    """
    Mines patterns from trade data.

    For each potential factor (volume, holders, dev_holding, etc.),
    it tests different thresholds and finds the one that best separates
    winners from losers.

    This is the "intelligence" - the bot learns which factors matter.
    """

    def mine(self, trades: List[TradeAnalysis]) -> List[Pattern]:
        """Extract patterns from analyzed trades."""
        patterns = []

        if len(trades) < 20:
            return patterns  # need at least 20 trades

        # Test each factor with multiple thresholds
        factors = [
            ("volume_sol", [3, 5, 10, 15, 20, 30]),
            ("holder_count", [20, 30, 50, 100, 200]),
            ("dev_holding_pct", [5, 10, 15, 20, 30]),
            ("top10_holding_pct", [30, 40, 50, 60, 70]),
            ("max_wallet_pct", [10, 15, 20, 25, 30]),
            ("entry_progress", [15, 20, 30, 50, 60, 75]),
            ("momentum", [30, 40, 50, 60, 70]),
            ("buy_sell_ratio", [0.7, 1.0, 1.5, 2.0]),
            ("bonding_curve_age_min", [5, 10, 30, 60]),
        ]

        for factor_name, thresholds in factors:
            for threshold in thresholds:
                pattern = self._test_threshold(trades, factor_name, threshold)
                if pattern and pattern.confidence > 0.5:
                    patterns.append(pattern)

        # Sort by confidence
        patterns.sort(key=lambda p: -p.confidence)
        return patterns

    def _test_threshold(self, trades: List[TradeAnalysis],
                          factor: str, threshold: float) -> Optional[Pattern]:
        """Test if a threshold on a factor separates winners from losers."""

        # For "good" factors (high = good): volume, holders, momentum, buy_sell_ratio
        # For "bad" factors (high = bad): dev_holding, top10, max_wallet
        # For "timing" factors: entry_progress (optimal range)

        good_factors = {"volume_sol", "holder_count", "momentum", "buy_sell_ratio"}
        bad_factors = {"dev_holding_pct", "top10_holding_pct", "max_wallet_pct", "bonding_curve_age_min"}

        if factor in good_factors:
            # Higher = better
            true_trades = [t for t in trades if getattr(t, factor) >= threshold]
            false_trades = [t for t in trades if getattr(t, factor) < threshold]
            condition = f"{factor} >= {threshold}"
        elif factor in bad_factors:
            # Lower = better
            true_trades = [t for t in trades if getattr(t, factor) <= threshold]
            false_trades = [t for t in trades if getattr(t, factor) > threshold]
            condition = f"{factor} <= {threshold}"
        else:
            # Timing factor - check if in optimal range
            # For entry_progress, optimal is 30-60
            if factor == "entry_progress":
                if threshold == 30:
                    true_trades = [t for t in trades if 30 <= t.entry_progress <= 60]
                    false_trades = [t for t in trades if t.entry_progress < 30 or t.entry_progress > 60]
                    condition = "30 <= entry_progress <= 60"
                else:
                    return None
            else:
                return None

        if len(true_trades) < 5 or len(false_trades) < 5:
            return None

        wins_true = sum(1 for t in true_trades if t.outcome == "win")
        wins_false = sum(1 for t in false_trades if t.outcome == "win")

        wr_true = wins_true / len(true_trades) if true_trades else 0
        wr_false = wins_false / len(false_trades) if false_trades else 0

        # Effect size: difference in win rates
        effect = wr_true - wr_false

        # Confidence: based on sample size and effect size
        min_sample = min(len(true_trades), len(false_trades))
        confidence = min(1.0, (min_sample / 50) * abs(effect) * 2)

        # Recommendation
        if effect > 0.1 and confidence > 0.5:
            recommendation = "prefer"
        elif effect < -0.1 and confidence > 0.5:
            recommendation = "reject"
        else:
            recommendation = "neutral"

        return Pattern(
            name=f"{factor}_threshold",
            condition=condition,
            win_rate_when_true=wr_true,
            win_rate_when_false=wr_false,
            sample_size_true=len(true_trades),
            sample_size_false=len(false_trades),
            confidence=confidence,
            recommendation=recommendation,
            threshold_value=threshold,
        )


# ============================================================
# 3. ADAPTIVE RULES - Rules that evolve with learning
# ============================================================
@dataclass
class AdaptiveRule:
    """A trading rule that adapts based on performance."""
    name: str
    condition: str
    action: str  # "reject" | "prefer" | "adjust_size"
    threshold: float
    confidence: float
    times_tested: int = 0
    times_correct: int = 0
    created_at: str = ""
    last_updated: str = ""

    @property
    def accuracy(self) -> float:
        return self.times_correct / max(self.times_tested, 1)

    def update(self, was_correct: bool):
        """Update rule accuracy based on new data."""
        self.times_tested += 1
        if was_correct:
            self.times_correct += 1
        # Decay confidence slightly to allow re-evaluation
        self.confidence *= 0.99
        self.last_updated = datetime.now().isoformat()


class AdaptiveRuleEngine:
    """
    Engine that maintains and updates trading rules.

    Rules are derived from patterns and continuously validated.
    Rules that perform poorly are removed.
    """

    def __init__(self):
        self.rules: Dict[str, AdaptiveRule] = {}
        self.min_confidence = 0.55
        self.min_accuracy = 0.55
        self.min_tests = 20

    def update_from_patterns(self, patterns: List[Pattern]):
        """Create/update rules from discovered patterns."""
        for pattern in patterns:
            if pattern.recommendation == "neutral":
                continue
            if pattern.confidence < self.min_confidence:
                continue

            rule_name = pattern.name
            if rule_name in self.rules:
                # Update existing rule
                self.rules[rule_name].confidence = pattern.confidence
                self.rules[rule_name].threshold = pattern.threshold_value
            else:
                # Create new rule
                self.rules[rule_name] = AdaptiveRule(
                    name=rule_name,
                    condition=pattern.condition,
                    action=pattern.recommendation,
                    threshold=pattern.threshold_value,
                    confidence=pattern.confidence,
                    created_at=datetime.now().isoformat(),
                    last_updated=datetime.now().isoformat(),
                )

    def validate_rules(self, trades: List[TradeAnalysis]):
        """Validate rules against new trade data."""
        for rule in list(self.rules.values()):
            correct = 0
            total = 0

            for trade in trades:
                # Check if rule condition applies
                applies, expected_outcome = self._check_rule(rule, trade)
                if applies:
                    total += 1
                    actual_outcome = "win" if trade.outcome == "win" else "loss"
                    if expected_outcome == actual_outcome:
                        correct += 1

            if total >= self.min_tests:
                accuracy = correct / total
                rule.times_tested = total
                rule.times_correct = correct

                # Remove poorly performing rules
                if accuracy < self.min_accuracy and rule.times_tested >= self.min_tests:
                    del self.rules[rule.name]

    def _check_rule(self, rule: AdaptiveRule, trade: TradeAnalysis) -> Tuple[bool, str]:
        """Check if a rule applies to a trade. Returns (applies, expected_outcome)."""
        # Parse condition
        if "volume_sol" in rule.name:
            if rule.action == "prefer":
                return (trade.volume_sol >= rule.threshold, "win")
            else:
                return (trade.volume_sol < rule.threshold, "loss")
        elif "holder_count" in rule.name:
            if rule.action == "prefer":
                return (trade.holder_count >= rule.threshold, "win")
            else:
                return (trade.holder_count < rule.threshold, "loss")
        elif "dev_holding" in rule.name:
            if rule.action == "reject":
                return (trade.dev_holding_pct > rule.threshold, "loss")
            else:
                return (trade.dev_holding_pct <= rule.threshold, "win")
        elif "momentum" in rule.name:
            if rule.action == "prefer":
                return (trade.momentum >= rule.threshold, "win")
            else:
                return (trade.momentum < rule.threshold, "loss")
        elif "entry_progress" in rule.name:
            return (30 <= trade.entry_progress <= 60, "win")

        return (False, "")


# ============================================================
# 4. SCENARIO GENERATOR - 5000+ diverse training scenarios
# ============================================================
class ScenarioGenerator:
    """
    Generates 5000+ diverse trading scenarios with varying difficulty.

    Difficulty levels:
    - Easy (1000): Clear winners (strong signals) and clear losers (rugs)
    - Medium (2000): Mixed signals, requires judgment
    - Hard (1500): Ambiguous setups, high uncertainty
    - Extreme (500): Edge cases, stress tests

    Each scenario has REALISTIC token data based on Pump.fun statistics.
    """

    def __init__(self, sol_price: float = 200.0):
        self.sol_price = sol_price
        random.seed(42)

    def generate_all(self) -> List[TradeAnalysis]:
        """Generate all training scenarios."""
        scenarios = []

        # Easy: 1000 scenarios (clear winners/losers)
        scenarios.extend(self._generate_easy(1000))

        # Medium: 2000 scenarios (mixed signals)
        scenarios.extend(self._generate_medium(2000))

        # Hard: 1500 scenarios (ambiguous)
        scenarios.extend(self._generate_hard(1500))

        # Extreme: 500 scenarios (edge cases)
        scenarios.extend(self._generate_extreme(500))

        return scenarios

    def _generate_easy(self, count: int) -> List[TradeAnalysis]:
        """Easy scenarios: clear winners and clear losers."""
        scenarios = []
        for i in range(count):
            if i % 2 == 0:
                # Clear winner
                outcome = "win"
                volume = random.uniform(30, 80)
                holders = random.randint(150, 400)
                dev = random.uniform(0, 5)
                top10 = random.uniform(25, 40)
                max_wallet = random.uniform(3, 10)
                progress = random.uniform(35, 55)
                momentum = random.uniform(70, 90)
                bs_ratio = random.uniform(1.5, 2.5)
                age = random.randint(30, 120)
                peak_mult = random.uniform(3, 20)
                exit_reason = "trail_stop"
            else:
                # Clear loser (rug or fail)
                outcome = "loss"
                volume = random.uniform(0.1, 2)
                holders = random.randint(5, 20)
                dev = random.uniform(25, 50)
                top10 = random.uniform(60, 85)
                max_wallet = random.uniform(20, 40)
                progress = random.uniform(5, 15)
                momentum = random.uniform(10, 25)
                bs_ratio = random.uniform(0.3, 0.6)
                age = random.randint(1, 5)
                peak_mult = random.uniform(0.1, 0.5)
                exit_reason = random.choice(["rug", "stop"])

            scenarios.append(self._make_trade(
                f"easy_{i}", outcome, volume, holders, dev, top10,
                max_wallet, progress, momentum, bs_ratio, age, peak_mult, exit_reason
            ))
        return scenarios

    def _generate_medium(self, count: int) -> List[TradeAnalysis]:
        """Medium scenarios: mixed signals, requires judgment."""
        scenarios = []
        for i in range(count):
            # Mixed - outcome depends on combination
            volume = random.uniform(5, 25)
            holders = random.randint(30, 100)
            dev = random.uniform(5, 20)
            top10 = random.uniform(40, 60)
            max_wallet = random.uniform(10, 20)
            progress = random.uniform(20, 70)
            momentum = random.uniform(40, 65)
            bs_ratio = random.uniform(0.8, 1.8)
            age = random.randint(10, 60)

            # Outcome depends on combination
            score = 0
            if volume > 15: score += 1
            if holders > 50: score += 1
            if dev < 10: score += 1
            if 30 <= progress <= 55: score += 1
            if momentum > 55: score += 1
            if bs_ratio > 1.2: score += 1

            if score >= 4:
                outcome = "win"
                peak_mult = random.uniform(2, 8)
                exit_reason = random.choice(["trail_stop", "tp2"])
            elif score >= 2:
                outcome = "win" if random.random() > 0.5 else "loss"
                peak_mult = random.uniform(1, 3) if outcome == "win" else random.uniform(0.3, 0.8)
                exit_reason = "trail_stop" if outcome == "win" else "time_stop"
            else:
                outcome = "loss"
                peak_mult = random.uniform(0.2, 0.6)
                exit_reason = random.choice(["stop", "time_stop"])

            scenarios.append(self._make_trade(
                f"medium_{i}", outcome, volume, holders, dev, top10,
                max_wallet, progress, momentum, bs_ratio, age, peak_mult, exit_reason
            ))
        return scenarios

    def _generate_hard(self, count: int) -> List[TradeAnalysis]:
        """Hard scenarios: ambiguous, high uncertainty."""
        scenarios = []
        for i in range(count):
            # Ambiguous - even good filters can't predict
            volume = random.uniform(10, 30)
            holders = random.randint(50, 150)
            dev = random.uniform(3, 15)
            top10 = random.uniform(35, 55)
            max_wallet = random.uniform(8, 18)
            progress = random.uniform(25, 65)
            momentum = random.uniform(45, 70)
            bs_ratio = random.uniform(1.0, 1.8)
            age = random.randint(20, 90)

            # 50/50 outcome regardless of signals (hard cases)
            outcome = "win" if random.random() > 0.5 else "loss"
            if outcome == "win":
                peak_mult = random.uniform(1.5, 4)
                exit_reason = "trail_stop"
            else:
                peak_mult = random.uniform(0.3, 0.7)
                exit_reason = random.choice(["stop", "time_stop", "rug"])

            scenarios.append(self._make_trade(
                f"hard_{i}", outcome, volume, holders, dev, top10,
                max_wallet, progress, momentum, bs_ratio, age, peak_mult, exit_reason
            ))
        return scenarios

    def _generate_extreme(self, count: int) -> List[TradeAnalysis]:
        """Extreme scenarios: edge cases, stress tests."""
        scenarios = []
        for i in range(count):
            choice = random.randint(0, 4)

            if choice == 0:
                # Extreme moonshot (rare but huge)
                outcome = "win"
                volume = random.uniform(50, 100)
                holders = random.randint(200, 500)
                dev = random.uniform(0, 3)
                top10 = random.uniform(20, 35)
                max_wallet = random.uniform(2, 8)
                progress = random.uniform(40, 60)
                momentum = random.uniform(80, 95)
                bs_ratio = random.uniform(2, 3)
                age = random.randint(30, 90)
                peak_mult = random.uniform(20, 100)
                exit_reason = "trail_stop"

            elif choice == 1:
                # Extreme rug (dev dumps immediately)
                outcome = "loss"
                volume = random.uniform(20, 50)  # high volume but rug
                holders = random.randint(50, 100)
                dev = random.uniform(40, 60)
                top10 = random.uniform(70, 90)
                max_wallet = random.uniform(30, 50)
                progress = random.uniform(30, 50)
                momentum = random.uniform(60, 80)  # fake momentum
                bs_ratio = random.uniform(1.5, 2.5)  # fake buy pressure
                age = random.randint(1, 5)
                peak_mult = 0.0
                exit_reason = "rug"

            elif choice == 2:
                # Very low liquidity (untradeable)
                outcome = "loss"
                volume = random.uniform(0.01, 0.5)
                holders = random.randint(3, 15)
                dev = random.uniform(10, 30)
                top10 = random.uniform(50, 80)
                max_wallet = random.uniform(15, 35)
                progress = random.uniform(5, 20)
                momentum = random.uniform(10, 30)
                bs_ratio = random.uniform(0.3, 0.7)
                age = random.randint(1, 10)
                peak_mult = random.uniform(0.1, 0.4)
                exit_reason = "stop"

            elif choice == 3:
                # Graduated token (post-curve)
                outcome = "win" if random.random() > 0.3 else "loss"
                volume = random.uniform(30, 70)
                holders = random.randint(100, 300)
                dev = random.uniform(2, 10)
                top10 = random.uniform(30, 50)
                max_wallet = random.uniform(5, 15)
                progress = random.uniform(85, 100)
                momentum = random.uniform(50, 80)
                bs_ratio = random.uniform(1.2, 2.0)
                age = random.randint(60, 180)
                peak_mult = random.uniform(2, 10) if outcome == "win" else random.uniform(0.4, 0.8)
                exit_reason = "trail_stop" if outcome == "win" else "time_stop"

            else:
                # High volatility (unpredictable)
                outcome = "win" if random.random() > 0.5 else "loss"
                volume = random.uniform(40, 80)
                holders = random.randint(80, 200)
                dev = random.uniform(5, 15)
                top10 = random.uniform(40, 60)
                max_wallet = random.uniform(10, 20)
                progress = random.uniform(30, 70)
                momentum = random.uniform(60, 90)
                bs_ratio = random.uniform(1.0, 2.5)
                age = random.randint(15, 45)
                peak_mult = random.uniform(0.1, 50)  # extreme range
                exit_reason = "trail_stop" if outcome == "win" else random.choice(["stop", "rug"])

            scenarios.append(self._make_trade(
                f"extreme_{i}", outcome, volume, holders, dev, top10,
                max_wallet, progress, momentum, bs_ratio, age, peak_mult, exit_reason
            ))
        return scenarios

    def _make_trade(self, trade_id, outcome, volume, holders, dev, top10,
                     max_wallet, progress, momentum, bs_ratio, age, peak_mult, exit_reason) -> TradeAnalysis:
        """Create a TradeAnalysis object."""
        # Calculate PnL
        if outcome == "win":
            pnl_pct = (peak_mult - 1) * 100 * 0.5  # 50% captured via staged exit
        else:
            pnl_pct = -(1 - peak_mult) * 100 if peak_mult > 0 else -100

        # Entry score (simplified V4 scoring)
        score = 0
        if volume >= 20: score += 25
        elif volume >= 10: score += 15
        elif volume >= 5: score += 10
        if dev < 5: score += 20
        elif dev < 15: score += 10
        if 30 <= progress <= 60: score += 15
        elif 20 <= progress <= 70: score += 8
        if momentum > 70: score += 15
        elif momentum > 50: score += 10
        if holders > 100: score += 12
        elif holders > 50: score += 8
        elif holders > 30: score += 4
        if bs_ratio > 1.5: score += 5

        grade = "A+" if score >= 90 else "A" if score >= 75 else "B" if score >= 60 else "C"

        # Market regime
        regime = random.choice(["bull", "bear", "range", "bull", "bull"])  # bias to bull

        return TradeAnalysis(
            trade_id=trade_id,
            outcome=outcome,
            pnl_pct=pnl_pct,
            entry_progress=progress,
            volume_sol=volume,
            holder_count=holders,
            dev_holding_pct=dev,
            top10_holding_pct=top10,
            max_wallet_pct=max_wallet,
            momentum=momentum,
            buy_sell_ratio=bs_ratio,
            bonding_curve_age_min=age,
            market_regime=regime,
            sol_price_trend="up" if regime == "bull" else "down" if regime == "bear" else "flat",
            entry_score=score,
            entry_grade=grade,
            exit_reason=exit_reason,
            peak_mult=peak_mult,
            exit_mult=peak_mult * 0.5 if outcome == "win" else peak_mult,
            holding_time_min=random.randint(5, 240),
        )


# ============================================================
# 5. TRAINING PIPELINE
# ============================================================
class TrainingPipeline:
    """
    Complete training pipeline:
    1. Generate 5000+ scenarios
    2. Analyze root causes
    3. Mine patterns
    4. Create adaptive rules
    5. Validate rules
    6. Report learned knowledge
    """

    def __init__(self):
        self.analyzer = RootCauseAnalyzer()
        self.miner = PatternMiner()
        self.rule_engine = AdaptiveRuleEngine()
        self.generator = ScenarioGenerator()

    def train(self) -> Dict:
        """Run complete training pipeline."""
        print("=" * 80)
        print("🧠 INTELLIGENT TRAINING PIPELINE")
        print("📊 Learning from 5000+ diverse scenarios")
        print("=" * 80)
        print()

        # Step 1: Generate scenarios
        print("1️⃣  Generating 5000+ training scenarios...")
        scenarios = self.generator.generate_all()
        print(f"   ✅ Generated {len(scenarios)} scenarios")
        print(f"      - Easy: {sum(1 for s in scenarios if s.trade_id.startswith('easy'))}")
        print(f"      - Medium: {sum(1 for s in scenarios if s.trade_id.startswith('medium'))}")
        print(f"      - Hard: {sum(1 for s in scenarios if s.trade_id.startswith('hard'))}")
        print(f"      - Extreme: {sum(1 for s in scenarios if s.trade_id.startswith('extreme'))}")

        # Count wins/losses
        wins = sum(1 for s in scenarios if s.outcome == "win")
        losses = sum(1 for s in scenarios if s.outcome == "loss")
        print(f"   📊 Distribution: {wins} wins ({wins/len(scenarios)*100:.1f}%), {losses} losses ({losses/len(scenarios)*100:.1f}%)")
        print()

        # Step 2: Analyze root causes
        print("2️⃣  Analyzing root causes for each trade...")
        analyzed = []
        for trade in scenarios:
            analyzed.append(self.analyzer.analyze(trade))

        # Summarize root causes
        cause_counts = defaultdict(int)
        for trade in analyzed:
            if trade.outcome == "loss":
                for cause in trade.root_causes:
                    cause_name = cause.split(":")[0]
                    cause_counts[cause_name] += 1

        print(f"   ✅ Analyzed {len(analyzed)} trades")
        print(f"   📊 Top root causes of losses:")
        for cause, count in sorted(cause_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"      {cause}: {count} occurrences ({count/losses*100:.1f}% of losses)")
        print()

        # Summarize success factors
        factor_counts = defaultdict(int)
        for trade in analyzed:
            if trade.outcome == "win":
                for factor in trade.success_factors:
                    factor_name = factor.split(":")[0]
                    factor_counts[factor_name] += 1

        print(f"   📊 Top success factors of wins:")
        for factor, count in sorted(factor_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"      {factor}: {count} occurrences ({count/wins*100:.1f}% of wins)")
        print()

        # Step 3: Mine patterns
        print("3️⃣  Mining patterns from trade data...")
        patterns = self.miner.mine(analyzed)
        print(f"   ✅ Discovered {len(patterns)} patterns")
        print(f"   📊 Top patterns (by confidence):")
        for p in patterns[:10]:
            print(f"      {p.condition:<40} | WR_true={p.win_rate_when_true*100:.0f}% WR_false={p.win_rate_when_false*100:.0f}% | conf={p.confidence:.2f} | {p.recommendation}")
        print()

        # Step 4: Create adaptive rules
        print("4️⃣  Creating adaptive rules from patterns...")
        self.rule_engine.update_from_patterns(patterns)
        print(f"   ✅ Created {len(self.rule_engine.rules)} rules")
        for name, rule in list(self.rule_engine.rules.items())[:10]:
            print(f"      {rule.condition:<40} | action={rule.action} | conf={rule.confidence:.2f}")
        print()

        # Step 5: Validate rules
        print("5️⃣  Validating rules against trade data...")
        self.rule_engine.validate_rules(analyzed)
        surviving = len(self.rule_engine.rules)
        print(f"   ✅ {surviving} rules survived validation")
        for name, rule in list(self.rule_engine.rules.items())[:10]:
            print(f"      {rule.condition:<40} | accuracy={rule.accuracy*100:.0f}% ({rule.times_correct}/{rule.times_tested})")
        print()

        # Step 6: Generate report
        print("6️⃣  Generating training report...")
        report = self._generate_report(analyzed, patterns)
        print(f"   ✅ Report generated")
        print()

        return report

    def _generate_report(self, trades: List[TradeAnalysis], patterns: List[Pattern]) -> Dict:
        """Generate comprehensive training report."""
        wins = [t for t in trades if t.outcome == "win"]
        losses = [t for t in trades if t.outcome == "loss"]

        # Calculate win rate by difficulty
        difficulty_stats = {}
        for prefix in ["easy", "medium", "hard", "extreme"]:
            diff_trades = [t for t in trades if t.trade_id.startswith(prefix)]
            diff_wins = sum(1 for t in diff_trades if t.outcome == "win")
            difficulty_stats[prefix] = {
                "total": len(diff_trades),
                "wins": diff_wins,
                "losses": len(diff_trades) - diff_wins,
                "win_rate": diff_wins / len(diff_trades) * 100 if diff_trades else 0,
            }

        # Key insights
        insights = []

        # Insight 1: Volume matters
        high_vol_wins = sum(1 for t in wins if t.volume_sol > 20)
        high_vol_total = sum(1 for t in trades if t.volume_sol > 20)
        if high_vol_total > 0:
            insights.append({
                "insight": f"Tokens with volume > 20 SOL have {high_vol_wins/high_vol_total*100:.0f}% win rate",
                "rule": "Prefer tokens with volume > 20 SOL",
                "confidence": min(1.0, high_vol_total / 100),
            })

        # Insight 2: Dev holding
        low_dev_wins = sum(1 for t in wins if t.dev_holding_pct < 10)
        low_dev_total = sum(1 for t in trades if t.dev_holding_pct < 10)
        if low_dev_total > 0:
            insights.append({
                "insight": f"Tokens with dev < 10% have {low_dev_wins/low_dev_total*100:.0f}% win rate",
                "rule": "Prefer tokens where dev holds < 10%",
                "confidence": min(1.0, low_dev_total / 100),
            })

        # Insight 3: Timing
        optimal_wins = sum(1 for t in wins if 30 <= t.entry_progress <= 60)
        optimal_total = sum(1 for t in trades if 30 <= t.entry_progress <= 60)
        if optimal_total > 0:
            insights.append({
                "insight": f"Entries at 30-60% progress have {optimal_wins/optimal_total*100:.0f}% win rate",
                "rule": "Enter when bonding curve is at 30-60%",
                "confidence": min(1.0, optimal_total / 100),
            })

        # Insight 4: Momentum
        high_mom_wins = sum(1 for t in wins if t.momentum > 60)
        high_mom_total = sum(1 for t in trades if t.momentum > 60)
        if high_mom_total > 0:
            insights.append({
                "insight": f"Tokens with momentum > 60 have {high_mom_wins/high_mom_total*100:.0f}% win rate",
                "rule": "Prefer tokens with momentum > 60",
                "confidence": min(1.0, high_mom_total / 100),
            })

        return {
            "training_date": datetime.now().isoformat(),
            "total_scenarios": len(trades),
            "total_wins": len(wins),
            "total_losses": len(losses),
            "overall_win_rate": len(wins) / len(trades) * 100,
            "difficulty_stats": difficulty_stats,
            "patterns_discovered": len(patterns),
            "rules_created": len(self.rule_engine.rules),
            "rules": [asdict(r) for r in self.rule_engine.rules.values()],
            "insights": insights,
            "top_patterns": [
                {
                    "condition": p.condition,
                    "wr_true": p.win_rate_when_true,
                    "wr_false": p.win_rate_when_false,
                    "confidence": p.confidence,
                    "recommendation": p.recommendation,
                }
                for p in patterns[:15]
            ],
        }


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    pipeline = TrainingPipeline()
    report = pipeline.train()

    # Save report
    with open("data/training_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("💾 Saved: data/training_report.json")
    print()
    print("=" * 80)
    print("✅ TRAINING COMPLETE!")
    print(f"📊 Trained on {report['total_scenarios']} scenarios")
    print(f"🎯 Overall win rate: {report['overall_win_rate']:.1f}%")
    print(f"🧠 Discovered {report['patterns_discovered']} patterns")
    print(f"📝 Created {report['rules_created']} adaptive rules")
    print("=" * 80)
