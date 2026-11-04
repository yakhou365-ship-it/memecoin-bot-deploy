"""Glue between the live orchestrator and Strategy V2.

* Solana tokens (chain == "solana"): full V2 evaluation on 15m candles (regime + pullback entry).
* DEX / pump.fun / FOMO tokens: they rarely have the 200h of history the regime filter needs, so only the
  COST gate applies - a trade is rejected when its take-profit distance is not at least N x round-trip cost
  (pump.fun's ~3% round trip makes small-TP scalping a guaranteed loser).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .config import StrategyConfig, VENUES, breakeven_win_rate
from .engine import StrategyV2Engine, TradePlan


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""
    plan: Optional[TradePlan] = None
    venue: str = "fomo"


def detect_venue(token) -> str:
    chain = (getattr(token, "chain", "") or "").lower()
    dex = (getattr(token, "dex", "") or "").lower()
    if chain == "solana":
        return "pumpfun"
    if "pump" in dex:
        return "pumpfun"
    return "fomo"


def cost_gate(token, entry: float, stop_loss: Optional[float], take_profit: Optional[float],
              min_tp_to_cost: float = 3.0) -> GateResult:
    venue = detect_venue(token)
    rt = VENUES[venue].round_trip
    if not entry or not take_profit or not stop_loss:
        return GateResult(False, "missing entry/SL/TP - cannot verify cost edge", venue=venue)
    tp_pct, sl_pct = (take_profit - entry) / entry, (entry - stop_loss) / entry
    if tp_pct < min_tp_to_cost * rt:
        return GateResult(False, f"TP {tp_pct:.2%} < {min_tp_to_cost:g}x round-trip cost ({rt:.2%}) on {venue}", venue=venue)
    be = breakeven_win_rate(tp_pct, sl_pct, rt)
    return GateResult(True, f"cost ok on {venue}: breakeven WR {be:.0%}", venue=venue)


async def v2_gate(ohlcv_fetcher, token, entry, stop_loss, take_profit, settings) -> GateResult:
    if not getattr(settings, "strategy_v2_enabled", False):
        return GateResult(True, "strategy_v2 disabled")
    venue = detect_venue(token)
    if venue != "pumpfun":
        return cost_gate(token, entry, stop_loss, take_profit, settings.strategy_v2_min_tp_to_cost)
    candles = await ohlcv_fetcher.fetch_and_store(token.id, timeframe="15m", limit=1000)
    if not candles:
        candles = await ohlcv_fetcher.get_latest_candles(token.id, "15m", 1000)
    if not candles or len(candles) < 900:
        ok = not settings.strategy_v2_strict
        return GateResult(ok, "not enough 15m history for V2 regime filter", venue=venue)
    df = pd.DataFrame([{"ts": c.timestamp, "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume} for c in candles])
    df["ts"] = pd.to_datetime(df["ts"], unit="s") if df["ts"].max() < 1e11 else pd.to_datetime(df["ts"], unit="ms")
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    cfg = StrategyConfig(venue="pumpfun", risk_per_trade=settings.strategy_v2_risk_per_trade,
                         enable_short=settings.strategy_v2_enable_short,
                         partial_fraction=0.0)   # live paper engine has no partial close yet
    plan = StrategyV2Engine(cfg).evaluate(df.iloc[:-1])      # drop the forming candle
    if not plan.should_trade:
        return GateResult(False, "; ".join(plan.rejections) or "no V2 setup", plan, venue)
    return GateResult(True, "; ".join(plan.reasons), plan, venue)
