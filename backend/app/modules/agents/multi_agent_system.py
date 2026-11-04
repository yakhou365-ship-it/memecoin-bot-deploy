"""
18-Agent Multi-Agent System for Memecoin Trading.

Architecture:
- 18 specialized agents, each analyzing from one angle
- Each agent returns an AgentResult with verdict, score (0-100), confidence, factors, concerns
- Rule-based logic for speed (default), LLM (OpenRouter) for complex reasoning when available
- Security & Risk agents have VETO power
- Weighted voting system combines all verdicts
- ChiefIntelligenceAgent makes the final decision

Agents (in order):
1.  MarketIntelligenceAgent - market regime / macro context
2.  TokenDiscoveryAgent - new tokens discovery & early signals
3.  TechnicalAnalysisAgent - 15+ technical indicators
4.  LiquidityIntelligenceAgent - slippage, depth, wash trading
5.  OnChainIntelligenceAgent - holders, whales, mint/freeze
6.  SecurityAgent - honeypot, rug pull (VETO)
7.  SocialIntelligenceAgent - sentiment (heuristic fallback)
8.  PredictionAgent - win probability calculation
9.  RiskManagementAgent - risk evaluation (VETO)
10. StrategyResearchAgent - strategy testing
11. HistoricalLearningAgent - learns from past trades
12. ErrorPreventionAgent - prevents repeated mistakes
13. MarketAnomalyAgent - detects manipulation
14. DecisionVerificationAgent - challenges conclusions
15. RedTeamAgent - actively tries to disprove trades
16. WalletIntelligenceAgent - wallet history analysis
17. MetaLearningAgent - improves learning process
18. ChiefIntelligenceAgent - combines all evidence, final decision
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger


# =========================================================================
# Core types
# =========================================================================
class AgentVerdict(str, Enum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    WAIT = "wait"
    REJECT = "reject"
    STRONG_REJECT = "strong_reject"
    VETO = "veto"


@dataclass
class AgentResult:
    agent_name: str
    role: str
    verdict: str = "wait"
    confidence: float = 0.0  # 0.0 - 1.0
    score: int = 50  # 0-100
    reasoning: str = ""
    key_factors: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    has_veto: bool = False
    weight: float = 1.0
    used_llm: bool = False
    elapsed_ms: int = 0
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @property
    def is_buy(self) -> bool:
        return self.verdict in ("strong_buy", "buy")

    @property
    def is_reject(self) -> bool:
        return self.verdict in ("reject", "strong_reject", "veto")

    @property
    def is_veto(self) -> bool:
        return self.verdict == "veto"


# =========================================================================
# Base agent
# =========================================================================
class BaseAgent:
    name: str = "base"
    role: str = "Base Agent"
    specialty: str = ""
    has_veto: bool = False
    weight: float = 1.0
    use_llm: bool = False  # whether to call LLM at all

    def __init__(self):
        self._llm_timeout = settings.ai_agent_timeout_seconds

    # ---- Public API ------------------------------------------------------
    async def analyze(self, token, analysis_context: Dict[str, Any]) -> AgentResult:
        """Run the agent. Subclasses override _rule_based_analyze and optionally _llm_analyze."""
        t0 = time.time()
        try:
            # Always run rule-based first (fast)
            rule_result = await self._rule_based_analyze(token, analysis_context)

            # Optionally enrich with LLM (slower)
            if self.use_llm and self._llm_available():
                try:
                    rule_result = await self._llm_analyze(token, analysis_context, rule_result)
                    rule_result.used_llm = True
                except Exception as exc:
                    logger.warning(f"[{self.name}] LLM enrichment failed: {exc}")
                    rule_result.used_llm = False

            rule_result.agent_name = self.name
            rule_result.role = self.role
            rule_result.has_veto = self.has_veto
            rule_result.weight = self.weight
            rule_result.elapsed_ms = int((time.time() - t0) * 1000)
            rule_result.timestamp = datetime.utcnow().isoformat()
            return rule_result
        except Exception as exc:
            logger.exception(f"[{self.name}] crashed: {exc}")
            return AgentResult(
                agent_name=self.name,
                role=self.role,
                verdict="wait",
                confidence=0.0,
                score=50,
                reasoning=f"Agent error: {str(exc)[:200]}",
                has_veto=self.has_veto,
                weight=self.weight,
                elapsed_ms=int((time.time() - t0) * 1000),
                timestamp=datetime.utcnow().isoformat(),
            )

    # ---- Subclass hooks -------------------------------------------------
    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        raise NotImplementedError

    async def _llm_analyze(
        self, token, ctx: Dict[str, Any], rule_result: AgentResult
    ) -> AgentResult:
        """Default: ask LLM to refine the rule-based verdict. Subclasses can override."""
        prompt = self._build_llm_prompt(token, ctx, rule_result)
        response = await self._call_openrouter(prompt)
        return self._merge_llm_response(rule_result, response)

    def _build_llm_prompt(self, token, ctx: Dict[str, Any], rule_result: AgentResult) -> str:
        return (
            f"You are {self.role} (specialty: {self.specialty}).\n\n"
            f"Token: {getattr(token, 'symbol', '?')} on {getattr(token, 'chain', '?')}\n"
            f"Context summary: {json.dumps(_safe_ctx(ctx), default=str)[:1500]}\n\n"
            f"Rule-based verdict: {rule_result.verdict}, score={rule_result.score}, "
            f"confidence={rule_result.confidence}\n"
            f"Reasoning: {rule_result.reasoning}\n\n"
            f"Reconsider and return JSON: "
            f'{{"verdict": "strong_buy|buy|wait|reject|strong_reject", '
            f'"confidence": 0.0-1.0, "score": 0-100, "reasoning": "...", '
            f'"key_factors": [...], "concerns": [...]}}'
        )

    def _merge_llm_response(self, rule_result: AgentResult, llm_text: str) -> AgentResult:
        try:
            data = _parse_json_loose(llm_text)
            verdict = str(data.get("verdict", rule_result.verdict)).lower()
            if verdict not in ("strong_buy", "buy", "wait", "reject", "strong_reject"):
                verdict = rule_result.verdict
            rule_result.verdict = verdict
            rule_result.confidence = float(data.get("confidence", rule_result.confidence))
            rule_result.score = int(data.get("score", rule_result.score))
            if data.get("reasoning"):
                rule_result.reasoning = str(data["reasoning"])[:1500]
            if isinstance(data.get("key_factors"), list):
                rule_result.key_factors = [str(x) for x in data["key_factors"]][:20]
            if isinstance(data.get("concerns"), list):
                rule_result.concerns = [str(x) for x in data["concerns"]][:20]
        except Exception as exc:
            logger.debug(f"[{self.name}] LLM response parse failed: {exc}")
        return rule_result

    # ---- LLM helper -----------------------------------------------------
    def _llm_available(self) -> bool:
        return bool(getattr(settings, "openrouter_api_key", "")) or \
               bool(getattr(settings, "gemini_api_key", "")) or \
               bool(getattr(settings, "groq_api_key", ""))

    async def _call_openrouter(self, prompt: str) -> str:
        """Call OpenRouter (or any configured LLM provider) via httpx."""
        api_key = getattr(settings, "openrouter_api_key", "")
        if not api_key:
            return ""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": f"You are a quantitative crypto trading agent ({self.role})."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
        }
        async with httpx.AsyncClient(timeout=self._llm_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()


# =========================================================================
# Helpers
# =========================================================================
def _safe_ctx(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Trim ctx for LLM prompts (avoid huge payloads)."""
    if not isinstance(ctx, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in ctx.items():
        if isinstance(v, (int, float, str, bool)):
            out[k] = v
        elif isinstance(v, list):
            out[k] = v[:10]
        elif isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in list(v.items())[:20]}
    return out


def _parse_json_loose(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        t = "\n".join(lines)
    try:
        return json.loads(t)
    except Exception:
        start = t.find("{")
        end = t.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end])
            except Exception:
                pass
    return {}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _ctx_indicators(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.get("indicators", {}) or {}


# =========================================================================
# 1. MarketIntelligenceAgent
# =========================================================================
class MarketIntelligenceAgent(BaseAgent):
    name = "market_intelligence"
    role = "Market Intelligence Agent"
    specialty = "Detects market regime and broad market direction"
    weight = 1.1

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        regime = ctx.get("market_regime", {}) or {}
        primary = str(regime.get("regime", "unknown")).lower()
        trading_rec = bool(regime.get("trading_recommended", False))
        vol = _num(regime.get("volatility_pct"))
        slope = _num(regime.get("trend_slope"))

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        if primary == "strong_momentum":
            score = 80
            factors.append("Strong market momentum")
        elif primary == "bull_market":
            score = 70
            factors.append("Bull market regime")
        elif primary == "sideways_market":
            score = 55
            factors.append("Sideways market")
        elif primary == "high_volatility":
            score = 35
            concerns.append(f"High market volatility ({vol:.1f}%)")
        elif primary == "bear_market":
            score = 25
            concerns.append("Bear market regime")
        elif primary == "panic_selling":
            score = 10
            concerns.append("Panic selling detected")
        else:
            score = 50
            concerns.append("Unknown market regime")

        if slope > 5:
            score = min(100, score + 5)
        elif slope < -5:
            score = max(0, score - 5)

        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )
        if not trading_rec and verdict in ("strong_buy", "buy"):
            verdict = "wait"
            concerns.append("Trading not recommended for current regime")

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.65,
            score=score,
            reasoning=f"Market regime: {primary}, slope={slope:.2f}%, vol={vol:.2f}%",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 2. TokenDiscoveryAgent
# =========================================================================
class TokenDiscoveryAgent(BaseAgent):
    name = "token_discovery"
    role = "Token Discovery Agent"
    specialty = "Discovers new tokens and early signals"
    weight = 0.9

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        first_seen = getattr(token, "first_seen_at", None) or datetime.utcnow()
        age_minutes = (datetime.utcnow() - first_seen).total_seconds() / 60.0
        vol_5m = _num(getattr(token, "volume_5m_usd", 0))
        vol_24h = _num(getattr(token, "volume_24h_usd", 0))
        chg_5m = _num(getattr(token, "price_change_5m_pct", 0))
        chg_24h = _num(getattr(token, "price_change_24h_pct", 0))
        liq = _num(getattr(token, "liquidity_usd", 0))
        watch = int(getattr(token, "watch_priority", 50) or 50)

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Age scoring
        if age_minutes < 60:
            score += 8
            factors.append(f"Newly discovered ({age_minutes:.0f}m old)")
        elif age_minutes < 240:
            score += 4
            factors.append("Recently listed")
        elif age_minutes > 4320:  # >3 days
            score -= 5
            concerns.append("Old token, lower discovery edge")

        # Volume surge
        if vol_5m > 0 and vol_24h > 0:
            ratio = vol_5m / max(vol_24h, 1.0) * 12 * 24  # normalize
            if ratio > 1.0:
                score += 8
                factors.append("Volume surge in last 5m")
            elif ratio < 0.05:
                score -= 8
                concerns.append("Volume drop-off")

        # Price action early signal
        if 0 < chg_5m < 15:
            score += 6
            factors.append(f"Healthy early pump +{chg_5m:.1f}%")
        elif chg_5m > 50:
            score -= 15
            concerns.append(f"Dangerous pump +{chg_5m:.1f}%")
        elif chg_5m < -20:
            score -= 10
            concerns.append(f"Sharp drop {chg_5m:.1f}%")

        # Liquidity floor
        if liq < 5_000:
            score -= 15
            concerns.append("Sub-5k liquidity")
        elif liq > 100_000:
            score += 5

        # Watch priority
        if watch >= 80:
            score += 5
            factors.append("High watch priority")
        elif watch < 30:
            score -= 5

        score = max(0, min(100, score))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.6,
            score=score,
            reasoning=f"Discovery: age={age_minutes:.0f}m, vol5m={vol_5m:.0f}, chg5m={chg_5m:.1f}%",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 3. TechnicalAnalysisAgent
# =========================================================================
class TechnicalAnalysisAgent(BaseAgent):
    name = "technical_analysis"
    role = "Technical Analysis Agent"
    specialty = "15+ technical indicators (RSI, MACD, EMA, Bollinger, ATR, ADX, OBV, VWAP, etc.)"
    weight = 1.3

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        tech = indicators.get("technical", {}) or {}

        rsi = _num(tech.get("rsi", 50))
        macd_hist = _num(tech.get("macd_histogram", 0))
        ema9 = _num(tech.get("ema_9", 0))
        ema50 = _num(tech.get("ema_50", 0))
        ema200 = _num(tech.get("ema_200", 0))
        adx = _num(tech.get("adx", 0))
        bb_position = _num(tech.get("bb_position", 0.5))  # 0 = lower band, 1 = upper
        atr_pct = _num(tech.get("atr_pct", 0))
        obv_slope = _num(tech.get("obv_slope", 0))
        vwap = _num(tech.get("vwap", 0))
        price = _num(getattr(token, "price_usd", 0))
        stoch_k = _num(tech.get("stoch_k", 50))
        stoch_d = _num(tech.get("stoch_d", 50))
        support_dist = _num(tech.get("distance_to_support_pct", 0))
        resist_dist = _num(tech.get("distance_to_resistance_pct", 0))

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Trend (EMA alignment)
        if ema9 > ema50 > ema200 and ema200 > 0:
            score += 15
            factors.append("EMA stack bullish (9>50>200)")
        elif ema9 < ema50 < ema200:
            score -= 15
            concerns.append("EMA stack bearish")
        elif ema9 > ema50:
            score += 5
        else:
            concerns.append("EMA stack mixed")

        # RSI
        if rsi < 30:
            score += 8
            factors.append(f"RSI oversold ({rsi:.0f})")
        elif 30 <= rsi < 45:
            score += 4
        elif 55 <= rsi < 70:
            score += 6
            factors.append(f"RSI bullish ({rsi:.0f})")
        elif rsi >= 80:
            score -= 10
            concerns.append(f"RSI overbought ({rsi:.0f})")

        # MACD
        if macd_hist > 0:
            score += 6
            factors.append("MACD histogram positive")
        else:
            score -= 4
            concerns.append("MACD histogram negative")

        # ADX (trend strength)
        if adx > 25:
            score += 5
            factors.append(f"Strong trend (ADX={adx:.0f})")
        elif adx < 15:
            score -= 4
            concerns.append("Weak/no trend")

        # Bollinger
        if bb_position < 0.2:
            score += 5
            factors.append("Near lower Bollinger Band")
        elif bb_position > 0.85:
            score -= 6
            concerns.append("At upper Bollinger Band")

        # Stochastic RSI
        if stoch_k < 20 and stoch_k > stoch_d:
            score += 6
            factors.append("Stoch RSI bullish cross from oversold")
        elif stoch_k > 80 and stoch_k < stoch_d:
            score -= 6
            concerns.append("Stoch RSI bearish cross from overbought")

        # OBV
        if obv_slope > 0:
            score += 4
            factors.append("OBV rising (accumulation)")
        else:
            score -= 3
            concerns.append("OBV falling (distribution)")

        # VWAP
        if vwap > 0 and price > 0:
            if price > vwap:
                score += 4
                factors.append("Price above VWAP")
            else:
                score -= 4
                concerns.append("Price below VWAP")

        # ATR (volatility)
        if atr_pct > 15:
            score -= 6
            concerns.append(f"Extreme volatility ATR={atr_pct:.1f}%")
        elif atr_pct < 1:
            score -= 3
            concerns.append("Very low volatility")

        # Support/Resistance
        if support_dist > 0 and support_dist < 3:
            score += 5
            factors.append("Near support")
        if resist_dist > 0 and resist_dist < 2:
            score -= 5
            concerns.append("Near resistance")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 78 else
            "buy" if score >= 62 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.7,
            score=score,
            reasoning=f"RSI={rsi:.0f} MACDh={macd_hist:.4f} ADX={adx:.0f} BB={bb_position:.2f}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 4. LiquidityIntelligenceAgent
# =========================================================================
class LiquidityIntelligenceAgent(BaseAgent):
    name = "liquidity_intelligence"
    role = "Liquidity Intelligence Agent"
    specialty = "Slippage, depth, wash trading detection"
    weight = 1.2

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        liq = indicators.get("liquidity", {}) or {}
        liq_usd = _num(liq.get("liquidity_usd", getattr(token, "liquidity_usd", 0)))
        vol_24h = _num(liq.get("volume_24h_usd", getattr(token, "volume_24h_usd", 0)))
        slippage = _num(liq.get("estimated_slippage_pct", 0))
        wash_prob = _num(liq.get("wash_trading_probability", 0))
        fake_liq = _num(liq.get("fake_liquidity_probability", 0))
        vol_to_liq = _num(liq.get("volume_to_liquidity_ratio", 0))
        liq_trend = str(liq.get("liquidity_trend", "stable"))
        liq_removed = bool(liq.get("liquidity_removed_recently", False))
        buy_sell = _num(liq.get("buy_to_sell_ratio", 1.0))

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Liquidity level
        if liq_usd >= 200_000:
            score += 15
            factors.append(f"Strong liquidity ${liq_usd:,.0f}")
        elif liq_usd >= 50_000:
            score += 8
            factors.append(f"Adequate liquidity ${liq_usd:,.0f}")
        elif liq_usd >= 10_000:
            score -= 5
            concerns.append(f"Thin liquidity ${liq_usd:,.0f}")
        else:
            score -= 20
            concerns.append(f"Dangerously low liquidity ${liq_usd:,.0f}")

        # Slippage
        if slippage > 5:
            score -= 15
            concerns.append(f"High slippage {slippage:.1f}%")
        elif slippage > 2:
            score -= 7
            concerns.append(f"Elevated slippage {slippage:.1f}%")
        elif slippage < 1:
            score += 6
            factors.append(f"Low slippage {slippage:.2f}%")

        # Wash trading
        if wash_prob > 0.7:
            score -= 18
            concerns.append(f"High wash trading probability ({wash_prob:.0%})")
        elif wash_prob > 0.4:
            score -= 8
            concerns.append(f"Possible wash trading ({wash_prob:.0%})")
        elif wash_prob < 0.15:
            score += 5
            factors.append("Low wash trading probability")

        # Fake liquidity
        if fake_liq > 0.5:
            score -= 12
            concerns.append("Possible fake liquidity")
        elif fake_liq > 0.3:
            score -= 5

        # Volume / liquidity ratio
        if vol_to_liq > 5:
            score += 5
            factors.append("Healthy volume/liquidity ratio")
        elif vol_to_liq < 0.5:
            score -= 6
            concerns.append("Weak volume vs liquidity")

        # Trend
        if liq_trend == "increasing":
            score += 4
            factors.append("Liquidity increasing")
        elif liq_trend in ("decreasing", "critical"):
            score -= 10
            concerns.append(f"Liquidity {liq_trend}")
        if liq_removed:
            score -= 20
            concerns.append("Liquidity was recently removed")

        # Buy/sell balance
        if buy_sell > 1.5:
            score += 5
            factors.append(f"Buy pressure ({buy_sell:.2f}x)")
        elif buy_sell < 0.6:
            score -= 6
            concerns.append(f"Sell pressure ({buy_sell:.2f}x)")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.7,
            score=score,
            reasoning=f"Liq ${liq_usd:,.0f}, slip={slippage:.2f}%, wash={wash_prob:.0%}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 5. OnChainIntelligenceAgent
# =========================================================================
class OnChainIntelligenceAgent(BaseAgent):
    name = "onchain_intelligence"
    role = "On-Chain Intelligence Agent"
    specialty = "Holders, whales, mint/freeze authority, bundling"
    weight = 1.2

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        on = indicators.get("onchain", {}) or {}

        holders = _num(on.get("total_holders", 0))
        top10 = _num(on.get("top_10_pct", 0))
        top20 = _num(on.get("top_20_pct", 0))
        whale = bool(on.get("whale_activity_detected", False))
        insider = _num(on.get("insider_activity_probability", 0))
        bundling = _num(on.get("token_bundling_probability", 0))
        mint_auth = bool(on.get("mint_authority_enabled", False))
        freeze_auth = bool(on.get("freeze_authority_enabled", False))
        liq_locked = on.get("liquidity_locked")  # None = unknown, True/False
        suspicious = _num(on.get("suspicious_wallet_count", 0))

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Mint/freeze (critical)
        if mint_auth:
            score -= 25
            concerns.append("MINT AUTHORITY ENABLED")
        else:
            score += 5
            factors.append("Mint authority renounced")
        if freeze_auth:
            score -= 25
            concerns.append("FREEZE AUTHORITY ENABLED")
        else:
            score += 5
            factors.append("Freeze authority renounced")

        # Holders distribution
        if holders >= 1000:
            score += 8
            factors.append(f"Wide distribution ({holders:.0f} holders)")
        elif holders >= 200:
            score += 4
        elif holders < 50:
            score -= 10
            concerns.append(f"Very few holders ({holders:.0f})")

        # Concentration
        if top10 > 80:
            score -= 15
            concerns.append(f"Top 10 holds {top10:.0f}%")
        elif top10 > 60:
            score -= 7
        elif top10 < 30:
            score += 6
            factors.append(f"Healthy distribution (top10={top10:.0f}%)")

        # Whale
        if whale:
            score -= 5
            concerns.append("Whale activity detected")

        # Insider
        if insider > 0.6:
            score -= 12
            concerns.append(f"Likely insider activity ({insider:.0%})")
        elif insider > 0.3:
            score -= 5

        # Bundling
        if bundling > 0.6:
            score -= 12
            concerns.append(f"Likely token bundling ({bundling:.0%})")
        elif bundling > 0.3:
            score -= 5

        # Liquidity lock
        if liq_locked is False:
            score -= 10
            concerns.append("Liquidity NOT locked")
        elif liq_locked is True:
            score += 8
            factors.append("Liquidity locked")

        # Suspicious wallets
        if suspicious >= 5:
            score -= 8
            concerns.append(f"{suspicious:.0f} suspicious wallets")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.7,
            score=score,
            reasoning=f"Holders={holders:.0f}, top10={top10:.0f}%, mint={mint_auth}, freeze={freeze_auth}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 6. SecurityAgent (VETO)
# =========================================================================
class SecurityAgent(BaseAgent):
    name = "security"
    role = "Security Agent"
    specialty = "Honeypot, rug pull, scam detection (VETO POWER)"
    has_veto = True
    weight = 2.0

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        sec = indicators.get("security", {}) or {}
        on = indicators.get("onchain", {}) or {}

        risk_score = _num(sec.get("risk_score", 50))
        honeypot = _num(sec.get("honeypot_probability", 0))
        rug = _num(sec.get("rug_pull_probability", 0))
        owner_conc = _num(sec.get("owner_concentration_risk", 0))
        suspicious = _num(sec.get("suspicious_wallets", 0))
        critical_flags = sec.get("critical_flags", []) or []
        mint_auth = bool(on.get("mint_authority_enabled", False))
        freeze_auth = bool(on.get("freeze_authority_enabled", False))
        liq_locked = on.get("liquidity_locked")

        # Veto conditions
        veto_reasons: List[str] = []
        if mint_auth:
            veto_reasons.append("MINT_AUTHORITY_ENABLED")
        if freeze_auth:
            veto_reasons.append("FREEZE_AUTHORITY_ENABLED")
        if honeypot > 0.7:
            veto_reasons.append(f"HONEYPOT_HIGH ({honeypot:.0%})")
        if rug > 0.7:
            veto_reasons.append(f"RUG_HIGH ({rug:.0%})")
        if owner_conc > 0.9:
            veto_reasons.append(f"OWNER_CONCENTRATION_EXTREME ({owner_conc:.2f})")
        if "HONEYPOT_HIGH_RISK" in critical_flags or "RUG_PULL_HIGH_RISK" in critical_flags:
            if not veto_reasons:
                veto_reasons.append("CRITICAL_SECURITY_FLAG")

        if veto_reasons:
            return AgentResult(
                agent_name=self.name,
                role=self.role,
                verdict="veto",
                confidence=0.95,
                score=0,
                reasoning=f"VETO triggered: {', '.join(veto_reasons)}",
                key_factors=[],
                concerns=veto_reasons,
                has_veto=self.has_veto,
                weight=self.weight,
            )

        # Score the security
        score = 100 - int(risk_score)
        factors: List[str] = []
        concerns: List[str] = []

        if honeypot < 0.2:
            factors.append(f"Low honeypot probability ({honeypot:.0%})")
        elif honeypot > 0.4:
            concerns.append(f"Elevated honeypot probability ({honeypot:.0%})")

        if rug < 0.2:
            factors.append(f"Low rug pull probability ({rug:.0%})")
        elif rug > 0.4:
            concerns.append(f"Elevated rug pull probability ({rug:.0%})")

        if liq_locked is True:
            factors.append("Liquidity locked")
        elif liq_locked is False:
            concerns.append("Liquidity NOT locked")

        if suspicious >= 5:
            concerns.append(f"{suspicious:.0f} suspicious wallets")

        verdict = (
            "strong_buy" if score >= 80 else
            "buy" if score >= 65 else
            "wait" if score >= 45 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.8,
            score=score,
            reasoning=f"Risk score={risk_score:.0f}, honeypot={honeypot:.0%}, rug={rug:.0%}",
            key_factors=factors,
            concerns=concerns,
            has_veto=self.has_veto,
            weight=self.weight,
        )


# =========================================================================
# 7. SocialIntelligenceAgent
# =========================================================================
class SocialIntelligenceAgent(BaseAgent):
    name = "social_intelligence"
    role = "Social Intelligence Agent"
    specialty = "Sentiment analysis (heuristic fallback)"
    weight = 0.8

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        sent = indicators.get("sentiment", {}) or {}
        score_val = _num(sent.get("sentiment_score", 50))
        mentions = _num(sent.get("mentions_24h", 0))
        positive_pct = _num(sent.get("positive_pct", 0.5))
        negative_pct = _num(sent.get("negative_pct", 0.5))
        volume_change = _num(sent.get("mention_volume_change_pct", 0))

        # Heuristic fallback if no real sentiment data
        if not sent:
            chg_24h = _num(getattr(token, "price_change_24h_pct", 0))
            vol_24h = _num(getattr(token, "volume_24h_usd", 0))
            if chg_24h > 30:
                score_val = 65
                positive_pct = 0.7
            elif chg_24h > 5:
                score_val = 58
                positive_pct = 0.6
            elif chg_24h < -30:
                score_val = 30
                negative_pct = 0.7
            elif chg_24h < -5:
                score_val = 42
                negative_pct = 0.6
            else:
                score_val = 50

            if vol_24h > 500_000:
                mentions = max(mentions, 200)
            elif vol_24h > 50_000:
                mentions = max(mentions, 50)

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        if score_val >= 70:
            score = 70
            factors.append(f"Positive sentiment ({score_val:.0f})")
        elif score_val >= 55:
            score = 60
            factors.append("Mildly positive sentiment")
        elif score_val <= 30:
            score = 25
            concerns.append(f"Negative sentiment ({score_val:.0f})")
        elif score_val <= 45:
            score = 40
            concerns.append("Mildly negative sentiment")
        else:
            score = 50

        if mentions > 500:
            score += 5
            factors.append(f"High social activity ({mentions:.0f} mentions)")
        elif mentions > 100:
            score += 3
        elif mentions < 10:
            score -= 4
            concerns.append("Low social activity")

        if positive_pct > 0.7:
            score += 5
        if negative_pct > 0.6:
            score -= 8
            concerns.append(f"High negative sentiment ({negative_pct:.0%})")

        if volume_change > 50:
            score += 4
            factors.append(f"Mentions surging (+{volume_change:.0f}%)")
        elif volume_change < -50:
            score -= 4

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.55,
            score=score,
            reasoning=f"Sentiment={score_val:.0f}, mentions={mentions:.0f}, +{positive_pct:.0%}/-{negative_pct:.0%}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 8. PredictionAgent
# =========================================================================
class PredictionAgent(BaseAgent):
    name = "prediction"
    role = "Prediction Agent"
    specialty = "Win probability calculation"
    weight = 1.4

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        win_prob = _num(indicators.get("win_probability"), 0.5)
        win_lower_ci = _num(indicators.get("win_probability_lower_ci"), win_prob)
        win_upper_ci = _num(indicators.get("win_probability_upper_ci"), win_prob)
        opportunity = _num(indicators.get("opportunity_score", 50))

        # If no win_probability was computed by upstream, derive from component scores
        if win_prob <= 0:
            tech = _num(indicators.get("technical_score", 50))
            liq = _num(indicators.get("liquidity_score", 50))
            on = _num(indicators.get("onchain_score", 50))
            sec = _num(indicators.get("security_score", 50))
            sent = _num(indicators.get("sentiment_score", 50))
            win_prob = (0.30 * tech + 0.20 * liq + 0.20 * on + 0.20 * sec + 0.10 * sent) / 100.0
            win_lower_ci = max(0.0, win_prob - 0.1)
            win_upper_ci = min(1.0, win_prob + 0.1)

        score = int(win_prob * 100)
        factors: List[str] = []
        concerns: List[str] = []

        # Confidence interval width
        ci_width = win_upper_ci - win_lower_ci
        if ci_width > 0.25:
            concerns.append(f"Wide CI ({ci_width:.2f}) - low confidence")
        else:
            factors.append(f"Narrow CI ({ci_width:.2f}) - high confidence")

        if win_prob >= 0.80:
            factors.append(f"High win probability ({win_prob:.0%})")
        elif win_prob >= 0.65:
            factors.append(f"Decent win probability ({win_prob:.0%})")
        elif win_prob < 0.40:
            concerns.append(f"Low win probability ({win_prob:.0%})")

        # Lower bound check
        if win_lower_ci < settings.min_confidence_interval:
            concerns.append(f"Lower CI ({win_lower_ci:.0%}) below threshold")

        verdict = (
            "strong_buy" if win_prob >= 0.80 and win_lower_ci >= 0.70 else
            "buy" if win_prob >= 0.65 else
            "wait" if win_prob >= 0.45 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=float(win_prob),
            score=score,
            reasoning=f"Win prob={win_prob:.0%} CI=[{win_lower_ci:.0%}, {win_upper_ci:.0%}] opp={opportunity:.0f}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 9. RiskManagementAgent (VETO)
# =========================================================================
class RiskManagementAgent(BaseAgent):
    name = "risk_management"
    role = "Risk Management Agent"
    specialty = "Risk evaluation, position sizing, portfolio protection (VETO POWER)"
    has_veto = True
    weight = 2.0

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        risk_score = _num(indicators.get("risk_score", 50))
        max_dd = _num(ctx.get("current_drawdown_pct", 0))
        daily_loss = _num(ctx.get("daily_loss_pct", 0))
        open_positions = int(ctx.get("open_positions_count", 0))
        max_open = settings.paper_max_open_positions
        consecutive_losses = int(ctx.get("consecutive_losses", 0))
        cooldown_after = settings.risk_cooldown_after_losses

        # Veto conditions
        veto_reasons: List[str] = []
        if risk_score > 90:
            veto_reasons.append(f"EXTREME_RISK_SCORE ({risk_score:.0f})")
        if max_dd >= settings.risk_max_drawdown_pct:
            veto_reasons.append(f"MAX_DRAWDOWN_BREACHED ({max_dd:.1f}%)")
        if daily_loss >= settings.risk_daily_loss_limit_pct:
            veto_reasons.append(f"DAILY_LOSS_LIMIT ({daily_loss:.1f}%)")
        if consecutive_losses >= cooldown_after * 2:
            veto_reasons.append(f"EXCESSIVE_CONSECUTIVE_LOSSES ({consecutive_losses})")

        if veto_reasons:
            return AgentResult(
                agent_name=self.name,
                role=self.role,
                verdict="veto",
                confidence=0.95,
                score=0,
                reasoning=f"VETO: {', '.join(veto_reasons)}",
                concerns=veto_reasons,
                has_veto=self.has_veto,
                weight=self.weight,
            )

        # Score risk
        score = 100 - int(risk_score)
        factors: List[str] = []
        concerns: List[str] = []

        if risk_score < 30:
            factors.append(f"Low risk score ({risk_score:.0f})")
        elif risk_score > 70:
            concerns.append(f"High risk score ({risk_score:.0f})")

        if max_dd > settings.risk_max_drawdown_pct * 0.8:
            concerns.append(f"Near max drawdown ({max_dd:.1f}%)")
        if daily_loss > settings.risk_daily_loss_limit_pct * 0.7:
            concerns.append(f"Approaching daily loss limit ({daily_loss:.1f}%)")

        if open_positions >= max_open:
            concerns.append(f"Max open positions reached ({open_positions}/{max_open})")
            score -= 20

        if consecutive_losses >= cooldown_after:
            concerns.append(f"In cooldown ({consecutive_losses} consecutive losses)")
            score -= 15

        score = max(0, min(100, score))
        verdict = (
            "strong_buy" if score >= 75 and risk_score < 30 else
            "buy" if score >= 55 else
            "wait" if score >= 35 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.75,
            score=score,
            reasoning=f"Risk={risk_score:.0f}, DD={max_dd:.1f}%, daily={daily_loss:.1f}%, "
                      f"open={open_positions}/{max_open}, consec_loss={consecutive_losses}",
            key_factors=factors,
            concerns=concerns,
            has_veto=self.has_veto,
            weight=self.weight,
        )


# =========================================================================
# 10. StrategyResearchAgent
# =========================================================================
class StrategyResearchAgent(BaseAgent):
    name = "strategy_research"
    role = "Strategy Research Agent"
    specialty = "Strategy testing and parameter optimization"
    weight = 0.9

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        regime = ctx.get("market_regime", {}) or {}
        primary = str(regime.get("regime", "unknown"))
        recommended = regime.get("recommended_strategies", []) or []
        strategy_id = ctx.get("strategy_id", "default")

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Strategy / regime fit
        if recommended and strategy_id in recommended:
            score += 15
            factors.append(f"Strategy '{strategy_id}' matches regime '{primary}'")
        elif recommended:
            score -= 8
            concerns.append(f"Strategy '{strategy_id}' not in recommended {recommended}")
        else:
            factors.append("Default strategy applied")

        # Strategy backtest metrics (if available)
        bt = ctx.get("strategy_backtest", {}) or {}
        if bt:
            wr = _num(bt.get("win_rate", 0))
            pf = _num(bt.get("profit_factor", 0))
            exp = _num(bt.get("expectancy", 0))
            if wr >= 0.65 and pf >= 1.5:
                score += 12
                factors.append(f"Strong backtest WR={wr:.0%} PF={pf:.2f}")
            elif wr >= 0.55 and pf >= 1.2:
                score += 5
                factors.append(f"Decent backtest WR={wr:.0%} PF={pf:.2f}")
            elif wr < 0.45 or pf < 1.0:
                score -= 10
                concerns.append(f"Weak backtest WR={wr:.0%} PF={pf:.2f}")

        # Out-of-sample check
        oos = bt.get("out_of_sample_win_rate")
        if oos is not None:
            if _num(oos) >= 0.55:
                score += 5
                factors.append("Out-of-sample win rate strong")
            else:
                score -= 5
                concerns.append(f"Out-of-sample WR weak ({oos:.0%})")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.6,
            score=score,
            reasoning=f"Strategy={strategy_id}, regime={primary}, fit={strategy_id in recommended}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 11. HistoricalLearningAgent
# =========================================================================
class HistoricalLearningAgent(BaseAgent):
    name = "historical_learning"
    role = "Historical Learning Agent"
    specialty = "Learns from past trades"
    weight = 1.0

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        # Pulls historical learnings (pre-fetched in ctx by caller)
        learnings: List[Dict[str, Any]] = ctx.get("applicable_learnings", []) or []
        mistakes: List[Dict[str, Any]] = ctx.get("applicable_mistakes", []) or []
        past_trades: List[Dict[str, Any]] = ctx.get("past_trades_same_token", []) or []

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Learnings: positive patterns
        positive_count = sum(1 for l in learnings if l.get("learning_type") == "success_factor")
        negative_count = sum(1 for l in learnings if l.get("learning_type") == "failure_factor")

        if positive_count >= 3:
            score += 10
            factors.append(f"{positive_count} positive patterns match")
        elif positive_count > 0:
            score += 4

        if negative_count >= 2:
            score -= 10
            concerns.append(f"{negative_count} failure patterns match")
        elif negative_count > 0:
            score -= 4

        # Mistakes (active blocks)
        active_blocks = [m for m in mistakes if m.get("active_block", False)]
        if active_blocks:
            score -= 15
            block_names = [m.get("mistake_type", "?") for m in active_blocks[:3]]
            concerns.append(f"Active mistake blocks: {', '.join(block_names)}")
        else:
            factors.append("No active mistake blocks")

        # Past trades on same token
        if past_trades:
            wins = sum(1 for t in past_trades if (t.get("net_pnl_usd") or 0) > 0)
            losses = sum(1 for t in past_trades if (t.get("net_pnl_usd") or 0) < 0)
            if len(past_trades) >= 3:
                wr = wins / len(past_trades)
                if wr >= 0.65:
                    score += 8
                    factors.append(f"Past trades on this token: {wr:.0%} WR")
                elif wr < 0.35:
                    score -= 8
                    concerns.append(f"Past trades on this token: {wr:.0%} WR")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.6,
            score=score,
            reasoning=f"learnings+={positive_count}, learnings-={negative_count}, "
                      f"blocks={len(active_blocks)}, past_trades={len(past_trades)}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 12. ErrorPreventionAgent
# =========================================================================
class ErrorPreventionAgent(BaseAgent):
    name = "error_prevention"
    role = "Error Prevention Agent"
    specialty = "Prevents repeated mistakes"
    weight = 1.1

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        mistakes: List[Dict[str, Any]] = ctx.get("applicable_mistakes", []) or []
        recent_loss_count = int(ctx.get("recent_loss_count", 0))
        cooldown_active = bool(ctx.get("cooldown_active", False))

        score = 60  # start neutral-positive
        factors: List[str] = []
        concerns: List[str] = []

        if cooldown_active:
            score -= 30
            concerns.append("Cooldown active after losses")

        if recent_loss_count >= 5:
            score -= 15
            concerns.append(f"{recent_loss_count} recent losses")

        # Specific mistake types
        mistake_types = [m.get("mistake_type", "") for m in mistakes if m.get("active_block", True)]
        late_entry = "late_entry" in mistake_types
        fake_breakout = "fake_breakout" in mistake_types
        stale_data = "stale_data" in mistake_types
        bad_regime = "bad_market_regime" in mistake_types
        overconfidence = "overconfidence" in mistake_types

        chg_5m = _num(getattr(token, "price_change_5m_pct", 0))
        if late_entry and chg_5m > 20:
            score -= 20
            concerns.append(f"Late entry risk (+{chg_5m:.1f}% in 5m)")
        if fake_breakout:
            score -= 10
            concerns.append("Past fake breakout mistakes on similar setup")
        if stale_data:
            data_age = int(ctx.get("data_age_seconds", 0))
            if data_age > 120:
                score -= 15
                concerns.append(f"Stale data risk ({data_age}s old)")
        if bad_regime:
            regime = ctx.get("market_regime", {}).get("regime", "")
            if regime in ("high_volatility", "panic_selling", "bear_market"):
                score -= 10
                concerns.append(f"Bad regime pattern active ({regime})")
        if overconfidence:
            win_prob = _num(ctx.get("indicators", {}).get("win_probability"), 0.5)
            if win_prob > 0.85:
                score -= 10
                concerns.append("Overconfidence risk (very high win prob)")

        if not mistake_types:
            score += 5
            factors.append("No prior mistakes matching current setup")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.65,
            score=score,
            reasoning=f"mistakes={len(mistake_types)}, cooldown={cooldown_active}, "
                      f"recent_losses={recent_loss_count}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 13. MarketAnomalyAgent
# =========================================================================
class MarketAnomalyAgent(BaseAgent):
    name = "market_anomaly"
    role = "Market Anomaly Agent"
    specialty = "Detects manipulation, pumps, spoofing"
    weight = 1.3

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        liq = indicators.get("liquidity", {}) or {}
        sec = indicators.get("security", {}) or {}
        on = indicators.get("onchain", {}) or {}

        wash_prob = _num(liq.get("wash_trading_probability", 0))
        fake_liq = _num(liq.get("fake_liquidity_probability", 0))
        liq_removed = bool(liq.get("liquidity_removed_recently", False))
        sudden_dump = bool(sec.get("sudden_dump_detected", False))
        abnormal_vol = bool(sec.get("abnormal_volume_detected", False))
        suspicious_wallets = _num(sec.get("suspicious_wallets", 0))
        insider = _num(on.get("insider_activity_probability", 0))
        bundling = _num(on.get("token_bundling_probability", 0))

        chg_5m = _num(getattr(token, "price_change_5m_pct", 0))
        chg_24h = _num(getattr(token, "price_change_24h_pct", 0))
        vol_5m = _num(getattr(token, "volume_5m_usd", 0))
        vol_24h = _num(getattr(token, "volume_24h_usd", 0))

        score = 60
        factors: List[str] = []
        concerns: List[str] = []

        # Sudden pumps
        if chg_5m > 100:
            score -= 25
            concerns.append(f"SUSPICIOUS PUMP +{chg_5m:.0f}% in 5m")
        elif chg_5m > 50:
            score -= 15
            concerns.append(f"Sharp 5m pump +{chg_5m:.0f}%")
        elif chg_24h > 500:
            score -= 20
            concerns.append(f"Extreme 24h pump +{chg_24h:.0f}%")

        # Wash trading
        if wash_prob > 0.7:
            score -= 20
            concerns.append(f"Likely wash trading ({wash_prob:.0%})")
        elif wash_prob > 0.4:
            score -= 10

        # Fake liquidity
        if fake_liq > 0.5:
            score -= 15
            concerns.append("Likely fake liquidity")

        # Liquidity removed
        if liq_removed:
            score -= 25
            concerns.append("LIQUIDITY RECENTLY REMOVED")

        # Sudden dump
        if sudden_dump:
            score -= 18
            concerns.append("Sudden dump detected")

        # Abnormal volume
        if abnormal_vol:
            score -= 10
            concerns.append("Abnormal volume pattern")

        # Suspicious wallets
        if suspicious_wallets >= 5:
            score -= 12
            concerns.append(f"{suspicious_wallets:.0f} suspicious wallets")

        # Insider
        if insider > 0.6:
            score -= 15
            concerns.append(f"Insider activity likely ({insider:.0%})")

        # Bundling
        if bundling > 0.6:
            score -= 15
            concerns.append(f"Token bundling likely ({bundling:.0%})")

        # 5m volume vs 24h (anomaly if 5m has > 30% of 24h)
        if vol_5m > 0 and vol_24h > 0:
            ratio = vol_5m / vol_24h
            if ratio > 0.5:
                score -= 8
                concerns.append(f"5m vol = {ratio:.0%} of 24h (concentrated)")

        # Clean case
        if not concerns:
            score += 10
            factors.append("No anomalies detected")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.7,
            score=score,
            reasoning=f"wash={wash_prob:.0%}, fake_liq={fake_liq:.0%}, insider={insider:.0%}, "
                      f"chg5m={chg_5m:.1f}%",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 14. DecisionVerificationAgent
# =========================================================================
class DecisionVerificationAgent(BaseAgent):
    name = "decision_verification"
    role = "Decision Verification Agent"
    specialty = "Challenges other agents' conclusions"
    weight = 1.0

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        # This agent runs after all others (in second phase)
        # It looks for inconsistencies between agents
        prior_verdicts: List[Dict[str, Any]] = ctx.get("prior_verdicts", []) or []
        if not prior_verdicts:
            return AgentResult(
                agent_name=self.name,
                role=self.role,
                verdict="wait",
                confidence=0.5,
                score=50,
                reasoning="No prior verdicts to verify",
            )

        buy_count = sum(1 for v in prior_verdicts if v.get("verdict") in ("buy", "strong_buy"))
        reject_count = sum(1 for v in prior_verdicts if v.get("verdict") in ("reject", "strong_reject", "veto"))
        wait_count = sum(1 for v in prior_verdicts if v.get("verdict") == "wait")
        total = len(prior_verdicts)

        scores = [v.get("score", 50) for v in prior_verdicts]
        avg_score = sum(scores) / max(len(scores), 1)
        score_spread = max(scores) - min(scores) if scores else 0

        score = int(avg_score)
        factors: List[str] = []
        concerns: List[str] = []

        # Consensus check
        if buy_count >= total * 0.75:
            score += 8
            factors.append(f"Strong consensus: {buy_count}/{total} buy")
        elif reject_count >= total * 0.5:
            score -= 15
            concerns.append(f"Majority reject: {reject_count}/{total}")

        # Spread too wide = disagreement
        if score_spread > 50:
            score -= 10
            concerns.append(f"High disagreement (spread={score_spread})")

        # Wait-heavy = unclear
        if wait_count >= total * 0.5:
            score -= 5
            concerns.append("Many agents uncertain")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.65,
            score=score,
            reasoning=f"Verified {total} verdicts: buy={buy_count}, reject={reject_count}, "
                      f"wait={wait_count}, avg={avg_score:.0f}, spread={score_spread}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 15. RedTeamAgent
# =========================================================================
class RedTeamAgent(BaseAgent):
    name = "red_team"
    role = "Red Team Agent"
    specialty = "Actively tries to disprove trades (adversarial)"
    weight = 1.4

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        prior_verdicts: List[Dict[str, Any]] = ctx.get("prior_verdicts", []) or []

        # Take the most bullish verdict and attack it
        target_score = 50
        if prior_verdicts:
            target_score = max(v.get("score", 50) for v in prior_verdicts)

        # Counter-arguments
        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        # Attack each bullish thesis
        chg_5m = _num(getattr(token, "price_change_5m_pct", 0))
        chg_24h = _num(getattr(token, "price_change_24h_pct", 0))
        liq = _num(getattr(token, "liquidity_usd", 0))
        vol_24h = _num(getattr(token, "volume_24h_usd", 0))
        win_prob = _num(indicators.get("win_probability"), 0.5)
        risk_score = _num(indicators.get("risk_score", 50))
        honeypot = _num(indicators.get("security", {}).get("honeypot_probability", 0))
        rug = _num(indicators.get("security", {}).get("rug_pull_probability", 0))

        # Counter: late entry
        if chg_5m > 30:
            score -= 15
            concerns.append(f"RED: entry after +{chg_5m:.0f}% pump = late, high reversion risk")
        elif chg_24h > 100:
            score -= 12
            concerns.append(f"RED: already pumped {chg_24h:.0f}% in 24h")

        # Counter: liquidity
        if liq < 20_000:
            score -= 15
            concerns.append(f"RED: thin liquidity ${liq:,.0f} - exit may be impossible")
        elif liq < 50_000:
            score -= 8

        # Counter: low volume
        if vol_24h < 10_000:
            score -= 10
            concerns.append("RED: very low volume - illiquid market")

        # Counter: overconfidence in model
        if win_prob > 0.90:
            score -= 10
            concerns.append(f"RED: win prob {win_prob:.0%} suspiciously high")
        elif win_prob > 0.80 and target_score > 80:
            score -= 5
            concerns.append("RED: model may be overconfident")

        # Counter: security risks
        if honeypot > 0.3:
            score -= 12
            concerns.append(f"RED: honeypot prob {honeypot:.0%}")
        if rug > 0.3:
            score -= 12
            concerns.append(f"RED: rug prob {rug:.0%}")
        if risk_score > 60:
            score -= 8
            concerns.append(f"RED: risk score {risk_score:.0f}")

        # Counter: buy pressure
        buys = _num(getattr(token, "txns_24h_buys", 0))
        sells = _num(getattr(token, "txns_24h_sells", 0))
        if buys > 0 and sells > 0:
            bs = buys / sells
            if bs < 0.8:
                score -= 8
                concerns.append(f"RED: sell pressure {bs:.2f}x")

        # Counter: age
        first_seen = getattr(token, "first_seen_at", None) or datetime.utcnow()
        age_min = (datetime.utcnow() - first_seen).total_seconds() / 60.0
        if age_min < 30:
            score -= 10
            concerns.append(f"RED: token only {age_min:.0f}m old - too early")
        elif age_min > 4320:
            score -= 5
            concerns.append("RED: aging token, momentum may be exhausted")

        if not concerns:
            score += 5
            factors.append("Red team found no critical flaws")

        # Reduce target if red team finds issues
        if target_score > 70 and len(concerns) >= 3:
            score = min(score, 50)
            concerns.append("RED: multiple red flags override bullish case")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.7,
            score=score,
            reasoning=f"Red team attacked target_score={target_score}, found {len(concerns)} issues",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 16. WalletIntelligenceAgent
# =========================================================================
class WalletIntelligenceAgent(BaseAgent):
    name = "wallet_intelligence"
    role = "Wallet Intelligence Agent"
    specialty = "Wallet history analysis (whales, insiders, snipers)"
    weight = 1.0

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        indicators = _ctx_indicators(ctx)
        on = indicators.get("onchain", {}) or {}

        suspicious = _num(on.get("suspicious_wallet_count", 0))
        whale = bool(on.get("whale_activity_detected", False))
        early = bool(on.get("early_wallet_activity", False))
        clusters = _num(on.get("related_wallet_clusters", 0))
        insider = _num(on.get("insider_activity_probability", 0))
        recent_buys = _num(on.get("recent_buys_count", 0))
        recent_sells = _num(on.get("recent_sells_count", 0))

        score = 55
        factors: List[str] = []
        concerns: List[str] = []

        if suspicious >= 5:
            score -= 15
            concerns.append(f"{suspicious:.0f} suspicious wallets")
        elif suspicious >= 2:
            score -= 7
        elif suspicious == 0:
            score += 5
            factors.append("No suspicious wallets")

        if whale:
            score -= 5
            concerns.append("Whale wallets active")
        else:
            factors.append("No whale activity")

        if early:
            score -= 8
            concerns.append("Early-bird wallets detected (likely insiders)")
        else:
            factors.append("No early-bird wallets")

        if clusters >= 3:
            score -= 10
            concerns.append(f"{clusters:.0f} related wallet clusters")
        elif clusters == 0:
            score += 3

        if insider > 0.5:
            score -= 12
            concerns.append(f"Insider probability {insider:.0%}")

        # Buy/sell flow
        if recent_buys > 0 and recent_sells > 0:
            bs = recent_buys / recent_sells
            if bs > 1.5:
                score += 5
                factors.append(f"Buy flow dominant ({bs:.2f}x)")
            elif bs < 0.6:
                score -= 8
                concerns.append(f"Sell flow dominant ({bs:.2f}x)")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.6,
            score=score,
            reasoning=f"suspicious={suspicious:.0f}, whale={whale}, clusters={clusters:.0f}, "
                      f"insider={insider:.0%}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 17. MetaLearningAgent
# =========================================================================
class MetaLearningAgent(BaseAgent):
    name = "meta_learning"
    role = "Meta Learning Agent"
    specialty = "Improves the learning process itself"
    weight = 0.8

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        # Looks at the overall learning system health
        learnings_total = int(ctx.get("total_learnings", 0))
        validated_learnings = int(ctx.get("validated_learnings", 0))
        adoption_rate = _num(ctx.get("learning_adoption_rate", 0))
        validation_rate = (validated_learnings / max(learnings_total, 1))
        recent_improvement = _num(ctx.get("recent_win_rate_delta", 0))

        score = 50
        factors: List[str] = []
        concerns: List[str] = []

        if learnings_total > 50:
            score += 8
            factors.append(f"Rich learning base ({learnings_total} learnings)")
        elif learnings_total > 10:
            score += 3
        else:
            score -= 8
            concerns.append(f"Thin learning base ({learnings_total})")

        if validation_rate > 0.7:
            score += 5
            factors.append(f"High validation rate ({validation_rate:.0%})")
        elif validation_rate < 0.3:
            score -= 5
            concerns.append(f"Low validation rate ({validation_rate:.0%})")

        if adoption_rate > 0.6:
            score += 5
            factors.append(f"Healthy adoption ({adoption_rate:.0%})")
        elif adoption_rate < 0.2:
            score -= 5
            concerns.append(f"Low adoption ({adoption_rate:.0%})")

        if recent_improvement > 0.05:
            score += 8
            factors.append(f"Win rate improving (+{recent_improvement:.0%})")
        elif recent_improvement < -0.05:
            score -= 8
            concerns.append(f"Win rate declining ({recent_improvement:.0%})")

        score = max(0, min(100, int(score)))
        verdict = (
            "strong_buy" if score >= 75 else
            "buy" if score >= 60 else
            "wait" if score >= 40 else
            "reject"
        )

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=0.55,
            score=score,
            reasoning=f"learnings={learnings_total}, validated={validated_learnings}, "
                      f"adopt={adoption_rate:.0%}, delta={recent_improvement:+.0%}",
            key_factors=factors,
            concerns=concerns,
        )


# =========================================================================
# 18. ChiefIntelligenceAgent
# =========================================================================
class ChiefIntelligenceAgent(BaseAgent):
    name = "chief_intelligence"
    role = "Chief Intelligence Agent"
    specialty = "Combines all evidence, final decision"
    weight = 1.5

    async def _rule_based_analyze(self, token, ctx: Dict[str, Any]) -> AgentResult:
        """Runs LAST - combines all other agents' verdicts."""
        prior_verdicts: List[Dict[str, Any]] = ctx.get("prior_verdicts", []) or []
        if not prior_verdicts:
            return AgentResult(
                agent_name=self.name,
                role=self.role,
                verdict="wait",
                confidence=0.5,
                score=50,
                reasoning="No prior verdicts to synthesize",
            )

        total_weight = 0.0
        weighted_score = 0.0
        buy_weight = 0.0
        reject_weight = 0.0
        wait_weight = 0.0
        veto_agents: List[str] = []
        all_concerns: List[str] = []
        all_factors: List[str] = []

        for v in prior_verdicts:
            w = float(v.get("weight", 1.0))
            s = float(v.get("score", 50))
            verdict = str(v.get("verdict", "wait"))
            total_weight += w
            weighted_score += w * s

            if verdict in ("buy", "strong_buy"):
                buy_weight += w
            elif verdict in ("reject", "strong_reject"):
                reject_weight += w
            elif verdict == "wait":
                wait_weight += w
            elif verdict == "veto":
                veto_agents.append(v.get("agent_name", "?"))
                reject_weight += w * 2.0  # veto heavily weighted

            for c in (v.get("concerns") or [])[:3]:
                if c not in all_concerns:
                    all_concerns.append(c)
            for f in (v.get("key_factors") or [])[:3]:
                if f not in all_factors:
                    all_factors.append(f)

        final_score = int(weighted_score / max(total_weight, 1.0))
        buy_pct = buy_weight / max(total_weight, 1.0)
        reject_pct = reject_weight / max(total_weight, 1.0)
        wait_pct = wait_weight / max(total_weight, 1.0)

        # Final verdict logic
        if veto_agents:
            verdict = "reject"
            reasoning = f"VETO by: {', '.join(veto_agents)}"
            confidence = 0.95
        elif reject_pct >= 0.4:
            verdict = "reject"
            confidence = 0.8
            reasoning = f"Reject consensus ({reject_pct:.0%} weight)"
        elif buy_pct >= 0.70 and final_score >= 70:
            verdict = "strong_buy"
            confidence = 0.85
            reasoning = f"Strong buy consensus ({buy_pct:.0%} weight, score={final_score})"
        elif buy_pct >= 0.55 and final_score >= 60:
            verdict = "buy"
            confidence = 0.75
            reasoning = f"Buy consensus ({buy_pct:.0%} weight, score={final_score})"
        elif wait_pct >= 0.5 or 0.40 <= buy_pct < 0.55:
            verdict = "wait"
            confidence = 0.65
            reasoning = f"Mixed consensus (buy={buy_pct:.0%}, wait={wait_pct:.0%})"
        else:
            verdict = "wait"
            confidence = 0.55
            reasoning = f"No clear consensus (buy={buy_pct:.0%}, reject={reject_pct:.0%})"

        return AgentResult(
            agent_name=self.name,
            role=self.role,
            verdict=verdict,
            confidence=confidence,
            score=final_score,
            reasoning=reasoning,
            key_factors=all_factors[:10],
            concerns=all_concerns[:10],
            weight=self.weight,
        )


# =========================================================================
# Multi-Agent System orchestrator
# =========================================================================
class MultiAgentSystem:
    """
    Orchestrates the 18-agent analysis pipeline.

    Pipeline:
    1. Phase 1: Run first 14 agents in parallel (each sees token + analysis_context)
    2. Phase 2: Run DecisionVerificationAgent, RedTeamAgent (they see phase-1 verdicts)
    3. Phase 3: ChiefIntelligenceAgent synthesizes everything
    4. Check for vetoes (SecurityAgent, RiskManagementAgent) - if any, REJECT
    5. Return comprehensive result
    """

    # Agents that run in phase 1 (independent analysis)
    PHASE1_AGENTS = [
        "market_intelligence",
        "token_discovery",
        "technical_analysis",
        "liquidity_intelligence",
        "onchain_intelligence",
        "security",
        "social_intelligence",
        "prediction",
        "risk_management",
        "strategy_research",
        "historical_learning",
        "error_prevention",
        "market_anomaly",
        "wallet_intelligence",
        "meta_learning",
    ]
    # Phase 2 agents (challenge phase)
    PHASE2_AGENTS = [
        "decision_verification",
        "red_team",
    ]
    # Phase 3 (final synthesis)
    PHASE3_AGENTS = ["chief_intelligence"]

    def __init__(self):
        self.agents = self._init_agents()
        self._agent_map: Dict[str, BaseAgent] = {a.name: a for a in self.agents}

    def _init_agents(self) -> List[BaseAgent]:
        return [
            MarketIntelligenceAgent(),
            TokenDiscoveryAgent(),
            TechnicalAnalysisAgent(),
            LiquidityIntelligenceAgent(),
            OnChainIntelligenceAgent(),
            SecurityAgent(),
            SocialIntelligenceAgent(),
            PredictionAgent(),
            RiskManagementAgent(),
            StrategyResearchAgent(),
            HistoricalLearningAgent(),
            ErrorPreventionAgent(),
            MarketAnomalyAgent(),
            DecisionVerificationAgent(),
            RedTeamAgent(),
            WalletIntelligenceAgent(),
            MetaLearningAgent(),
            ChiefIntelligenceAgent(),
        ]

    async def run_full_analysis(
        self,
        token,
        analysis_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Run all 18 agents and produce a comprehensive decision.

        Returns dict with:
        - final_verdict: strong_buy | buy | wait | reject | strong_reject | veto
        - final_score: 0-100
        - final_confidence: 0.0-1.0
        - vetoed: bool
        - veto_reasons: list
        - agent_results: list of AgentResult.to_dict()
        - chief_reasoning: str
        - timestamp: ISO datetime
        - elapsed_ms: int
        """
        t0 = time.time()
        logger.info(
            f"🧠 Starting 18-agent analysis for {getattr(token, 'symbol', '?')}"
        )

        # === Phase 1 ===
        phase1_agents = [self._agent_map[n] for n in self.PHASE1_AGENTS if n in self._agent_map]
        phase1_tasks = [a.analyze(token, analysis_context) for a in phase1_agents]
        phase1_results: List[AgentResult] = await asyncio.gather(*phase1_tasks, return_exceptions=False)

        # Check for early veto (Security / Risk)
        veto_results = [r for r in phase1_results if r.is_veto]
        if veto_results:
            logger.warning(
                f"⛔ VETO triggered by {[r.agent_name for r in veto_results]} - aborting trade"
            )
            return self._build_result(
                phase1_results, [], None,
                final_verdict="veto",
                vetoed=True,
                veto_reasons=[r.reasoning for r in veto_results],
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # === Phase 2 ===
        prior_verdicts_dicts = [r.to_dict() for r in phase1_results]
        phase2_ctx = {**analysis_context, "prior_verdicts": prior_verdicts_dicts}
        phase2_agents = [self._agent_map[n] for n in self.PHASE2_AGENTS if n in self._agent_map]
        phase2_tasks = [a.analyze(token, phase2_ctx) for a in phase2_agents]
        phase2_results: List[AgentResult] = await asyncio.gather(*phase2_tasks, return_exceptions=False)

        # === Phase 3 (Chief) ===
        all_results = phase1_results + phase2_results
        all_dicts = [r.to_dict() for r in all_results]
        phase3_ctx = {**analysis_context, "prior_verdicts": all_dicts}
        chief = self._agent_map.get("chief_intelligence")
        if chief:
            chief_result = await chief.analyze(token, phase3_ctx)
        else:
            chief_result = self._fallback_chief(all_results)

        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info(
            f"✅ 18-agent analysis done in {elapsed_ms}ms - "
            f"verdict={chief_result.verdict} score={chief_result.score}"
        )

        return self._build_result(
            all_results, phase2_results, chief_result,
            elapsed_ms=elapsed_ms,
        )

    # ---- Helpers ---------------------------------------------------------
    def _build_result(
        self,
        phase1_results: List[AgentResult],
        phase2_results: List[AgentResult],
        chief_result: Optional[AgentResult],
        final_verdict: Optional[str] = None,
        vetoed: bool = False,
        veto_reasons: Optional[List[str]] = None,
        elapsed_ms: int = 0,
    ) -> Dict[str, Any]:
        all_results = list(phase1_results) + list(phase2_results)
        if chief_result is not None:
            all_results.append(chief_result)

        if vetoed:
            # Veto overrides everything — final verdict is always "veto"
            final_verdict = "veto"
            final_score = 0
            final_confidence = 0.95
            chief_reasoning = f"VETO: {', '.join(veto_reasons or [])}"
        elif final_verdict is None and chief_result is not None:
            final_verdict = chief_result.verdict
            final_score = chief_result.score
            final_confidence = chief_result.confidence
            chief_reasoning = chief_result.reasoning
        elif chief_result is not None:
            # final_verdict was passed in (e.g., "veto" - though we handle that above)
            final_score = chief_result.score
            final_confidence = chief_result.confidence
            chief_reasoning = chief_result.reasoning
        else:
            # Fallback when no chief and no veto
            final_verdict = final_verdict or "wait"
            final_score = 50
            final_confidence = 0.5
            chief_reasoning = "No chief agent"

        return {
            "final_verdict": final_verdict,
            "final_score": final_score,
            "final_confidence": final_confidence,
            "vetoed": vetoed,
            "veto_reasons": veto_reasons or [],
            "agent_results": [r.to_dict() for r in all_results],
            "phase1_results": [r.to_dict() for r in phase1_results],
            "phase2_results": [r.to_dict() for r in phase2_results],
            "chief_reasoning": chief_reasoning,
            "chief_factors": chief_result.key_factors if chief_result else [],
            "chief_concerns": chief_result.concerns if chief_result else [],
            "timestamp": datetime.utcnow().isoformat(),
            "elapsed_ms": elapsed_ms,
            "agent_count": len(all_results),
        }

    def _fallback_chief(self, all_results: List[AgentResult]) -> AgentResult:
        """Weighted-average fallback if ChiefIntelligenceAgent is missing."""
        total_w = sum(r.weight for r in all_results) or 1.0
        score = sum(r.score * r.weight for r in all_results) / total_w
        return AgentResult(
            agent_name="chief_intelligence_fallback",
            role="Chief Intelligence Agent (fallback)",
            verdict="wait" if 40 <= score < 60 else ("buy" if score >= 60 else "reject"),
            confidence=0.6,
            score=int(score),
            reasoning="Fallback weighted average (chief agent missing)",
            weight=1.5,
        )


__all__ = [
    "AgentVerdict",
    "AgentResult",
    "BaseAgent",
    "MultiAgentSystem",
    "MarketIntelligenceAgent",
    "TokenDiscoveryAgent",
    "TechnicalAnalysisAgent",
    "LiquidityIntelligenceAgent",
    "OnChainIntelligenceAgent",
    "SecurityAgent",
    "SocialIntelligenceAgent",
    "PredictionAgent",
    "RiskManagementAgent",
    "StrategyResearchAgent",
    "HistoricalLearningAgent",
    "ErrorPreventionAgent",
    "MarketAnomalyAgent",
    "DecisionVerificationAgent",
    "RedTeamAgent",
    "WalletIntelligenceAgent",
    "MetaLearningAgent",
    "ChiefIntelligenceAgent",
]
