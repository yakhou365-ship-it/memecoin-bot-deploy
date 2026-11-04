"""
Live trading executor (real SOL on Pump.fun via PumpPortal).

This is the only module allowed to move real money.  It is deliberately
conservative:

* Every guard in :mod:`app.core.config` must pass before a buy is attempted
  (``is_live_trading`` checks mode, keys and the explicit confirmation phrase).
* A hard per-trade cap (``PUMP_MAX_TRADE_SOL``, default 0.1 SOL).
* A hard daily spend cap (``LIVE_DAILY_CAP_SOL``, default 0.5 SOL).
* A hard daily realised-loss limit (``LIVE_DAILY_LOSS_LIMIT_SOL``, default
  0.2 SOL) that stops trading for the rest of the day.
* A hard limit on concurrent live positions (``LIVE_MAX_OPEN_POSITIONS``).
* The on-chain wallet balance is checked before every buy.  If the wallet has
  no SOL the trade is rejected with a precise reason instead of pretending.

Live trades are stored in the same ``trades`` table as paper trades but tagged
with ``strategy_id = "live_pumpfun"``.  The paper engine ignores them, and the
dashboard can tell them apart.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import SystemState, Token, Trade
from ..market_scanner.pumpfun_client import PumpPortalClient

LIVE_STRATEGY_ID = "live_pumpfun"
DAILY_STATE_KEY = "live_daily_state"
# PumpPortal Lightning fee is 1% of notional, charged on each side.
PUMPPORTAL_FEE_PCT = 0.01


def is_live_trade(trade: Trade) -> bool:
    """True when a Trade row was executed with real funds."""
    return (trade.strategy_id or "").startswith("live")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today() -> str:
    return _utcnow().date().isoformat()


class LiveTradingExecutor:
    """Execute and manage real Pump.fun positions."""

    def __init__(self, client: Optional[PumpPortalClient] = None):
        self.client = client or PumpPortalClient()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Configuration / status
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return settings.is_live_trading

    async def status(self) -> Dict[str, Any]:
        daily = await self._load_daily()
        wallet = await self.client.get_wallet_balance()
        open_count = await self._count_open_live()
        return {
            "configured": self.configured,
            "missing_requirements": settings.live_requirements,
            "trading_mode": settings.trading_mode,
            "wallet_address": settings.solana_wallet_address,
            "wallet_sol": round(float(wallet.get("sol", 0.0)), 9),
            "max_trade_sol": settings.pump_max_trade_sol,
            "daily_cap_sol": settings.live_daily_cap_sol,
            "daily_loss_limit_sol": settings.live_daily_loss_limit_sol,
            "max_open_positions": settings.live_max_open_positions,
            "open_live_positions": open_count,
            "daily": daily,
            "can_open": (
                self.configured
                and float(wallet.get("sol", 0.0)) > 0
                and daily["sol_spent"] < settings.live_daily_cap_sol
                and daily["sol_pnl"] > -settings.live_daily_loss_limit_sol
                and open_count < settings.live_max_open_positions
            ),
        }

    async def _count_open_live(self) -> int:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id == LIVE_STRATEGY_ID,
                    )
                )
            ).scalars().all()
            return len(rows)

    # ------------------------------------------------------------------
    # Daily accounting (SOL denominated)
    # ------------------------------------------------------------------
    async def _load_daily(self) -> Dict[str, Any]:
        default = {
            "date": _today(),
            "sol_spent": 0.0,
            "sol_pnl": 0.0,
            "trades_opened": 0,
            "trades_closed": 0,
        }
        async with get_session() as session:
            row = await session.get(SystemState, DAILY_STATE_KEY)
            data = dict(row.value_json or {}) if row else {}
        if data.get("date") != _today():
            return default
        for key, value in default.items():
            data.setdefault(key, value)
        return data

    async def _save_daily(self, data: Dict[str, Any]) -> None:
        async with get_session() as session:
            row = await session.get(SystemState, DAILY_STATE_KEY)
            if row is None:
                row = SystemState(key=DAILY_STATE_KEY, value_json=dict(data))
                session.add(row)
            else:
                row.value_json = dict(data)
                session.add(row)
            await session.commit()

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------
    async def open_position(
        self,
        token_id: str,
        amount_sol: Optional[float] = None,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        holding_period_minutes: int = 60,
        analysis_snapshot_id: Optional[str] = None,
        entry_reasons: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Attempt a real buy.  Returns a result dict, never raises."""
        async with self._lock:
            if not self.configured:
                return self._reject(
                    "live_trading_not_configured",
                    "Live trading requirements missing: "
                    + ", ".join(settings.live_requirements),
                )

            amount = float(amount_sol or settings.pump_max_trade_sol)
            if amount <= 0:
                return self._reject("invalid_amount", "Amount must be positive")
            if amount > settings.pump_max_trade_sol + 1e-9:
                return self._reject(
                    "amount_above_cap",
                    f"{amount} SOL exceeds per-trade cap {settings.pump_max_trade_sol} SOL",
                )

            daily = await self._load_daily()
            if daily["sol_spent"] + amount > settings.live_daily_cap_sol + 1e-9:
                return self._reject(
                    "daily_cap_reached",
                    f"Daily cap {settings.live_daily_cap_sol} SOL would be exceeded "
                    f"(spent {daily['sol_spent']:.4f} SOL)",
                )
            if daily["sol_pnl"] <= -settings.live_daily_loss_limit_sol:
                return self._reject(
                    "daily_loss_limit_reached",
                    f"Daily loss limit {settings.live_daily_loss_limit_sol} SOL reached "
                    f"(pnl {daily['sol_pnl']:.4f} SOL)",
                )

            open_count = await self._count_open_live()
            if open_count >= settings.live_max_open_positions:
                return self._reject(
                    "max_open_positions",
                    f"Already {open_count} live positions open "
                    f"(max {settings.live_max_open_positions})",
                )

            # Resolve token
            async with get_session() as session:
                token = await session.get(Token, token_id)
                if token is None:
                    return self._reject("token_not_found", f"Token {token_id} not found")
                mint = token.address
                symbol = token.symbol
                price_usd = float(entry_price or token.price_usd or 0.0)
                pool = self._pool_for(getattr(token, "dex", None))

            if not mint:
                return self._reject("missing_mint", "Token has no on-chain address")

            # Wallet must actually hold SOL
            wallet = await self.client.get_wallet_balance()
            sol_balance = float(wallet.get("sol", 0.0))
            required = amount + 0.005  # leave room for priority fee + ATA rent
            if sol_balance < required:
                return self._reject(
                    "insufficient_wallet_balance",
                    f"Wallet {settings.solana_wallet_address} holds {sol_balance:.6f} SOL; "
                    f"{required:.4f} SOL required. Fund the wallet before live trading.",
                    extra={"wallet_sol": sol_balance, "required_sol": required},
                )

            sol_price_usd = await self.client._fetch_sol_price()
            result = await self.client.buy_token(mint_address=mint, amount_sol=amount, pool=pool)
            if not result.success:
                logger.warning(f"Live buy failed for {symbol}: {result.error}")
                return self._reject("buy_failed", result.error or "PumpPortal rejected the buy")

            # Estimate fill price / quantity.  PumpPortal does not return the
            # exact token amount, so we derive it from the pre-trade price.
            notional_usd = amount * sol_price_usd
            fill_price = price_usd if price_usd > 0 else notional_usd
            quantity = notional_usd / fill_price if fill_price > 0 else 0.0
            entry_fee_usd = notional_usd * PUMPPORTAL_FEE_PCT

            reasons = list(entry_reasons or [])
            reasons.append(f"LIVE buy {amount} SOL via PumpPortal")
            reasons.append(f"tx:{result.tx_signature}")

            trade = Trade(
                token_id=token_id,
                analysis_snapshot_id=analysis_snapshot_id,
                side="long",
                entry_price=fill_price,
                entry_size_usd=notional_usd,
                entry_quantity=quantity,
                entry_fee_usd=entry_fee_usd,
                entry_slippage_usd=0.0,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                planned_holding_period_minutes=holding_period_minutes,
                strategy_id=LIVE_STRATEGY_ID,
                fees_total_usd=entry_fee_usd,
                slippage_total_usd=0.0,
                entry_reasons_json=reasons,
                status="open",
            )
            async with get_session() as session:
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                trade_id = trade.id

            daily["sol_spent"] = round(float(daily["sol_spent"]) + amount, 9)
            daily["trades_opened"] = int(daily["trades_opened"]) + 1
            await self._save_daily(daily)

            logger.info(
                f"🟢 LIVE BUY {symbol} {amount} SOL @ ~${fill_price:.10f} "
                f"tx={result.tx_signature} trade={trade_id}"
            )
            return {
                "success": True,
                "trade_id": trade_id,
                "tx_signature": result.tx_signature,
                "symbol": symbol,
                "amount_sol": amount,
                "fill_price": fill_price,
                "notional_usd": notional_usd,
                "wallet_sol_before": sol_balance,
            }

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------
    async def close_position(self, trade_id: str, exit_reason: str = "manual") -> Dict[str, Any]:
        """Sell 100% of a live position and record the realised result."""
        async with self._lock:
            async with get_session() as session:
                trade = await session.get(Trade, trade_id)
                if trade is None or trade.status != "open" or not is_live_trade(trade):
                    return self._reject("not_an_open_live_trade", "No open live trade with that id")
                token = await session.get(Token, trade.token_id)
                mint = token.address if token else ""
                symbol = token.symbol if token else trade.token_id
                exit_price = float(token.price_usd or trade.entry_price or 0.0) if token else float(trade.entry_price or 0)
                pool = self._pool_for(getattr(token, "dex", None)) if token else None

            if not mint:
                return self._reject("missing_mint", "Cannot sell: token mint unknown")

            result = await self.client.sell_token(mint_address=mint, amount_tokens="100%", pool=pool)
            if not result.success:
                logger.warning(f"Live sell failed for {symbol}: {result.error}")
                return self._reject("sell_failed", result.error or "PumpPortal rejected the sell")

            sol_price_usd = await self.client._fetch_sol_price()
            proceeds_usd = trade.entry_quantity * exit_price
            exit_fee_usd = proceeds_usd * PUMPPORTAL_FEE_PCT
            gross_pnl = (exit_price - float(trade.entry_price)) * float(trade.entry_quantity)
            net_pnl = gross_pnl - float(trade.entry_fee_usd or 0) - exit_fee_usd
            pnl_pct = (net_pnl / float(trade.entry_size_usd) * 100) if trade.entry_size_usd else 0.0

            async with get_session() as session:
                db_trade = await session.get(Trade, trade_id)
                if db_trade is None or db_trade.status != "open":
                    return self._reject("already_closed", "Trade was already closed")
                db_trade.exit_price = exit_price
                db_trade.exit_timestamp = _utcnow()
                db_trade.exit_fee_usd = exit_fee_usd
                db_trade.exit_reason = exit_reason
                db_trade.gross_pnl_usd = gross_pnl
                db_trade.net_pnl_usd = net_pnl
                db_trade.pnl_pct = pnl_pct
                db_trade.fees_total_usd = float(db_trade.entry_fee_usd or 0) + exit_fee_usd
                db_trade.status = "closed"
                db_trade.exit_reasons_json = list(db_trade.exit_reasons_json or []) + [
                    exit_reason,
                    f"LIVE sell 100% tx:{result.tx_signature}",
                    "pnl_estimated_from_market_price",
                ]
                session.add(db_trade)
                await session.commit()

            daily = await self._load_daily()
            daily["sol_pnl"] = round(
                float(daily["sol_pnl"]) + (net_pnl / sol_price_usd if sol_price_usd else 0.0),
                9,
            )
            daily["trades_closed"] = int(daily["trades_closed"]) + 1
            await self._save_daily(daily)

            logger.info(
                f"🔴 LIVE SELL {symbol} @ ~${exit_price:.10f} pnl=${net_pnl:.4f} "
                f"({pnl_pct:+.2f}%) reason={exit_reason} tx={result.tx_signature}"
            )
            return {
                "success": True,
                "trade_id": trade_id,
                "tx_signature": result.tx_signature,
                "symbol": symbol,
                "exit_price": exit_price,
                "net_pnl_usd": net_pnl,
                "pnl_pct": pnl_pct,
            }

    # ------------------------------------------------------------------
    # Manage open live positions (SL / TP / time exit)
    # ------------------------------------------------------------------
    async def manage_open_positions(self) -> Dict[str, int]:
        closed = {"stop_loss": 0, "take_profit": 0, "time_exit": 0, "trailing_stop": 0}
        async with get_session() as session:
            trades = (
                await session.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id == LIVE_STRATEGY_ID,
                    )
                )
            ).scalars().all()

        for trade in trades:
            try:
                async with get_session() as session:
                    token = await session.get(Token, trade.token_id)
                    price = float(token.price_usd) if token and token.price_usd else None
                if price is None or price <= 0:
                    continue

                reason = None
                if trade.stop_loss_price and price <= trade.stop_loss_price:
                    reason = "stop_loss"
                elif trade.take_profit_price and price >= trade.take_profit_price:
                    reason = "take_profit"
                elif trade.planned_holding_period_minutes and trade.entry_timestamp:
                    age_min = (_utcnow() - trade.entry_timestamp).total_seconds() / 60
                    if age_min >= trade.planned_holding_period_minutes:
                        reason = "time_exit"

                if reason:
                    result = await self.close_position(trade.id, reason)
                    if result.get("success"):
                        closed[reason] = closed.get(reason, 0) + 1
            except Exception as exc:
                logger.exception(f"Live position management failed for {trade.id}: {exc}")

        return closed

    # ------------------------------------------------------------------
    @staticmethod
    def _pool_for(dex: Optional[str]) -> Optional[str]:
        """Map a DexScreener dex id to a PumpPortal pool.

        pump.fun bonding-curve tokens use "pump"; graduated tokens must trade
        on their actual venue (pump-amm/raydium); unknown/other DEXes use
        "auto" so PumpPortal routes correctly. None keeps the client default.
        """
        if not dex:
            return None
        d = str(dex).strip().lower()
        if d in ("pumpfun", "pump"):
            return "pump"
        if d in ("pumpswap", "pump-amm", "pump_amm"):
            return "pump-amm"
        if d in ("raydium", "raydium-cpmm", "raydium_cpmm"):
            return "raydium-cpmm" if "cpmm" in d else "raydium"
        if d in ("launchlab", "bonk"):
            return d
        return "auto"

    @staticmethod
    def _reject(code: str, message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"success": False, "code": code, "error": message}
        if extra:
            payload.update(extra)
        return payload
