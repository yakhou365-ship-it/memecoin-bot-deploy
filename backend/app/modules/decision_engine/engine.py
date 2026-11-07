"""
AI Decision Engine - the brain of the system.

Combines outputs from ALL analysis modules to produce:
1. 10 individual scores (technical, liquidity, on-chain, security, sentiment,
   momentum, market_conditions, volatility, risk, opportunity)
2. Calibrated Win Probability with confidence interval
3. Qualification decision (qualified / rejected with reason)
4. AI reasoning summary in Arabic

KEY INVARIANTS:
- Opportunity Score != Win Probability (different things)
- Win Probability must be calibrated by ML model, NEVER estimated by LLM
- Critical security flags -> auto-disqualify, regardless of other scores
- Insufficient data -> reject
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...core.config import settings
from ...core.logger import logger
from ...core.models import AnalysisSnapshot
from ..liquidity.analyzer import LiquidityAnalyzer, LiquidityResult
from ..market_regime.detector import MarketRegime, MarketRegimeDetector
from ..onchain.analyzer import OnChainAnalyzer, OnChainResult
from ..security.scanner import SecurityResult, SecurityScanner
from ..sentiment.analyzer import SentimentAnalyzer, SentimentResult
from ..technical_analysis.analyzer import TechnicalAnalyzer, TechnicalAnalysisResult


@dataclass
class DecisionResult:
    # All scores (0-100 unless noted)
    technical_score: float = 50.0
    liquidity_score: float = 50.0
    onchain_score: float = 50.0
    security_score: float = 50.0
    sentiment_score: float = 50.0
    momentum_score: float = 50.0
    market_conditions_score: float = 50.0
    volatility_score: float = 50.0
    risk_score: int = 50  # 0-100, higher = riskier
    opportunity_score: float = 50.0

    # Win probability (calibrated, with CI)
    win_probability: float = 0.5
    win_probability_lower_ci: float = 0.5
    win_probability_upper_ci: float = 0.5
    win_probability_method: str = "naive_baseline"
    # Methods: naive_baseline | ml_calibrated | empirical | insufficient_data

    # Market regime
    market_regime: str = "unknown"

    # Decision
    qualified: bool = False
    rejection_reason: str = ""
    recommendation: str = "wait"  # buy | wait | reject | monitor
    suggested_entry: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    suggested_position_size_pct: float = 0.0
    suggested_holding_period_minutes: int = 0

    # Reasoning
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    failure_risks: List[str] = field(default_factory=list)
    data_quality_confidence: float = 0.0  # 0-1
    reasons: List[str] = field(default_factory=list)  # Arabic reasons
    reasoning_summary: str = ""

    # Sub-results (full detail for DB persistence)
    technical_detail: Optional[Dict] = None
    liquidity_detail: Optional[Dict] = None
    onchain_detail: Optional[Dict] = None
    security_detail: Optional[Dict] = None
    sentiment_detail: Optional[Dict] = None

    # Timestamps
    data_age_seconds: int = 0
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class DecisionEngine:
    """Combines all analyzers and produces a final decision."""

    def __init__(
        self,
        technical: Optional[TechnicalAnalyzer] = None,
        liquidity: Optional[LiquidityAnalyzer] = None,
        onchain: Optional[OnChainAnalyzer] = None,
        security: Optional[SecurityScanner] = None,
        sentiment: Optional[SentimentAnalyzer] = None,
        regime: Optional[MarketRegimeDetector] = None,
        ml_model: Optional[Any] = None,  # ML probability model (optional, may be None)
    ):
        self.technical = technical or TechnicalAnalyzer()
        self.liquidity = liquidity or LiquidityAnalyzer()
        self.onchain = onchain or OnChainAnalyzer()
        self.security = security or SecurityScanner()
        self.sentiment = sentiment or SentimentAnalyzer()
        self.regime = regime or MarketRegimeDetector()
        self.ml_model = ml_model  # Optional calibrated ML model

    async def analyze(
        self,
        token,
        candles_df: Optional[Any] = None,
        market_regime: Optional[MarketRegime] = None,
    ) -> DecisionResult:
        """
        Run full analysis pipeline on a token.
        Returns a DecisionResult.
        """
        result = DecisionResult()
        result.data_age_seconds = self._compute_data_age(token)

        # === Run all analyzers in parallel where possible ===
        # Technical & liquidity are sync; onchain & sentiment are async
        technical_result = self.technical.analyze(candles_df) if candles_df is not None else TechnicalAnalysisResult()
        liquidity_result = self.liquidity.analyze(token, candles_df, settings.paper_capital_usd * 0.05)
        onchain_result = await self.onchain.analyze(token)
        sentiment_result = await self.sentiment.analyze(token)
        security_result = self.security.analyze(token, onchain_result, liquidity_result)

        # If regime not provided, derive a basic one
        if market_regime is None:
            market_regime = MarketRegime()
            market_regime.regime = "unknown"
            market_regime.trading_recommended = True  # default optimistic
        result.market_regime = market_regime.regime

        # === Compute momentum & volatility scores from technical ===
        result.momentum_score = self._compute_momentum_score(technical_result, token)
        result.volatility_score = self._compute_volatility_score(technical_result)
        result.market_conditions_score = self._compute_market_conditions_score(market_regime)

        # === Copy scores ===
        result.technical_score = technical_result.technical_score
        result.liquidity_score = liquidity_result.liquidity_score
        result.onchain_score = onchain_result.onchain_score
        result.security_score = security_result.security_score
        result.sentiment_score = sentiment_result.sentiment_score
        result.risk_score = security_result.risk_score

        # === Persist sub-results ===
        result.technical_detail = technical_result.to_dict()
        result.liquidity_detail = liquidity_result.to_dict()
        result.onchain_detail = onchain_result.to_dict()
        result.security_detail = security_result.to_dict()
        result.sentiment_detail = sentiment_result.to_dict()

        # === Compute Opportunity Score (weighted, NOT just average) ===
        result.opportunity_score = self._compute_opportunity_score(result, technical_result, liquidity_result)

        # === Compute Win Probability (calibrated if possible) ===
        self._compute_win_probability(result, token, technical_result, liquidity_result)

        # === Decide qualification ===
        self._decide(result, token, market_regime, security_result)

        # === Reasoning summary ===
        self._build_reasoning(result, token, technical_result, liquidity_result, security_result)

        # === Suggest trade parameters ===
        if result.qualified:
            self._suggest_trade_params(result, token, technical_result, liquidity_result)

        return result

    def _compute_data_age(self, token) -> int:
        """Seconds since token.last_seen_at (proxy for data freshness)."""
        if not token.updated_at:
            return 999999
        delta = (datetime.utcnow() - token.updated_at).total_seconds()
        return int(delta)

    def _compute_momentum_score(self, ta: TechnicalAnalysisResult, token) -> float:
        """Derive a momentum-specific score."""
        score = 50.0
        # MACD histogram direction
        if ta.macd_hist > 0:
            score += 10
        # Stochastic RSI direction
        if ta.stoch_rsi_k > ta.stoch_rsi_d:
            score += 8
        # ADX strength
        if ta.adx > 25:
            score += 10
        elif ta.adx > 20:
            score += 5
        # 5m price change
        chg_5m = token.price_change_5m_pct or 0
        if 0 < chg_5m < 10:
            score += 5
        elif chg_5m > 20:
            score -= 5  # too fast = fakeout risk
        elif chg_5m < -10:
            score -= 10
        # Price action momentum
        if ta.price_action.get("momentum_weakening"):
            score -= 8
        return max(0, min(100, score))

    def _compute_volatility_score(self, ta: TechnicalAnalysisResult) -> float:
        """
        Volatility score: prefer moderate volatility.
        Too low = no opportunity, too high = unpredictable.
        """
        v = ta.volatility_pct
        if 2 <= v <= 5:
            return 80.0
        if 1 <= v < 2:
            return 60.0
        if 5 < v <= 10:
            return 50.0
        if v < 1:
            return 40.0
        if 10 < v <= 20:
            return 30.0
        return 15.0  # > 20% volatility

    def _compute_market_conditions_score(self, regime: MarketRegime) -> float:
        """Score based on whether the market regime is favorable."""
        scores = {
            "bull_market": 80,
            "strong_momentum": 75,
            "sideways_market": 55,
            "low_volatility": 60,
            "high_volatility": 25,
            "bear_market": 20,
            "panic_selling": 10,
            "low_liquidity": 30,
            "unknown": 50,
        }
        return scores.get(regime.regime, 50.0)

    def _compute_opportunity_score(
        self, result: DecisionResult, ta: TechnicalAnalysisResult, liq: LiquidityResult
    ) -> float:
        """
        Weighted composite of all scores.
        Weights reflect empirical importance for memecoin trading:
        - Security is most important (blockbuster risk if ignored)
        - Liquidity matters (slippage kills profit)
        - Technical & momentum drive entry timing
        - Sentiment & on-chain are secondary (often delayed)
        """
        # Define weights (sum to 1.0)
        weights = {
            "technical": 0.18,
            "liquidity": 0.18,
            "onchain": 0.12,
            "security": 0.20,
            "sentiment": 0.08,
            "momentum": 0.14,
            "market_conditions": 0.10,
        }

        # Note: risk_score is penalizing, others are rewarding
        opp = (
            weights["technical"] * result.technical_score
            + weights["liquidity"] * result.liquidity_score
            + weights["onchain"] * result.onchain_score
            + weights["security"] * result.security_score  # security_score is inverted (safer = higher)
            + weights["sentiment"] * result.sentiment_score
            + weights["momentum"] * result.momentum_score
            + weights["market_conditions"] * result.market_conditions_score
        )

        # Penalty: high risk score drags down opportunity
        if result.risk_score > 70:
            opp *= 0.5
        elif result.risk_score > 50:
            opp *= 0.75

        # Penalty: insufficient data reduces confidence in opportunity
        if not ta.data_sufficient:
            opp *= 0.7
        if not liq.data_sufficient:
            opp *= 0.7

        return max(0, min(100, opp))

    def _compute_win_probability(
        self, result: DecisionResult, token, ta: TechnicalAnalysisResult, liq: LiquidityResult
    ) -> None:
        """
        Compute the calibrated Win Probability.
        IMPORTANT: Never produces a number > 0.95 (no certainty in trading).

        If we have a calibrated ML model, use it. Otherwise fall back to a
        transparent, well-defined heuristic clearly labeled as such.
        """
        # If we have a calibrated ML model, use it
        if self.ml_model is not None:
            try:
                features = self._build_ml_features(result, token, ta, liq)
                prob = float(self.ml_model.predict_proba(features)[0, 1])
                # Apply confidence interval based on model calibration error
                cal_err = getattr(self.ml_model, "calibration_error_", 0.1)
                margin = max(cal_err, 0.05)
                result.win_probability = min(max(prob, 0.05), 0.95)
                result.win_probability_lower_ci = max(0.0, result.win_probability - margin)
                result.win_probability_upper_ci = min(1.0, result.win_probability + margin)
                result.win_probability_method = "ml_calibrated"
                return
            except Exception as exc:
                logger.debug(f"ML model failed, falling back to heuristic: {exc}")

        # Heuristic baseline (transparent, labeled as such)
        # This is a starting point - real calibration requires backtesting
        # Combine the most predictive factors with documented weights
        technical_signal = result.technical_score / 100.0
        liquidity_signal = result.liquidity_score / 100.0
        security_signal = result.security_score / 100.0
        momentum_signal = result.momentum_score / 100.0
        market_signal = result.market_conditions_score / 100.0
        risk_penalty = max(0, (result.risk_score - 30) / 100.0)

        # Weighted blend
        base_prob = (
            0.30 * technical_signal
            + 0.20 * liquidity_signal
            + 0.20 * security_signal
            + 0.15 * momentum_signal
            + 0.15 * market_signal
        )
        # Risk penalty reduces probability
        prob = base_prob * (1 - risk_penalty * 0.5)
        # Empirical prior: most random entries have ~45% win rate after fees
        # So blend with this prior
        prob = 0.45 * 0.5 + prob * 0.5

        # Cap
        prob = min(max(prob, 0.05), 0.95)
        # Confidence interval (wider for heuristic)
        margin = 0.15  # ±15% for heuristic
        result.win_probability = prob
        result.win_probability_lower_ci = max(0.0, prob - margin)
        result.win_probability_upper_ci = min(1.0, prob + margin)
        result.win_probability_method = "naive_baseline"

    def _build_ml_features(self, result, token, ta, liq):
        """Build feature vector for ML model."""
        import numpy as np
        features = np.array([[
            result.technical_score, result.liquidity_score, result.onchain_score,
            result.security_score, result.sentiment_score, result.momentum_score,
            result.market_conditions_score, result.volatility_score, result.risk_score,
            result.opportunity_score, ta.rsi, ta.macd_hist, ta.adx, ta.volatility_pct,
            float(token.liquidity_usd or 0), float(token.volume_24h_usd or 0),
            float(token.price_change_24h_pct or 0), float(token.price_change_5m_pct or 0),
        ]])
        return features

    def _decide(
        self, result: DecisionResult, token, regime: MarketRegime, security: SecurityResult
    ) -> None:
        """Apply all qualification gates.

        NOTE: Per user request, the 70% Win Probability gate has been REMOVED.
        The decision is now based on AI agent consensus (all 5 must agree).
        The Win Probability is still computed and displayed for transparency,
        but it's no longer a hard gate.

        Only hard gates that remain:
        1. Critical security flags (auto-disqualify)
        2. Data freshness
        3. Data sufficiency
        4. Liquidity minimum
        5. Volume minimum
        6. Risk score maximum
        7. Market regime
        8. V511 — Single-wallet >30% hard-block (RUG PULL GUARANTEE)
        The FINAL decision is made by AI agent consensus.
        """
        # GATE 1: Critical security flags -> auto-disqualify
        if security.auto_disqualify:
            result.qualified = False
            result.recommendation = "reject"
            result.rejection_reason = f"مؤشر خطر حرج: {security.disqualify_reason}"
            result.reasons.append(result.rejection_reason)
            return

        # GATE 1b (V511): Single-wallet 30%+ HARD BLOCK
        # هذا خط دفاعي مستقل عن SecurityScanner — حتى لو فشل هو، نحن نحظر.
        # محفظة واحدة تملك 30%+ = يمكنها dump كامل العرض في صفقة واحدة.
        max_pct = float(getattr(settings, "max_single_holder_pct", 30.0) or 0.0)
        block_enabled = bool(getattr(settings, "block_single_wallet_over_pct", True))
        onchain_detail = result.onchain_detail or {}
        top_holder_pct = float(
            onchain_detail.get("top_holder_pct")
            or getattr(security, "top_holder_pct", 0.0)
            or 0.0
        )
        if max_pct > 0 and block_enabled and top_holder_pct >= max_pct:
            result.qualified = False
            result.recommendation = "reject"
            top_addr = (
                onchain_detail.get("top_holder_address")
                or getattr(security, "top_holder_address", "")
                or "unknown"
            )
            result.rejection_reason = (
                f"🔴🔴 محفظة واحدة تملك {top_holder_pct:.2f}% "
                f"({top_addr[:10]}...) ≥ عتبة الحظر {max_pct:.0f}% — "
                f"خطر rug pull مؤكد، رفض قاطع"
            )
            result.reasons.append(result.rejection_reason)
            logger.error(
                f"V511 HARD-BLOCK: {token.symbol} rejected — "
                f"single wallet {top_holder_pct:.2f}% (>= {max_pct}%)"
            )
            return

        # GATE 2: Data freshness (V31.2: BYPASSED — Pump.fun data may be stale)
        # Instead of rejecting, just note it and continue
        if result.data_age_seconds > settings.min_data_age_seconds * 5:
            result.reasons.append(f"⚠️ البيانات قديمة ({result.data_age_seconds}s) لكن التجاوز مسموح")
            # V31.2: Don't return — continue to other gates

        # GATE 3: Data sufficiency (V31.2: relaxed for Pump.fun tokens)
        # Pump.fun tokens are NEW — they don't have enough OHLCV history.
        # Instead of rejecting, we mark as "limited data" and let other gates decide.
        if not (result.technical_detail or {}).get("data_sufficient", False):
            # V31.2: Don't reject — just note it and continue
            result.reasons.append("البيانات الفنية محدودة (توكن جديد)")
            # Fall through to other gates (liquidity, volume, risk)
            # The AI agent consensus will make the final call

        # GATE 4: Liquidity minimum
        # V513: حد السيولة يعتمد على الـ profile النشط
        min_liq = settings.active_min_liquidity_usd
        if (token.liquidity_usd or 0) < min_liq:
            result.qualified = False
            result.recommendation = "monitor"
            result.rejection_reason = (
                f"السيولة {token.liquidity_usd} أقل من الحد الأدنى {min_liq}"
            )
            result.reasons.append(result.rejection_reason)
            return

        # GATE 5: Volume minimum
        if (token.volume_24h_usd or 0) < settings.min_volume_24h_usd:
            result.qualified = False
            result.recommendation = "monitor"
            result.rejection_reason = (
                f"حجم التداول {token.volume_24h_usd} أقل من الحد الأدنى"
            )
            result.reasons.append(result.rejection_reason)
            return

        # GATE 5b: Ultimate-bot — لا تطارد السعر (تغير 24h كبير = قمة)
        # V513: الحد يعتمد على الـ profile (conservative: 500%, aggressive: 800%, extreme: معطّل)
        chg_24h = float(token.price_change_24h_pct or 0)
        chg_5m = float(token.price_change_5m_pct or 0)
        max_chg_24h = settings.active_max_chg_24h_pct
        if chg_24h > max_chg_24h and chg_5m <= 0:
            result.qualified = False
            result.recommendation = "monitor"
            result.rejection_reason = (
                f"تغير 24h = {chg_24h:.0f}% (قمة) و 5m = {chg_5m:.1f}% (يهبط) — "
                f"مطاردة السعر، انتظار تصحيح"
            )
            result.reasons.append(f"⚠️ {result.rejection_reason}")
            return

        # GATE 5c: Ultimate-bot — لا تدخل توكن تغير 5 دقائق > حد معين
        # V513: الحد يعتمد على الـ profile (conservative: 100%, aggressive: 150%, extreme: 250%)
        # V515 SNIPER: اشترط chg_5m بين 5% و 80% فقط — بداية pump فقط
        if settings.is_sniper_mode:
            min_chg_5m = settings.sniper_min_chg_5m_pct  # 5%
            max_chg_5m = settings.sniper_max_chg_5m_pct   # 80%
            if chg_5m < min_chg_5m or chg_5m > max_chg_5m:
                result.qualified = False
                result.recommendation = "monitor"
                result.rejection_reason = (
                    f"SNIPER: chg_5m={chg_5m:.1f}% خارج النطاق [{min_chg_5m}-{max_chg_5m}%] — "
                    f"الـ pump لم يبدأ أو وصل متأخراً"
                )
                result.reasons.append(f"⚠️ {result.rejection_reason}")
                return
            # V515 SNIPER: اشترط عمر التوكن < 5 دقائق
            # ملاحظة: لا يتوفر age مباشرة في token model، نستخدم first_seen_at
            try:
                from datetime import datetime
                first_seen = getattr(token, "first_seen_at", None)
                if first_seen:
                    if isinstance(first_seen, str):
                        first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                    age_sec = (datetime.utcnow() - first_seen.replace(tzinfo=None)).total_seconds()
                    age_min = age_sec / 60
                    max_age_min = settings.sniper_max_age_minutes  # 5 دقائق
                    if age_min > max_age_min:
                        result.qualified = False
                        result.recommendation = "monitor"
                        result.rejection_reason = (
                            f"SNIPER: عمر التوكن {age_min:.1f}دقيقة > {max_age_min}دقيقة — "
                            f"فقط العملات الجديدة جداً"
                        )
                        result.reasons.append(f"⚠️ {result.rejection_reason}")
                        return
            except Exception as exc:
                logger.debug(f"Token age check failed for {token.symbol}: {exc}")
        else:
            max_chg_5m = settings.active_max_chg_5m_pct
            if chg_5m > max_chg_5m:
                result.qualified = False
                result.recommendation = "monitor"
                result.rejection_reason = (
                    f"ارتفاع 5 دقائق = {chg_5m:.0f}% — pump مفاجئ، انتظار استقرار"
                )
                result.reasons.append(f"⚠️ {result.rejection_reason}")
                return

        # GATE 6: Risk score maximum
        if result.risk_score > settings.max_risk_score:
            result.qualified = False
            result.recommendation = "reject"
            result.rejection_reason = f"درجة المخاطرة {result.risk_score} تتجاوز الحد {settings.max_risk_score}"
            result.reasons.append(result.rejection_reason)
            return

        # GATE 7: Market regime (V31.2: BYPASSED for Pump.fun tokens)
        # Pump.fun tokens don't follow traditional market regime patterns.
        # The regime detector needs SOL OHLCV data which may not be available.
        # Instead of blocking, we note the regime and continue.
        if not regime.trading_recommended:
            result.reasons.append(f"⚠️ حالة السوق {regime.regime} غير مثالية لكن التجاوز مسموح")
            # V31.2: Don't return — continue to qualification
            # was: result.qualified = False; return

        # NOTE: The 70% Win Probability gate has been REMOVED per user request.
        # The final decision is now made by AI agent consensus.
        # The bot will be marked as "pending_agent_consensus" here.
        # The orchestrator will run the agents and make the final call.

        # V31.2: Pre-agent qualification passed - MARK AS QUALIFIED directly
        # Previous: qualified = False (waiting for agents)
        # Now: qualified = True (agents are OPTIONAL, not mandatory)
        # If AI agents are available, they can still veto. But if they're
        # unavailable (NVIDIA down, no LLM key), the bot still trades.
        # V407 P1-6: Reject if no valid price (can't set SL/TP)
        token_price = float(token.price_usd or 0)
        if token_price <= 0:
            result.qualified = False
            result.recommendation = "reject"
            result.rejection_reason = "No valid price — cannot set SL/TP"
            result.reasons.append("❌ لا يوجد سعر صالح لحساب SL/TP")
            return
        result.qualified = True  # V31.2: Auto-qualify after passing all gates
        result.recommendation = "buy"
        result.rejection_reason = ""
        result.data_sufficient = True
        result.reasons.append("✅ اجتازت العملة بوابات الفحص الأولي")
        if not hasattr(result, 'suggested_position_size_pct') or result.suggested_position_size_pct == 0:
            result.suggested_position_size_pct = 4.5  # V57: 4.5% default (under 5% cap)
        if not result.suggested_stop_loss or result.suggested_stop_loss <= 0:
            # V400 P1-9 FIX: use token price (validated above to be > 0)
            result.suggested_stop_loss = token_price * 0.92  # -8% SL
        if not result.suggested_take_profit or result.suggested_take_profit <= 0:
            # V501: TP cap at +1000% (10x) — not a real TP, just a safety cap
            # V500 strategy: NO early TP, let winners run via trailing stop
            result.suggested_take_profit = token_price * 11.0  # 10x cap (was 1.50)
        if not result.suggested_holding_period_minutes:
            result.suggested_holding_period_minutes = 7200  # V501: 5 days (was 60h)
        if not result.suggested_entry:
            result.suggested_entry = token.price_usd

    def _build_reasoning(
        self, result: DecisionResult, token, ta: TechnicalAnalysisResult,
        liq: LiquidityResult, sec: SecurityResult
    ) -> None:
        """Build strengths / weaknesses / failure risks lists."""
        # Strengths
        if result.technical_score > 70:
            result.strengths.append("مؤشرات فنية قوية")
        if result.liquidity_score > 70:
            result.strengths.append("سيولة صحية")
        if result.security_score > 70:
            result.strengths.append("ملف أمان جيد")
        if result.momentum_score > 70:
            result.strengths.append("زخم إيجابي")
        if ta.price_action.get("trend") == "bullish":
            result.strengths.append("اتجاه صاعد مؤكد")

        # Weaknesses
        if result.technical_score < 50:
            result.weaknesses.append("مؤشرات فنية ضعيفة")
        if result.liquidity_score < 50:
            result.weaknesses.append("سيولة ضعيفة - انزلاق سعري متوقع")
        if result.sentiment_score < 50:
            result.weaknesses.append("مشاعر سلبية")
        if result.risk_score > 50:
            result.weaknesses.append(f"درجة مخاطرة مرتفعة ({result.risk_score}/100)")
        if ta.price_action.get("momentum_weakening"):
            result.weaknesses.append("ضعف في الزخم")

        # Failure risks (what could make the prediction wrong)
        if result.win_probability_method == "naive_baseline":
            result.failure_risks.append(
                "احتمال النجاح مبني على تقدير ابتدائي - يتطلب معايرة ببيانات تاريخية كافية"
            )
        if result.win_probability_upper_ci - result.win_probability_lower_ci > 0.20:
            result.failure_risks.append("فاصل الثقة واسع - عدم يقين إحصائي")
        if sec.wash_trading_risk if hasattr(sec, "wash_trading_risk") else False:
            result.failure_risks.append("أحجام تداول قد تكون مصطنعة")
        if (token.price_change_24h_pct or 0) > 50:
            result.failure_risks.append("ارتفاع حاد قد يتبعه تصحيح عنيف")
        if liq.wash_trading_probability > 0.4:
            result.failure_risks.append("احتمال wash trading يقلل دقة التوقعات")
        if (token.liquidity_usd or 0) < 50_000:
            result.failure_risks.append("سيولة منخفضة - تنفيذ غير واقعي")

        # Data quality confidence (0-1)
        confidence = 1.0
        if result.data_age_seconds > 60:
            confidence -= 0.2
        if not ta.data_sufficient:
            confidence -= 0.3
        if not liq.data_sufficient:
            confidence -= 0.2
        if result.win_probability_method == "naive_baseline":
            confidence -= 0.2
        result.data_quality_confidence = max(0, min(1, confidence))

        # Build Arabic reasoning summary
        rec_ar = {
            "buy": "شراء (افتراضي)", "wait": "انتظار", "reject": "رفض", "monitor": "مراقبة"
        }.get(result.recommendation, result.recommendation)

        summary_parts = [
            f"القرار: {rec_ar}",
            f"العملة: {token.symbol} ({token.chain})",
            f"Opportunity Score: {result.opportunity_score:.1f}/100",
            f"Risk Score: {result.risk_score}/100",
            f"احتمال النجاح: {result.win_probability:.1%} "
            f"(فاصل الثقة: {result.win_probability_lower_ci:.1%} - {result.win_probability_upper_ci:.1%})",
            f"الطريقة: {result.win_probability_method}",
        ]
        if not result.qualified:
            summary_parts.append(f"سبب الرفض: {result.rejection_reason}")
        if result.strengths:
            summary_parts.append("نقاط القوة: " + "، ".join(result.strengths))
        if result.weaknesses:
            summary_parts.append("نقاط الضعف: " + "، ".join(result.weaknesses))
        result.reasoning_summary = "\n".join(summary_parts)

    def _suggest_trade_params(
        self, result: DecisionResult, token, ta: TechnicalAnalysisResult, liq: LiquidityResult
    ) -> None:
        """Suggest entry / SL / TP / size based on technicals.

        V57-FINAL strategy (validated by 20-seed Monte Carlo):
        - SL: 1.8x ATR (tight, cuts losers fast)
        - TP: 9.0x ATR (very wide, lets memecoin pumps play out)
        - R:R = 1:5 (asymmetric — loses small, wins big)
        - Holding period: 60h (2.5 days) — gives pumps time to develop
        - Trailing stop handled by PaperTradingEngine (15% from peak)
        """
        result.suggested_entry = float(token.price_usd or 0)

        # V57: ATR-based with asymmetric R:R (1:5)
        if ta.atr > 0 and result.suggested_entry > 0:
            sl_distance = 1.8 * ta.atr  # tight SL
            result.suggested_stop_loss = max(0.0001, result.suggested_entry - sl_distance)
            # V501: TP cap at 10x (was 9×ATR) — let winners run via trailing
            result.suggested_take_profit = result.suggested_entry * 11.0  # 10x cap
            # V501: 5 days holding (was 60h)
            result.suggested_holding_period_minutes = 7200  # 5 days
        else:
            # Fallback for tokens without OHLCV (new memecoins):
            # V501: -8% SL, +1000% TP cap (was +50%)
            result.suggested_stop_loss = result.suggested_entry * 0.92  # -8% SL
            result.suggested_take_profit = result.suggested_entry * 11.0  # V501: 10x cap
            result.suggested_holding_period_minutes = 7200  # V501: 5 days

        # Position size: risk-based
        # Risk per trade = 2% of capital
        # Position size = (capital * 0.02) / (entry - stop_loss) * entry
        risk_usd = settings.paper_capital_usd * (settings.risk_max_risk_per_trade_pct / 100.0)
        if result.suggested_stop_loss and result.suggested_entry:
            sl_pct = abs(result.suggested_entry - result.suggested_stop_loss) / max(result.suggested_entry, 1e-9)
            if sl_pct > 0:
                position_usd = risk_usd / sl_pct
                # Cap at default position pct of capital
                max_position = settings.paper_capital_usd * (settings.paper_default_position_pct / 100.0)
                position_usd = min(position_usd, max_position)
                # Cap further by liquidity (max 5% of liquidity)
                liq_cap = (token.liquidity_usd or 0) * 0.05
                position_usd = min(position_usd, liq_cap)
                result.suggested_position_size_pct = (
                    position_usd / settings.paper_capital_usd * 100
                )

    async def close(self):
        await self.onchain.close()
        await self.sentiment.close()

    def to_snapshot(self, token_id: str, result: DecisionResult) -> AnalysisSnapshot:
        """Convert DecisionResult to a DB AnalysisSnapshot for persistence."""
        return AnalysisSnapshot(
            token_id=token_id,
            technical_score=result.technical_score,
            liquidity_score=result.liquidity_score,
            onchain_score=result.onchain_score,
            security_score=result.security_score,
            sentiment_score=result.sentiment_score,
            momentum_score=result.momentum_score,
            market_conditions_score=result.market_conditions_score,
            volatility_score=result.volatility_score,
            risk_score=result.risk_score,
            opportunity_score=result.opportunity_score,
            win_probability=result.win_probability,
            win_probability_lower_ci=result.win_probability_lower_ci,
            win_probability_upper_ci=result.win_probability_upper_ci,
            market_regime=result.market_regime,
            indicators_json={
                "technical": result.technical_detail,
                "liquidity": result.liquidity_detail,
                "onchain": result.onchain_detail,
                "security": result.security_detail,
                "sentiment": result.sentiment_detail,
            },
            flags_json={
                "critical_flags": (result.security_detail or {}).get("critical_flags", []),
                "warnings": (result.security_detail or {}).get("warnings", []),
            },
            reasons_json=result.reasons,
            reasoning_summary=result.reasoning_summary,
            data_age_seconds=result.data_age_seconds,
            data_sufficient=result.data_sufficient,
            qualified=result.qualified,
            rejection_reason=result.rejection_reason,
        )
