"""
GeckoTerminal API client (free, no key required, ~30 req/min).
Used as a fallback / supplementary data source.
Docs: https://www.geckoterminal.com/dex-api
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...core.config import settings
from ...core.logger import logger
from ...services.http_client import HttpClient


CHAIN_MAP = {
    "solana": "solana",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
}


class GeckoTerminalClient:
    def __init__(self):
        self.http = HttpClient(
            source_name="geckoterminal",
            base_url=settings.geckoterminal_api_url,
            default_headers={"Accept": "application/json"},
            max_concurrency=3,  # conservative due to rate limits
        )

    async def get_trending_pools(self, chain: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get trending pools on a chain."""
        chain_id = CHAIN_MAP.get(chain, chain)
        try:
            data = await self.http.request(
                "GET",
                f"/networks/{chain_id}/trending_pools",
                params={"page": 1},
            )
            pools = data.get("data", [])
            logger.info(f"GeckoTerminal: {len(pools)} trending pools for {chain}")
            return pools[:limit]
        except Exception as exc:
            logger.warning(f"GeckoTerminal trending pools failed: {exc}")
            return []

    async def get_ohlcv(
        self,
        chain: str,
        pool_address: str,
        timeframe: str = "minute",
        limit: int = 300,
        before: int = 0,
        after: int = 0,
    ) -> List[List[Any]]:
        """
        Get OHLCV candles for a pool.
        timeframe: minute | hour | four_hour | day
        Returns list of [timestamp, open, high, low, close, volume].
        """
        chain_id = CHAIN_MAP.get(chain, chain)
        # GeckoTerminal: aggregate=False returns latest `limit` candles
        params = {
            "aggregate": 1,
            "limit": min(limit, 1000),
            "currency": "usd",
            "token": "base",
        }
        if before:
            params["before"] = before
        if after:
            params["after"] = after

        # Map timeframe to API parameter
        tf_map = {
            "1m": "minute",
            "5m": "minute",  # aggregate=5 not supported via this API directly
            "15m": "minute",
            "1h": "hour",
            "4h": "four_hour",
            "1d": "day",
        }
        api_tf = tf_map.get(timeframe, "minute")
        params["aggregate"] = int(timeframe.replace("m", "").replace("h", "").replace("d", "")) if timeframe[0].isdigit() else 1

        data = await self.http.request(
            "GET",
            f"/networks/{chain_id}/pools/{pool_address}/ohlcv/{api_tf}",
            params=params,
        )
        ohlcv = data.get("data", {}).get("ohlcv_list") or []
        return ohlcv

    async def get_pool_info(self, chain: str, pool_address: str) -> Dict[str, Any]:
        """Get info for a specific pool."""
        chain_id = CHAIN_MAP.get(chain, chain)
        data = await self.http.request(
            "GET", f"/networks/{chain_id}/pools/{pool_address}"
        )
        return data.get("data", {})

    async def close(self):
        await self.http.close()
