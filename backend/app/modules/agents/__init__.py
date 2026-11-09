"""
18-Agent Multi-Agent System.

Exposes:
- MultiAgentSystem: main orchestrator
- AgentVerdict, AgentResult: structured verdicts
- All 18 agent classes
"""
from .multi_agent_system import (
    AgentVerdict,
    AgentResult,
    BaseAgent,
    MultiAgentSystem,
    MarketIntelligenceAgent,
    TokenDiscoveryAgent,
    TechnicalAnalysisAgent,
    LiquidityIntelligenceAgent,
    OnChainIntelligenceAgent,
    SecurityAgent,
    SocialIntelligenceAgent,
    PredictionAgent,
    RiskManagementAgent,
    StrategyResearchAgent,
    HistoricalLearningAgent,
    ErrorPreventionAgent,
    MarketAnomalyAgent,
    DecisionVerificationAgent,
    RedTeamAgent,
    WalletIntelligenceAgent,
    MetaLearningAgent,
    ChiefIntelligenceAgent,
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
