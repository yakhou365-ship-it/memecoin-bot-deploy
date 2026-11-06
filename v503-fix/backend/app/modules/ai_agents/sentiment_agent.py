"""
Ultimate Agent 4: Sentiment Analysis Agent
Specialty: Social hype, community activity, trader behavior,
buy/sell ratio, volume as hype indicator, momentum sentiment.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...core.config import settings
from ...core.logger import logger
from .base_agent import BaseAgent, AgentVerdict


class SentimentAgent(BaseAgent):
    name = "sentiment_agent"
    role = "Ultimate Sentiment Analyst"
    specialty = "Social sentiment, community hype, trader behavior"
    enabled_setting = "ai_agent_sentiment_enabled"
    has_veto = False

    def _system_prompt(self) -> str:
        return (
            "You are an elite sentiment analyst for memecoins.\n"
            "Analyze: social buzz, community growth, trader sentiment via buy/sell ratio,\n"
            "volume as hype indicator, price momentum as sentiment proxy, community health.\n"
            "Return JSON: {decision, confidence, score, reasoning, key_factors, concerns, suggested_position_size_pct}"
        )

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        sent = analysis_context.get("sentiment", {}) or {}
        return (
            f"Token: {token.symbol}\n"
            f"Social mentions 24h: {sent.get('social_mentions_24h', 0)}\n"
            f"Sentiment score: {sent.get('sentiment_score', 50)}\n"
            f"Community growth 24h: {sent.get('community_growth_24h', 0)}%\n"
            f"5m change: {token.price_change_5m_pct or 0}%\n"
            f"24h change: {token.price_change_24h_pct or 0}%\n"
            f"Volume 24h: ${token.volume_24h_usd or 0}\n"
            f"Buys: {token.txns_24h_buys or 0}, Sells: {token.txns_24h_sells or 0}\n"
            "Return sentiment decision JSON."
        )

    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        sent = analysis_context.get("sentiment", {}) or {}
        vol_24h = token.volume_24h_usd or 0
        chg_5m = token.price_change_5m_pct or 0
        chg_24h = token.price_change_24h_pct or 0
        buys = token.txns_24h_buys or 0
        sells = token.txns_24h_sells or 0
        mcap = token.market_cap_usd or 0

        score = 50
        factors: List[str] = []
        concerns: List[str] = []
        decision = "WAIT"

        # === Social Mentions ===
        mentions = sent.get("social_mentions_24h", 0) or 0
        if mentions >= 1000:
            score += 15
            factors.append(f"High social buzz: {mentions} mentions/24h")
        elif mentions >= 100:
            score += 8
            factors.append(f"Moderate buzz: {mentions} mentions")
        elif mentions < 10:
            score -= 10
            concerns.append("Very low social mentions")

        # === Sentiment Score ===
        sent_score = sent.get("sentiment_score", 50) or 50
        if sent_score >= 70:
            score += 12
            factors.append(f"Positive community sentiment: {sent_score}/100")
        elif sent_score >= 55:
            score += 6
            factors.append(f"Somewhat positive: {sent_score}/100")
        elif sent_score <= 30:
            score -= 18
            concerns.append(f"Negative community sentiment: {sent_score}/100")
        elif sent_score <= 45:
            score -= 8
            concerns.append(f"Weak sentiment: {sent_score}/100")

        # === Community Growth ===
        growth = sent.get("community_growth_24h", 0) or 0
        if growth >= 30:
            score += 12
            factors.append(f"Explosive community growth: +{growth:.1f}%")
        elif growth >= 10:
            score += 7
            factors.append(f"Community growing: +{growth:.1f}%")
        elif growth >= 0:
            score += 2
            factors.append(f"Community stable: +{growth:.1f}%")
        elif growth <= -20:
            score -= 15
            concerns.append(f"Community shrinking: {growth:.1f}%")

        # === Price Momentum as Sentiment Proxy ===
        if chg_5m >= 10:
            score += 12
            factors.append(f"Strong 5m momentum: +{chg_5m:.1f}%")
        elif chg_5m >= 3:
            score += 6
            factors.append(f"Positive 5m: +{chg_5m:.1f}%")
        elif chg_5m <= -10:
            score -= 20
            concerns.append(f"Negative 5m momentum: {chg_5m:.1f}%")

        if chg_24h >= 30:
            score += 10
            factors.append(f"Strong 24h uptrend: +{chg_24h:.1f}%")
        elif chg_24h >= 10:
            score += 5
            factors.append(f"Positive 24h: +{chg_24h:.1f}%")
        elif chg_24h <= -30:
            score -= 20
            concerns.append(f"Severe 24h decline: {chg_24h:.1f}%")

        # === Buy/Sell Ratio as Trader Sentiment ===
        total_txns = buys + sells
        if total_txns > 100:
            buy_ratio = buys / total_txns
            if buy_ratio >= 0.7:
                score += 15
                factors.append(f"Bullish trader sentiment: {buy_ratio:.0%} buys")
            elif buy_ratio >= 0.6:
                score += 8
                factors.append(f"Slightly bullish: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.3:
                score -= 20
                concerns.append(f"Bearish trader sentiment: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.4:
                score -= 10
                concerns.append(f"More selling than buying: {buy_ratio:.0%} buys")
        elif total_txns < 20:
            score -= 10
            concerns.append("Very few traders active")

        # === Volume as Hype ===
        if vol_24h >= 300000:
            score += 12
            factors.append(f"High trading activity: ${vol_24h:,.0f}")
        elif vol_24h >= 100000:
            score += 7
            factors.append(f"Good activity: ${vol_24h:,.0f}")
        elif vol_24h < 5000:
            score -= 15
            concerns.append("Low activity suggests low interest")

        # === Market Cap Sentiment ===
        if mcap < 1000000:
            score += 8
            factors.append("Small cap - high pump potential")
        elif mcap > 50000000:
            score -= 10
            concerns.append("Large cap - limited upside")

        # === Clamp ===
        score = max(0, min(100, score))

        # === Decision ===
        if score >= 75:
            decision = "STRONG_BUY"
        elif score >= 60:
            decision = "BUY"
        elif score <= 30:
            decision = "REJECT"
        else:
            decision = "WAIT"

        # === Position Size ===
        if decision in ("STRONG_BUY", "BUY"):
            suggested_size = min(3.0, max(0.5, score / 25))
        else:
            suggested_size = 0.0

        confidence = min(0.9, abs(score - 50) / 50 + 0.35)

        total_txns_str = f"{buys}/{total_txns}" if total_txns > 0 else "N/A"
        reasoning = (
            f"Sentiment analysis for {token.symbol}: "
            f"social_score={sent_score}, mentions={mentions}, "
            f"growth={growth:+.1f}%, 5m={chg_5m:+.1f}%, 24h={chg_24h:+.1f}%, "
            f"buy_ratio={total_txns_str}. Score: {score}/100."
        )

        return AgentVerdict(
            agent_name=self.name,
            agent_role=self.role,
            decision=decision,
            confidence=confidence,
            score=score,
            reasoning=reasoning,
            key_factors=factors,
            concerns=concerns,
            suggested_position_size_pct=suggested_size,
            raw_response="rule-based",
        )

    def _rule_based_consult(
        self,
        token,
        analysis_context: Dict[str, Any],
        other_verdicts: List[AgentVerdict],
    ) -> AgentVerdict:
        base = self._rule_based_analyze(token, analysis_context)

        # If technical + liquidity approve but sentiment very negative, be cautious
        tech_ok = any(v.agent_name == "technical_agent" and v.approves for v in other_verdicts)
        liq_ok = any(v.agent_name == "liquidity_agent" and v.approves for v in other_verdicts)
        if tech_ok and liq_ok and base.decision == "REJECT":
            base.decision = "WAIT"
            base.reasoning += " Tech + liquidity approve despite negative sentiment."
            base.score = max(base.score, 40)

        # If sentiment strongly bullish and others are WAIT, nudge toward BUY
        if base.decision == "WAIT" and base.score >= 70:
            bullish_others = sum(1 for v in other_verdicts if v.approves)
            if bullish_others >= 1:
                base.decision = "BUY"
                base.reasoning += " Sentiment bullish with supporting agents."

        return base
