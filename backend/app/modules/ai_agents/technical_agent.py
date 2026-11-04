"""
Ultimate Agent 1: Technical Analysis Agent
Specialty: Price action, momentum, volume, RSI, trend, support/resistance,
bonding curve analysis, pump.fun-specific patterns.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...core.config import settings
from ...core.logger import logger
from .base_agent import BaseAgent, AgentVerdict


class TechnicalAgent(BaseAgent):
    name = "technical_agent"
    role = "Ultimate Technical Analyst"
    specialty = "Price action, momentum, volume, RSI, trend, bonding curve"
    enabled_setting = "ai_agent_technical_enabled"
    has_veto = False

    def _system_prompt(self) -> str:
        return (
            "You are an elite technical analysis expert for Pump.fun memecoins on Solana.\n"
            "Analyze: price momentum, volume spikes, price action patterns, bonding curve progress,\n"
            "support/resistance, trend direction, reversal signals.\n"
            "Return JSON: {decision, confidence, score, reasoning, key_factors, concerns, suggested_position_size_pct}\n"
            "Score 0-100. BUY/STRONG_BUY for strong setups, WAIT for unclear, REJECT/STRONG_REJECT for dangerous."
        )

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        return (
            f"Token: {token.symbol} ({token.address[:8]}...)\n"
            f"Price: ${token.price_usd or 0}\n"
            f"5m change: {token.price_change_5m_pct or 0}%\n"
            f"24h change: {token.price_change_24h_pct or 0}%\n"
            f"24h volume: ${token.volume_24h_usd or 0}\n"
            f"5m volume: ${token.volume_5m_usd or 0}\n"
            f"Liquidity: ${token.liquidity_usd or 0}\n"
            f"Market cap: ${token.market_cap_usd or 0}\n"
            f"Bonding curve: {analysis_context.get('bonding_curve_progress', 'unknown')}%\n"
            f"Buy txns: {token.txns_24h_buys or 0}, Sell txns: {token.txns_24h_sells or 0}\n"
            "Return trading decision JSON."
        )

    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        price = token.price_usd or 0
        chg_5m = token.price_change_5m_pct or 0
        chg_24h = token.price_change_24h_pct or 0
        vol_24h = token.volume_24h_usd or 0
        vol_5m = token.volume_5m_usd or 0
        liq = token.liquidity_usd or 0
        mcap = token.market_cap_usd or 0
        buys = token.txns_24h_buys or 0
        sells = token.txns_24h_sells or 0
        curve = analysis_context.get("bonding_curve_progress")
        curve = curve if curve is not None else 0

        score = 50
        factors: List[str] = []
        concerns: List[str] = []
        decision = "WAIT"

        # === Momentum Analysis ===
        if chg_5m >= 20:
            score += 25
            factors.append(f"Explosive 5m momentum: +{chg_5m:.1f}%")
        elif chg_5m >= 10:
            score += 15
            factors.append(f"Strong 5m momentum: +{chg_5m:.1f}%")
        elif chg_5m >= 3:
            score += 8
            factors.append(f"Positive 5m: +{chg_5m:.1f}%")
        elif chg_5m <= -10:
            score -= 25
            concerns.append(f"Sharp 5m drop: {chg_5m:.1f}%")
        elif chg_5m <= -3:
            score -= 10
            concerns.append(f"Weak 5m: {chg_5m:.1f}%")

        # === 24h Trend ===
        if chg_24h >= 50:
            score += 20
            factors.append(f"Parabolic 24h trend: +{chg_24h:.1f}%")
        elif chg_24h >= 20:
            score += 12
            factors.append(f"Strong 24h uptrend: +{chg_24h:.1f}%")
        elif chg_24h >= 5:
            score += 6
            factors.append(f"Positive 24h: +{chg_24h:.1f}%")
        elif chg_24h <= -50:
            score -= 30
            concerns.append(f"Crashing 24h: {chg_24h:.1f}%")
        elif chg_24h <= -20:
            score -= 15
            concerns.append(f"Weak 24h: {chg_24h:.1f}%")

        # === Volume Analysis ===
        if vol_24h >= 500000:
            score += 15
            factors.append(f"High 24h volume: ${vol_24h:,.0f}")
        elif vol_24h >= 100000:
            score += 10
            factors.append(f"Good volume: ${vol_24h:,.0f}")
        elif vol_24h >= 20000:
            score += 5
            factors.append(f"Moderate volume: ${vol_24h:,.0f}")
        elif vol_24h < 2000:
            score -= 20
            concerns.append(f"Critically low volume: ${vol_24h:,.0f}")

        # === 5m Volume Spike (urgent signal) ===
        if vol_24h > 0 and vol_5m > vol_24h * 0.5:
            score += 15
            factors.append("Major 5m volume spike (>50% of 24h volume)")
        elif vol_24h > 0 and vol_5m > vol_24h * 0.2:
            score += 8
            factors.append("Notable 5m volume activity")

        # === Liquidity ===
        if liq >= 100000:
            score += 12
            factors.append(f"Excellent liquidity: ${liq:,.0f}")
        elif liq >= 50000:
            score += 8
            factors.append(f"Good liquidity: ${liq:,.0f}")
        elif liq >= 10000:
            score += 4
            factors.append(f"Adequate liquidity: ${liq:,.0f}")
        elif liq < 2000:
            score -= 25
            concerns.append(f"Dangerously low liquidity: ${liq:,.0f}")

        # === Market Cap Sweet Spot for Pump.fun ===
        if 100000 <= mcap <= 5000000:
            score += 10
            factors.append(f"Sweet spot mcap: ${mcap:,.0f}")
        elif mcap < 50000:
            score -= 8
            concerns.append(f"Very small mcap: ${mcap:,.0f}")
        elif mcap > 50000000:
            score -= 15
            concerns.append(f"Mcap too high (likely graduated): ${mcap:,.0f}")

        # === Buy/Sell Pressure ===
        total_txns = buys + sells
        if total_txns > 0:
            buy_ratio = buys / total_txns
            if buy_ratio >= 0.75:
                score += 12
                factors.append(f"Strong buying pressure: {buy_ratio:.0%} buys")
            elif buy_ratio >= 0.6:
                score += 6
                factors.append(f"Net buying: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.25:
                score -= 20
                concerns.append(f"Heavy selling pressure: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.4:
                score -= 10
                concerns.append(f"More sells: {buy_ratio:.0%} buys")

        # === Bonding Curve Progress ===
        if curve is not None:
            if curve >= 80:
                score -= 10
                concerns.append(f"Curve near graduation ({curve:.0f}%) - limited upside")
            elif curve >= 50:
                score += 0  # neutral
                factors.append(f"Curve mid-stage ({curve:.0f}%)")
            elif curve <= 10:
                score += 5
                factors.append(f"Early curve stage ({curve:.0f}%) - high upside potential")
            else:
                factors.append(f"Curve progress: {curve:.0f}%")

        # === Price Sanity ===
        if price <= 0:
            score -= 100
            concerns.append("Invalid price")

        # === Clamp ===
        score = max(0, min(100, score))

        # === Decision ===
        if score >= 80:
            decision = "STRONG_BUY"
        elif score >= 65:
            decision = "BUY"
        elif score <= 25:
            decision = "REJECT"
        elif score <= 40:
            decision = "WAIT"
        else:
            decision = "WAIT"

        # === Position Size ===
        if decision in ("STRONG_BUY", "BUY"):
            suggested_size = min(5.0, max(0.5, score / 20))
        else:
            suggested_size = 0.0

        confidence = min(0.95, abs(score - 50) / 50 + 0.35)

        reasoning = (
            f"Technical analysis for {token.symbol}: "
            f"5m={chg_5m:+.1f}%, 24h={chg_24h:+.1f}%, vol24h=${vol_24h:,.0f}, "
            f"vol5m=${vol_5m:,.0f}, liq=${liq:,.0f}, mcap=${mcap:,.0f}, "
            f"curve={curve}%, buys/sells={buys}/{sells}. Score: {score}/100."
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

        approvals = sum(1 for v in other_verdicts if v.approves)
        strong_rejects = sum(1 for v in other_verdicts if v.decision == "STRONG_REJECT")
        vetoes = sum(1 for v in other_verdicts if v.veto)

        if vetoes > 0:
            base.decision = "REJECT"
            base.reasoning += f" Overruled by {vetoes} veto(s)."
            base.score = min(base.score, 20)
            return base

        if approvals >= 3 and base.decision == "WAIT":
            base.decision = "BUY"
            base.reasoning += " Multi-agent consensus supports entry."
            base.score = min(100, base.score + 10)

        if strong_rejects >= 2 and base.decision in ("BUY", "STRONG_BUY"):
            base.decision = "WAIT"
            base.reasoning += " Multiple strong rejections - caution."
            base.score = max(0, base.score - 15)

        return base
