"""
Research & meta-analysis modules.

Exposes:
- CounterfactualTracker: tracks rejected opportunities and what would've happened
- MonteCarloSimulator: stress-tests strategy under random scenarios
- StrategyLab: automated strategy research (champion vs challenger)
- IndependentAuditor: verifies win/loss data integrity and calibration
- HealthMonitor: monitors data freshness, model drift, system health
- MarketRegimeMemory: regime -> best-strategy memory
"""
from .counterfactual_tracker import CounterfactualTracker, CounterfactualRecord
from .monte_carlo import MonteCarloSimulator, MonteCarloResult
from .strategy_lab import StrategyLab, StrategyHypothesis, ChampionChallengerResult
from .independent_auditor import IndependentAuditor, AuditReport
from .health_monitor import HealthMonitor, HealthStatus
from .market_regime_memory import MarketRegimeMemory, RegimePerformanceRecord

__all__ = [
    "CounterfactualTracker",
    "CounterfactualRecord",
    "MonteCarloSimulator",
    "MonteCarloResult",
    "StrategyLab",
    "StrategyHypothesis",
    "ChampionChallengerResult",
    "IndependentAuditor",
    "AuditReport",
    "HealthMonitor",
    "HealthStatus",
    "MarketRegimeMemory",
    "RegimePerformanceRecord",
]
