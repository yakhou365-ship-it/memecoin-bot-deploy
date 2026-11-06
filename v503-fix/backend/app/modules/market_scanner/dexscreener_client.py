"""
DexScreener API client.
Free, no API key required, generous rate limits (~300 req/min).
Docs: https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.config import settings
from ...core.logger import logger
from ...services.http_client import HttpClient


# Chain name mapping: our internal name -> DexScreener chain identifier
CHAIN_MAP = {
    "solana": "solana",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "avalanche": "avalanche",
    "optimism": "optimism",
}


class DexScreenerClient:
    """Async client for DexScreener API."""

    def __init__(self):
        self.http = HttpClient(
            source_name="dexscreener",
            base_url=settings.dexscreener_api_url,
            default_headers={"Accept": "application/json"},
            max_concurrency=8,
        )

    async def get_trending_tokens(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get currently trending tokens across all chains.
        Uses the boost-ranked endpoint first; falls back to top boosted list.
        Returns a list of dicts with keys: chain, tokenAddress, ... (DexScreener format)
        """
        # Try the boost-ranked endpoint first (most active)
        try:
            data = await self.http.request("GET", "/token-boosts/top/v1")
            if isinstance(data, list) and data:
                logger.info(f"DexScreener: {len(data)} trending tokens fetched (boosts)")
                return data[:limit]
        except Exception as exc:
            logger.warning(f"DexScreener trending fetch failed: {exc}")
        # Fall back to recent boosts
        try:
            data = await self.http.request("GET", "/token-boosts/latest/v1")
            if isinstance(data, list) and data:
                logger.info(f"DexScreener: {len(data)} recent tokens fetched (latest boosts)")
                return data[:limit]
        except Exception as exc:
            logger.warning(f"DexScreener latest fetch failed: {exc}")
        return []

    async def search_pairs(
        self, chain: str, query: str = "", limit: int = 25
    ) -> List[Dict[str, Any]]:
        """Search for pairs by chain & query."""
        params: Dict[str, Any] = {}
        chain_id = CHAIN_MAP.get(chain, chain)
        if query:
            # Search globally
            data = await self.http.request("GET", "/latest/dex/search", params={"q": query})
            pairs = data.get("pairs") or []
            # Filter by chain
            pairs = [p for p in pairs if p.get("chainId") == chain_id]
            return pairs[:limit]
        return []

    async def get_token_pairs(self, chain: str, token_address: str) -> List[Dict[str, Any]]:
        """
        Get all DEX pairs for a specific token address.
        Returns the most liquid pair first.
        """
        chain_id = CHAIN_MAP.get(chain, chain)
        data = await self.http.request(
            "GET", f"/latest/dex/tokens/{token_address}"
        )
        pairs = data.get("pairs") or []
        # Filter by chain just in case
        pairs = [p for p in pairs if p.get("chainId") == chain_id]
        # Sort by liquidity descending
        pairs.sort(
            key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0,
            reverse=True,
        )
        return pairs

    async def get_pairs_by_chain(
        self, chain: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get the most active pairs for a chain via the search endpoint.
        DexScreener doesn't have a direct 'top gainers' endpoint,
        so we use trending boosts + per-chain search.
        """
        # Use trending + filter
        trending = await self.get_trending_tokens(limit=200)
        chain_id = CHAIN_MAP.get(chain, chain)
        filtered = [t for t in trending if t.get("chain") == chain_id]
        return filtered[:limit]

    async def get_pair_by_address(
        self, chain: str, pair_address: str
    ) -> Optional[Dict[str, Any]]:
        """Get a specific pair by its pair/pool address."""
        data = await self.http.request(
            "GET", f"/latest/dex/pairs/{chain}/{pair_address}"
        )
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    async def close(self):
        await self.http.close()
