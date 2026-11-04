"""
Ultimate Agent 5: Risk Management Agent (VETO POWER)
Specialty: Position sizing, portfolio risk, circuit breaker, daily/weekly loss limits,
drawdown control, consecutive loss cooldown, exit strategy, final go/no-go decision.
"""
from __future__ import annotations

from typing import Any, Dict

from ...core.config import settings
from ...core.logger import logger
from .base_agent import BaseAgent, AgentVerdict


class RiskAgent(BaseAgent):
    name = "risk_agent"
    role = "Ultimate Risk Manager"
    specialty = "Position sizing, circuit breaker, loss limits, exit strategy"
    enabled_setting = "ai_agent_risk_enabled"
    has_veto = True  # ONLY agent with veto power - protects capital above all

    def _system_prompt(self) -> str:
        return (
            "You are an elite risk manager for Pump.fun trading.\n"
            "Your job: PROTECT CAPITAL above all else. You have VETO power.\n"
            "Check: circuit breaker, daily/weekly loss limits, max drawdown,\n"
            "consecutive losses, open positions count, position sizing.\n"
            "If any critical risk → STRONG_REJECT with VETO.\n"
            "Return JSON: {decision, confidence, score, reasoning, key_factors, concerns, suggested_position_size_pct, veto}"
        )

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        risk = analysis_context.get("risk_state", {}) or {}
        return (
            f"Token: {token.symbol}\n"
            f"Daily PnL %: {risk.get('daily_pnl_pct', 0):.2f}%\n"
            f"Weekly PnL %: {risk.get('weekly_pnl_pct', 0):.2f}%\n"
            f"Max Drawdown %: {risk.get('max_drawdown_pct', 0):.2f}%\n"
            f"Open positions: {risk.get('open_positions_count', 0)}\n"
            f"Consecutive losses: {risk.get('consecutive_losses', 0)}\n"
            f"Cooldown active: {risk.get('cooldown_active', False)}\n"
            f"Circuit breaker: {risk.get('circuit_breaker_active', False)}\n"
            f"Max trade SOL: {settings.pump_max_trade_sol}\n"
            f"Daily cap SOL: {settings.live_daily_cap_sol}\n"
            "Return risk decision JSON."
        )

    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        risk_state = analysis_context.get("risk_state", {}) or {}

        # Extract all agent scores for consensus weighting
        tech_score = analysis_context.get("technical_score", 50) or 50
        liq_score = analysis_context.get("liquidity_score", 50) or 50
        sec_score = analysis_context.get("security_score", 50) or 50
        sent_score = analysis_context.get("sentiment_score", 50) or 50

        score = 50
        factors: List[str] = []
        concerns: List[str] = []
        decision = "WAIT"
        veto = False

        # === CRITICAL RISK CHECKS (immediate veto) ===

        # Circuit breaker active
        if risk_state.get("circuit_breaker_active", False):
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append("🚨 CIRCUIT BREAKER ACTIVE - ALL TRADING HALTED")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # Daily loss limit reached
        daily_pnl = risk_state.get("daily_pnl_pct", 0) or 0
        daily_limit = settings.risk_daily_loss_limit_pct or 5.0
        if daily_pnl <= -daily_limit:
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append(f"🚨 Daily loss limit reached: {daily_pnl:.1f}% <= -{daily_limit}%")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # Weekly loss limit
        weekly_pnl = risk_state.get("weekly_pnl_pct", 0) or 0
        weekly_limit = settings.risk_weekly_loss_limit_pct or 10.0
        if weekly_pnl <= -weekly_limit:
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append(f"🚨 Weekly loss limit reached: {weekly_pnl:.1f}% <= -{weekly_limit}%")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # Max drawdown
        max_dd = risk_state.get("max_drawdown_pct", 0) or 0
        dd_limit = settings.risk_max_drawdown_pct or 20.0
        if max_dd >= dd_limit:
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append(f"🚨 Max drawdown reached: {max_dd:.1f}% >= {dd_limit}%")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # Cooldown after consecutive losses
        consecutive = risk_state.get("consecutive_losses", 0) or 0
        cooldown_after = settings.risk_cooldown_after_losses or 3
        if consecutive >= cooldown_after:
            score = 10
            decision = "WAIT"
            concerns.append(f"⏸️ Cooldown: {consecutive} consecutive losses")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # Max open positions
        open_positions = risk_state.get("open_positions_count", 0) or 0
        max_open = settings.live_max_open_positions or 3
        if open_positions >= max_open:
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append(f"🚨 Max open positions: {open_positions}/{max_open}")
            return self._build_verdict(score, decision, veto, factors, concerns, token)

        # === AGENT CONSENSUS WEIGHTING ===
        agent_scores = [s for s in [tech_score, liq_score, sec_score, sent_score] if s > 0]
        if agent_scores:
            avg_score = sum(agent_scores) / len(agent_scores)
            score += (avg_score - 50) * 0.35  # Weight consensus

            if avg_score >= 75:
                factors.append(f"Strong agent consensus: avg {avg_score:.0f}/100")
            elif avg_score >= 60:
                factors.append(f"Good agent consensus: avg {avg_score:.0f}/100")
            elif avg_score <= 35:
                score -= 15
                concerns.append(f"Weak agent consensus: avg {avg_score:.0f}/100")

        # === POSITION SIZING ===
        base_risk_pct = settings.risk_max_risk_per_trade_pct or 2.0  # % of capital per trade

        # Reduce size after losses
        if consecutive >= 1:
            size_multiplier = 0.4
            factors.append(f"Reducing size after {consecutive} loss(es)")
        else:
            size_multiplier = 1.0

        # Score-based adjustment
        if score >= 80:
            size_multiplier *= 1.0
        elif score >= 65:
            size_multiplier *= 0.75
        elif score >= 55:
            size_multiplier *= 0.5
        else:
            size_multiplier *= 0.25

        suggested_size = base_risk_pct * size_multiplier
        suggested_size = max(0.0, min(5.0, suggested_size))  # Hard cap 5%

        # === FINAL DECISION (if not already vetoed) ===
        if not veto:
            if score >= 70:
                decision = "STRONG_BUY"
            elif score >= 55:
                decision = "BUY"
            elif score <= 25:
                decision = "REJECT"
            else:
                decision = "WAIT"

            # Downgrade if any critical concerns
            if any("🚨" in c for c in concerns):
                decision = "REJECT"
                veto = True

        confidence = min(0.95, abs(score - 50) / 50 + 0.45)

        reasoning = (
            f"Risk analysis for {token.symbol}: "
            f"Daily PnL={daily_pnl:+.1f}%, Weekly={weekly_pnl:+.1f}%, "
            f"Drawdown={max_dd:.1f}%, Consecutive={consecutive}, "
            f"Positions={open_positions}/{max_open}, "
            f"Agent consensus avg={avg_score if agent_scores else 'N/A'}. "
            f"Score: {score}/100. Suggested size: {suggested_size:.1f}%."
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
            veto=veto,
            raw_response="rule-based",
        )

    def _build_verdict(
        self, score: int, decision: str, veto: bool,
        factors: List[str], concerns: List[str], token
    ) -> AgentVerdict:
        return AgentVerdict(
            agent_name=self.name,
            agent_role=self.role,
            decision=decision,
            confidence=0.95,
            score=score,
            reasoning=" | ".join(concerns) if concerns else f"Risk decision for {token.symbol}",
            key_factors=factors,
            concerns=concerns,
            suggested_position_size_pct=0.0,
            veto=veto,
            raw_response="rule-based",
        )

    def _rule_based_consult(
        self,
        token,
        analysis_context: Dict[str, Any],
        other_verdicts: List[AgentVerdict],
    ) -> AgentVerdict:
        """
        FINAL CONSULTATION: Risk agent has FINAL SAY with veto power.
        Even if all other agents say BUY, risk agent can VETO.
        """
        base = self._rule_based_analyze(token, analysis_context)

        # Count other agents' decisions
        approvals = sum(1 for v in other_verdicts if v.approves)
        strong_rejects = sum(1 for v in other_verdicts if v.decision == "STRONG_REJECT")
        total_others = len(other_verdicts)

        # RISK AGENT HAS FINAL SAY:

        # 1. Any veto from others (shouldn't happen but safety)
        other_vetoes = sum(1 for v in other_verdicts if v.veto)
        if other_vetoes > 0:
            base.decision = "REJECT"
            base.veto = True
            base.reasoning += f" Overruled by {other_vetoes} veto(s)."
            return base

        # 2. All other agents STRONG_REJECT → reject
        if total_others > 0 and strong_rejects == total_others:
            base.decision = "REJECT"
            base.veto = True
            base.reasoning += " All other agents strongly rejected."
            return base

        # 3. Majority rejects → reject (risk-averse)
        if total_others > 0 and (total_others - approvals) > approvals:
            base.decision = "REJECT"
            base.reasoning += " Majority of agents rejected - risk override."
            base.score = min(base.score, 30)
            return base

        # 4. ALL agents approve AND risk score good → STRONG_BUY with full confidence
        if total_others > 0 and approvals == total_others and base.score >= 60:
            base.decision = "STRONG_BUY"
            base.reasoning += " UNANIMOUS agent approval - final risk clearance."
            base.confidence = min(0.95, base.confidence + 0.1)

        # 5. No agents approve → reject
        if total_others > 0 and approvals == 0:
            base.decision = "REJECT"
            base.reasoning += " No agents approved - risk override."
            base.score = min(base.score, 20)
            return base

        return base
