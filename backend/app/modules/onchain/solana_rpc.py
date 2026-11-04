"""
Minimal Solana RPC client for on-chain analysis.
Uses public RPC endpoints (free, no key).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ...core.config import settings
from ...core.logger import logger


class SolanaRPC:
    """Lightweight async Solana JSON-RPC client."""

    def __init__(self, rpc_url: Optional[str] = None):
        self.rpc_url = rpc_url or settings.solana_rpc_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def _call(self, method: str, params: List[Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        # Try primary, fall back to backups
        urls = [self.rpc_url, settings.solana_rpc_backup_1, settings.solana_rpc_backup_2]
        last_err = None
        for url in urls:
            try:
                resp = await self.client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    last_err = data["error"]
                    continue
                return data.get("result")
            except Exception as exc:
                last_err = exc
                continue
        logger.debug(f"Solana RPC {method} failed: {last_err}")
        return None

    async def get_account_info(self, address: str) -> Optional[Dict[str, Any]]:
        """Get account info for an SPL token or any account."""
        result = await self._call("getAccountInfo", [address, {"encoding": "base64"}])
        if not result or not result.get("value"):
            return None
        return result["value"]

    async def get_token_largest_accounts(self, mint: str) -> List[Dict[str, Any]]:
        """Get largest token holders (accounts holding this SPL token)."""
        result = await self._call("getTokenLargestAccounts", [mint])
        if not result:
            return []
        return result.get("value", [])

    async def get_token_supply(self, mint: str) -> Optional[Dict[str, Any]]:
        """Get total supply of an SPL token."""
        return await self._call("getTokenSupply", [mint])

    async def get_signatures_for_address(
        self, address: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get recent transaction signatures for an address."""
        result = await self._call(
            "getSignaturesForAddress", [address, {"limit": limit}]
        )
        return result or []

    async def close(self):
        await self.client.aclose()
