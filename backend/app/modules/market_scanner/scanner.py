"""
Market Scanner engine.

Continuously monitors DEX markets for:
- New tokens
- Tokens with surging volume
- Tokens with growing liquidity
- Tokens losing momentum

Outputs a ranked watch-list stored in DB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, ScanRun, OHLCV
from .dexscreener_client import DexScreenerClient
from .geckoterminal_client import GeckoTerminalClient
from .pumpfun_client import PumpPortalClient, PumpFunToken
from .fomo_client import FomoClient


# =========================================================================
# Data normalizer: convert DexScreener pair -> our Token schema
# =========================================================================
def normalize_dexscreener_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    """Map DexScreener pair JSON into our internal token dict."""
    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    price_change = pair.get("priceChange") or {}
    fdv = pair.get("fdv") or 0
    info = pair.get("info") or {}

    h24 = txns.get("h24") or {}
    m5 = txns.get("m5") or {}

    return {
        "address": base_token.get("address", "").lower(),
        "chain": pair.get("chainId", ""),
        "symbol": base_token.get("symbol", ""),
        "name": base_token.get("name", ""),
        "pair_address": pair.get("pairAddress", "").lower(),
        "dex": pair.get("dexId", ""),
        "price_usd": float(pair.get("priceUsd") or 0),
        "market_cap_usd": float(pair.get("marketCap") or 0) or None,
        "liquidity_usd": float(liquidity.get("usd", 0) or 0),
        "volume_24h_usd": float(volume.get("h24", 0) or 0),
        "volume_5m_usd": float(volume.get("m5", 0) or 0),
        "txns_24h_buys": int(h24.get("buys", 0) or 0),
        "txns_24h_sells": int(h24.get("sells", 0) or 0),
        "price_change_24h_pct": float(price_change.get("h24", 0) or 0),
        "price_change_5m_pct": float(price_change.get("m5", 0) or 0),
        "fdv_usd": float(fdv or 0) or None,
    }


# =========================================================================
# Persistence helpers
# =========================================================================
async def upsert_token(session: AsyncSession, data: Dict[str, Any]) -> Optional[Token]:
    """Insert or update a token. Returns the Token instance or None if invalid."""
    addr = data.get("address")
    chain = data.get("chain")
    if not addr or not chain:
        return None

    # Find existing
    stmt = select(Token).where(
        and_(Token.chain == chain, Token.address == addr)
    )
    result = await session.execute(stmt)
    token = result.scalar_one_or_none()

    if token is None:
        token = Token(
            address=addr,
            chain=chain,
            symbol=data.get("symbol", "")[:64],
            name=(data.get("name") or "")[:256],
            pair_address=data.get("pair_address"),
            dex=data.get("dex"),
        )
        session.add(token)
        await session.flush()

    # Update mutable fields
    for field in [
        "symbol", "name", "pair_address", "dex",
        "price_usd", "market_cap_usd", "liquidity_usd",
        "volume_24h_usd", "volume_5m_usd",
        "txns_24h_buys", "txns_24h_sells",
        "price_change_24h_pct", "price_change_5m_pct", "fdv_usd",
    ]:
        if field in data and data[field] is not None:
            setattr(token, field, data[field])

    token.last_seen_at = datetime.utcnow()

    # Recompute watch_priority
    token.watch_priority = compute_watch_priority(token)
    return token


def compute_watch_priority(token: Token) -> int:
    """
    Score 0-100 reflecting how interesting this token is right now.
    Combines volume, liquidity, transaction count, momentum.
    """
    liq = token.liquidity_usd or 0
    vol24 = token.volume_24h_usd or 0
    buys = token.txns_24h_buys or 0
    sells = token.txns_24h_sells or 0
    chg24 = token.price_change_24h_pct or 0

    # Volume vs liquidity ratio (high = active trading)
    vol_liq_ratio = vol24 / max(liq, 1)

    # Buy/sell balance (>1 = buy pressure, healthy for memecoin but not extreme)
    bs_ratio = buys / max(sells, 1)
    bs_balance = 1.0 - min(abs(bs_ratio - 1.5) / 2.0, 1.0)  # peak at 1.5

    # Liquidity tier (capped)
    liq_score = min(liq / 200_000, 1.0) * 30  # max 30 for 200k+ liquidity

    # Volume tier
    vol_score = min(vol24 / 500_000, 1.0) * 30  # max 30 for 500k+ volume

    # Activity tier
    activity_score = min((buys + sells) / 1000, 1.0) * 20  # max 20 for 1000+ txns

    # Momentum: positive change up to +20, extreme negative penalized
    if chg24 > 0:
        mom_score = min(chg24 / 50, 1.0) * 20  # max 20 for +50%
    else:
        mom_score = max(chg24 / 20, -1.0) * 10  # max -10 for -20%

    bs_score = bs_balance * 10  # max 10

    return max(0, min(100, int(liq_score + vol_score + activity_score + mom_score + bs_score)))


# =========================================================================
# Scanner engine
# =========================================================================
class MarketScanner:
    """Scans DEX markets and maintains the watch-list in DB."""

    def __init__(self):
        self.dex = DexScreenerClient()
        self.gecko = GeckoTerminalClient()
        self.pump = PumpPortalClient()
        self.fomo = FomoClient()

    async def scan_chain(self, chain: str) -> Dict[str, int]:
        """
        Run one scan cycle for a single chain.
        Returns summary stats.
        """
        scan_run = ScanRun(chain=chain)
        async with get_session() as session:
            session.add(scan_run)
            await session.flush()
            run_id = scan_run.id

        discovered = 0
        updated = 0
        blacklisted = 0
        errors = 0
        error_log: List[str] = []

        try:
            # 1. Trending tokens from DexScreener
            trending = await self.dex.get_trending_tokens(limit=settings.scan_max_tokens)
            # DexScreener returns chainId field, filter by chain
            chain_filtered = [t for t in trending if t.get("chainId") == chain]
            if not chain_filtered:
                # Fallback: GeckoTerminal trending pools
                pools = await self.gecko.get_trending_pools(chain, limit=50)
                # Convert GeckoTerminal format to our schema
                for pool in pools:
                    try:
                        attrs = pool.get("attributes") or {}
                        rels = pool.get("relationships") or {}
                        base_token_rel = rels.get("base_token") or {}
                        base_token_data = base_token_rel.get("data") or {}
                        base_addr = base_token_data.get("id", "").split("_")[-1].lower()

                        if not base_addr:
                            continue

                        data = {
                            "address": base_addr,
                            "chain": chain,
                            "symbol": attrs.get("base_token_symbol", ""),
                            "name": "",
                            "pair_address": attrs.get("address", "").lower(),
                            "dex": attrs.get("dex_id", ""),
                            "price_usd": float(attrs.get("base_token_price_usd") or 0),
                            "market_cap_usd": None,
                            "liquidity_usd": float(attrs.get("reserve_in_usd") or 0),
                            "volume_24h_usd": float(attrs.get("volume_usd", {}).get("h24") or 0),
                            "volume_5m_usd": float(attrs.get("volume_usd", {}).get("m5") or 0),
                            "txns_24h_buys": 0,
                            "txns_24h_sells": 0,
                            "price_change_24h_pct": float(attrs.get("price_change_percentage", {}).get("h24") or 0),
                            "price_change_5m_pct": float(attrs.get("price_change_percentage", {}).get("m5") or 0),
                            "fdv_usd": None,
                        }
                        async with get_session() as s:
                            tok = await upsert_token(s, data)
                            if tok:
                                if tok.created_at == tok.updated_at:
                                    discovered += 1
                                else:
                                    updated += 1
                    except Exception as exc:
                        errors += 1
                        error_log.append(f"gecko:{str(exc)[:200]}")
            else:
                # Use DexScreener trending data
                for entry in chain_filtered:
                    try:
                        # entry structure: {chain, tokenAddress, ...}
                        token_addr = entry.get("tokenAddress", "").lower()
                        if not token_addr:
                            continue

                        # Fetch the actual pairs to get market data
                        pairs = await self.dex.get_token_pairs(chain, token_addr)
                        if not pairs:
                            continue
                        # Take the most liquid pair
                        data = normalize_dexscreener_pair(pairs[0])

                        async with get_session() as s:
                            tok = await upsert_token(s, data)
                            if tok:
                                if tok.created_at == tok.updated_at:
                                    discovered += 1
                                else:
                                    updated += 1

                                # Auto-blacklist extremely low liquidity / obvious scams
                                if (tok.liquidity_usd or 0) < 1000:
                                    tok.blacklisted = True
                                    tok.blacklist_reason = "liquidity below 1k USD"
                                    blacklisted += 1

                    except Exception as exc:
                        errors += 1
                        error_log.append(f"ds:{str(exc)[:200]}")
                        continue

            # 2. Mark dead tokens: not seen in last 6h
            cutoff = datetime.utcnow() - timedelta(hours=6)
            async with get_session() as s:
                await s.execute(
                    update(Token)
                    .where(
                        and_(
                            Token.chain == chain,
                            Token.active == True,
                            Token.last_seen_at < cutoff,
                        )
                    )
                    .values(active=False)
                )

            # 3. Persist scan run summary
            async with get_session() as s:
                run = await s.get(ScanRun, run_id)
                if run:
                    run.finished_at = datetime.utcnow()
                    run.tokens_discovered = discovered
                    run.tokens_updated = updated
                    run.tokens_blacklisted = blacklisted
                    run.errors = errors
                    run.error_log = error_log[:50]
                    run.status = "done"

            logger.info(
                f"[Scanner:{chain}] discovered={discovered} updated={updated} "
                f"blacklisted={blacklisted} errors={errors}"
            )
            return {
                "discovered": discovered,
                "updated": updated,
                "blacklisted": blacklisted,
                "errors": errors,
            }

        except Exception as exc:
            logger.exception(f"[Scanner:{chain}] fatal: {exc}")
            async with get_session() as s:
                run = await s.get(ScanRun, run_id)
                if run:
                    run.finished_at = datetime.utcnow()
                    run.status = "failed"
                    run.error_log = [str(exc)[:500]]
            return {
                "discovered": discovered,
                "updated": updated,
                "blacklisted": blacklisted,
                "errors": errors + 1,
            }

    async def scan_all_chains(self) -> Dict[str, Dict[str, int]]:
        """Run scan for all configured chains in parallel."""
        chains = settings.networks_list
        results = await asyncio.gather(
            *[self.scan_chain(c) for c in chains],
            return_exceptions=True,
        )
        out: Dict[str, Dict[str, int]] = {}
        for chain, res in zip(chains, results):
            if isinstance(res, Exception):
                logger.error(f"[Scanner:{chain}] returned exception: {res}")
                out[chain] = {"discovered": 0, "updated": 0, "blacklisted": 0, "errors": 1}
            else:
                out[chain] = res
        return out

    async def get_watch_list(self, limit: int = 50) -> List[Token]:
        """Get the top N most promising tokens by watch_priority."""
        async with get_session() as s:
            stmt = (
                select(Token)
                .where(Token.active == True, Token.blacklisted == False)
                .order_by(desc(Token.watch_priority))
                .limit(limit)
            )
            result = await s.execute(stmt)
            return list(result.scalars().all())

    # ============================================================
    # Pump.fun integration
    # ============================================================
    async def scan_pumpfun_new_tokens(self, limit: int = 50) -> List[PumpFunToken]:
        """
        Get newly launched tokens on Pump.fun.
        These are the freshest memecoins on Solana - high risk, high reward.
        """
        return await self.pump.get_new_tokens(limit=limit)

    async def scan_pumpfun_trending(self, limit: int = 50) -> List[PumpFunToken]:
        """Get trending Pump.fun tokens (most active in last 24h)."""
        return await self.pump.get_trending_tokens(limit=limit)

    async def scan_pumpfun_graduated(self, limit: int = 50) -> List[PumpFunToken]:
        """
        Get graduated Pump.fun tokens (moved to Raydium LP).
        Safer to trade - proven demand.
        """
        return await self.pump.get_graduated_tokens(limit=limit)

    async def scan_pumpfun_all(self, limit: int = 30) -> Dict[str, List[PumpFunToken]]:
        """
        Scan all Pump.fun token categories in parallel:
        - New tokens (just launched)
        - Trending tokens (most active)
        - Graduated tokens (proven demand)
        """
        new_tokens, trending, graduated = await asyncio.gather(
            self.pump.get_new_tokens(limit=limit),
            self.pump.get_trending_tokens(limit=limit),
            self.pump.get_graduated_tokens(limit=limit),
            return_exceptions=True,
        )
        # Handle exceptions gracefully
        return {
            "new": new_tokens if isinstance(new_tokens, list) else [],
            "trending": trending if isinstance(trending, list) else [],
            "graduated": graduated if isinstance(graduated, list) else [],
        }

    async def get_pumpfun_token_info(self, mint_address: str) -> Optional[PumpFunToken]:
        """Get detailed info about a specific Pump.fun token."""
        return await self.pump.get_token_info(mint_address)

    async def get_pumpfun_token_holders(self, mint_address: str) -> List[Dict[str, Any]]:
        """Get holders list for a Pump.fun token (for concentration analysis)."""
        return await self.pump.get_token_holders(mint_address)

    # ============================================================
    # FOMO integration (social trading signals)
    # ============================================================
    async def get_fomo_trending_tokens(self, hours: int = 1, limit: int = 20):
        """Get top tokens being bought on FOMO in the last N hours."""
        return await self.fomo.get_top_tokens_bought(hours=hours, limit=limit)

    async def get_fomo_top_traders(self, hours: int = 6, limit: int = 10):
        """Get top traders on FOMO - potential copy-trade targets."""
        return await self.fomo.get_top_traders_to_copy(hours=hours, limit=limit)

    async def get_fomo_latest_trades(self, limit: int = 20):
        """Get latest trades on FOMO (real-time memecoin activity)."""
        return await self.fomo.get_latest_trades(limit=limit)

    async def get_fomo_stats(self):
        """Get FOMO platform stats (volume proxy via fees)."""
        return await self.fomo.get_stats()

    async def close(self):
        await self.dex.close()
        await self.gecko.close()
        await self.pump.close()
        await self.fomo.close()
