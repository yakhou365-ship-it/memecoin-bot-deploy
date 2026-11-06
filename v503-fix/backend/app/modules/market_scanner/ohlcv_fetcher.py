"""
Fetch & persist OHLCV candles for tokens.
Uses GeckoTerminal (free, no key).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, OHLCV
from .geckoterminal_client import GeckoTerminalClient


class OHLCVFetcher:
    """Fetches and caches OHLCV candles."""

    def __init__(self):
        self.gecko = GeckoTerminalClient()

    async def fetch_and_store(
        self,
        token_id: str,
        timeframe: str = "1m",
        limit: int = 300,
    ) -> List[OHLCV]:
        """
        Fetch OHLCV for a token and persist to DB.
        Returns the inserted rows.
        """
        async with get_session() as session:
            token = await session.get(Token, token_id)
            if not token or not token.pair_address:
                return []

            # GeckoTerminal needs pool_address not token_address
            pair_addr = token.pair_address
            if not pair_addr:
                return []

        try:
            candles = await self.gecko.get_ohlcv(
                chain=token.chain,
                pool_address=pair_addr,
                timeframe=timeframe,
                limit=limit,
            )
        except Exception as exc:
            logger.debug(f"OHLCV fetch failed for {token.symbol}: {exc}")
            return []

        if not candles:
            return []

        rows: List[OHLCV] = []
        async with get_session() as session:
            for c in candles:
                # GeckoTerminal: [timestamp, open, high, low, close, volume]
                if len(c) < 6:
                    continue
                ts = int(c[0])
                # Skip if exists (idempotent)
                exists_stmt = select(OHLCV).where(
                    and_(
                        OHLCV.token_id == token_id,
                        OHLCV.timeframe == timeframe,
                        OHLCV.timestamp == ts,
                    )
                )
                existing = await session.execute(exists_stmt)
                if existing.scalar_one_or_none() is not None:
                    continue

                row = OHLCV(
                    token_id=token_id,
                    timeframe=timeframe,
                    timestamp=ts,
                    open=float(c[1]),
                    high=float(c[2]),
                    low=float(c[3]),
                    close=float(c[4]),
                    volume=float(c[5]),
                )
                session.add(row)
                rows.append(row)
            await session.flush()

        logger.debug(f"OHLCV {token.symbol} {timeframe}: +{len(rows)} rows")
        return rows

    async def get_latest_candles(
        self, token_id: str, timeframe: str, limit: int = 200
    ) -> List[OHLCV]:
        """Get the latest N candles from DB."""
        async with get_session() as session:
            stmt = (
                select(OHLCV)
                .where(
                    and_(
                        OHLCV.token_id == token_id,
                        OHLCV.timeframe == timeframe,
                    )
                )
                .order_by(desc(OHLCV.timestamp))
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            rows.reverse()  # ascending for charting / indicator math
            return rows

    async def close(self):
        await self.gecko.close()
