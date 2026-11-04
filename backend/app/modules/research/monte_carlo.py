"""
Monte Carlo Simulation Engine.

Stress-tests trading strategies under randomized scenarios to estimate:

1. Distribution of outcomes (P5, P50, P95 PnL)
2. Risk of Ruin (probability of hitting a stop-loss threshold)
3. Consecutive losing streaks
4. Tail-risk scenarios (extreme volatility, slippage spikes, liquidity drops)
5. Worst-case drawdown distributions

Inputs:
- Historical trade list (entry/exit/pnl per trade) OR
- Strategy parameters (win_rate, avg_win_pct, avg_loss_pct, num_trades)

Outputs:
- MonteCarloResult with percentiles, risk-of-ruin, max-streak distributions, etc.
"""
from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ...core.config import settings
from ...core.logger import logger


# =========================================================================
# Result structures
# =========================================================================
@dataclass
class MonteCarloResult:
    """Outcome of a Monte Carlo simulation."""
    # Inputs
    strategy_id: str = ""
    num_simulations: int = 0
    num_trades_per_sim: int = 0
    starting_capital_usd: float = 0.0

    # PnL distribution percentiles (in USD)
    pnl_p5: float = 0.0
    pnl_p25: float = 0.0
    pnl_p50: float = 0.0
    pnl_p75: float = 0.0
    pnl_p95: float = 0.0
    pnl_mean: float = 0.0
    pnl_std: float = 0.0
    pnl_min: float = 0.0
    pnl_max: float = 0.0

    # Return distribution percentiles (in %)
    return_p5: float = 0.0
    return_p50: float = 0.0
    return_p95: float = 0.0
    return_mean: float = 0.0

    # Risk of Ruin (% of simulations that hit the ruin threshold)
    risk_of_ruin_pct: float = 0.0
    ruin_threshold_pct: float = 0.0  # drawdown level considered "ruin"

    # Drawdown distribution
    max_drawdown_p50: float = 0.0
    max_drawdown_p95: float = 0.0
    max_drawdown_mean: float = 0.0

    # Streak distribution
    max_winning_streak_mean: float = 0.0
    max_losing_streak_mean: float = 0.0
    max_losing_streak_p95: float = 0.0
    worst_consecutive_loss_usd: float = 0.0

    # Final capital distribution
    final_capital_p5: float = 0.0
    final_capital_p50: float = 0.0
    final_capital_p95: float = 0.0

    # Stress test results
    stress_high_volatility_ruin_pct: float = 0.0
    stress_high_fees_ruin_pct: float = 0.0
    stress_liquidity_drop_ruin_pct: float = 0.0

    # Confidence intervals
    profit_probability: float = 0.0  # P(final > starting capital)
    expected_value_usd: float = 0.0

    # Diagnostics
    simulation_seed: int = 0
    elapsed_ms: int = 0
    timestamp: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================================================================
# Simulator
# =========================================================================
class MonteCarloSimulator:
    """
    Monte Carlo trading strategy simulator.

    Supports two input modes:
    1. From historical trades (bootstraps from actual trade distribution)
    2. From parameters (synthesizes trades from win_rate + avg_win/loss)

    Each simulation runs N trades, applying:
    - Randomized trade sequence (with optional block bootstrap)
    - Position sizing (fixed-pct or Kelly fraction)
    - Fees and slippage (configurable, with random spikes)
    - Consecutive-loss tracking
    - Drawdown tracking

    Final output: distribution of outcomes + Risk of Ruin.
    """

    def __init__(
        self,
        default_capital_usd: float = 10000.0,
        default_position_pct: float = 5.0,
        fee_pct: float = 0.25,
        slippage_pct: float = 0.5,
    ):
        self.default_capital_usd = default_capital_usd
        self.default_position_pct = default_position_pct
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct

    # =====================================================================
    # Public API
    # =====================================================================
    async def simulate_from_trades(
        self,
        trades: List[Dict[str, Any]],
        num_simulations: int = 1000,
        trades_per_sim: int = 100,
        starting_capital_usd: Optional[float] = None,
        position_pct: Optional[float] = None,
        ruin_threshold_pct: float = 20.0,
        seed: Optional[int] = None,
    ) -> MonteCarloResult:
        """
        Simulate by bootstrapping from historical trades.

        Args:
            trades: list of dicts with at least {pnl_pct, net_pnl_usd}
            num_simulations: number of Monte Carlo paths
            trades_per_sim: number of trades per simulation path
            starting_capital_usd: starting capital (defaults to settings.paper_capital_usd)
            position_pct: position size as % of capital
            ruin_threshold_pct: drawdown level considered "ruin" (e.g. 20 = -20%)
            seed: optional RNG seed for reproducibility
        """
        if not trades:
            return self._empty_result("no_trades", num_simulations, trades_per_sim)

        # Extract pnl percentages
        pnls_pct = [
            float(t.get("pnl_pct", 0))
            for t in trades
            if t.get("pnl_pct") is not None
        ]
        if not pnls_pct:
            # Try to derive from USD pnl and size
            pnls_pct = []
            for t in trades:
                net = float(t.get("net_pnl_usd", 0) or 0)
                size = float(t.get("entry_size_usd", 0) or 0)
                if size > 0:
                    pnls_pct.append((net / size) * 100)

        if not pnls_pct:
            return self._empty_result("no_pnl_data", num_simulations, trades_per_sim)

        return await self._run_simulation(
            pnls_pct=pnls_pct,
            num_simulations=num_simulations,
            trades_per_sim=trades_per_sim,
            starting_capital_usd=starting_capital_usd or self.default_capital_usd,
            position_pct=position_pct or self.default_position_pct,
            ruin_threshold_pct=ruin_threshold_pct,
            seed=seed,
            strategy_id="bootstrap",
        )

    async def simulate_from_params(
        self,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        num_simulations: int = 1000,
        trades_per_sim: int = 100,
        starting_capital_usd: Optional[float] = None,
        position_pct: Optional[float] = None,
        ruin_threshold_pct: float = 20.0,
        seed: Optional[int] = None,
        win_std_pct: float = 0.0,
        loss_std_pct: float = 0.0,
    ) -> MonteCarloResult:
        """
        Simulate by synthesizing trades from parameters.

        Args:
            win_rate: probability of winning (0-1)
            avg_win_pct: average winning trade % (e.g. 15.0 = +15%)
            avg_loss_pct: average losing trade % (e.g. 5.0 = -5%, positive number)
            win_std_pct: std dev of winning trades
            loss_std_pct: std dev of losing trades
        """
        rng = random.Random(seed)
        # Generate base pnl pool by sampling from the parametric distribution
        pnls_pct: List[float] = []
        for _ in range(max(trades_per_sim * 5, 1000)):
            if rng.random() < win_rate:
                pnl = avg_win_pct
                if win_std_pct > 0:
                    pnl = max(0.1, rng.gauss(avg_win_pct, win_std_pct))
            else:
                pnl = -abs(avg_loss_pct)
                if loss_std_pct > 0:
                    pnl = -abs(rng.gauss(avg_loss_pct, loss_std_pct))
            pnls_pct.append(pnl)

        return await self._run_simulation(
            pnls_pct=pnls_pct,
            num_simulations=num_simulations,
            trades_per_sim=trades_per_sim,
            starting_capital_usd=starting_capital_usd or self.default_capital_usd,
            position_pct=position_pct or self.default_position_pct,
            ruin_threshold_pct=ruin_threshold_pct,
            seed=seed,
            strategy_id="parametric",
        )

    # =====================================================================
    # Core simulation
    # =====================================================================
    async def _run_simulation(
        self,
        pnls_pct: List[float],
        num_simulations: int,
        trades_per_sim: int,
        starting_capital_usd: float,
        position_pct: float,
        ruin_threshold_pct: float,
        seed: Optional[int],
        strategy_id: str,
    ) -> MonteCarloResult:
        """Run the Monte Carlo loop in a thread to avoid blocking the event loop."""
        import concurrent.futures

        t0 = datetime.utcnow()
        loop = asyncio.get_event_loop()

        # Run heavy CPU work in a thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            stats = await loop.run_in_executor(
                pool,
                self._simulate_sync,
                pnls_pct,
                num_simulations,
                trades_per_sim,
                starting_capital_usd,
                position_pct,
                ruin_threshold_pct,
                seed,
            )

        elapsed_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
        result = MonteCarloResult(
            strategy_id=strategy_id,
            num_simulations=num_simulations,
            num_trades_per_sim=trades_per_sim,
            starting_capital_usd=starting_capital_usd,
            pnl_p5=stats["pnl_p5"],
            pnl_p25=stats["pnl_p25"],
            pnl_p50=stats["pnl_p50"],
            pnl_p75=stats["pnl_p75"],
            pnl_p95=stats["pnl_p95"],
            pnl_mean=stats["pnl_mean"],
            pnl_std=stats["pnl_std"],
            pnl_min=stats["pnl_min"],
            pnl_max=stats["pnl_max"],
            return_p5=stats["return_p5"],
            return_p50=stats["return_p50"],
            return_p95=stats["return_p95"],
            return_mean=stats["return_mean"],
            risk_of_ruin_pct=stats["risk_of_ruin_pct"],
            ruin_threshold_pct=ruin_threshold_pct,
            max_drawdown_p50=stats["max_dd_p50"],
            max_drawdown_p95=stats["max_dd_p95"],
            max_drawdown_mean=stats["max_dd_mean"],
            max_winning_streak_mean=stats["max_win_streak_mean"],
            max_losing_streak_mean=stats["max_loss_streak_mean"],
            max_losing_streak_p95=stats["max_loss_streak_p95"],
            worst_consecutive_loss_usd=stats["worst_consec_loss_usd"],
            final_capital_p5=stats["final_cap_p5"],
            final_capital_p50=stats["final_cap_p50"],
            final_capital_p95=stats["final_cap_p95"],
            stress_high_volatility_ruin_pct=stats["stress_vol_ruin"],
            stress_high_fees_ruin_pct=stats["stress_fees_ruin"],
            stress_liquidity_drop_ruin_pct=stats["stress_liq_ruin"],
            profit_probability=stats["profit_prob"],
            expected_value_usd=stats["ev_usd"],
            simulation_seed=seed or 0,
            elapsed_ms=elapsed_ms,
            timestamp=datetime.utcnow().isoformat(),
            notes=[
                f"Strategy: {strategy_id}",
                f"Source PnL samples: {len(pnls_pct)}",
                f"Ruin threshold: -{ruin_threshold_pct}%",
            ],
        )

        logger.info(
            f"🎲 Monte Carlo: {num_simulations} sims × {trades_per_sim} trades | "
            f"P50 PnL=${stats['pnl_p50']:.0f}, RoR={stats['risk_of_ruin_pct']:.1f}%, "
            f"MaxDD P95={stats['max_dd_p95']:.1f}%"
        )
        return result

    def _simulate_sync(
        self,
        pnls_pct: List[float],
        num_simulations: int,
        trades_per_sim: int,
        starting_capital_usd: float,
        position_pct: float,
        ruin_threshold_pct: float,
        seed: Optional[int],
    ) -> Dict[str, float]:
        """Synchronous Monte Carlo - heavy CPU work."""
        rng = random.Random(seed)
        np_rng = np.random.default_rng(seed)
        pnl_pool = np.array(pnls_pct, dtype=np.float64)

        if len(pnl_pool) == 0:
            return self._zero_stats()

        # Vectorize: sample (num_simulations × trades_per_sim) trades at once
        idx = np_rng.integers(0, len(pnl_pool), size=(num_simulations, trades_per_sim))
        sampled = pnl_pool[idx]  # shape: (num_sim, trades_per_sim)

        # Apply cost (fees + slippage) to every trade
        cost_pct = self.fee_pct + self.slippage_pct
        sampled_net = sampled - cost_pct  # subtract cost from each trade's pct return

        # Position size as a fraction of capital (compounded)
        pos_fraction = position_pct / 100.0

        # Capital trajectory: start with starting_capital, each trade changes it by
        #   capital * pos_fraction * pnl_pct / 100
        # Vectorized cumulative product approach
        per_trade_return = sampled_net * pos_fraction / 100.0  # fraction gain per trade
        # Note: per_trade_return is the % change in capital, NOT additive in capital terms
        # Capital evolution: cap[i] = cap[i-1] * (1 + per_trade_return[i])
        # Final cap = start * product(1 + r_i)
        # log(final/start) = sum(log(1+r_i))
        log_returns = np.log1p(per_trade_return)
        total_log_return = log_returns.sum(axis=1)
        final_capitals = starting_capital_usd * np.exp(total_log_return)
        pnls_usd = final_capitals - starting_capital_usd
        returns_pct = (final_capitals / starting_capital_usd - 1) * 100.0

        # Drawdown calculation per simulation
        # Capital at each step:
        cum_log = np.cumsum(log_returns, axis=1)
        cum_capital = starting_capital_usd * np.exp(cum_log)
        # Track peak
        peaks = np.maximum.accumulate(cum_capital, axis=1)
        drawdowns_pct = (cum_capital - peaks) / peaks * 100.0  # negative
        max_dd_per_sim = drawdowns_pct.min(axis=1)  # most negative per sim

        # Risk of ruin: count sims where max_dd went below -ruin_threshold_pct
        ruin_count = int((max_dd_per_sim <= -ruin_threshold_pct).sum())
        risk_of_ruin_pct = (ruin_count / num_simulations) * 100.0

        # Streaks: count consecutive losses (negative returns)
        # Use np operations for speed
        is_loss = (sampled_net < 0).astype(np.int8)
        max_loss_streaks = np.zeros(num_simulations, dtype=np.int32)
        max_win_streaks = np.zeros(num_simulations, dtype=np.int32)
        worst_consec_loss_usd = np.zeros(num_simulations, dtype=np.float64)

        # Streak calculation (loop is acceptable - vectorized version is complex)
        for i in range(num_simulations):
            row = is_loss[i]
            cur_loss = 0
            cur_win = 0
            cur_loss_usd = 0.0
            worst_loss_usd = 0.0
            for j in range(trades_per_sim):
                if row[j] == 1:
                    cur_loss += 1
                    cur_win = 0
                    # Track cumulative USD loss in this streak
                    cur_loss_usd += (
                        starting_capital_usd * pos_fraction * sampled_net[i, j] / 100.0
                    )
                    if cur_loss_usd < worst_loss_usd:
                        worst_loss_usd = cur_loss_usd
                else:
                    if cur_loss > max_loss_streaks[i]:
                        max_loss_streaks[i] = cur_loss
                    if cur_loss_usd < worst_consec_loss_usd[i]:
                        worst_consec_loss_usd[i] = cur_loss_usd
                    cur_loss = 0
                    cur_loss_usd = 0.0
                    cur_win += 1
                    if cur_win > max_win_streaks[i]:
                        max_win_streaks[i] = cur_win
            # End-of-sim update
            if cur_loss > max_loss_streaks[i]:
                max_loss_streaks[i] = cur_loss
            if cur_loss_usd < worst_consec_loss_usd[i]:
                worst_consec_loss_usd[i] = cur_loss_usd

        # Stress tests
        stress_vol_ruin = self._stress_test(
            pnl_pool, num_simulations, trades_per_sim, starting_capital_usd,
            pos_fraction, ruin_threshold_pct, np_rng, mode="high_vol"
        )
        stress_fees_ruin = self._stress_test(
            pnl_pool, num_simulations, trades_per_sim, starting_capital_usd,
            pos_fraction, ruin_threshold_pct, np_rng, mode="high_fees"
        )
        stress_liq_ruin = self._stress_test(
            pnl_pool, num_simulations, trades_per_sim, starting_capital_usd,
            pos_fraction, ruin_threshold_pct, np_rng, mode="liquidity_drop"
        )

        profit_prob = float((final_capitals > starting_capital_usd).mean())

        return {
            "pnl_p5": float(np.percentile(pnls_usd, 5)),
            "pnl_p25": float(np.percentile(pnls_usd, 25)),
            "pnl_p50": float(np.percentile(pnls_usd, 50)),
            "pnl_p75": float(np.percentile(pnls_usd, 75)),
            "pnl_p95": float(np.percentile(pnls_usd, 95)),
            "pnl_mean": float(pnls_usd.mean()),
            "pnl_std": float(pnls_usd.std()),
            "pnl_min": float(pnls_usd.min()),
            "pnl_max": float(pnls_usd.max()),
            "return_p5": float(np.percentile(returns_pct, 5)),
            "return_p50": float(np.percentile(returns_pct, 50)),
            "return_p95": float(np.percentile(returns_pct, 95)),
            "return_mean": float(returns_pct.mean()),
            "risk_of_ruin_pct": risk_of_ruin_pct,
            "max_dd_p50": float(np.percentile(max_dd_per_sim, 50)),
            "max_dd_p95": float(np.percentile(max_dd_per_sim, 5)),  # 5th percentile of DDs = worst 5%
            "max_dd_mean": float(max_dd_per_sim.mean()),
            "max_win_streak_mean": float(max_win_streaks.mean()),
            "max_loss_streak_mean": float(max_loss_streaks.mean()),
            "max_loss_streak_p95": float(np.percentile(max_loss_streaks, 95)),
            "worst_consec_loss_usd": float(worst_consec_loss_usd.min()),
            "final_cap_p5": float(np.percentile(final_capitals, 5)),
            "final_cap_p50": float(np.percentile(final_capitals, 50)),
            "final_cap_p95": float(np.percentile(final_capitals, 95)),
            "stress_vol_ruin": stress_vol_ruin,
            "stress_fees_ruin": stress_fees_ruin,
            "stress_liq_ruin": stress_liq_ruin,
            "profit_prob": profit_prob,
            "ev_usd": float(pnls_usd.mean()),
        }

    def _stress_test(
        self,
        pnl_pool: np.ndarray,
        num_simulations: int,
        trades_per_sim: int,
        starting_capital: float,
        pos_fraction: float,
        ruin_threshold: float,
        np_rng: np.random.Generator,
        mode: str,
    ) -> float:
        """Run a stress-test variant and return the risk-of-ruin percentage."""
        # Use fewer simulations for stress (faster)
        n = min(num_simulations, 500)
        idx = np_rng.integers(0, len(pnl_pool), size=(n, trades_per_sim))
        sampled = pnl_pool[idx]

        cost_pct = self.fee_pct + self.slippage_pct
        if mode == "high_vol":
            # Widen loss/win dispersion
            noise = np_rng.normal(0, 5.0, size=sampled.shape)
            sampled = sampled + noise
        elif mode == "high_fees":
            cost_pct = cost_pct * 3.0  # triple fees/slippage
        elif mode == "liquidity_drop":
            # Randomly truncate some wins (exit early) by multiplying positive PnLs
            mask = sampled > 0
            dampener = np_rng.uniform(0.3, 0.9, size=sampled.shape)
            sampled = np.where(mask, sampled * dampener, sampled)
            cost_pct = cost_pct * 2.0

        net = sampled - cost_pct
        per_trade = net * pos_fraction / 100.0
        log_ret = np.log1p(per_trade)
        cum_log = np.cumsum(log_ret, axis=1)
        cum_cap = starting_capital * np.exp(cum_log)
        peaks = np.maximum.accumulate(cum_cap, axis=1)
        dd = (cum_cap - peaks) / peaks * 100.0
        max_dd = dd.min(axis=1)
        ruin_count = int((max_dd <= -ruin_threshold).sum())
        return (ruin_count / n) * 100.0

    def _zero_stats(self) -> Dict[str, float]:
        return {k: 0.0 for k in [
            "pnl_p5", "pnl_p25", "pnl_p50", "pnl_p75", "pnl_p95",
            "pnl_mean", "pnl_std", "pnl_min", "pnl_max",
            "return_p5", "return_p50", "return_p95", "return_mean",
            "risk_of_ruin_pct", "max_dd_p50", "max_dd_p95", "max_dd_mean",
            "max_win_streak_mean", "max_loss_streak_mean", "max_loss_streak_p95",
            "worst_consec_loss_usd", "final_cap_p5", "final_cap_p50", "final_cap_p95",
            "stress_vol_ruin", "stress_fees_ruin", "stress_liq_ruin",
            "profit_prob", "ev_usd",
        ]}

    def _empty_result(
        self, strategy_id: str, num_sims: int, trades_per_sim: int
    ) -> MonteCarloResult:
        return MonteCarloResult(
            strategy_id=strategy_id,
            num_simulations=num_sims,
            num_trades_per_sim=trades_per_sim,
            notes=["Empty input - no trades to sample from"],
            timestamp=datetime.utcnow().isoformat(),
        )


__all__ = ["MonteCarloSimulator", "MonteCarloResult"]
