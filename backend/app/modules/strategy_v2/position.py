"""Single-position state machine with improved exits.

Improvements applied:
- Section 4.1: TP2 at 2.5R (closes additional 25%)
- Section 4.2: Swing-low trailing combined with Chandelier
- Section 4.3: Momentum exit after TP1
- Section 4.4: Earlier breakeven (0.8R)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .config import StrategyConfig


@dataclass
class Fill:
    fraction: float
    price: float
    reason: str


@dataclass
class Position:
    side: int
    entry: float
    atr: float
    cfg: StrategyConfig
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    remaining: float = 1.0
    tp1_done: bool = False
    tp2_done: bool = False
    extreme: float = 0.0
    bars: int = 0
    fills: List[Fill] = field(default_factory=list)
    recent_lows: List[float] = field(default_factory=list)
    recent_highs: List[float] = field(default_factory=list)
    closes_below_ema20_count: int = 0
    rsi_history: List[float] = field(default_factory=list)

    def __post_init__(self):
        s = self.side
        self.stop = self.entry - s * self.cfg.sl_atr * self.atr
        self.tp1 = self.entry + s * self.cfg.tp1_atr * self.atr
        self.tp2 = self.entry + s * self.cfg.tp2_atr * self.atr
        self.extreme = self.entry

    @property
    def sl_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry

    def _close(self, frac: float, price: float, reason: str):
        frac = min(frac, self.remaining)
        self.fills.append(Fill(frac, price, reason))
        self.remaining -= frac

    @property
    def done(self) -> bool:
        return self.remaining <= 1e-9

    def on_bar(self, o: float, h: float, l: float, c: float,
               ema20: float = 0.0, rsi_val: float = 50.0) -> None:
        s, cfg = self.side, self.cfg
        self.bars += 1
        self.extreme = max(self.extreme, h) if s > 0 else min(self.extreme, l)
        if s > 0:
            self.recent_lows.append(l)
            if len(self.recent_lows) > cfg.swing_low_lookback:
                self.recent_lows = self.recent_lows[-cfg.swing_low_lookback:]
        else:
            self.recent_highs.append(h)
            if len(self.recent_highs) > cfg.swing_low_lookback:
                self.recent_highs = self.recent_highs[-cfg.swing_low_lookback:]
        if ema20 > 0:
            if (s > 0 and c < ema20) or (s < 0 and c > ema20):
                self.closes_below_ema20_count += 1
            else:
                self.closes_below_ema20_count = 0
        self.rsi_history.append(rsi_val)
        if len(self.rsi_history) > 20:
            self.rsi_history = self.rsi_history[-20:]

        stop_hit = (l <= self.stop) if s > 0 else (h >= self.stop)
        if stop_hit:
            gap = min(o, self.stop) if s > 0 else max(o, self.stop)
            reason = "stop" if not self.tp1_done else "trail_stop"
            self._close(self.remaining, gap, reason)
            return

        if not self.tp1_done:
            tp_hit = (h >= self.tp1) if s > 0 else (l <= self.tp1)
            if tp_hit:
                frac = cfg.partial_fraction if cfg.partial_fraction > 0 else 0.0
                if frac > 0:
                    self._close(frac, self.tp1, "tp1")
                self.tp1_done = True
                # V539 FIX: وقف التعادل يجب أن يغطي تكلفة الذهاب والإياب للصفقة
                # الكاملة، لا للمركز المتبقي فقط. المعادلة القديمة entry×(1+RT)
                # أهملت أن التكلفة تُدفع على كامل المبلغ بينما المتبقي `remaining`
                # فقط، وأن ربح TP1 الجزئي سدّد جزءاً من التكلفة. التعادل الحقيقي:
                #   remaining×x = RT − frac×tp1_gain  →  x = (RT − frac×g₁)/remaining
                # (مثال: RT=3%، frac=40%، g₁=1.5% → x = (3%−0.6%)/60% = +4%)
                tp1_gain = abs(self.tp1 - self.entry) / self.entry if self.entry else 0.0
                remaining = self.remaining if self.remaining > 1e-9 else 1.0
                be_gain = max(
                    0.0,
                    (cfg.costs.round_trip + cfg.breakeven_buffer - frac * tp1_gain) / remaining,
                )
                be = self.entry * (1 + s * be_gain)
                self.stop = max(self.stop, be) if s > 0 else min(self.stop, be)

        # IMPROVEMENT 4.1: TP2 at 2.5R
        if self.tp1_done and not self.tp2_done and cfg.tp2_atr > cfg.tp1_atr:
            tp2_hit = (h >= self.tp2) if s > 0 else (l <= self.tp2)
            if tp2_hit:
                if cfg.tp2_fraction > 0:
                    self._close(cfg.tp2_fraction * self.remaining, self.tp2, "tp2")
                self.tp2_done = True

        if self.tp1_done:
            chandelier = self.extreme - s * cfg.trail_atr * self.atr
            new_stop = chandelier
            # IMPROVEMENT 4.2: Swing-low trailing
            if cfg.use_swing_low_trail and len(self.recent_lows if s > 0 else self.recent_highs) >= 2:
                if s > 0:
                    swing = min(self.recent_lows)
                    swing_trail = swing - cfg.swing_low_buffer_atr * self.atr
                    new_stop = max(new_stop, swing_trail)
                else:
                    swing = max(self.recent_highs)
                    swing_trail = swing + cfg.swing_low_buffer_atr * self.atr
                    new_stop = min(new_stop, swing_trail)
            self.stop = max(self.stop, new_stop) if s > 0 else min(self.stop, new_stop)

        # IMPROVEMENT 4.3: Momentum exit
        if self.tp1_done and cfg.use_momentum_exit and not self.done:
            if self.closes_below_ema20_count >= cfg.momentum_exit_bars:
                self._close(self.remaining, c, "momentum_exit")
                return
            if len(self.rsi_history) >= 5:
                if self.rsi_history[-1] < cfg.momentum_exit_rsi and max(self.rsi_history[-5:]) > 70:
                    self._close(self.remaining, c, "momentum_rsi_exit")
                    return

        if self.done:
            return
        progress = s * (c - self.entry) / (cfg.sl_atr * self.atr)
        if not self.tp1_done and self.bars >= cfg.time_stop_bars and progress < cfg.time_stop_min_r:
            self._close(self.remaining, c, "time_stop")
        elif self.bars >= cfg.max_hold_bars:
            self._close(self.remaining, c, "max_hold")
