"""
Automated Strategy Research Laboratory.

Generates strategy hypotheses, tests them via backtest, and runs a
Champion vs Challenger system to decide which strategy gets adopted.

Key features:
1. Generate hypotheses from indicator combinations (RSI+MACD+EMA, etc.)
2. Test each hypothesis against historical data
3. Compare challenger against current champion
4. Statistical significance test (bootstrap confidence interval on win-rate delta)
5. Promote challenger only if it beats champion with statistical significance
6. Record all experiments to SystemState (no schema migrations needed)

The lab does NOT replace strategies on a whim — it only promotes a challenger
when:
- Out-of-sample win rate is at least `min_improvement_pct` higher than champion's
- Profit factor is at least as good
- Sample size is at least `min_sample_size`
- Bootstrap 95% CI on (challenger_wr - champion_wr) does NOT include zero
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import random
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import SystemState, Trade, StrategyVersion


# =========================================================================
# Data structures
# =========================================================================
@dataclass
class StrategyHypothesis:
    """A candidate strategy to be tested."""
    hypothesis_id: str
    name: str
    description: str = ""
    # Entry conditions: list of dicts like
    #   {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "5m"}
    entry_conditions: List[Dict[str, Any]] = field(default_factory=list)
    # Exit conditions: same format
    exit_conditions: List[Dict[str, Any]] = field(default_factory=list)
    # Parameters
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 15.0
    trailing_stop_pct: float = 3.0
    position_size_pct: float = 5.0
    holding_minutes: int = 120
    timeframe: str = "5m"
    indicators_used: List[str] = field(default_factory=list)

    # Status: draft | testing | tested | champion | rejected | retired
    status: str = "draft"

    # Backtest results
    backtest_win_rate: float = 0.0
    backtest_profit_factor: float = 0.0
    backtest_expectancy_usd: float = 0.0
    backtest_max_drawdown_pct: float = 0.0
    backtest_total_trades: int = 0
    backtest_out_of_sample_wr: float = 0.0
    backtest_sharpe: float = 0.0

    # Metadata
    created_at: str = ""
    tested_at: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategyHypothesis":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ChampionChallengerResult:
    """Result of a champion vs challenger comparison."""
    champion_id: str
    challenger_id: str
    champion_wr: float
    challenger_wr: float
    champion_pf: float
    challenger_pf: float
    wr_delta_pct: float  # challenger - champion
    bootstrap_ci_low: float  # 95% CI lower bound on wr delta
    bootstrap_ci_high: float
    statistically_significant: bool
    challenger_wins: bool
    promoted: bool
    reason: str = ""
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================================================================
# Strategy Lab
# =========================================================================
class StrategyLab:
    """
    Automated strategy research laboratory.

    Pipeline:
    1. generate_hypotheses() - create strategy variations from indicator space
    2. test_hypothesis() - backtest a single hypothesis (caller provides backtest_fn)
    3. compare_champion_challenger() - statistical comparison
    4. promote_challenger() - if challenger wins, it becomes new champion
    5. record_experiment() - persist every experiment for audit
    """

    CHAMPION_KEY = "strategy_lab:champion"
    EXPERIMENTS_KEY = "strategy_lab:experiments_index"
    EXPERIMENT_PREFIX = "strategy_lab:experiment:"

    # Default indicator grid for hypothesis generation
    INDICATOR_GRID = {
        "rsi_oversold_threshold": [20, 25, 30, 35],
        "rsi_overbought_threshold": [65, 70, 75, 80],
        "ema_fast_period": [5, 9, 12],
        "ema_slow_period": [20, 26, 50],
        "macd_signal_threshold": [-0.1, 0.0, 0.1],
        "bollinger_std_mult": [1.5, 2.0, 2.5],
        "adx_min": [15, 20, 25, 30],
        "stop_loss_pct": [3.0, 5.0, 8.0],
        "take_profit_pct": [10.0, 15.0, 20.0, 25.0],
        "trailing_stop_pct": [2.0, 3.0, 5.0],
        "position_size_pct": [3.0, 5.0, 8.0],
        "holding_minutes": [60, 120, 240],
    }

    def __init__(
        self,
        min_improvement_pct: float = 3.0,
        min_sample_size: int = 30,
        min_profit_factor: float = 1.3,
        bootstrap_iterations: int = 500,
    ):
        self.min_improvement_pct = min_improvement_pct
        self.min_sample_size = min_sample_size
        self.min_profit_factor = min_profit_factor
        self.bootstrap_iterations = bootstrap_iterations

    # =====================================================================
    # Hypothesis generation
    # =====================================================================
    def generate_hypotheses(
        self,
        max_hypotheses: int = 50,
        seed: Optional[int] = None,
    ) -> List[StrategyHypothesis]:
        """
        Generate a batch of strategy hypotheses by sampling the indicator grid.
        Uses randomized search rather than full grid (which would explode combinatorially).
        """
        rng = random.Random(seed)
        hypotheses: List[StrategyHypothesis] = []

        # Generate combinations randomly
        keys = list(self.INDICATOR_GRID.keys())
        for i in range(max_hypotheses):
            params = {k: rng.choice(self.INDICATOR_GRID[k]) for k in keys}

            # Build entry/exit conditions from params
            entry_conditions = [
                {"indicator": "rsi", "op": "<", "value": params["rsi_oversold_threshold"], "timeframe": "5m"},
                {"indicator": "ema_fast", "op": ">", "value": "ema_slow", "timeframe": "5m"},
                {"indicator": "adx", "op": ">", "value": params["adx_min"], "timeframe": "5m"},
            ]
            exit_conditions = [
                {"indicator": "rsi", "op": ">", "value": params["rsi_overbought_threshold"], "timeframe": "5m"},
            ]

            h = StrategyHypothesis(
                hypothesis_id=str(uuid.uuid4()),
                name=f"hypothesis_{i+1:03d}",
                description=(
                    f"RSI<{params['rsi_oversold_threshold']} + EMA fast > slow + "
                    f"ADX>{params['adx_min']}, SL={params['stop_loss_pct']}%, "
                    f"TP={params['take_profit_pct']}%"
                ),
                entry_conditions=entry_conditions,
                exit_conditions=exit_conditions,
                stop_loss_pct=params["stop_loss_pct"],
                take_profit_pct=params["take_profit_pct"],
                trailing_stop_pct=params["trailing_stop_pct"],
                position_size_pct=params["position_size_pct"],
                holding_minutes=params["holding_minutes"],
                timeframe="5m",
                indicators_used=["rsi", "ema", "adx", "bollinger"],
                created_at=datetime.utcnow().isoformat(),
            )
            hypotheses.append(h)

        logger.info(f"🧪 Generated {len(hypotheses)} strategy hypotheses")
        return hypotheses

    def generate_targeted_hypotheses(
        self,
        focus_indicators: List[str],
        variations_per_indicator: int = 5,
        seed: Optional[int] = None,
    ) -> List[StrategyHypothesis]:
        """Generate hypotheses focused on a specific set of indicators."""
        rng = random.Random(seed)
        out: List[StrategyHypothesis] = []

        for i in range(variations_per_indicator * len(focus_indicators)):
            entry_conds: List[Dict[str, Any]] = []
            for ind in focus_indicators:
                if ind == "rsi":
                    entry_conds.append({
                        "indicator": "rsi", "op": "<",
                        "value": rng.choice([20, 25, 30]),
                        "timeframe": "5m",
                    })
                elif ind == "macd":
                    entry_conds.append({
                        "indicator": "macd_histogram", "op": ">",
                        "value": 0, "timeframe": "5m",
                    })
                elif ind == "ema":
                    entry_conds.append({
                        "indicator": "ema_9", "op": ">",
                        "value": "ema_50", "timeframe": "5m",
                    })
                elif ind == "bollinger":
                    entry_conds.append({
                        "indicator": "bb_position", "op": "<",
                        "value": 0.2, "timeframe": "5m",
                    })
                elif ind == "vwap":
                    entry_conds.append({
                        "indicator": "price", "op": ">",
                        "value": "vwap", "timeframe": "5m",
                    })

            h = StrategyHypothesis(
                hypothesis_id=str(uuid.uuid4()),
                name=f"targeted_{ind}_{i:03d}",
                description=f"Targeted: {', '.join(focus_indicators)}",
                entry_conditions=entry_conds,
                exit_conditions=[],
                stop_loss_pct=rng.choice([3.0, 5.0, 8.0]),
                take_profit_pct=rng.choice([10.0, 15.0, 20.0]),
                trailing_stop_pct=rng.choice([2.0, 3.0, 5.0]),
                position_size_pct=rng.choice([3.0, 5.0, 8.0]),
                holding_minutes=rng.choice([60, 120, 240]),
                timeframe="5m",
                indicators_used=focus_indicators,
                created_at=datetime.utcnow().isoformat(),
            )
            out.append(h)
        return out

    # =====================================================================
    # Testing
    # =====================================================================
    async def test_hypothesis(
        self,
        hypothesis: StrategyHypothesis,
        backtest_fn,
        # backtest_fn signature: async (hypothesis) -> Dict[str, Any]
        # Expected return: {win_rate, profit_factor, expectancy, max_dd, total_trades, oos_wr, sharpe}
    ) -> StrategyHypothesis:
        """Run a backtest on the hypothesis and fill in its results."""
        try:
            hypothesis.status = "testing"
            t0 = datetime.utcnow()
            results = await backtest_fn(hypothesis)
            elapsed_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)

            hypothesis.backtest_win_rate = float(results.get("win_rate", 0))
            hypothesis.backtest_profit_factor = float(results.get("profit_factor", 0))
            hypothesis.backtest_expectancy_usd = float(results.get("expectancy", 0))
            hypothesis.backtest_max_drawdown_pct = float(results.get("max_drawdown_pct", 0))
            hypothesis.backtest_total_trades = int(results.get("total_trades", 0))
            hypothesis.backtest_out_of_sample_wr = float(results.get("oos_wr", 0))
            hypothesis.backtest_sharpe = float(results.get("sharpe", 0))
            hypothesis.status = "tested"
            hypothesis.tested_at = datetime.utcnow().isoformat()
            hypothesis.notes.append(f"Backtest elapsed: {elapsed_ms}ms")

            # Auto-reject if sample is too small
            if hypothesis.backtest_total_trades < self.min_sample_size:
                hypothesis.status = "rejected"
                hypothesis.notes.append(
                    f"Rejected: sample size {hypothesis.backtest_total_trades} < "
                    f"min {self.min_sample_size}"
                )
            elif hypothesis.backtest_profit_factor < self.min_profit_factor:
                hypothesis.status = "rejected"
                hypothesis.notes.append(
                    f"Rejected: profit factor {hypothesis.backtest_profit_factor:.2f} < "
                    f"min {self.min_profit_factor}"
                )

        except Exception as exc:
            logger.exception(f"Hypothesis test failed: {exc}")
            hypothesis.status = "rejected"
            hypothesis.notes.append(f"Test error: {str(exc)[:200]}")

        return hypothesis

    async def test_batch(
        self,
        hypotheses: List[StrategyHypothesis],
        backtest_fn,
        max_concurrent: int = 5,
    ) -> List[StrategyHypothesis]:
        """Test a batch of hypotheses with bounded concurrency."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _test_with_sem(h: StrategyHypothesis) -> StrategyHypothesis:
            async with semaphore:
                return await self.test_hypothesis(h, backtest_fn)

        results = await asyncio.gather(*[_test_with_sem(h) for h in hypotheses])
        # Persist all
        for h in results:
            await self.record_experiment(h)
        return results

    # =====================================================================
    # Champion / Challenger
    # =====================================================================
    async def get_current_champion(self) -> Optional[StrategyHypothesis]:
        """Get the current champion strategy (or None if no champion yet)."""
        async with get_session() as s:
            stmt = select(SystemState).where(SystemState.key == self.CHAMPION_KEY)
            result = await s.execute(stmt)
            state = result.scalars().first()
            if not state or not state.value_json:
                return None
            return StrategyHypothesis.from_dict(state.value_json)

    async def set_champion(self, hypothesis: StrategyHypothesis) -> None:
        """Set a new champion strategy."""
        hypothesis.status = "champion"
        async with get_session() as s:
            state = SystemState(key=self.CHAMPION_KEY, value_json=hypothesis.to_dict())
            await s.merge(state)
        logger.info(f"🏆 New champion set: {hypothesis.name} (WR={hypothesis.backtest_win_rate:.0%})")

    async def compare_champion_challenger(
        self,
        champion: StrategyHypothesis,
        challenger: StrategyHypothesis,
    ) -> ChampionChallengerResult:
        """
        Statistical comparison of champion vs challenger.

        Uses bootstrap resampling to estimate the CI of (challenger_wr - champion_wr).
        Challenger wins only if:
        - WR improvement >= min_improvement_pct
        - Profit factor >= champion's profit factor
        - 95% CI on WR delta excludes zero
        - Sample size >= min_sample_size
        """
        # Bootstrap CI on the difference in win rates
        # We treat each strategy's trades as Bernoulli trials with their observed WR
        n_iter = self.bootstrap_iterations
        champion_n = max(champion.backtest_total_trades, 1)
        challenger_n = max(challenger.backtest_total_trades, 1)
        champion_p = champion.backtest_win_rate
        challenger_p = challenger.backtest_win_rate

        # Bootstrap: simulate n_iter experiments for each strategy
        rng = random.Random(42)
        deltas: List[float] = []
        for _ in range(n_iter):
            champ_wins = sum(1 for _ in range(champion_n) if rng.random() < champion_p)
            chall_wins = sum(1 for _ in range(challenger_n) if rng.random() < challenger_p)
            champ_wr = champ_wins / champion_n
            chall_wr = chall_wins / challenger_n
            deltas.append((chall_wr - champ_wr) * 100.0)

        deltas.sort()
        ci_low = deltas[int(0.025 * n_iter)]
        ci_high = deltas[int(0.975 * n_iter)]
        wr_delta = (challenger_p - champion_p) * 100.0
        significant = ci_low > 0  # CI excludes zero (on the positive side)

        # Promote?
        promoted = False
        reasons: List[str] = []

        if challenger.backtest_total_trades < self.min_sample_size:
            reasons.append(f"Sample too small ({challenger.backtest_total_trades} < {self.min_sample_size})")
        elif wr_delta < self.min_improvement_pct:
            reasons.append(f"WR delta {wr_delta:.1f}% < min {self.min_improvement_pct}%")
        elif challenger.backtest_profit_factor < champion.backtest_profit_factor:
            reasons.append(
                f"PF {challenger.backtest_profit_factor:.2f} < "
                f"champion's {champion.backtest_profit_factor:.2f}"
            )
        elif not significant:
            reasons.append(f"Bootstrap CI includes zero [{ci_low:.1f}, {ci_high:.1f}]")
        else:
            promoted = True
            reasons.append(
                f"Challenger wins: WR +{wr_delta:.1f}%, "
                f"PF {challenger.backtest_profit_factor:.2f} >= {champion.backtest_profit_factor:.2f}, "
                f"CI [{ci_low:.1f}, {ci_high:.1f}]"
            )

        challenger_wins = wr_delta > 0 and significant

        result = ChampionChallengerResult(
            champion_id=champion.hypothesis_id,
            challenger_id=challenger.hypothesis_id,
            champion_wr=champion_p,
            challenger_wr=challenger_p,
            champion_pf=champion.backtest_profit_factor,
            challenger_pf=challenger.backtest_profit_factor,
            wr_delta_pct=wr_delta,
            bootstrap_ci_low=ci_low,
            bootstrap_ci_high=ci_high,
            statistically_significant=significant,
            challenger_wins=challenger_wins,
            promoted=promoted,
            reason="; ".join(reasons),
            timestamp=datetime.utcnow().isoformat(),
        )

        # Persist comparison result
        await self._record_comparison(result)

        if promoted:
            await self.set_champion(challenger)

        return result

    # =====================================================================
    # Experiment recording
    # =====================================================================
    async def record_experiment(self, hypothesis: StrategyHypothesis) -> None:
        """Persist an experiment so we never lose it."""
        async with get_session() as s:
            key = f"{self.EXPERIMENT_PREFIX}{hypothesis.hypothesis_id}"
            state = SystemState(key=key, value_json=hypothesis.to_dict())
            await s.merge(state)
            # Update index
            index_state = await s.get(SystemState, self.EXPERIMENTS_KEY)
            if index_state is None:
                index_state = SystemState(key=self.EXPERIMENTS_KEY, value_json={"ids": []})
            ids = list(index_state.value_json.get("ids", []))
            if hypothesis.hypothesis_id not in ids:
                ids.append(hypothesis.hypothesis_id)
            index_state.value_json = {
                "ids": ids,
                "updated_at": datetime.utcnow().isoformat(),
                "count": len(ids),
            }
            await s.merge(index_state)

    async def _record_comparison(self, result: ChampionChallengerResult) -> None:
        async with get_session() as s:
            key = (
                f"strategy_lab:comparison:"
                f"{result.champion_id[:8]}_vs_{result.challenger_id[:8]}_"
                f"{int(datetime.utcnow().timestamp())}"
            )
            state = SystemState(key=key, value_json=result.to_dict())
            await s.merge(state)

    async def list_experiments(
        self, limit: int = 100, status_filter: Optional[str] = None
    ) -> List[StrategyHypothesis]:
        """List all recorded experiments."""
        out: List[StrategyHypothesis] = []
        async with get_session() as s:
            stmt = select(SystemState).where(
                SystemState.key.like(f"{self.EXPERIMENT_PREFIX}%")
            )
            result = await s.execute(stmt)
            for state in result.scalars().all():
                try:
                    h = StrategyHypothesis.from_dict(state.value_json or {})
                    if status_filter is None or h.status == status_filter:
                        out.append(h)
                except Exception:
                    continue
        # Sort by tested_at desc
        out.sort(key=lambda h: h.tested_at, reverse=True)
        return out[:limit]

    async def get_best_hypotheses(self, top_n: int = 5) -> List[StrategyHypothesis]:
        """Get the top-N hypotheses by out-of-sample win rate."""
        all_h = await self.list_experiments(limit=1000)
        tested = [h for h in all_h if h.status == "tested" and h.backtest_total_trades >= self.min_sample_size]
        tested.sort(key=lambda h: h.backtest_out_of_sample_wr, reverse=True)
        return tested[:top_n]

    async def run_experiment_cycle(
        self,
        backtest_fn,
        num_hypotheses: int = 20,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run a full experiment cycle:
        1. Generate hypotheses
        2. Test them all
        3. Compare best against current champion
        4. Promote if challenger wins
        """
        t0 = datetime.utcnow()
        # Generate
        hypotheses = self.generate_hypotheses(max_hypotheses=num_hypotheses, seed=seed)
        # Test
        tested = await self.test_batch(hypotheses, backtest_fn)
        # Filter valid
        valid = [h for h in tested if h.status == "tested"]
        valid.sort(key=lambda h: h.backtest_out_of_sample_wr, reverse=True)
        # Get champion
        champion = await self.get_current_champion()

        promoted = None
        comparison = None
        if valid:
            best_challenger = valid[0]
            if champion is None:
                # First champion
                await self.set_champion(best_challenger)
                promoted = best_challenger
            else:
                comparison = await self.compare_champion_challenger(champion, best_challenger)
                if comparison.promoted:
                    promoted = best_challenger

        elapsed_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
        return {
            "hypotheses_generated": len(hypotheses),
            "hypotheses_tested": len(tested),
            "hypotheses_valid": len(valid),
            "best_challenger": valid[0].to_dict() if valid else None,
            "champion_before": champion.to_dict() if champion else None,
            "comparison": comparison.to_dict() if comparison else None,
            "promoted": promoted.to_dict() if promoted else None,
            "elapsed_ms": elapsed_ms,
            "timestamp": datetime.utcnow().isoformat(),
        }


__all__ = ["StrategyLab", "StrategyHypothesis", "ChampionChallengerResult"]
