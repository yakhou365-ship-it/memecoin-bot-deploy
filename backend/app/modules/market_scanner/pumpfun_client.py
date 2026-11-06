"""
PumpPortal + Pump.fun API Client.
================================

Provides real-time access to Pump.fun (the largest memecoin launchpad on Solana)
via the PumpPortal 3rd-party API.

PumpPortal API structure (from official docs at https://pumpportal.fun):

1. LIGHTNING TRANSACTION API (easiest):
   - Endpoint: POST https://pumpportal.fun/api/trade?api-key=YOUR_API_KEY
   - PumpPortal signs and sends the trade from their hosted wallet
   - Fee: 1% of each trade (calculated before slippage)
   - Required params: action, mint, amount, denominatedInSol, slippage, priorityFee
   - Optional: pool (pump, raydium, pump-amm, launchlab, raydium-cpmm, bonk, auto)

2. LOCAL TRANSACTION API (self-custody):
   - Endpoint: POST https://pumpportal.fun/api/trade-local
   - Returns unsigned transaction for you to sign with your own keypair
   - Fee: 0.5% of each trade (calculated before slippage)
   - Required params: publicKey, action, mint, amount, denominatedInSol, slippage, priorityFee
   - More secure: PumpPortal never sees your private key

3. DATA API (real-time WebSocket streaming):
   - Endpoint: wss://pumpportal.fun/api/data?api-key=YOUR_API_KEY
   - Methods: subscribeNewToken (free), subscribeMigration (free),
              subscribeTokenTrade, subscribeAccountTrade (paid: 0.01 SOL per 10k events)
   - Requires funded wallet (≥0.02 SOL) linked to API key

4. TOKEN CREATION API:
   - Endpoint: https://pumpportal.fun/api/trade (with action: "create")
   - Requires IPFS metadata upload (via Pinata)

Pump.fun program address: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
Official docs: https://pumpportal.fun

AUTH:
- Get a free API key at https://pumpportal.fun (Developer Docs section)
- Set as env: PUMPPORTAL_API_KEY
- For Lightning trade: also set SOLANA_PRIVATE_KEY (Lightning signs from linked wallet)
- For Local trade: only need SOLANA_PRIVATE_KEY (signs in your own code)

Rate Limits: 25 trade requests/second
"""
from __future__ import annotations
import os

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ...core.config import settings
from ...core.logger import logger


# Pump.fun constants (from official docs)
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_PORTAL_BASE = "https://pumpportal.fun"
PUMP_PORTAL_TRADE_URL = f"{PUMP_PORTAL_BASE}/api/trade"
PUMP_PORTAL_TRADE_LOCAL_URL = f"{PUMP_PORTAL_BASE}/api/trade-local"
PUMP_PORTAL_WS_URL = "wss://pumpportal.fun/api/data"
PUMP_FUN_API_BASE = "https://pump.fun"

# Bonding curve constants (from pump.fun/docs/bonding-curve)
VIRTUAL_SOL_RESERVES = 30.0  # SOL
VIRTUAL_TOKEN_RESERVES = 1_073_000_000.0  # tokens
REAL_SOL_RESERVES = 0.0  # SOL
REAL_TOKEN_RESERVES = 793_000_000.0  # tokens (initial)
TOTAL_SUPPLY = 1_000_000_000.0  # 1B tokens
GRADUATION_THRESHOLD_SOL = 85.0  # Market cap that triggers Raydium migration
SOL_PRICE_USD_FALLBACK = 200.0  # Used if we can't fetch SOL price

# Supported DEX pools (from PumpPortal docs, October 2026)
SUPPORTED_POOLS = ["pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"]


# ============================================================
# Data classes
# ============================================================
@dataclass
class PumpFunToken:
    """A token launched on Pump.fun."""
    address: str  # mint address
    symbol: str
    name: str
    description: str = ""
    image_uri: str = ""
    creator: str = ""
    created_timestamp: int = 0
    market_cap_usd: float = 0.0
    price_usd: float = 0.0
    price_sol: float = 0.0
    bonding_curve_progress: float = 0.0  # 0-100
    virtual_sol_reserves: float = 0.0
    virtual_token_reserves: float = 0.0
    real_sol_reserves: float = 0.0
    real_token_reserves: float = 0.0
    total_supply: float = 0.0
    website: str = ""
    twitter: str = ""
    telegram: str = ""
    is_graduated: bool = False
    raydium_pool: str = ""
    holder_count: int = 0
    volume_24h_usd: float = 0.0
    volume_5m_usd: float = 0.0
    price_change_24h_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PumpTradeResult:
    """Result of a pump.fun trade."""
    success: bool
    tx_signature: str = ""
    error: str = ""
    token_address: str = ""
    side: str = ""  # buy | sell
    amount_sol: float = 0.0
    amount_tokens: float = 0.0
    timestamp: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# Bonding curve math (from pump.fun docs)
# ============================================================
class BondingCurveMath:
    """
    Compute token price from bonding curve reserves.

    Pump.fun uses a constant-product AMM:
        price = (virtual_sol_reserves * total_supply) /
                (virtual_token_reserves * sol_price_usd)

    When tokens are bought:
        - real_sol_reserves increases
        - real_token_reserves decreases
        - virtual_sol_reserves stays fixed at 30 SOL
        - virtual_token_reserves stays fixed at 1.073B tokens

    Graduation happens when real_sol_reserves reaches 85 SOL.
    """

    @staticmethod
    def compute_price_usd(
        real_sol_reserves: float,
        sol_price_usd: float = SOL_PRICE_USD_FALLBACK,
    ) -> float:
        """
        Compute token price in USD given the current SOL reserves in the curve.

        Pump.fun uses a constant-product bonding curve:
            k = (virtual_sol + real_sol) * (virtual_tokens - tokens_bought)

        Initially: k = 30 SOL * 1.073B tokens
        As SOL enters curve: token supply decreases, price increases
        Price per token = (virtual_sol + real_sol) / (virtual_tokens - tokens_bought)

        At graduation (real_sol=85): price reaches ~$0.00016 (when SOL=$200)
        """
        if real_sol_reserves < 0:
            return 0.0

        # Compute tokens bought so far from curve
        # When real_sol enters: tokens bought = real_sol / virtual_sol * virtual_tokens (linear approx)
        # But actual pump.fun curve is constant-product, so:
        # k = (virtual_sol + real_sol) * (virtual_tokens - tokens_bought)
        # Initial k = virtual_sol * virtual_tokens
        # So: tokens_bought = virtual_tokens - (virtual_sol * virtual_tokens) / (virtual_sol + real_sol)
        # tokens_bought = virtual_tokens * (1 - virtual_sol / (virtual_sol + real_sol))
        # tokens_bought = virtual_tokens * real_sol / (virtual_sol + real_sol)

        if real_sol_reserves == 0:
            # Initial price = virtual_sol / virtual_tokens
            price_sol = VIRTUAL_SOL_RESERVES / VIRTUAL_TOKEN_RESERVES
        else:
            # After buying, effective token reserves decrease
            tokens_bought = VIRTUAL_TOKEN_RESERVES * real_sol_reserves / (VIRTUAL_SOL_RESERVES + real_sol_reserves)
            effective_tokens = VIRTUAL_TOKEN_RESERVES - tokens_bought
            effective_sol = VIRTUAL_SOL_RESERVES + real_sol_reserves

            if effective_tokens <= 0:
                # Curve exhausted (shouldn't happen before graduation)
                return 0.0

            price_sol = effective_sol / effective_tokens

        price_usd = price_sol * sol_price_usd
        return price_usd

    @staticmethod
    def compute_bonding_progress(real_sol_reserves: float) -> float:
        """
        Compute how far the bonding curve has progressed (0-100%).
        Graduation happens at 85 SOL.
        """
        if real_sol_reserves <= 0:
            return 0.0
        progress = (real_sol_reserves / GRADUATION_THRESHOLD_SOL) * 100
        return min(progress, 100.0)

    @staticmethod
    def compute_market_cap(real_sol_reserves: float, sol_price_usd: float = SOL_PRICE_USD_FALLBACK) -> float:
        """Compute market cap = price * total_supply."""
        price = BondingCurveMath.compute_price_usd(real_sol_reserves, sol_price_usd)
        return price * TOTAL_SUPPLY

    @staticmethod
    def compute_tokens_for_sol(
        sol_amount: float,
        real_sol_reserves: float,
        fee_bps: int = 100,  # 1% pump.fun fee
    ) -> float:
        """
        How many tokens you get for buying `sol_amount` SOL worth.

        Uses the constant-product formula:
            new_sol_reserves = real_sol_reserves + sol_amount * (1 - fee/10000)
            tokens_out = effective_tokens_before - effective_tokens_after

        where: k = effective_sol * effective_tokens = constant
        """
        if sol_amount <= 0 or real_sol_reserves < 0:
            return 0.0

        # Apply fee
        fee_pct = fee_bps / 10000.0
        sol_after_fee = sol_amount * (1 - fee_pct)

        # Compute current effective reserves
        if real_sol_reserves == 0:
            effective_sol_before = VIRTUAL_SOL_RESERVES
            effective_tokens_before = VIRTUAL_TOKEN_RESERVES
        else:
            tokens_bought_so_far = VIRTUAL_TOKEN_RESERVES * real_sol_reserves / (VIRTUAL_SOL_RESERVES + real_sol_reserves)
            effective_tokens_before = VIRTUAL_TOKEN_RESERVES - tokens_bought_so_far
            effective_sol_before = VIRTUAL_SOL_RESERVES + real_sol_reserves

        if effective_tokens_before <= 0:
            return 0.0

        # Constant k
        k = effective_sol_before * effective_tokens_before

        # After buy: effective_sol increases, effective_tokens decreases
        effective_sol_after = effective_sol_before + sol_after_fee
        if effective_sol_after <= 0:
            return 0.0

        effective_tokens_after = k / effective_sol_after
        if effective_tokens_after >= effective_tokens_before:
            return 0.0

        tokens_out = effective_tokens_before - effective_tokens_after
        return max(tokens_out, 0.0)


# ============================================================
# Main PumpPortal client
# ============================================================
class PumpPortalClient:
    """
    Client for PumpPortal API - the 3rd-party API for Pump.fun.

    Provides:
    1. Lightning Transaction API: buy/sell tokens with SOL via PumpPortal's hosted wallet
    2. Local Transaction API: build unsigned transactions to sign with your own keypair
    3. Data API (WebSocket): real-time token creation, migration, and trade events

    AUTH (for trade execution):
    - settings.pumportal_api_key: Get free at https://pumpportal.fun
    - settings.solana_private_key: Required for Lightning API (signs from linked wallet)
    - settings.solana_wallet_address: Required for Local API (PumpPortal returns unsigned tx)

    NO AUTH needed for:
    - Bonding curve math (computed locally)
    - SOL price fetching
    - WebSocket subscribeNewToken (free) and subscribeMigration (free)
    """

    def __init__(self):
        self.base_url = PUMP_PORTAL_BASE
        self.trade_url = PUMP_PORTAL_TRADE_URL
        self.trade_local_url = PUMP_PORTAL_TRADE_LOCAL_URL
        self.ws_url = PUMP_PORTAL_WS_URL
        # Solana wallet (needed for trade execution)
        self.api_key = getattr(settings, "pumportal_api_key", "") or os.environ.get("PUMPPORTAL_API_KEY", "") or ""
        self.wallet_private_key = getattr(settings, "solana_private_key", "") or os.environ.get("SOLANA_PRIVATE_KEY", "") or ""
        self.wallet_address = getattr(settings, "solana_wallet_address", "") or os.environ.get("SOLANA_WALLET_ADDRESS", "") or ""
        self.slippage_pct = getattr(settings, "pump_slippage_bps", 500) / 100  # Convert bps to %
        self.priority_fee_sol = getattr(settings, "pump_priority_fee_sol", 0.0001)
        self.default_pool = getattr(settings, "pump_default_pool", "auto") or "auto"
        self._client: Optional[httpx.AsyncClient] = None
        # V504 FIX: None means "no valid price fetched yet" — callers MUST skip
        # the trade (not fall back to SOL_PRICE_USD_FALLBACK = $200) because a
        # stale $200 number produces wildly wrong PnL / position-size math.
        # SOL_PRICE_USD_FALLBACK is still used by _parse_token() for non-critical
        # market-cap DISPLAY when no real price is available yet.
        self._sol_price_usd: Optional[float] = None
        self._sol_price_last_fetch: float = 0.0
        self._sol_price_last_success: float = 0.0  # V504: when we last got a real price

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            self._client = httpx.AsyncClient(timeout=15.0, headers=headers)
        return self._client

    async def _fetch_sol_price(self) -> Optional[float]:
        """Fetch current SOL price in USD (cached for 60s).

        V504 FIX: returns None (instead of the $200 fallback) when no real
        price is available, so callers MUST skip the trade rather than size a
        position with a wildly wrong constant.  Sources tried in order:
        Jupiter → Binance → Coinbase → DexScreener.  A stale cache is reused
        only if it is less than 5 minutes old; older stale values are dropped.
        """
        now = time.time()
        if now - self._sol_price_last_fetch < 60 and self._sol_price_usd:
            return self._sol_price_usd

        self._sol_price_last_fetch = now
        client = await self._get_client()

        # Source 1: Jupiter price API (current primary)
        price = await self._try_jupiter_price(client)

        # Source 2: Binance public ticker (no auth, very reliable)
        if not price or price <= 0:
            price = await self._try_binance_sol_price(client)

        # Source 3: Coinbase public spot price
        if not price or price <= 0:
            price = await self._try_coinbase_sol_price(client)

        # Source 4: DexScreener SOL/USDC pair on Solana
        if not price or price <= 0:
            price = await self._try_dexscreener_sol_price(client)

        if price and price > 0:
            self._sol_price_usd = float(price)
            self._sol_price_last_success = now
            return self._sol_price_usd

        # All sources failed.  Use stale cache only if recent enough.
        if (
            self._sol_price_usd
            and self._sol_price_last_success > 0
            and now - self._sol_price_last_success < 300  # 5 min staleness window
        ):
            logger.warning(
                f"SOL price fetch failed from all sources; using stale cache "
                f"${self._sol_price_usd:.2f} (age={int(now - self._sol_price_last_success)}s)"
            )
            return self._sol_price_usd

        # No valid price available anywhere — caller MUST skip the trade.
        logger.error(
            "SOL price unavailable from all sources (Jupiter, Binance, "
            "Coinbase, DexScreener). Skipping trade — refusing to fall back to $200."
        )
        return None

    async def _try_jupiter_price(self, client: httpx.AsyncClient) -> Optional[float]:
        try:
            resp = await client.get(
                "https://lite-api.jup.ag/price/v2",
                params={"ids": "So11111111111111111111111111111111111111112"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                price = (
                    data.get("data", {})
                    .get("So11111111111111111111111111111111111111112", {})
                    .get("price")
                )
                if price and float(price) > 0:
                    return float(price)
        except Exception as exc:
            logger.debug(f"Jupiter SOL price fetch failed: {str(exc)[:150]}")
        return None

    async def _try_binance_sol_price(self, client: httpx.AsyncClient) -> Optional[float]:
        try:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "SOLUSDT"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                price = float(resp.json().get("price", 0) or 0)
                if price > 0:
                    return price
        except Exception as exc:
            logger.debug(f"Binance SOL price fetch failed: {str(exc)[:150]}")
        return None

    async def _try_coinbase_sol_price(self, client: httpx.AsyncClient) -> Optional[float]:
        try:
            resp = await client.get(
                "https://api.coinbase.com/v2/prices/SOL-USD/spot",
                timeout=5.0,
            )
            if resp.status_code == 200:
                payload = resp.json() or {}
                price = float(
                    (payload.get("data") or {}).get("amount", 0) or 0
                )
                if price > 0:
                    return price
        except Exception as exc:
            logger.debug(f"Coinbase SOL price fetch failed: {str(exc)[:150]}")
        return None

    async def _try_dexscreener_sol_price(self, client: httpx.AsyncClient) -> Optional[float]:
        try:
            # Wrapped SOL mint on Solana; DexScreener returns its pairs.
            resp = await client.get(
                "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
                timeout=5.0,
            )
            if resp.status_code == 200:
                pairs = (resp.json() or {}).get("pairs", []) or []
                # Prefer a Solana / USDC pair with healthy liquidity.
                for p in pairs:
                    if p.get("chainId") != "solana":
                        continue
                    if (p.get("quoteToken") or {}).get("symbol", "").upper() != "USDC":
                        continue
                    price = float(p.get("priceUsd", 0) or 0)
                    if price > 0:
                        return price
                # Fall back to the first pair that has any price.
                for p in pairs:
                    price = float(p.get("priceUsd", 0) or 0)
                    if price > 0:
                        return price
        except Exception as exc:
            logger.debug(f"DexScreener SOL price fetch failed: {str(exc)[:150]}")
        return None

    @property
    def is_configured_for_trading(self) -> bool:
        """Check if all required settings are present for live trading."""
        return bool(self.api_key and self.wallet_address and self.wallet_private_key)

    # ========================================================
    # DATA API endpoints (no auth required)
    # ========================================================
    async def get_new_tokens(self, limit: int = 50) -> List[PumpFunToken]:
        """
        Get recently launched tokens on Pump.fun.
        Useful for catching new memecoins in the first few minutes.
        """
        client = await self._get_client()
        try:
            # PumpPortal data API
            resp = await client.post(
                f"{self.base_url}/api/data",
                json={"action": "newTokens", "limit": min(limit, 100)},
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens = []
                for item in data if isinstance(data, list) else []:
                    token = self._parse_token(item)
                    if token:
                        tokens.append(token)
                return tokens[:limit]
        except Exception as exc:
            logger.debug(f"PumpPortal newTokens failed: {exc}")
        return []

    async def get_trending_tokens(self, limit: int = 50) -> List[PumpFunToken]:
        """Get trending Pump.fun tokens."""
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://pump.fun/api/coin/trending",
                params={"limit": min(limit, 100)},
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens = []
                for item in data if isinstance(data, list) else []:
                    token = self._parse_token(item)
                    if token:
                        tokens.append(token)
                return tokens[:limit]
        except Exception as exc:
            logger.debug(f"PumpPortal trendingTokens failed: {exc}")
        return []

    async def get_token_info(self, mint_address: str) -> Optional[PumpFunToken]:
        """Get detailed info about a specific Pump.fun token."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"https://pump.fun/api/coin/{mint_address}",
            )
            if resp.status_code == 200:
                data = resp.json()
                # Could be a single token or list
                if isinstance(data, list) and data:
                    return self._parse_token(data[0])
                if isinstance(data, dict):
                    return self._parse_token(data)
        except Exception as exc:
            logger.debug(f"PumpPortal tokenInfo failed: {exc}")
        return None

    async def get_token_holders(self, mint_address: str) -> List[Dict[str, Any]]:
        """Get list of holders for a Pump.fun token."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"https://pump.fun/api/coin/{mint_address}/holders",
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
        except Exception as exc:
            logger.debug(f"PumpPortal holders failed: {exc}")
        return []

    async def get_graduated_tokens(self, limit: int = 50) -> List[PumpFunToken]:
        """Get graduated tokens (moved to Raydium LP)."""
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://pump.fun/api/coin/graduated",
                params={"limit": min(limit, 100)},
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens = []
                for item in data if isinstance(data, list) else []:
                    token = self._parse_token(item)
                    if token:
                        token.is_graduated = True
                        tokens.append(token)
                return tokens[:limit]
        except Exception as exc:
            logger.debug(f"PumpPortal graduatedTokens failed: {exc}")
        return []

    async def search_tokens(self, query: str, limit: int = 20) -> List[PumpFunToken]:
        """Search Pump.fun tokens by name or symbol."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"https://pump.fun/api/search",
                params={"q": query, "limit": limit},
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens = []
                for item in data if isinstance(data, list) else []:
                    token = self._parse_token(item)
                    if token:
                        tokens.append(token)
                return tokens[:limit]
        except Exception as exc:
            logger.debug(f"PumpPortal search failed: {exc}")
        return []

    # ========================================================
    # Lightning Transaction API (trade execution)
    # ========================================================
    async def _lightning_post(
        self,
        payload: Dict[str, Any],
        timeout: float,
        max_retries: int = 2,
    ):
        """POST to PumpPortal Lightning API with network-level retries."""
        client = await self._get_client()
        url = f"{self.trade_url}?api-key={self.api_key}"
        last_error = ""
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, json=payload, timeout=timeout)
                return resp, None
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as exc:
                last_error = str(exc)[:200]
                logger.warning(
                    f"PumpPortal request attempt {attempt + 1}/{max_retries + 1} failed: {last_error}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        return None, last_error

    async def buy_token(
        self,
        mint_address: str,
        amount_sol: float,
        pool: Optional[str] = None,
    ) -> PumpTradeResult:
        """
        Buy a Pump.fun token with SOL via PumpPortal Lightning API.

        Args:
            mint_address: Token mint address (Solana)
            amount_sol: SOL amount to spend (e.g., 0.1)
            pool: DEX to trade on (pump, raydium, pump-amm, launchlab, raydium-cpmm, bonk, auto)

        Returns: PumpTradeResult with tx_signature on success

        REQUIRES:
        - settings.pumportal_api_key set
        - settings.solana_private_key set (base58 encoded)
        - Wallet has SOL balance
        """
        if not self.api_key:
            return PumpTradeResult(
                success=False,
                error="PumpPortal API key not configured. Set PUMPPORTAL_API_KEY env var.",
                token_address=mint_address,
                side="buy",
                amount_sol=amount_sol,
            )
        if not self.wallet_private_key:
            return PumpTradeResult(
                success=False,
                error="Solana wallet private key not configured. Set SOLANA_PRIVATE_KEY env var.",
                token_address=mint_address,
                side="buy",
                amount_sol=amount_sol,
            )

        try:
            payload = {
                "action": "buy",
                "mint": mint_address,
                "amount": amount_sol,
                "denominatedInSol": "true",
                "slippage": self.slippage_pct,
                "priorityFee": self.priority_fee_sol * 3,
                "skipPreflight": "true",
                "pool": pool or "pump",
            }
            resp, net_error = await self._lightning_post(payload, timeout=8.0, max_retries=2)
            if net_error:
                return PumpTradeResult(
                    success=False,
                    error=f"Network error after 3 attempts: {net_error}",
                    token_address=mint_address,
                    side="buy",
                    amount_sol=amount_sol,
                )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("signature"):
                    return PumpTradeResult(
                        success=True,
                        tx_signature=data["signature"],
                        token_address=mint_address,
                        side="buy",
                        amount_sol=amount_sol,
                        timestamp=int(time.time()),
                    )
                return PumpTradeResult(
                    success=False,
                    error=data.get("errors", data.get("error", "Unknown error")),
                    token_address=mint_address,
                    side="buy",
                    amount_sol=amount_sol,
                )
            return PumpTradeResult(
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                token_address=mint_address,
                side="buy",
                amount_sol=amount_sol,
            )
        except Exception as exc:
            logger.exception(f"PumpPortal buy failed: {exc}")
            return PumpTradeResult(
                success=False,
                error=str(exc)[:200],
                token_address=mint_address,
                side="buy",
                amount_sol=amount_sol,
            )

    async def sell_token(
        self,
        mint_address: str,
        amount_tokens: float,
        pool: Optional[str] = None,
    ) -> PumpTradeResult:
        """
        Sell a Pump.fun token for SOL via PumpPortal Lightning API.

        Args:
            mint_address: Token mint address
            amount_tokens: Number of tokens to sell (or "100%" to sell all)

        Returns: PumpTradeResult
        """
        if not self.api_key:
            return PumpTradeResult(
                success=False,
                error="PumpPortal API key not configured",
                token_address=mint_address,
                side="sell",
                amount_tokens=amount_tokens,
            )
        if not self.wallet_private_key:
            return PumpTradeResult(
                success=False,
                error="Solana wallet private key not configured",
                token_address=mint_address,
                side="sell",
                amount_tokens=amount_tokens,
            )

        try:
            payload = {
                "action": "sell",
                "mint": mint_address,
                "amount": amount_tokens,
                "denominatedInSol": "false",
                "slippage": self.slippage_pct,
                "priorityFee": self.priority_fee_sol * 3,
                "skipPreflight": "true",
                "jitoOnly": "true",
                "pool": pool or "pump",
            }
            resp, net_error = await self._lightning_post(payload, timeout=10.0, max_retries=2)
            if net_error:
                return PumpTradeResult(
                    success=False,
                    error=f"Network error after 3 attempts: {net_error}",
                    token_address=mint_address,
                    side="sell",
                    amount_tokens=amount_tokens,
                )

            # If the sell fails with "insufficient", retry with the full balance ("100%").
            if resp.status_code == 200:
                data = resp.json()
                err_text = str(data.get("errors", data.get("error", ""))).lower()
                if not data.get("signature") and "insufficient" in err_text:
                    logger.info("Sell insufficient balance, retrying with amount='100%'")
                    retry_payload = dict(payload)
                    retry_payload["amount"] = "100%"
                    resp, net_error = await self._lightning_post(retry_payload, timeout=10.0, max_retries=1)
                    if net_error:
                        return PumpTradeResult(
                            success=False,
                            error=f"Retry network error: {net_error}",
                            token_address=mint_address,
                            side="sell",
                            amount_tokens=amount_tokens,
                        )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("signature"):
                    return PumpTradeResult(
                        success=True,
                        tx_signature=data["signature"],
                        token_address=mint_address,
                        side="sell",
                        amount_tokens=amount_tokens,
                        timestamp=int(time.time()),
                    )
                return PumpTradeResult(
                    success=False,
                    error=data.get("errors", data.get("error", "Unknown error")),
                    token_address=mint_address,
                    side="sell",
                    amount_tokens=amount_tokens,
                )
            return PumpTradeResult(
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                token_address=mint_address,
                side="sell",
                amount_tokens=amount_tokens,
            )
        except Exception as exc:
            logger.exception(f"PumpPortal sell failed: {exc}")
            return PumpTradeResult(
                success=False,
                error=str(exc)[:200],
                token_address=mint_address,
                side="sell",
                amount_tokens=amount_tokens,
            )

    async def confirm_signature(
        self,
        signature: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """Confirm a Solana transaction signature via RPC.

        Fast polling (0.5s), short RPC timeout (3s), accepts any of
        processed/confirmed/finalized as success.
        """
        if not signature:
            return {"confirmed": False, "error": "empty signature"}

        client = await self._get_client()
        deadline = time.time() + timeout_seconds
        accepted = ("processed", "confirmed", "finalized")
        last_status = "unknown"
        rpc_url = settings.solana_rpc_url

        while time.time() < deadline:
            try:
                resp = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[signature], {"searchTransactionHistory": True}],
                    },
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    value = (data.get("result", {}) or {}).get("value", [])
                    info = value[0] if value else None
                    if info:
                        # V407 P1-10: Check err FIRST — a tx can be "processed" but failed
                        if info.get("err"):
                            return {
                                "confirmed": False,
                                "error": str(info.get("err")),
                                "status": info.get("confirmationStatus", "unknown"),
                                "signature": signature,
                            }
                        last_status = info.get("confirmationStatus", "unknown")
                        if last_status in accepted:
                            return {
                                "confirmed": True,
                                "status": last_status,
                                "slot": info.get("slot"),
                                "err": None,
                                "signature": signature,
                            }
                        if info.get("err"):
                            return {
                                "confirmed": False,
                                "error": str(info.get("err")),
                                "status": last_status,
                                "signature": signature,
                            }
            except Exception as exc:
                logger.debug(f"getSignatureStatuses attempt failed: {str(exc)[:150]}")

            await asyncio.sleep(0.5)

        return {
            "confirmed": False,
            "error": f"timeout after {timeout_seconds}s (last status: {last_status})",
            "status": last_status,
            "signature": signature,
        }

    # ========================================================
    # Wallet operations
    # ========================================================
    async def get_wallet_balance(self) -> Dict[str, float]:
        """Get wallet SOL and token balances."""
        if not self.wallet_address:
            return {"sol": 0.0, "tokens": []}

        client = await self._get_client()
        try:
            # Use Solana RPC to get balance
            resp = await client.post(
                settings.solana_rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [self.wallet_address],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                lamports = data.get("result", {}).get("value", 0)
                sol_balance = lamports / 1_000_000_000  # 1 SOL = 1e9 lamports
                return {"sol": sol_balance, "tokens": []}
        except Exception as exc:
            logger.debug(f"Wallet balance failed: {exc}")
        return {"sol": 0.0, "tokens": []}

    async def get_wallet_token_balances(self) -> List[Dict[str, Any]]:
        """Get all token balances in the wallet (SPL tokens)."""
        if not self.wallet_address:
            return []

        client = await self._get_client()
        try:
            resp = await client.post(
                settings.solana_rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.wallet_address,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                accounts = data.get("result", {}).get("value", [])
                tokens = []
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    if info:
                        tokens.append({
                            "mint": info.get("mint", ""),
                            "amount": float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0),
                            "decimals": info.get("tokenAmount", {}).get("decimals", 0),
                        })
                return tokens
        except Exception as exc:
            logger.debug(f"Wallet token balances failed: {exc}")
        return []

    # ========================================================
    # Internal helpers
    # ========================================================
    def _parse_token(self, data: Dict[str, Any]) -> Optional[PumpFunToken]:
        """Parse a token dict from PumpPortal API into PumpFunToken."""
        if not data or not isinstance(data, dict):
            return None

        try:
            sol_price = self._sol_price_usd or SOL_PRICE_USD_FALLBACK
            real_sol = float(data.get("sol_reserves", 0) or 0)
            real_tokens = float(data.get("token_reserves", 0) or 0)

            # Compute derived fields if not provided
            market_cap = float(data.get("market_cap", 0) or 0)
            price_usd = float(data.get("usd_market_cap", 0) or 0) / TOTAL_SUPPLY if not market_cap else market_cap / TOTAL_SUPPLY

            # Use bonding curve math if market cap is 0
            if market_cap <= 0 and real_sol > 0:
                market_cap = BondingCurveMath.compute_market_cap(real_sol, sol_price)
                price_usd = market_cap / TOTAL_SUPPLY

            progress = float(data.get("bonding_curve_progress", 0) or 0)
            if progress <= 0 and real_sol > 0:
                progress = BondingCurveMath.compute_bonding_progress(real_sol)

            return PumpFunToken(
                address=data.get("mint", "") or data.get("address", ""),
                symbol=data.get("symbol", ""),
                name=data.get("name", ""),
                description=data.get("description", ""),
                image_uri=data.get("image_uri", ""),
                creator=data.get("creator", ""),
                created_timestamp=int(data.get("created_timestamp", 0) or 0),
                market_cap_usd=market_cap,
                price_usd=price_usd,
                price_sol=price_usd / sol_price if sol_price > 0 else 0,
                bonding_curve_progress=progress,
                virtual_sol_reserves=float(data.get("virtual_sol_reserves", VIRTUAL_SOL_RESERVES) or VIRTUAL_SOL_RESERVES),
                virtual_token_reserves=float(data.get("virtual_token_reserves", VIRTUAL_TOKEN_RESERVES) or VIRTUAL_TOKEN_RESERVES),
                real_sol_reserves=real_sol,
                real_token_reserves=real_tokens,
                total_supply=float(data.get("total_supply", TOTAL_SUPPLY) or TOTAL_SUPPLY),
                website=data.get("website", ""),
                twitter=data.get("twitter", ""),
                telegram=data.get("telegram", ""),
                is_graduated=bool(data.get("complete", False) or data.get("is_graduated", False)),
                raydium_pool=data.get("raydium_pool", ""),
                holder_count=int(data.get("holder_count", 0) or 0),
                volume_24h_usd=float(data.get("volume_24h", 0) or 0),
                volume_5m_usd=float(data.get("volume_5m", 0) or 0),
                price_change_24h_pct=float(data.get("price_change_24h", 0) or 0),
            )
        except Exception as exc:
            logger.debug(f"PumpFun token parse failed: {exc}")
            return None

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
