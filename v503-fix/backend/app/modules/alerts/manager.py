"""
Alert system - persists to DB + sends Telegram notifications (if configured).

Alert types:
- new_token_discovered
- opportunity_qualifying
- opportunity_qualified
- opportunity_rejected
- trade_opened
- trade_closed
- profit_realized
- loss_realized
- scam_detected
- market_regime_changed
- mistake_pattern_detected
- strategy_performance_change
- circuit_breaker_triggered
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Alert


SEVERITY_MAP = {
    "new_token_discovered": "info",
    "opportunity_qualifying": "info",
    "opportunity_qualified": "info",
    "opportunity_rejected": "warning",
    "trade_opened": "info",
    "trade_closed": "info",
    "profit_realized": "info",
    "loss_realized": "warning",
    "scam_detected": "critical",
    "market_regime_changed": "warning",
    "mistake_pattern_detected": "warning",
    "strategy_performance_change": "warning",
    "circuit_breaker_triggered": "critical",
}


class AlertManager:
    """Sends alerts to DB and optionally to Telegram."""

    def __init__(self):
        self.telegram_token = settings.telegram_bot_token
        self.telegram_chat_id = settings.telegram_chat_id
        self._tg_client: Optional[httpx.AsyncClient] = None

    async def send(
        self,
        alert_type: str,
        title: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        send_telegram: bool = True,
    ) -> Alert:
        """Send an alert."""
        severity = SEVERITY_MAP.get(alert_type, "info")

        # Persist to DB
        alert = Alert(
            alert_type=alert_type,
            severity=severity,
            title=title[:256],
            message=message,
            data_json=data or {},
            sent_dashboard=True,
            sent_telegram=False,
        )
        async with get_session() as s:
            s.add(alert)

        # Telegram
        if send_telegram and self.telegram_token and self.telegram_chat_id:
            try:
                await self._send_telegram(alert_type, severity, title, message)
                alert.sent_telegram = True
                async with get_session() as s:
                    s.add(alert)
            except Exception as exc:
                logger.warning(f"Telegram send failed: {exc}")

        if severity == "critical":
            logger.critical(f"🚨 ALERT [{alert_type}]: {title} - {message}")
        elif severity == "warning":
            logger.warning(f"⚠️ ALERT [{alert_type}]: {title} - {message}")
        else:
            logger.info(f"📢 ALERT [{alert_type}]: {title} - {message}")

        return alert

    async def _send_telegram(self, alert_type: str, severity: str, title: str, message: str) -> None:
        if self._tg_client is None:
            self._tg_client = httpx.AsyncClient(timeout=10.0)

        emoji = {"info": "📢", "warning": "⚠️", "critical": "🚨"}.get(severity, "📢")
        text = f"{emoji} *{title}*\n\n{message}\n\n_Type: {alert_type}_"

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        resp = await self._tg_client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning(f"Telegram API error: {resp.status_code} {resp.text[:200]}")

    async def get_recent_alerts(self, limit: int = 50) -> list:
        async with get_session() as s:
            stmt = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
            result = await s.execute(stmt)
            return list(result.scalars().all())

    async def close(self):
        if self._tg_client:
            await self._tg_client.aclose()
