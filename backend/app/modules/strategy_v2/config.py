"""Central configuration for Strategy V2 (IMPROVED VERSION).

All improvements from IMPROVEMENTS_PROPOSAL.txt applied:
- Section 1: Reduced costs (BNB 25% discount, maker limit orders)
- Section 4: Improved exits (TP2 at 2.5R, swing-low trailing, momentum exit, earlier breakeven)
- Section 5: Better regime detection (3rd volatility state, ADX rising option)
- Section 6: Capital management (risk scaling, Kelly cap, kill switches)
- Tuned parameters: SL=2.5×ATR, TP1=1.2×ATR, TP2=2.5×ATR, trail=1.2×ATR
"""
from __future__ import annotations

from dataclasses import dataclass, replace, asdict
from typing import Dict


@dataclass(frozen=True)
class VenueCosts:
    """Round-trip trading cost as a FRACTION of notional (0.003 = 0.3%)."""
    name: str
    fee_per_side: float
    slippage_per_side: float
    funding_per_8h: float = 0.0   # futures only

    @property
    def round_trip(self) -> float:
        return 2 * (self.fee_per_side + self.slippage_per_side)


VENUES: Dict[str, VenueCosts] = {
    # IMPROVEMENT 1.1+1.3: BNB 25% discount on fees + maker limit order slippage
    # Original: 0.1% fee + 0.05% slippage = 0.30% RT
    # Improved: 0.075% fee (BNB discount) + 0.02% slippage (maker) = ~0.19% RT
    "pumpfun": VenueCosts("pumpfun", 0.01, 0.005),
    
    # PumpPortal Lightning 1%/side + 0.5% slippage/side = 3% round trip
    "pumpfun": VenueCosts("pumpfun", 0.01, 0.005),
    # DEX 0.3% + slippage
    "fomo": VenueCosts("fomo", 0.003, 0.003),
}


@dataclass(frozen=True)
class StrategyConfig:
    # ---- regime (1h) ----
    regime_ema: int = 200
    regime_slope_bars: int = 20
    regime_adx_min: float = 20.0
    range_adx_max: float = 18.0
    # IMPROVEMENT 5.1: 3rd volatility state (dead/extreme market)
    volatility_lookback: int = 100
    dead_market_pct: float = 20.0            # < 20th percentile = dead market
    extreme_market_pct: float = 90.0         # > 90th percentile = extreme (50% size)
    # IMPROVEMENT 5.2: ADX must be rising (option)
    require_adx_rising: bool = False         # disabled by default (too strict)
    adx_rising_lookback: int = 5

    # ---- entry (15m) ----
    # IMPROVEMENT 1.4: balanced cost filter
    min_atr_pct: float = 0.004               # 0.4% (relaxed to allow more trades)
    cost_filter_multiplier: float = 2.0      # TP1 must be >= 2x round-trip cost
    pullback_lookback: int = 6
    pullback_rsi_below: float = 50.0         # relaxed (was 45)
    pullback_rsi_resume: float = 45.0         # relaxed (was 40)
    near_ma_tolerance: float = 0.005          # relaxed (was 0.003)
    vol_mult: float = 1.2
    vol_lookback: int = 20

    # IMPROVEMENT 2.1: Relative Strength filter (tracked, optional)
    rs_lookback_bars: int = 96               # 24h on 15m
    rs_min_outperform: float = 0.0
    rs_top_pct: float = 40.0

    # IMPROVEMENT 2.2: Room to run filter (tracked, optional)
    room_to_run_lookback: int = 50
    room_to_run_min_r: float = 1.5           # was 2.0 - too strict

    # IMPROVEMENT 2.3: Pullback depth (tracked, optional)
    pullback_max_depth_atr: float = 0.5

    # IMPROVEMENT 2.5: Candle quality (tracked, optional)
    candle_min_body_pct: float = 0.5
    candle_max_atr: float = 2.5

    # IMPROVEMENT 2.7: Extreme volatility filter
    max_atr_percentile: float = 95.0

    # ---- exits (ATR multiples) - V31.2 AGGRESSIVE ----
    sl_atr: float = 1.5                      # tighter stop-loss
    tp1_atr: float = 1.5                     # TP1 at 1.5 ATR
    # TP2 at 4R
    tp2_atr: float = 4.0
    tp2_fraction: float = 0.30
    partial_fraction: float = 0.40           # 40% at TP1
    trail_atr: float = 1.5                   # wider trailing
    # Swing-low trailing
    use_swing_low_trail: bool = True
    swing_low_lookback: int = 5
    swing_low_buffer_atr: float = 0.2
    # Momentum exit after TP1
    use_momentum_exit: bool = True
    momentum_exit_bars: int = 2              # 2 closes below EMA20
    momentum_exit_rsi: float = 50.0
    # Earlier breakeven
    breakeven_after_r: float = 0.5           # move to breakeven at +0.5R
    breakeven_buffer: float = 0.0005
    # time_stop
    time_stop_bars: int = 4                  # exit if stalling
    time_stop_min_r: float = 0.5             # require progress
    max_hold_bars: int = 24                  # 6h max

    # ---- sizing / portfolio risk - V31.2 AGGRESSIVE ----
    risk_per_trade: float = 0.03             # 3% risk per trade (was 1%)
    max_position_pct: float = 0.18           # max 18% per trade (was 15%)
    max_open_positions: int = 3              # max 3 concurrent (was 4)
    daily_loss_limit: float = 0.15           # 15% daily loss limit (was 4%)
    weekly_loss_limit: float = 0.08          # 8% weekly (was 6%)
    max_drawdown_halt: float = 0.30          # halt at 30% DD (was 10%)
    halt_days: int = 3                       # 3 day cooldown (was 7)
    cooldown_after_losses: int = 2           # 2 losses = cooldown (was 3)
    cooldown_bars: int = 16
    min_notional_usd: float = 5.0

    # IMPROVEMENT 6.1: Risk scaling after losses/wins
    risk_scale_after_2_losses: float = 0.5
    risk_scale_after_3_wins: float = 1.0
    # IMPROVEMENT 6.2: Total open risk cap
    max_total_open_risk: float = 0.025
    # IMPROVEMENT 6.3: Kelly cap
    kelly_cap: float = 0.40
    kelly_min_trades: int = 200
    # V31.2: Pyramid (add to winners)
    pyramid_enabled: bool = True
    pyramid_trigger_r: float = 1.5           # add on +1.5R move
    pyramid_add_pct: float = 1.0              # add 1x base position
    # IMPROVEMENT 6.4: Per-symbol kill switch
    symbol_kill_after_trades: int = 20
    symbol_kill_max_avg_r: float = -0.3
    symbol_kill_days: int = 14
    # IMPROVEMENT 6.5: System kill switch
    system_kill_halt_count: int = 2
    system_kill_window_days: int = 60

    # ---- optional ----
    enable_short: bool = False               # disabled (Pump.fun only)
    require_market_ok: bool = True
    venue: str = "pumpfun"

    @property
    def costs(self) -> VenueCosts:
        return VENUES[self.venue]

    def with_(self, **kw) -> "StrategyConfig":
        return replace(self, **kw)

    def to_dict(self) -> Dict:
        return asdict(self)


def breakeven_win_rate(tp_pct: float, sl_pct: float, round_trip_cost: float) -> float:
    """Win rate required to break even (all values as fractions, e.g. 0.005 = 0.5%)."""
    win = tp_pct - round_trip_cost
    loss = sl_pct + round_trip_cost
    if win <= 0:
        return 1.0
    return loss / (win + loss)
