"""
Liquidity & Volume Intelligence engine.

Computes:
- Slippage estimates for hypothetical trade size
- Real vs wash trading indicators
- Liquidity-to-volume ratio
- Sudden liquidity removal detection
- Liquidity health score
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class LiquidityResult:
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    volume_5m_usd: float = 0.0
    volume_to_liquidity_ratio: float = 0.0
    volume_to_mcap_ratio: float = 0.0
    estimated_slippage_pct: float = 0.0  # for default trade size
    trade_impact_pct: float = 0.0  # price impact of simulated trade
    buy_to_sell_ratio: float = 1.0
    real_volume_estimate: float = 0.0
    wash_trading_probability: float = 0.0  # 0-1
    fake_liquidity_probability: float = 0.0  # 0-1
    liquidity_removed_recently: bool = False
    liquidity_trend: str = "stable"  # increasing | stable | decreasing | critical

    liquidity_score: float = 50.0
    reasons: List[str] = field(default_factory=list)
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class LiquidityAnalyzer:
    """Analyzes liquidity quality and trading integrity."""

    def analyze(
        self,
        token,
        recent_candles: pd.DataFrame | None = None,
        trade_size_usd: float | None = None,
    ) -> LiquidityResult:
        """Analyze liquidity for a token.

        Args:
            token: Token ORM instance
            recent_candles: recent OHLCV candles (optional, improves wash detection)
            trade_size_usd: simulated trade size for slippage calc
        """
        result = LiquidityResult()
        result.liquidity_usd = float(token.liquidity_usd or 0)
        result.volume_24h_usd = float(token.volume_24h_usd or 0)
        result.volume_5m_usd = float(token.volume_5m_usd or 0)

        if result.liquidity_usd < 1:
            result.data_sufficient = False
            result.reasons.append("السيولة غير متوفرة أو صفر")
            return result

        result.data_sufficient = True
        trade_size = trade_size_usd or 1000.0  # default $1k simulation

        # Volume / Liquidity ratio (>1 = highly active, suspicious if >10)
        result.volume_to_liquidity_ratio = result.volume_24h_usd / result.liquidity_usd

        # Volume / Market cap ratio
        if token.market_cap_usd and token.market_cap_usd > 0:
            result.volume_to_mcap_ratio = result.volume_24h_usd / token.market_cap_usd

        # Buy/sell balance
        buys = token.txns_24h_buys or 0
        sells = token.txns_24h_sells or 1
        result.buy_to_sell_ratio = buys / max(sells, 1)

        # Slippage estimate (constant-product AMM approximation)
        # For 50/50 pool: price_impact = 2 * trade_size / liquidity
        result.estimated_slippage_pct = min(100, 200 * trade_size / result.liquidity_usd)
        result.trade_impact_pct = result.estimated_slippage_pct

        # Wash trading probability (heuristic)
        wash_signals = 0
        # High volume/liquidity with low tx count = suspicious
        if result.volume_to_liquidity_ratio > 5 and (buys + sells) < 100:
            wash_signals += 1
        # Perfect 1:1 buy/sell ratio with high volume
        if 0.95 < result.buy_to_sell_ratio < 1.05 and result.volume_24h_usd > 100_000 and (buys + sells) > 100:
            wash_signals += 1
        # Round number transactions (very rough heuristic from candle data)
        if recent_candles is not None and len(recent_candles) >= 20:
            vols = recent_candles["volume"].astype(float).tail(20)
            if vols.std() > 0:
                cv = vols.std() / max(vols.mean(), 1e-9)
                if cv < 0.15:  # suspiciously uniform
                    wash_signals += 1
                # Repeat transactions (same size cluster)
                unique_sizes = vols.round(-2).nunique()
                if unique_sizes < 3:
                    wash_signals += 1

        result.wash_trading_probability = min(wash_signals / 3.0, 1.0)

        # Fake liquidity probability
        fake_signals = 0
        if result.liquidity_usd < 10_000:
            fake_signals += 1
        if result.volume_24h_usd > result.liquidity_usd * 20:
            fake_signals += 1  # too much volume vs liquidity
        # TODO: when we have order book, check for one-sided walls
        result.fake_liquidity_probability = min(fake_signals / 3.0, 1.0)

        # Liquidity trend (placeholder - needs time-series of liquidity snapshots)
        # Could query historical liquidity if we stored snapshots
        if result.liquidity_usd < 5000:
            result.liquidity_trend = "critical"
        elif result.liquidity_usd < 20000:
            result.liquidity_trend = "decreasing"
        elif result.liquidity_usd > 200_000:
            result.liquidity_trend = "increasing"
        else:
            result.liquidity_trend = "stable"

        # Compute composite score
        self._score(result)
        return result

    def _score(self, r: LiquidityResult) -> None:
        score = 50.0
        reasons: List[str] = []

        # Liquidity tier
        if r.liquidity_usd >= 200_000:
            score += 25
            reasons.append("سيولة ممتازة (>$200k)")
        elif r.liquidity_usd >= 50_000:
            score += 15
            reasons.append("سيولة جيدة")
        elif r.liquidity_usd >= 20_000:
            score += 5
            reasons.append("سيولة مقبولة لكن منخفضة")
        elif r.liquidity_usd >= 5_000:
            score -= 10
            reasons.append("سيولة ضعيفة - مخاطرة انزلاق سعري عالية")
        else:
            score -= 30
            reasons.append("سيولة حرجة جداً - تجنب التداول")

        # Volume/liquidity activity
        if 0.3 <= r.volume_to_liquidity_ratio <= 5.0:
            score += 10
            reasons.append("نشاط تداول صحي مقارنة بالسيولة")
        elif r.volume_to_liquidity_ratio > 10:
            score -= 5
            reasons.append("نشاط مرتفع بشكل مريب - احتمال wash trading")
        elif r.volume_to_liquidity_ratio < 0.1:
            score -= 8
            reasons.append("نشاط منخفض جداً - لا يوجد اهتمام")

        # Slippage for simulated trade
        if r.estimated_slippage_pct < 0.5:
            score += 8
            reasons.append(f"انزلاق سعري منخفض ({r.estimated_slippage_pct:.2f}%)")
        elif r.estimated_slippage_pct < 2:
            score += 2
        elif r.estimated_slippage_pct < 5:
            score -= 10
            reasons.append(f"انزلاق سعري مرتفع ({r.estimated_slippage_pct:.2f}%) - سيفقد ربحاً")
        else:
            score -= 25
            reasons.append(f"انزلاق سعري كارثي ({r.estimated_slippage_pct:.2f}%)")

        # Buy/sell balance
        if 0.7 <= r.buy_to_sell_ratio <= 2.0:
            score += 5
        elif r.buy_to_sell_ratio > 3:
            score -= 5
            reasons.append("ضغط بيعي قوي")

        # Wash trading
        if r.wash_trading_probability > 0.7:
            score -= 20
            reasons.append("احتمال wash trading عالٍ جداً")
        elif r.wash_trading_probability > 0.4:
            score -= 10
            reasons.append("احتمال wash trading")

        # Fake liquidity
        if r.fake_liquidity_probability > 0.5:
            score -= 25
            reasons.append("احتمال سيولة وهمية عالٍ")

        # Volume vs market cap
        if r.volume_to_mcap_ratio > 0 and r.volume_to_mcap_ratio < 1.0:
            score += 3  # healthy turnover
        elif r.volume_to_mcap_ratio >= 5:
            score -= 5
            reasons.append("حجم تداول يتجاوز القيمة السوقية - احتمال pump")

        r.liquidity_score = max(0, min(100, score))
        r.reasons = reasons
