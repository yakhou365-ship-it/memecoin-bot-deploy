"""
ML Trainer - DISABLED.
Bot now uses SmartDecisionEngine (rule-based) instead of ML.
This file kept for backward compatibility (imports won't crash).
"""

FEATURE_COLUMNS = [
    "technical_score", "liquidity_score", "onchain_score",
    "security_score", "sentiment_score", "momentum_score",
    "market_conditions_score", "volatility_score", "risk_score",
    "opportunity_score", "rsi", "macd_hist", "adx", "volatility_pct",
    "liquidity_usd", "volume_24h_usd", "price_change_24h", "price_change_5m",
]


class MLTrainer:
    """Disabled - uses SmartDecisionEngine instead."""
    async def train(self, **kwargs):
        return {"status": "disabled", "message": "Using SmartDecisionEngine"}
    
    async def load_training_data(self):
        return None


class MLModelLoader:
    """Disabled - bot uses rule-based engine."""
    @staticmethod
    async def load_latest():
        return None
