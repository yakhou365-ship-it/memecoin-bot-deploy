"""
Agent Orchestrator - coordinates the 5 AI agents.

Workflow:
1. All 5 agents analyze in PARALLEL (each from its own angle)
2. They see each other's verdicts (consultation phase)
3. Risk Agent makes final decision
4. Apply consensus rule: ALL agents must approve → execute
   If even one rejects → abort
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import SystemState
from .base_agent import AgentVerdict
from .technical_agent import TechnicalAgent
from .security_agent import SecurityAgent
from .liquidity_agent import LiquidityAgent
from .sentiment_agent import SentimentAgent
from .risk_agent import RiskAgent


@dataclass
class ConsensusResult:
    """Final result of agent consensus."""
    approved: bool
    unanimity: bool  # True if ALL agents approved
    approvals_count: int
    rejections_count: int
    total_agents: int
    verdicts: List[AgentVerdict] = field(default_factory=list)
    final_decision: str = "REJECT"  # EXECUTE | REJECT | WAIT
    final_position_size_pct: float = 0.0
    final_reasoning: str = ""
    consultation_log: List[Dict] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


class AgentOrchestrator:
    """Coordinates 5 AI agents for trade decisions."""

    def __init__(self):
        self.agents = [
            TechnicalAgent(),
            SecurityAgent(),
            LiquidityAgent(),
            SentimentAgent(),
            RiskAgent(),
        ]
        self._enabled_agents = [a for a in self.agents if a.is_enabled]

    async def run_consensus(
        self,
        token,
        analysis_context: Dict[str, Any],
    ) -> ConsensusResult:
        """
        Run the full agent consensus workflow.

        Returns ConsensusResult with the final decision.
        """
        logger.info(
            f"🤖 Starting AI agent consensus for {token.symbol} "
            f"({len(self._enabled_agents)} agents active)"
        )

        # === Phase 1: Parallel analysis ===
        phase1_tasks = [
            agent.analyze(token, analysis_context)
            for agent in self._enabled_agents
        ]
        phase1_verdicts = await asyncio.gather(*phase1_tasks, return_exceptions=False)

        logger.info(
            f"Phase 1 complete: {sum(1 for v in phase1_verdicts if v.approves)}/"
            f"{len(phase1_verdicts)} agents approved"
        )

        # === Phase 2: Consultation (each agent sees others) ===
        phase2_tasks = []
        for i, agent in enumerate(self._enabled_agents):
            others = [v for j, v in enumerate(phase1_verdicts) if j != i]
            phase2_tasks.append(agent.consult(token, analysis_context, others))

        phase2_verdicts = await asyncio.gather(*phase2_tasks, return_exceptions=False)

        logger.info(
            f"Phase 2 (consultation) complete: "
            f"{sum(1 for v in phase2_verdicts if v.approves)}/"
            f"{len(phase2_verdicts)} agents approved after consultation"
        )

        # === Phase 3: Compute consensus ===
        approvals = [v for v in phase2_verdicts if v.approves]
        rejections = [v for v in phase2_verdicts if not v.approves]
        unanimity = len(rejections) == 0

        # V31.1 MAJORITY RULE: 3/5 approval is enough (was: unanimity 5/5)
        # This makes the bot trade much more often
        # Still safe: if any agent has veto=true, we reject
        has_veto = any(v.veto for v in phase2_verdicts)
        majority_threshold = max(3, len(phase2_verdicts) // 2 + 1)  # 3/5 or majority

        # Apply the rule: majority approves AND no veto → EXECUTE
        approved = (len(approvals) >= majority_threshold) and not has_veto

        # If approved, compute final position size (min of all suggestions)
        if approved:
            sizes = [v.suggested_position_size_pct for v in phase2_verdicts if v.suggested_position_size_pct > 0]
            final_size = min(sizes) if sizes else 12.0  # V31: default 12% (was 2%)
            final_size = min(final_size, 18.0)  # V31: hard cap 18% (was 5%)
            final_decision = "EXECUTE"
        else:
            final_size = 0.0
            # If most approved but not enough for majority → WAIT
            if len(approvals) > len(rejections):
                final_decision = "WAIT"
            else:
                final_decision = "REJECT"

        # Build final reasoning
        reasoning_parts = [
            f"إجماع الوكلاء: {len(approvals)} موافق / {len(rejections)} رافض / {len(phase2_verdicts)} إجمالي",
        ]
        if not unanimity:
            reasoning_parts.append("الوكلاء الرافضون:")
            for v in rejections:
                reasoning_parts.append(f"  • {v.agent_name}: {v.decision} - {v.reasoning[:100]}")
        if approved:
            reasoning_parts.append(f"✅ الحجم النهائي المعتمد: {final_size:.2f}% من رأس المال")

        result = ConsensusResult(
            approved=approved,
            unanimity=unanimity,
            approvals_count=len(approvals),
            rejections_count=len(rejections),
            total_agents=len(phase2_verdicts),
            verdicts=phase2_verdicts,
            final_decision=final_decision,
            final_position_size_pct=final_size,
            final_reasoning="\n".join(reasoning_parts),
            consultation_log=[
                {
                    "phase": "initial_analysis",
                    "verdicts": [v.to_dict() for v in phase1_verdicts],
                },
                {
                    "phase": "consultation",
                    "verdicts": [v.to_dict() for v in phase2_verdicts],
                },
            ],
            timestamp=datetime.utcnow().isoformat(),
        )

        # Persist to system state for dashboard
        await self._persist_consensus(token.id, result)

        return result

    async def _persist_consensus(self, token_id: str, result: ConsensusResult) -> None:
        """Persist the latest consensus for dashboard access."""
        from sqlalchemy.orm.attributes import flag_modified

        try:
            async with get_session() as session:
                sys_state = await session.get(SystemState, "latest_agent_consensus")
                if not sys_state:
                    sys_state = SystemState(key="latest_agent_consensus", value_json={})
                    session.add(sys_state)
                    await session.flush()

                v = dict(sys_state.value_json or {})
                v["token_id"] = token_id
                v["approved"] = result.approved
                v["unanimity"] = result.unanimity
                v["approvals_count"] = result.approvals_count
                v["rejections_count"] = result.rejections_count
                v["final_decision"] = result.final_decision
                v["final_position_size_pct"] = result.final_position_size_pct
                v["timestamp"] = result.timestamp
                v["verdicts"] = [v.to_dict() if hasattr(v, 'to_dict') else v for v in result.verdicts]
                v["reasoning"] = result.final_reasoning
                sys_state.value_json = v
                flag_modified(sys_state, "value_json")
        except Exception as exc:
            logger.warning(f"Failed to persist consensus: {exc}")

    async def get_latest_consensus(self) -> Optional[Dict]:
        """Get the latest consensus result."""
        async with get_session() as session:
            sys_state = await session.get(SystemState, "latest_agent_consensus")
            if not sys_state:
                return None
            return sys_state.value_json
