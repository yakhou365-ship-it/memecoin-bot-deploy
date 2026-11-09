"""
Jupiter API Client - Real Solana DEX data.

Provides real token prices, swap quotes, and trending tokens
from Jupiter (the largest Solana DEX aggregator).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from ...core.config import settings
from ...core.logger import logger


class JupiterClient:
    """Real Solana DEX data via Jupiter API."""

    def __init__(self):
        self.api_key = settings.jupiter_api_key
        self.base_url = settings.jupiter_api_url
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers=headers,
            )
        return self._client

    async def get_price(self, token_mint: str) -> Optional[float]:
        """Get real-time price for a Solana token."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/price/v2",
                params={"ids": token_mint},
            )
            if resp.status_code == 200:
                data = resp.json()
                token_data = data.get("data", {}).get(token_mint, {})
                price = token_data.get("price")
                if price:
                    return float(price)
        except Exception as exc:
            logger.debug(f"Jupiter price failed: {exc}")
        return None

    async def get_swap_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
    ) -> Optional[Dict]:
        """Get a real swap quote from Jupiter (includes slippage, route, etc)."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/swap/v1/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount,
                    "slippageBps": slippage_bps,
                },
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug(f"Jupiter quote failed: {exc}")
        return None

    async def search_tokens(self, query: str) -> List[Dict]:
        """Search for tokens on Jupiter."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/tokens/v1/search",
                params={"query": query},
            )
            if resp.status_code == 200:
                return resp.json() if isinstance(resp.json(), list) else []
        except Exception as exc:
            logger.debug(f"Jupiter search failed: {exc}")
        return []

    async def get_trending_tokens(self) -> List[Dict]:
        """Get trending tokens from Jupiter (if available)."""
        client = await self._get_client()
        try:
            # Jupiter doesn't have a direct trending endpoint,
            # but we can use the token list
            resp = await client.get(f"{self.base_url}/tokens/v1/tagged/verified")
            if resp.status_code == 200:
                tokens = resp.json() if isinstance(resp.json(), list) else []
                # Return top 50 by some criteria
                return tokens[:50]
        except Exception as exc:
            logger.debug(f"Jupiter trending failed: {exc}")
        return []

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
