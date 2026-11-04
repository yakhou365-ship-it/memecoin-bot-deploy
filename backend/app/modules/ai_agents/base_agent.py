"""
Base agent class - shared structure for all Ultimate AI agents.

Each agent:
1. Receives token context + analysis results
2. Analyzes using intelligent RULE-BASED logic (no external LLM dependency needed)
3. Can optionally enhance with LLM if API keys are available
4. Returns structured verdict with decision, confidence, score, reasoning, veto
5. Supports consultation phase to see other agents' opinions
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from ...core.config import settings
from ...core.logger import logger
from .llm_provider import get_llm


@dataclass
class AgentVerdict:
    """Structured output from each agent."""
    agent_name: str
    agent_role: str
    decision: str = "WAIT"  # BUY | WAIT | REJECT | STRONG_BUY | STRONG_REJECT
    confidence: float = 0.0  # 0.0 - 1.0
    score: int = 50  # 0-100, higher = more bullish
    reasoning: str = ""
    key_factors: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    suggested_position_size_pct: float = 0.0
    raw_response: str = ""
    veto: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)

    @property
    def approves(self) -> bool:
        return self.decision in ("BUY", "STRONG_BUY")


class BaseAgent:
    """Ultimate base class for all AI agents with rule-based intelligence."""

    name: str = "base_agent"
    role: str = "Base Agent"
    specialty: str = ""
    enabled_setting: str = "ai_agent_technical_enabled"
    has_veto: bool = False

    def __init__(self):
        self.llm = get_llm()
        self._use_llm = bool(getattr(settings, "openrouter_api_key", "") or getattr(settings, "nvidia_api_key", ""))

    @property
    def is_enabled(self) -> bool:
        return getattr(settings, self.enabled_setting, True)

    # =====================================================================
    # Public API
    # =====================================================================
    async def analyze(
        self,
        token,
        analysis_context: Dict[str, Any],
    ) -> AgentVerdict:
        if not self.is_enabled:
            return AgentVerdict(
                agent_name=self.name,
                agent_role=self.role,
                decision="WAIT",
                reasoning=f"Agent {self.name} is disabled",
                raw_response="disabled",
            )

        try:
            # PRIMARY: Rule-based analysis (works without external LLM keys)
            # This is the AUTHORITATIVE verdict. LLM enhancement may only
            # enrich the reasoning text - it must NEVER override the
            # rule-based decision, score, or veto. Otherwise a failed LLM
            # call (timeout, parse error, fallback) would silently replace
            # a correct BUY/REJECT with a default WAIT.
            verdict = self._rule_based_analyze(token, analysis_context)
            logger.debug(f"Agent {self.name} rule-based: {verdict.decision} score={verdict.score}")

            # OPTIONAL ENHANCEMENT: LLM may add a natural-language narrative
            # on top of the rule-based verdict. We do NOT touch decision /
            # score / suggested_size / veto - those are always rule-based.
            if self._use_llm:
                try:
                    prompt = self._build_prompt(token, analysis_context)
                    response = await self.llm.chat_json(
                        system_prompt=self._system_prompt(),
                        user_prompt=prompt,
                    )
                    llm_verdict = self._parse_response(response)
                    # Only adopt LLM's reasoning text if it is meaningful.
                    # The fallback returns "تم اكتشاف عملات..." - skip that.
                    llm_reasoning = (llm_verdict.reasoning or "").strip()
                    fallback_markers = (
                        "تم اكتشاف",
                        "لا توجد صفقات",
                        "5 وكلاء AI",
                        "مرحباً! أنا مساعد",
                        "تحليل أساسي",
                        "لا يوجد مزود AI",
                    )
                    looks_like_fallback = any(m in llm_reasoning for m in fallback_markers)
                    if llm_reasoning and not looks_like_fallback:
                        verdict.reasoning = llm_reasoning
                        if llm_verdict.key_factors:
                            verdict.key_factors = list(verdict.key_factors) + [
                                f for f in llm_verdict.key_factors
                                if f not in verdict.key_factors
                            ][:3]  # max 3 LLM factors appended
                        if llm_verdict.concerns:
                            verdict.concerns = list(verdict.concerns) + [
                                c for c in llm_verdict.concerns
                                if c not in verdict.concerns
                            ][:3]
                    verdict.raw_response = "rule-based+llm"
                except Exception as llm_exc:
                    logger.debug(f"LLM enhancement skipped for {self.name}: {str(llm_exc)[:120]}")

            return verdict

        except Exception as exc:
            logger.exception(f"Agent {self.name} failed: {exc}")
            return AgentVerdict(
                agent_name=self.name,
                agent_role=self.role,
                decision="WAIT",
                reasoning=f"Agent error: {str(exc)[:200]}",
                raw_response=str(exc),
            )

    async def consult(
        self,
        token,
        analysis_context: Dict[str, Any],
        other_verdicts: List[AgentVerdict],
    ) -> AgentVerdict:
        if not self.is_enabled:
            return AgentVerdict(
                agent_name=self.name,
                agent_role=self.role,
                decision="WAIT",
                reasoning=f"Agent {self.name} is disabled",
            )

        try:
            verdict = self._rule_based_consult(token, analysis_context, other_verdicts)
            return verdict
        except Exception as exc:
            logger.exception(f"Agent {self.name} consultation failed: {exc}")
            return AgentVerdict(
                agent_name=self.name,
                agent_role=self.role,
                decision="WAIT",
                reasoning=f"Consultation error: {str(exc)[:200]}",
            )

    # =====================================================================
    # Abstract methods
    # =====================================================================
    def _system_prompt(self) -> str:
        raise NotImplementedError

    def _build_prompt(self, token, analysis_context: Dict) -> str:
        raise NotImplementedError

    def _build_consultation_prompt(
        self, token, analysis_context: Dict, others: List[AgentVerdict]
    ) -> str:
        return f"Token: {token.symbol}. Re-evaluate based on other agents' opinions."

    def _parse_response(self, response: Dict[str, Any]) -> AgentVerdict:
        decision = str(response.get("decision", "WAIT")).upper()
        if decision not in ("BUY", "WAIT", "REJECT", "STRONG_BUY", "STRONG_REJECT"):
            decision = "WAIT"

        confidence = float(response.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        score = int(response.get("score", 50))
        score = max(0, min(100, score))

        return AgentVerdict(
            agent_name=self.name,
            agent_role=self.role,
            decision=decision,
            confidence=confidence,
            score=score,
            reasoning=str(response.get("reasoning", "")),
            key_factors=list(response.get("key_factors", [])),
            concerns=list(response.get("concerns", [])),
            suggested_position_size_pct=float(response.get("suggested_position_size_pct", 0)),
            raw_response=str(response),
            veto=bool(response.get("veto", False)),
        )

    # =====================================================================
    # Rule-based analysis (implemented by subclasses)
    # =====================================================================
    def _rule_based_analyze(
        self, token, analysis_context: Dict[str, Any]
    ) -> AgentVerdict:
        raise NotImplementedError

    def _rule_based_consult(
        self,
        token,
        analysis_context: Dict[str, Any],
        other_verdicts: List[AgentVerdict],
    ) -> AgentVerdict:
        raise NotImplementedError
