"""
Ultimate Agent 3: Liquidity Analysis Agent
Specialty: Liquidity depth, volume analysis, buy/sell pressure,
market cap vs liquidity ratio, trading activity, entry/exit viability.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...core.config import settings
from ...core.logger import logger
from .base_agent import BaseAgent, AgentVerdict


class LiquidityAgent(BaseAgent):
    name = "liquidity_agent"
    role = "Ultimate Liquidity Analyst"
    specialty = "Liquidity depth, volume, buy/sell pressure, mcap/liq ratio"
    enabled_setting = "ai_agent_liquidity_enabled"
    has_veto = True

    def _system_prompt(self) -> str:
        return (
            "You are an elite liquidity analyst for Pump.fun memecoins.\n"
            "Analyze: liquidity depth, 24h volume, volume/liq ratio, buy vs sell pressure,\n"
            "market cap vs liquidity, recent activity, entry/exit viability.\n"
            "Return JSON: {decision, confidence, score, reasoning, key_factors, concerns, suggested_position_size_pct}"
        )

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        return (
            f"Token: {token.symbol}\n"
            f"Liquidity: ${token.liquidity_usd or 0}\n"
            f"24h Volume: ${token.volume_24h_usd or 0}\n"
            f"5m Volume: ${token.volume_5m_usd or 0}\n"
            f"Market Cap: ${token.market_cap_usd or 0}\n"
            f"Buys: {token.txns_24h_buys or 0}, Sells: {token.txns_24h_sells or 0}\n"
            f"24h Change: {token.price_change_24h_pct or 0}%\n"
            "Return liquidity decision JSON."
        )

    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        liq_usd = token.liquidity_usd or 0
        vol_24h = token.volume_24h_usd or 0
        vol_5m = token.volume_5m_usd or 0
        mcap = token.market_cap_usd or 0
        chg_24h = token.price_change_24h_pct or 0

        # Optional transaction counts: None = unknown (neutral), 0 = no activity
        raw_buys = getattr(token, "txns_24h_buys", None)
        raw_sells = getattr(token, "txns_24h_sells", None)
        txns_known = raw_buys is not None and raw_sells is not None
        buys = int(raw_buys or 0)
        sells = int(raw_sells or 0)

        score = 50
        factors: List[str] = []
        concerns: List[str] = []
        decision = "WAIT"

        # === Liquidity Depth ===
        if liq_usd >= 200000:
            score += 25
            factors.append(f"Deep liquidity: ${liq_usd:,.0f}")
        elif liq_usd >= 100000:
            score += 18
            factors.append(f"Strong liquidity: ${liq_usd:,.0f}")
        elif liq_usd >= 50000:
            score += 12
            factors.append(f"Good liquidity: ${liq_usd:,.0f}")
        elif liq_usd >= 10000:
            score += 5
            factors.append(f"Adequate liquidity: ${liq_usd:,.0f}")
        elif liq_usd < 2000:
            score -= 30
            concerns.append(f"🚨 Critically low liquidity: ${liq_usd:,.0f}")

        # === Volume Analysis ===
        if vol_24h >= 1000000:
            score += 20
            factors.append(f"Massive 24h volume: ${vol_24h:,.0f}")
        elif vol_24h >= 500000:
            score += 15
            factors.append(f"High 24h volume: ${vol_24h:,.0f}")
        elif vol_24h >= 100000:
            score += 10
            factors.append(f"Good volume: ${vol_24h:,.0f}")
        elif vol_24h >= 20000:
            score += 5
            factors.append(f"Moderate volume: ${vol_24h:,.0f}")
        elif vol_24h < 2000:
            score -= 25
            concerns.append(f"🚨 Almost no volume: ${vol_24h:,.0f}")

        # === Volume to Liquidity Ratio ===
        if liq_usd > 0:
            ratio = vol_24h / liq_usd
            if ratio >= 3:
                score += 15
                factors.append(f"Very active: volume is {ratio:.1f}x liquidity")
            elif ratio >= 1:
                score += 10
                factors.append(f"Active: volume is {ratio:.1f}x liquidity")
            elif ratio >= 0.3:
                score += 5
                factors.append(f"Decent activity: {ratio:.1f}x liquidity")
            elif ratio < 0.05:
                score -= 15
                concerns.append(f"Stagnant: volume only {ratio:.2f}x liquidity")

        # === Recent 5m Activity ===
        if vol_5m >= 50000:
            score += 12
            factors.append(f"Strong 5m volume: ${vol_5m:,.0f}")
        elif vol_5m >= 10000:
            score += 7
            factors.append(f"Good 5m volume: ${vol_5m:,.0f}")
        elif vol_5m >= 2000:
            score += 3
            factors.append(f"Some 5m activity: ${vol_5m:,.0f}")
        elif vol_5m < 100:
            score -= 15
            concerns.append("No recent trading (5m volume near zero)")

        # === Buy/Sell Pressure ===
        # Unknown counts (None) are neutral — do not penalize missing data.
        total_txns = buys + sells
        if not txns_known:
            factors.append("Transaction counts unknown (neutral)")
        elif total_txns > 50:
            buy_ratio = buys / total_txns
            if buy_ratio >= 0.7:
                score += 15
                factors.append(f"Strong buying pressure: {buy_ratio:.0%} buys")
            elif buy_ratio >= 0.6:
                score += 8
                factors.append(f"Net buying: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.3:
                score -= 20
                concerns.append(f"Heavy selling pressure: {buy_ratio:.0%} buys")
            elif buy_ratio <= 0.4:
                score -= 10
                concerns.append(f"Selling pressure: {buy_ratio:.0%} buys")
        elif total_txns < 10:
            score -= 10
            concerns.append("Very few transactions")

        # === Market Cap vs Liquidity ===
        if liq_usd > 0 and mcap > 0:
            ratio = mcap / liq_usd
            if 1 <= ratio <= 10:
                score += 10
                factors.append(f"Healthy mcap/liq: {ratio:.1f}x")
            elif ratio > 50:
                score -= 20
                concerns.append(f"🚨 Mcap too high vs liq: {ratio:.1f}x")
            elif ratio > 20:
                score -= 10
                concerns.append(f"High mcap/liq ratio: {ratio:.1f}x")
            elif ratio < 1:
                score += 5
                factors.append("Liquidity exceeds market cap (very safe)")

        # === Price Trend ===
        if chg_24h >= 30:
            score += 10
            factors.append(f"Strong 24h uptrend: +{chg_24h:.1f}%")
        elif chg_24h >= 10:
            score += 5
            factors.append(f"Positive 24h: +{chg_24h:.1f}%")
        elif chg_24h <= -30:
            score -= 20
            concerns.append(f"Severe 24h decline: {chg_24h:.1f}%")
        elif chg_24h <= -10:
            score -= 10
            concerns.append(f"Negative 24h trend: {chg_24h:.1f}%")

        # === Clamp ===
        score = max(0, min(100, score))

        # === Decision ===
        if score >= 80:
            decision = "STRONG_BUY"
        elif score >= 65:
            decision = "BUY"
        elif score <= 30:
            decision = "REJECT"
        else:
            decision = "WAIT"

        # === Position Size ===
        if decision in ("STRONG_BUY", "BUY"):
            suggested_size = min(4.0, max(0.5, score / 22))
        else:
            suggested_size = 0.0

        confidence = min(0.95, abs(score - 50) / 50 + 0.35)

        reasoning = (
            f"Liquidity analysis for {token.symbol}: "
            f"liq=${liq_usd:,.0f}, vol24h=${vol_24h:,.0f}, vol5m=${vol_5m:,.0f}, "
            f"mcap=${mcap:,.0f}, buys={buys}, sells={sells}, "
            f"24h={chg_24h:+.1f}%. Score: {score}/100."
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

        # If liquidity is critically low, override other agents' approval
        token_liq = token.liquidity_usd or 0
        if base.decision == "REJECT" or token_liq < 5000:
            base.decision = "REJECT"
            base.reasoning += " Liquidity too low for safe trading."
            base.score = min(base.score, 15)

        # If technical + sentiment both strong but liquidity mediocre, reduce size
        tech_approve = any(v.agent_name == "technical_agent" and v.approves for v in other_verdicts)
        sent_approve = any(v.agent_name == "sentiment_agent" and v.approves for v in other_verdicts)
        if tech_approve and sent_approve and base.decision == "WAIT":
            base.decision = "BUY"
            base.reasoning += " Tech + sentiment support entry despite moderate liquidity."
            base.score = min(100, base.score + 5)

        return base
