"""Live-facing decision API for Strategy V2 (fast: a few vectorised pandas ops, ~ms per token)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from .config import StrategyConfig, breakeven_win_rate
from .signals import BULL, BEAR, build_features


@dataclass
class TradePlan:
    should_trade: bool = False
    side: int = 0                      # +1 long / -1 short
    entry_ref: float = 0.0             # last close; execute at next open / market
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    atr: float = 0.0
    sl_pct: float = 0.0
    tp1_pct: float = 0.0
    trail_atr: float = 0.0
    partial_fraction: float = 0.0
    risk_pct: float = 0.0              # fraction of equity to risk
    position_size_pct: float = 0.0     # notional as % of equity
    breakeven_win_rate: float = 0.0
    regime: str = "unknown"
    reasons: List[str] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)


class StrategyV2Engine:
    def __init__(self, cfg: Optional[StrategyConfig] = None):
        self.cfg = cfg or StrategyConfig()

    def evaluate(self, df15: pd.DataFrame, market_regime: Optional[pd.Series] = None) -> TradePlan:
        """df15 must have a DatetimeIndex (bar open time) and >= ~900 bars (200 hourly bars for the regime).

        The LAST row must be a CLOSED candle."""
        cfg, plan = self.cfg, TradePlan()
        if len(df15) < 900:
            plan.rejections.append(f"insufficient history ({len(df15)} < 900 x 15m bars)")
            return plan
        f = build_features(df15, cfg, market_regime)
        row = f.iloc[-1]
        plan.regime = {BULL: "bull", BEAR: "bear"}.get(int(row["regime"]), "range")
        if plan.regime == "range":
            plan.rejections.append("range regime - no trade")
        if row["atr_pct"] < cfg.min_atr_pct or pd.isna(row["atr_pct"]):
            plan.rejections.append(f"ATR% {row['atr_pct']:.3%} below cost filter {cfg.min_atr_pct:.2%}")
        side = 1 if row["long_signal"] else (-1 if row["short_signal"] else 0)
        if side == 0:
            if not plan.rejections:
                plan.rejections.append("no pullback/trigger setup")
            return plan

        entry, a = float(row["close"]), float(row["atr"])
        plan.side, plan.entry_ref, plan.atr = side, entry, a
        plan.stop_loss = entry - side * cfg.sl_atr * a
        plan.take_profit_1 = entry + side * cfg.tp1_atr * a
        plan.sl_pct = cfg.sl_atr * a / entry
        plan.tp1_pct = cfg.tp1_atr * a / entry
        rt = cfg.costs.round_trip
        plan.breakeven_win_rate = breakeven_win_rate(plan.tp1_pct, plan.sl_pct, rt)
        plan.trail_atr, plan.partial_fraction = cfg.trail_atr, cfg.partial_fraction
        plan.risk_pct = cfg.risk_per_trade
        plan.position_size_pct = min(cfg.max_position_pct * 100,
                                     cfg.risk_per_trade / (plan.sl_pct + rt) * 100)
        if plan.tp1_pct < 3 * rt:
            plan.rejections.append(f"TP1 {plan.tp1_pct:.2%} < 3x round-trip cost {rt:.2%}")
            return plan
        plan.should_trade = True
        plan.reasons = [f"{plan.regime} regime (1h)", "pullback+trigger (15m)",
                        f"ATR {a / entry:.2%}", f"SL {plan.sl_pct:.2%} / TP1 {plan.tp1_pct:.2%}"]
        return plan
