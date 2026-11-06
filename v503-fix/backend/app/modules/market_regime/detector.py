"""
Market Regime Detection.

Classifies the current market state into one of:
- bull_market
- bear_market
- sideways_market
- high_volatility
- low_volatility
- panic_selling
- strong_momentum
- low_liquidity

The classification drives which trading strategies are eligible.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class MarketRegime:
    regime: str = "unknown"  # primary
    secondary: str = "unknown"
    confidence: float = 0.0  # 0-1
    volatility_pct: float = 0.0
    trend_slope: float = 0.0
    rsi_avg: float = 50.0
    volume_trend: str = "stable"  # increasing | stable | decreasing
    reasons: List[str] = field(default_factory=list)
    trading_recommended: bool = False
    recommended_strategies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


class MarketRegimeDetector:
    """Detects market regime from a basket of memecoin price series."""

    def detect(
        self,
        basket_prices: Optional[pd.DataFrame] = None,
        broad_market_candles: Optional[pd.DataFrame] = None,
    ) -> MarketRegime:
        """
        Detect the current market regime.

        Args:
            basket_prices: DataFrame where each column is a token's close prices
            broad_market_candles: aggregated OHLCV for the broad market (e.g., SOL)
        """
        r = MarketRegime()

        if broad_market_candles is None or len(broad_market_candles) < 30:
            r.regime = "unknown"
            r.trading_recommended = False
            r.reasons.append("بيانات السوق غير كافية لتحديد النظام")
            return r

        df = broad_market_candles
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        # Compute features
        returns = close.pct_change().dropna()
        r.volatility_pct = float(returns.std() * 100)
        r.trend_slope = float((close.iloc[-1] - close.iloc[-20]) / max(close.iloc[-20], 1e-9) * 100)

        # RSI proxy
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).fillna(50)
        r.rsi_avg = float(rsi.iloc[-1])

        # Volume trend
        if len(volume) >= 20:
            recent_vol = volume.iloc[-5:].mean()
            prior_vol = volume.iloc[-20:-5].mean()
            if recent_vol > prior_vol * 1.3:
                r.volume_trend = "increasing"
            elif recent_vol < prior_vol * 0.7:
                r.volume_trend = "decreasing"

        # Classify
        # Panic selling: big drop + high volume + high volatility
        if r.trend_slope < -10 and r.volume_trend == "increasing" and r.volatility_pct > 8:
            r.regime = "panic_selling"
            r.confidence = 0.8
            r.trading_recommended = False
            r.reasons.append("بيع ذعرّي - يُمنع التداول")
        # Strong momentum: positive trend + high volume + high RSI
        elif r.trend_slope > 10 and r.volume_trend == "increasing" and r.rsi_avg > 60:
            r.regime = "strong_momentum"
            r.confidence = 0.75
            r.trading_recommended = True
            r.recommended_strategies = ["momentum_breakout", "trend_follow"]
            r.reasons.append("زخم قوي - فرص جيدة")
        # Bull market: positive trend, moderate vol
        elif r.trend_slope > 5 and r.volatility_pct < 8:
            r.regime = "bull_market"
            r.confidence = 0.7
            r.trading_recommended = True
            r.recommended_strategies = ["trend_follow", "breakout"]
            r.reasons.append("سوق صاعد")
        # Bear market: negative trend
        elif r.trend_slope < -5:
            r.regime = "bear_market"
            r.confidence = 0.7
            r.trading_recommended = False
            r.recommended_strategies = []
            r.reasons.append("سوق هابط - يُنصح بالانتظار")
        # Sideways: low trend, low vol
        elif abs(r.trend_slope) < 3 and r.volatility_pct < 5:
            r.regime = "sideways_market"
            r.confidence = 0.65
            r.trading_recommended = True
            r.recommended_strategies = ["range_trading", "mean_reversion"]
            r.reasons.append("سوق متذبذب")
        # High volatility: vol > 10% regardless of trend
        elif r.volatility_pct > 10:
            r.regime = "high_volatility"
            r.confidence = 0.7
            r.trading_recommended = False
            r.reasons.append("تقلب عالٍ - تجنب التداول")
        else:
            r.regime = "sideways_market"
            r.confidence = 0.5
            r.trading_recommended = True
            r.recommended_strategies = ["mean_reversion"]

        # Secondary classification
        if r.volatility_pct > 10:
            r.secondary = "high_volatility"
        elif r.volatility_pct < 3:
            r.secondary = "low_volatility"

        return r
