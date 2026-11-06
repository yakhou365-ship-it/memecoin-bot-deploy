"""
PumpPortal WebSocket Manager — Real-time token discovery.

Connects to PumpPortal WebSocket (wss://pumpportal.fun/api/data) and
listens for:
1. subscribeNewToken — fires the moment a new token is launched on pump.fun
2. subscribeMigration — fires when a token graduates to Raydium

This eliminates the 2-5 minute DexScreener delay. The bot now sees new
tokens in REAL TIME, same as the pump.fun website.

Free methods (no API key needed):
- subscribeNewToken
- subscribeMigration

Paid methods (need funded wallet linked to API key):
- subscribeTokenTrade
- subscribeAccountTrade

We only use the free methods.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import websockets

from ...core.config import settings
from ...core.logger import logger


PUMP_PORTAL_WS_URL = "wss://pumpportal.fun/api/data"


class PumpPortalWebSocket:
    """Real-time WebSocket listener for pump.fun token events."""

    def __init__(self):
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 5.0  # seconds between reconnection attempts
        self._max_reconnect_delay = 60.0
        self._on_new_token: Optional[Callable] = None
        self._on_migration: Optional[Callable] = None
        self._last_token_at: float = 0.0
        self._tokens_received: int = 0
        self._connected_since: Optional[float] = None

    def on_new_token(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback for new token events."""
        self._on_new_token = callback

    def on_migration(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback for token migration events."""
        self._on_migration = callback

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "connected": self.is_connected,
            "connected_since": datetime.utcfromtimestamp(self._connected_since).isoformat()
                if self._connected_since else None,
            "tokens_received": self._tokens_received,
            "last_token_at": datetime.utcfromtimestamp(self._last_token_at).isoformat()
                if self._last_token_at else None,
        }

    async def start(self) -> None:
        """Start the WebSocket listener (runs forever until stop())."""
        self._running = True
        logger.info("🔌 PumpPortal WebSocket listener starting...")

        while self._running:
            try:
                # Build URL — free methods don't need API key, but if we have one, include it
                url = PUMP_PORTAL_WS_URL
                if settings.pumportal_api_key:
                    url = f"{PUMP_PORTAL_WS_URL}?api-key={settings.pumportal_api_key}"

                self._ws = await websockets.connect(url, ping_interval=30, ping_timeout=10)
                self._connected_since = time.time()
                logger.info("✅ PumpPortal WebSocket connected!")

                # Subscribe to free events
                await self._ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.info("📡 Subscribed to subscribeNewToken (free)")

                await self._ws.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("📡 Subscribed to subscribeMigration (free)")

                # Listen for messages
                async for raw_msg in self._ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw_msg)
                        await self._handle_message(msg)
                    except json.JSONDecodeError:
                        logger.debug(f"WS non-JSON message: {raw_msg[:100]}")
                    except Exception as exc:
                        logger.debug(f"WS message handling error: {exc}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("⚠️ PumpPortal WebSocket closed, reconnecting...")
            except Exception as exc:
                logger.warning(f"⚠️ PumpPortal WebSocket error: {exc}")

            if self._running:
                # Reconnect with exponential backoff
                logger.info(f"🔄 Reconnecting in {self._reconnect_delay:.0f}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, self._max_reconnect_delay)
            else:
                break

        logger.info("🔌 PumpPortal WebSocket stopped")

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        # New token created
        if "mint" in msg and "traderPublicKey" in msg:
            self._tokens_received += 1
            self._last_token_at = time.time()
            token_data = {
                "mint": msg.get("mint", ""),
                "creator": msg.get("traderPublicKey", ""),
                "signature": msg.get("signature", ""),
                "timestamp": msg.get("timestamp", int(time.time())),
                "bonding_curve_key": msg.get("bondingCurveKey", ""),
                "name": msg.get("name", ""),
                "symbol": msg.get("symbol", ""),
                "uri": msg.get("uri", ""),
            }
            logger.info(
                f"🆕 NEW TOKEN: {token_data['symbol']} ({token_data['mint'][:12]}...) "
                f"by {token_data['creator'][:8]}..."
            )
            if self._on_new_token:
                try:
                    await self._on_new_token(token_data)
                except Exception as exc:
                    logger.exception(f"New token callback failed: {exc}")

        # Token migration (graduated to Raydium)
        elif "mint" in msg and "raydiumPool" in msg:
            migration_data = {
                "mint": msg.get("mint", ""),
                "raydium_pool": msg.get("raydiumPool", ""),
                "signature": msg.get("signature", ""),
                "timestamp": msg.get("timestamp", int(time.time())),
            }
            logger.info(
                f"🎓 GRADUATION: {migration_data['mint'][:12]}... → Raydium"
            )
            if self._on_migration:
                try:
                    await self._on_migration(migration_data)
                except Exception as exc:
                    logger.exception(f"Migration callback failed: {exc}")

        # Subscription confirmation
        elif "message" in msg:
            logger.debug(f"WS subscription: {msg['message']}")

    async def stop(self) -> None:
        """Stop the WebSocket listener."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        logger.info("PumpPortal WebSocket stopping...")
