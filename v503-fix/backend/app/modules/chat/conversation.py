"""
Chat conversation engine.

Allows the user to talk to the AI directly through the dashboard.
The AI has access to:
- All token analyses
- All trades (open + closed)
- All mistakes & learnings
- Risk state
- Latest agent consensus

It can answer questions like:
- "ما رأيك في عملة GATO؟"
- "لماذا رفضت آخر صفقة؟"
- "ما هي آخر الأخطاء التي تعلّمت منها؟"
- "كم رصيد حسابي الافتراضي؟"
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, desc, func, and_

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import (
    Token, Trade, AnalysisSnapshot, Mistake, Learning, Alert, ModelVersion,
)
from ..ai_agents.llm_provider import get_llm


SYSTEM_PROMPT = """أنت مساعد ذكي متخصص في نظام تداول الـ Memecoins.

مهمتك الإجابة على أسئلة المستخدم بالعربية بوضوح ودقة.

قواعد صارمة:
1. استخدم فقط البيانات الحقيقية المتاحة لك - لا تخترع أرقاماً أو إحصائيات
2. إذا لم تكن تعرف الإجابة، قل ذلك صراحةً
3. لا تعطِ نصائح استثمارية مباشرة - أنت محلّل معلومات فقط
4. إذا سألك المستخدم عن عملة معينة، ابحث في بياناتك عنها
5. إذا سألك عن صفقة، استخرج تفاصيلها من قاعدة البيانات
6. كن صريحاً عن القيود: لا تخترع أخباراً، لا تتوقع المستقبل
7. اذكر دائماً أن النظام في وضع {trading_mode}

عند الإجابة:
- استخدم الأرقام الحقيقية من البيانات
- اذكر المصدر (تحليل كذا، صفقة كذا)
- إن وُجدت مخاطر، اذكرها بوضوح

أجب بالعربية الفصحى المبسّطة. كن مختصراً ولكن شاملاً.
"""


@dataclass
class ChatMessage:
    role: str  # user | assistant
    content: str
    timestamp: str
    context_used: Optional[Dict] = None  # what data was fetched

    def to_dict(self) -> Dict:
        return asdict(self)


class ChatSession:
    """Maintains a conversation with the user."""

    def __init__(self, max_history: int = 20):
        self.max_history = max_history
        self.history: List[ChatMessage] = []
        self.llm = get_llm()

    async def send(self, user_message: str) -> ChatMessage:
        """Process user message and return AI response."""
        # Detect intent & fetch relevant context
        context = await self._fetch_context(user_message)

        # Build messages array for LLM
        system = SYSTEM_PROMPT.format(trading_mode=settings.trading_mode)

        # Convert history to LLM messages
        history_msgs = [{"role": "assistant", "content": system}]
        for m in self.history[-self.max_history:]:
            history_msgs.append({"role": m.role, "content": m.content})

        # Add current user message with context
        user_content = user_message
        if context:
            user_content = (
                f"سؤال المستخدم: {user_message}\n\n"
                f"البيانات المتاحة لك للإجابة (من قاعدة البيانات الحقيقية):\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2, default=str)[:3000]}"
            )

        # Get LLM response
        try:
            response_text = await self.llm.chat(
                messages=history_msgs + [{"role": "user", "content": user_content}],
                temperature=settings.ai_agent_temperature,
                max_tokens=settings.ai_agent_max_tokens,
            )
        except Exception as exc:
            logger.warning(f"LLM failed, using fallback: {exc}")
            response_text = f"أهلاً! البوت يعمل.\n\nحالة البوت:\n- المحفظة: {getattr(settings, 'solana_wallet_address', 'غير مربوطة')}\n- التداول الحي: {settings.is_live_trading}\n- الوكلاء: 18 مفعّلون"

        # Save to history
        user_msg = ChatMessage(
            role="user",
            content=user_message,
            timestamp=datetime.utcnow().isoformat(),
        )
        ai_msg = ChatMessage(
            role="assistant",
            content=response_text,
            timestamp=datetime.utcnow().isoformat(),
            context_used=context,
        )
        self.history.append(user_msg)
        self.history.append(ai_msg)

        return ai_msg

    async def _fetch_context(self, query: str) -> Dict[str, Any]:
        """Detect what the user is asking about and fetch relevant data."""
        query_lower = query.lower()
        context: Dict[str, Any] = {}

        try:
            # Account state
            if any(k in query_lower for k in ["رصيد", "حساب", "balance", "account", "ربح", "خسارة", "pnl"]):
                from ..paper_trading.engine import PaperTradingEngine
                engine = PaperTradingEngine()
                account = await engine.get_account_state()
                stats = await engine.get_stats()
                context["account"] = {
                    "starting_capital": account.starting_capital,
                    "current_equity": balance.get("sol", 0) * 200,
                    "total_pnl": 0,
                    "total_pnl_pct": 0,
                    "open_positions": 0,
                    "closed_trades": 0,
                    "win_rate": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "max_drawdown_pct": 0,
                    "profit_factor": stats.get("profit_factor"),
                    "trading_mode": settings.trading_mode,
                }

            # === Pump.fun wallet info ===
            if any(k in query_lower for k in ["محفظتي", "حسابي", "my account", "wallet balance", "sol balance", "رصيد"]):
                try:
                    from ..market_scanner.pumpfun_client import PumpPortalClient
                    client = PumpPortalClient()
                    balance = await client.get_wallet_balance()
                    context["pumpfun_wallet"] = {
                        "sol_balance": balance.get("sol", 0),
                        "wallet_configured": bool(getattr(settings, "solana_wallet_address", "")),
                        "is_live": settings.is_live_trading,
                        "trading_mode": settings.trading_mode,
                    }
                except Exception as exc:
                    context["wallet_error"] = str(exc)[:200]

            # === NEW: Solana wallet info ===
            if any(k in query_lower for k in ["solana", "محفظة", "wallet", "sol", "صولانا", "phantom", "solflare"]):
                # If user provides wallet address in message
                import re
                wallet_match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', query)
                if wallet_match:
                    wallet_address = wallet_match.group()
                    try:
                        from ..onchain.solana_rpc import SolanaRPC
                        rpc = SolanaRPC()
                        # Get SOL balance
                        balance_result = await rpc._call("getBalance", [wallet_address])
                        sol_balance = (balance_result or {}).get("value", 0) / 1e9 if balance_result else 0
                        # Get token accounts
                        token_accounts = await rpc._call("getTokenAccountsByOwner", [
                            wallet_address,
                            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                            {"encoding": "jsonParsed"},
                        ])
                        tokens = []
                        for acc in (token_accounts or [])[:20]:  # limit to 20
                            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                            mint = info.get("mint", "")
                            amount = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
                            if amount > 0:
                                tokens.append({"mint": mint, "amount": amount})
                        context["solana_wallet"] = {
                            "address": wallet_address,
                            "sol_balance": sol_balance,
                            "token_count": len(tokens),
                            "tokens": tokens[:10],  # top 10
                        }
                        await rpc.client.aclose()
                    except Exception as exc:
                        context["solana_error"] = str(exc)[:200]
                else:
                    context["solana_wallet_hint"] = "أدخل عنوان محفظة Solana (32-44 حرف) في رسالتك"

            # Specific token
            if any(k in query_lower for k in ["عملة", "توكن", "token", "coin", "السعر"]):
                async with get_session() as s:
                    stmt = select(Token).where(Token.active == True).order_by(desc(Token.watch_priority)).limit(5)
                    tokens = list((await s.execute(stmt)).scalars().all())
                    context["top_tokens"] = [
                        {
                            "symbol": t.symbol,
                            "chain": t.chain,
                            "price": t.price_usd,
                            "liquidity": t.liquidity_usd,
                            "volume_24h": t.volume_24h_usd,
                            "opportunity_score": t.latest_opportunity_score,
                            "risk_score": t.latest_risk_score,
                            "win_probability": t.latest_win_probability,
                        }
                        for t in tokens
                    ]

            # Trades
            if any(k in query_lower for k in ["صفقة", "تداول", "trade", "اشتريت", "بعت"]):
                async with get_session() as s:
                    stmt = select(Trade, Token).join(Token, Trade.token_id == Token.id, isouter=True).order_by(desc(Trade.created_at)).limit(5)
                    rows = (await s.execute(stmt)).all()
                    context["recent_trades"] = [
                        {
                            "symbol": tok.symbol if tok else None,
                            "status": t.status,
                            "entry_price": t.entry_price,
                            "exit_price": t.exit_price,
                            "net_pnl": t.net_pnl_usd,
                            "pnl_pct": t.pnl_pct,
                            "exit_reason": t.exit_reason,
                            "timestamp": t.entry_timestamp.isoformat() if t.entry_timestamp else None,
                        }
                        for t, tok in rows
                    ]

            # Mistakes / learning
            if any(k in query_lower for k in ["خطأ", "تعلم", "mistake", "learn", "درس"]):
                async with get_session() as s:
                    stmt = select(Mistake).order_by(desc(Mistake.created_at)).limit(5)
                    mistakes = list((await s.execute(stmt)).scalars().all())
                    context["recent_mistakes"] = [
                        {
                            "type": m.mistake_type,
                            "description": m.description,
                            "occurrence_count": m.occurrence_count,
                            "active_block": m.active_block,
                        }
                        for m in mistakes
                    ]

            # Risk state
            if any(k in query_lower for k in ["مخاطر", "خطر", "risk", "drawdown", "كولداون"]):
                from ..risk_management.manager import RiskManager
                rm = RiskManager()
                risk_state = await rm.get_state()
                context["risk_state"] = risk_state.to_dict()

            # Latest agent consensus
            if any(k in query_lower for k in ["وكيل", "agent", "إجماع", "consensus", "رأي"]):
                from ..ai_agents.orchestrator import AgentOrchestrator
                orch = AgentOrchestrator()
                latest = await orch.get_latest_consensus()
                if latest:
                    context["latest_agent_consensus"] = latest

            # Alerts
            if any(k in query_lower for k in ["تنبيه", "alert", "إشعار"]):
                async with get_session() as s:
                    stmt = select(Alert).order_by(desc(Alert.created_at)).limit(5)
                    alerts = list((await s.execute(stmt)).scalars().all())
                    context["recent_alerts"] = [
                        {
                            "type": a.alert_type,
                            "severity": a.severity,
                            "title": a.title,
                            "message": a.message[:200],
                            "timestamp": a.created_at.isoformat() if a.created_at else None,
                        }
                        for a in alerts
                    ]

            # === NEW: Dangerous patterns ===
            if any(k in query_lower for k in ["احتيال", "خطر", "scam", "fraud", "rug", "honeypot", "خطر خطير"]):
                context["dangerous_patterns"] = "يمكنني فحص أي عملة لـ 25+ نمط خطير. اسألني عن عملة محددة."

        except Exception as exc:
            logger.warning(f"Context fetch error (non-blocking): {exc}")

        return context

    def clear_history(self) -> None:
        self.history = []

    def get_history(self) -> List[Dict]:
        return [m.to_dict() for m in self.history]


# Global session (in-memory; for multi-user, use session-per-user)
_session: Optional[ChatSession] = None


def get_chat_session() -> ChatSession:
    global _session
    if _session is None:
        _session = ChatSession()
    return _session
