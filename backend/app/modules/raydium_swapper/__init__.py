"""
Raydium post-graduation swapper (V901).

When a Pump.fun token's bonding curve completes (~$69K market cap), it
"graduates" and migrates to a Raydium AMM pool (Raydium AMM v4 or Raydium
CPMM). After March 2025 some tokens migrate to PumpSwap instead, but the
PumpPortal `pool` parameter accepts "raydium", "raydium-cpmm", and "pump-amm"
so we route accordingly.

This module:
  1. Detects graduation (by polling the token's `complete` flag / `raydium_pool`
     address via PumpPortalClient.get_token_info).
  2. Migrates the runner portion (the 40% left after the +30% partial TP)
     from Pump.fun trading to Raydium trading.
  3. Applies the wider Raydium exit ladder:
       - No hard stop loss on Raydium (price action is steadier with real LP).
       - 40% trail width (vs 30% on Pump.fun) — let moonshots run further.
       - 48h stagnation exit, 10-day max hold.
       - 1.2% round-trip fee (vs 3% on Pump.fun).

The actual swap is routed through PumpPortalClient.sell_token() with the right
`pool` parameter. We do NOT sign locally — PumpPortal Lightning signs from the
linked wallet, same as Pump.fun buys/sells. This keeps the security model
identical.

Integration points:
  - LiveTradingExecutor calls `RaydiumSwapper.try_migrate(token, position)`
    after each pump.fun sell that leaves a runner.
  - The orchestrator's exit loop calls `RaydiumSwapper.check_raydium_exit(...)`
    for positions flagged `on_raydium = True` and uses `sell_on_raydium()`
    to execute the exit.

NOTE: PumpPortal's WebSocket supports `subscribeMigration` for real-time
graduation events, but the current PumpPortalClient does not implement it yet.
Until it does, we poll get_token_info on each exit check. This is acceptable
because the exit loop already polls price every ~10s; adding a graduation
check there costs one extra HTTP call.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ...core.config import settings
from ...core.logger import logger
from ..market_scanner.pumpfun_client import PumpFunToken, PumpPortalClient


# Pool routing: which Raydium variant the token migrated to.
# PumpPortal accepts these as the `pool` parameter on sell_token().
RAYDIUM_POOL_KEYS = {
    "raydium": "raydium",            # Raydium AMM v4 (legacy, most common pre-2025)
    "raydium_cpmm": "raydium-cpmm",  # Raydium CPMM (concentrated liquidity)
    "pumpswap": "pump-amm",          # Pump.fun's own AMM (post March 2025)
    "pump-amm": "pump-amm",
}


@dataclass
class MigrationEvent:
    """Fired when a Pump.fun token graduates to a DEX."""
    mint: str
    pool_type: str               # "raydium" | "raydium_cpmm" | "pumpswap"
    pool_address: str
    graduated_at: datetime
    dex: str = ""                # raw dex field from token info


class RaydiumSwapper:
    """Detect Pump.fun graduation and continue trading the runner on Raydium.

    Thin coordination layer — the actual swap call goes through the existing
    PumpPortalClient.sell_token() with the right `pool` parameter. We do not
    re-implement swap logic; we route the trade to the right pool and apply the
    wider Raydium exit ladder.
    """

    def __init__(self, client: Optional[PumpPortalClient] = None):
        self.client = client or PumpPortalClient()
        self._migrations: Dict[str, MigrationEvent] = {}  # mint -> event
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Graduation detection
    # ------------------------------------------------------------------
    async def detect_migration(self, mint: str) -> Optional[MigrationEvent]:
        """Check if a token has graduated from Pump.fun.

        Polls PumpPortalClient.get_token_info(). If the bonding curve is
        complete (`token.complete == True`) and a pool address exists, the
        token has migrated. We cache the result so we don't re-poll.
        """
        if mint in self._migrations:
            return self._migrations[mint]

        try:
            token = await self.client.get_token_info(mint)
        except Exception as exc:
            logger.warning(f"raydium: get_token_info failed for {mint}: {exc}")
            return None

        if token is None:
            return None

        # PumpFunToken.complete: bonding curve finished -> token migrated.
        # PumpFunToken.raydium_pool: the migrated pool address (if any).
        if not getattr(token, "complete", False):
            return None

        pool_address = getattr(token, "raydium_pool", "") or ""
        if not pool_address:
            logger.info(f"raydium: {mint} graduated but no pool address yet")
            return None

        # Determine pool type from the dex field. PumpPortal exposes this in
        # the token data; if absent or unknown, default to "raydium".
        dex = (getattr(token, "dex", "") or "").lower()
        if dex in ("raydium_cpmm", "raydium-cpmm"):
            pool_type = "raydium_cpmm"
        elif dex in ("pumpswap", "pump-amm", "pumpamm"):
            pool_type = "pumpswap"
        else:
            pool_type = "raydium"  # default to AMM v4

        event = MigrationEvent(
            mint=mint,
            pool_type=pool_type,
            pool_address=pool_address,
            graduated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            dex=dex,
        )
        async with self._lock:
            self._migrations[mint] = event
        logger.info(
            f"raydium: {mint} graduated -> pool={pool_type} "
            f"addr={pool_address[:12] if pool_address else 'none'}..."
        )
        return event

    # ------------------------------------------------------------------
    # Swap routing
    # ------------------------------------------------------------------
    async def sell_on_raydium(self, mint: str, amount_tokens: float,
                              slippage_hint_pct: float = 15.0) -> Dict[str, Any]:
        """Sell the runner portion on the migrated Raydium/PumpSwap pool.

        Routes through PumpPortal Lightning via sell_token() with the right
        `pool` parameter. `amount_tokens` is the number of tokens to sell
        (or pass the string "100%" to liquidate the whole remaining balance).

        `slippage_hint_pct` is advisory — the actual slippage comes from the
        client's configured `slippage_pct`. We log it for visibility.
        """
        migration = self._migrations.get(mint)
        if migration is None:
            migration = await self.detect_migration(mint)
            if migration is None:
                logger.warning(f"raydium: cannot sell {mint} — not migrated yet")
                return {"ok": False, "reason": "not_migrated"}

        pool_key = RAYDIUM_POOL_KEYS.get(migration.pool_type, "raydium")
        logger.info(
            f"raydium: SELL {mint} tokens={amount_tokens} pool={pool_key} "
            f"slippage_hint={slippage_hint_pct}%"
        )

        try:
            result = await self.client.sell_token(
                mint_address=mint,
                amount_tokens=amount_tokens,
                pool=pool_key,
            )
            ok = getattr(result, "success", False)
            return {
                "ok": ok,
                "result": result,
                "pool": pool_key,
                "error": getattr(result, "error", None) if not ok else None,
            }
        except Exception as exc:
            logger.error(f"raydium: sell failed for {mint}: {exc}")
            return {"ok": False, "reason": str(exc), "pool": pool_key}

    # ------------------------------------------------------------------
    # Exit ladder (Raydium-specific, wider than Pump.fun)
    # ------------------------------------------------------------------
    def check_raydium_exit(self, entry_price: float, peak_price: float,
                           current_price: float, elapsed_minutes: float
                           ) -> Tuple[Optional[str], float]:
        """V901 Raydium exit ladder.

        Returns (reason, sell_frac) where reason is None if no exit.
        Wider than Pump.fun: 40% trail, no SL, 48h stagnation, 10-day max hold.
        """
        if entry_price <= 0 or peak_price <= 0 or current_price <= 0:
            return None, 0.0

        peak_gain = peak_price / entry_price - 1.0
        drop_from_peak = (peak_price - current_price) / peak_price

        # 1. Wide trailing stop (40% on Raydium — let moonshots run further)
        trail_start = settings.v901_raydium_trail_start_pct / 100.0
        trail_width = settings.v901_raydium_trail_width_pct / 100.0
        if peak_gain >= trail_start:
            if current_price <= peak_price * (1.0 - trail_width):
                return "raydium_trail", 1.0

        # 2. Sudden dump (protect against catastrophic LP drain)
        sudden_gain = settings.v901_sudden_dump_gain_pct / 100.0
        sudden_drop = settings.v901_sudden_dump_drop_pct / 100.0
        if peak_gain >= sudden_gain and drop_from_peak >= sudden_drop:
            return "raydium_sudden_dump", 1.0

        # 3. Stagnation exit (48h with no meaningful gain)
        if elapsed_minutes >= settings.v901_raydium_stagnation_minutes:
            if peak_gain < 0.05:
                return "raydium_stagnation", 1.0

        # 4. Time exit (10 days max hold on Raydium)
        if elapsed_minutes >= settings.v901_raydium_max_hold_minutes:
            return "raydium_time_exit", 1.0

        return None, 0.0

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def is_on_raydium(self, mint: str) -> bool:
        return mint in self._migrations

    def get_migration(self, mint: str) -> Optional[MigrationEvent]:
        return self._migrations.get(mint)

    def clear(self) -> None:
        """Clear cached migrations (for tests / restart)."""
        self._migrations.clear()


# Module-level singleton for convenience.
_default_swapper: Optional[RaydiumSwapper] = None


def get_raydium_swapper() -> RaydiumSwapper:
    global _default_swapper
    if _default_swapper is None:
        _default_swapper = RaydiumSwapper()
    return _default_swapper
