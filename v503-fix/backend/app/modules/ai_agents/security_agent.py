"""
Ultimate Agent 2: Security Analysis Agent
Specialty: Honeypot detection, rug pull prevention, contract safety,
ownership renounced, mint/freeze authority, holder concentration, LP lock.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...core.config import settings
from ...core.logger import logger
from .base_agent import BaseAgent, AgentVerdict


class SecurityAgent(BaseAgent):
    name = "security_agent"
    role = "Ultimate Security Analyst"
    specialty = "Honeypot, rug pull, contract safety, authority checks"
    enabled_setting = "ai_agent_security_enabled"
    has_veto = True  # Security can veto to prevent catastrophic loss

    def _system_prompt(self) -> str:
        return (
            "You are an elite smart contract security expert for Solana memecoins.\n"
            "CRITICAL: Detect honeypots, rug pulls, malicious ownership, mint authority,\n"
            "freeze authority, LP lock status, holder concentration.\n"
            "If ANY critical danger detected → STRONG_REJECT with VETO.\n"
            "Return JSON: {decision, confidence, score, reasoning, key_factors, concerns, suggested_position_size_pct, veto}"
        )

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        sec = analysis_context.get("security", {}) or {}
        onchain = analysis_context.get("onchain", {}) or {}
        return (
            f"Token: {token.symbol}\n"
            f"Honeypot: {sec.get('is_honeypot', False)}\n"
            f"Ownership renounced: {sec.get('ownership_renounced', False)}\n"
            f"Mint authority disabled: {sec.get('mint_authority_disabled', False)}\n"
            f"Freeze authority disabled: {sec.get('freeze_authority_disabled', False)}\n"
            f"LP locked: {sec.get('lp_locked', False)}\n"
            f"Top holders concentration: {onchain.get('top_holders_concentration', 0)}%\n"
            f"Verified: {sec.get('is_verified', False)}\n"
            "Return security decision JSON."
        )

    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        sec = analysis_context.get("security", {}) or {}
        onchain = analysis_context.get("onchain", {}) or {}

        score = 50
        factors: List[str] = []
        concerns: List[str] = []
        decision = "WAIT"
        veto = False

        # === CRITICAL: Honeypot ===
        is_honeypot = sec.get("is_honeypot", False)
        if is_honeypot:
            score = 0
            veto = True
            decision = "REJECT"
            concerns.append("🚨 HONEYPOT DETECTED - Cannot sell tokens!")
            return self._build_verdict(score, decision, veto, factors, concerns, token)
        else:
            factors.append("No honeypot detected")
            score += 20

        # === Ownership Renounced (tri-state: True / False / unknown) ===
        ownership_renounced = sec.get("ownership_renounced")
        if ownership_renounced is True:
            score += 20
            factors.append("Ownership renounced (dev cannot rug)")
        elif ownership_renounced is False:
            score -= 20
            concerns.append("Ownership NOT renounced (dev has control)")
        else:
            factors.append("Ownership status unknown (neutral)")

        # === Mint Authority ===
        # Tri-state: True (disabled, safe) / False (active, dangerous) / None (unknown)
        mint_disabled = sec.get("mint_authority_disabled")
        if mint_disabled is True:
            score += 15
            factors.append("Mint authority disabled (no new tokens)")
        elif mint_disabled is False:
            score -= 15
            concerns.append("Mint authority ACTIVE (inflation/rug risk)")
        else:
            factors.append("Mint authority status unknown (neutral)")

        # === Freeze Authority ===
        # Tri-state: True (disabled, safe) / False (active, dangerous) / None (unknown)
        freeze_disabled = sec.get("freeze_authority_disabled")
        if freeze_disabled is True:
            score += 10
            factors.append("Freeze authority disabled")
        elif freeze_disabled is False:
            score -= 10
            concerns.append("Freeze authority ACTIVE (accounts can be frozen)")
        else:
            factors.append("Freeze authority status unknown (neutral)")

        # === LP Locked (tri-state: True / False / unknown) ===
        lp_locked = sec.get("lp_locked")
        if lp_locked is True:
            score += 15
            factors.append("LP tokens locked (rug protection)")
        elif lp_locked is False:
            score -= 20
            concerns.append("LP NOT locked (dev can withdraw liquidity)")
        else:
            factors.append("LP lock status unknown (neutral)")

        # === Contract Verified ===
        is_verified = sec.get("is_verified", False)
        if is_verified:
            score += 10
            factors.append("Contract verified")
        else:
            score -= 5
            concerns.append("Contract not verified")

        # === Holder Concentration ===
        top_holders = onchain.get("top_holders_concentration", 0) or 0
        if top_holders > 0:
            if top_holders >= 60:
                score -= 25
                concerns.append(f"🚨 Extreme holder concentration: top holders own {top_holders}%")
            elif top_holders >= 40:
                score -= 15
                concerns.append(f"High holder concentration: {top_holders}%")
            elif top_holders >= 25:
                score -= 8
                concerns.append(f"Moderate concentration: {top_holders}%")
            else:
                score += 5
                factors.append(f"Good holder distribution: {top_holders}%")

        # === Clamp ===
        score = max(0, min(100, score))

        # === Decision ===
        critical = is_honeypot or (lp_locked is False) or (top_holders >= 60)
        if critical:
            decision = "REJECT"
            veto = True
            if lp_locked is False:
                concerns.append("🔴 LP unlocked - rug pull possible")
            if top_holders >= 60:
                concerns.append("🔴 Extreme holder concentration")
        elif score >= 70:
            decision = "BUY"
        elif score >= 50:
            decision = "WAIT"
        else:
            decision = "REJECT"

        # === Position Size ===
        # Security cap stays conservative (3%) but no longer bottlenecks the
        # final size as severely; the risk agent still applies the final cap.
        if decision in ("BUY", "STRONG_BUY"):
            suggested_size = min(5.0, max(0.5, score / 20))
        else:
            suggested_size = 0.0

        confidence = min(0.95, abs(score - 50) / 50 + 0.4)

        reasoning = (
            f"Security analysis for {token.symbol}: "
            f"Honeypot={is_honeypot}, Ownership={ownership_renounced}, "
            f"Mint={mint_disabled}, Freeze={freeze_disabled}, "
            f"LP Locked={lp_locked}, Verified={is_verified}, "
            f"Top Holders={top_holders}%. Score: {score}/100."
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
            reasoning=" | ".join(concerns) if concerns else f"Security decision for {token.symbol}",
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
        base = self._rule_based_analyze(token, analysis_context)

        # Security agent has FINAL veto - if it says reject, nothing overrides
        if base.veto:
            return base

        # If any other agent detected critical issues, be extra cautious
        critical = any(
            "honeypot" in c.lower() or "rug" in c.lower() or "critical" in c.lower()
            for v in other_verdicts for c in v.concerns
        )
        if critical and base.decision in ("BUY", "STRONG_BUY"):
            base.decision = "WAIT"
            base.reasoning += " Other agents raised critical security flags."
            base.score = max(0, base.score - 20)

        return base
