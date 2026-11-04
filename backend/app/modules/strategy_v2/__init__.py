"""Strategy V2: cost-aware, ATR-based, regime-filtered trading strategy.

Design goal: positive expectancy (in R) AFTER fees/slippage - not a headline win rate.
"""
from .config import StrategyConfig, VenueCosts, VENUES, breakeven_win_rate
from .engine import StrategyV2Engine, TradePlan

__all__ = [
    "StrategyConfig", "VenueCosts", "VENUES", "breakeven_win_rate",
    "StrategyV2Engine", "TradePlan",
]
