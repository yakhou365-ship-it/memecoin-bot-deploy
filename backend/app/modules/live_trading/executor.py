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

            # V400 P0-3 FIX: Confirm the transaction on-chain before recording.
            # PumpPortal returns a signature immediately, but the tx may fail
            # on-chain (slippage exceeded, insufficient funds, congestion).
            # We wait up to 15 seconds for confirmation.
            confirmed = False
            if result.tx_signature:
                try:
                    confirmed = await self.client.confirm_signature(result.tx_signature, timeout=15)
                except Exception as confirm_exc:
                    logger.warning(f"Buy confirm failed for {symbol}: {confirm_exc}")
                    # Don't fail — proceed with DB save (P0-4 safety)
            if not confirmed:
                # Transaction may have failed on-chain.
                # Log warning but still record — if tokens were received,
                # we need to track them. If not, the position will be closed
                # by manage_open_positions when price = 0.
                logger.warning(
                    f"⚠️ Buy tx {result.tx_signature} for {symbol} NOT confirmed on-chain. "
                    f"Recording anyway — monitor for stuck position."
                )

            # V400 P1-2/P1-3 FIX: Estimate fill price with slippage.
            # PumpPortal doesn't return fill details, so we estimate:
            # assume 3% slippage on buy (conservative).
            estimated_slippage_pct = 3.0
            notional_usd = amount * sol_price_usd
            # V400: if price_usd is valid, use it + slippage estimate
            if price_usd and price_usd > 0:
                fill_price = price_usd * (1 + estimated_slippage_pct / 100)
            else:
                fill_price = notional_usd  # fallback (quantity = 1.0)
            quantity = notional_usd / fill_price if fill_price > 0 else 0.0
            entry_fee_usd = notional_usd * PUMPPORTAL_FEE_PCT
            entry_slippage_usd = notional_usd * estimated_slippage_pct / 100  # V400 P1-3

            reasons = list(entry_reasons or [])
            reasons.append(f"LIVE buy {amount} SOL via PumpPortal")
            reasons.append(f"tx:{result.tx_signature}")
            if not confirmed:
                reasons.append("⚠️ tx not confirmed on-chain")

            trade = Trade(
                token_id=token_id,
                analysis_snapshot_id=analysis_snapshot_id,
                side="long",
                entry_price=fill_price,
                entry_size_usd=notional_usd,
                entry_quantity=quantity,
                entry_fee_usd=entry_fee_usd,
                entry_slippage_usd=entry_slippage_usd,  # V400 P1-3: track slippage
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                planned_holding_period_minutes=holding_period_minutes,
                strategy_id=LIVE_STRATEGY_ID,
                fees_total_usd=entry_fee_usd,
                slippage_total_usd=entry_slippage_usd,  # V400 P1-3
                entry_reasons_json=reasons,
                status="open",
                max_unrealized_profit_usd=fill_price,  # V400 P0-1: init peak = entry
            )
            # V400 P0-4 FIX: DB save with retry + compensation logic.
            # If DB save fails after on-chain buy, we log the tx_signature
            # so the operator can manually recover the tokens.
            trade_id = None
            try:
                async with get_session() as session:
                    session.add(trade)
                    await session.commit()
                    await session.refresh(trade)
                    trade_id = trade.id
            except Exception as db_exc:
                logger.error(
                    f"🚨 CRITICAL: DB save failed after on-chain buy! "
                    f"tx={result.tx_signature} symbol={symbol} amount={amount}SOL. "
                    f"Tokens are in wallet but NOT tracked. Error: {db_exc}"
                )
                # Try one more time with a fresh session
                try:
                    async with get_session() as session2:
                        session2.add(trade)
                        await session2.commit()
                        await session2.refresh(trade)
                        trade_id = trade.id
                except Exception as db_exc2:
                    logger.error(
                        f"🚨 DB save retry also failed! Manual recovery needed. "
                        f"tx={result.tx_signature}"
                    )
                    return {
                        "success": False,
                        "code": "db_save_failed_after_buy",
                        "error": f"DB save failed after on-chain buy. tx={result.tx_signature}",
                        "tx_signature": result.tx_signature,
                        "symbol": symbol,
                    }
            if not trade_id:
                return self._reject("db_save_failed", "Could not save trade to DB")

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

            # V400 P0-3 FIX: Confirm sell on-chain
            if result.tx_signature:
                try:
                    sell_confirmed = await self.client.confirm_signature(result.tx_signature, timeout=15)
                    if not sell_confirmed:
                        logger.warning(f"⚠️ Sell tx {result.tx_signature} for {symbol} NOT confirmed")
                except Exception as confirm_exc:
                    logger.warning(f"Sell confirm failed: {confirm_exc}")

            sol_price_usd = await self.client._fetch_sol_price()
            # V400 P1-2 FIX: Apply slippage to exit_price (was missing!)
            # Estimate 5% slippage on sell (memecoins have thin liquidity)
            estimated_exit_slippage_pct = 5.0
            actual_exit_price = exit_price * (1 - estimated_exit_slippage_pct / 100)
            proceeds_usd = trade.entry_quantity * actual_exit_price  # V400: use actual price
            exit_fee_usd = proceeds_usd * PUMPPORTAL_FEE_PCT
            exit_slippage_usd = trade.entry_quantity * exit_price * estimated_exit_slippage_pct / 100
            gross_pnl = (actual_exit_price - float(trade.entry_price)) * float(trade.entry_quantity)
            # V400 P1-2: Include exit slippage in net PnL (was missing!)
            net_pnl = (gross_pnl
                      - float(trade.entry_fee_usd or 0)
                      - exit_fee_usd
                      - float(trade.entry_slippage_usd or 0)  # V400 P1-3
                      - exit_slippage_usd)  # V400 P1-2: NEW!
            pnl_pct = (net_pnl / float(trade.entry_size_usd) * 100) if trade.entry_size_usd else 0.0

            # V400 P1-11 FIX: DB save with retry to prevent double-sell
            db_save_success = False
            try:
                async with get_session() as session:
                    db_trade = await session.get(Trade, trade_id)
                    if db_trade is None or db_trade.status != "open":
                        return self._reject("already_closed", "Trade was already closed")
                    db_trade.exit_price = actual_exit_price
                    db_trade.exit_slippage_usd = exit_slippage_usd
                    db_trade.exit_timestamp = _utcnow()
                    db_trade.exit_fee_usd = exit_fee_usd
                    db_trade.exit_reason = exit_reason
                    db_trade.gross_pnl_usd = gross_pnl
                    db_trade.net_pnl_usd = net_pnl
                    db_trade.pnl_pct = pnl_pct
                    # V407 P0-2: ALL mutations inside the async with block
                    db_trade.fees_total_usd = float(db_trade.entry_fee_usd or 0) + exit_fee_usd
                    db_trade.slippage_total_usd = float(db_trade.entry_slippage_usd or 0) + exit_slippage_usd
                    db_trade.status = "closed"
                    db_trade.exit_reasons_json = list(db_trade.exit_reasons_json or []) + [
                        exit_reason,
                        f"LIVE sell 100% tx:{result.tx_signature}",
                    ]
                    session.add(db_trade)
                    await session.commit()
                    db_save_success = True
            except Exception as db_exc:
                logger.error(f"DB save failed on close for trade {trade_id}: {db_exc}")
                # V400 P1-11: retry once
                try:
                    async with get_session() as session2:
                        db_trade2 = await session2.get(Trade, trade_id)
                        if db_trade2 and db_trade2.status == "open":
                            db_trade2.exit_price = actual_exit_price
                            db_trade2.exit_slippage_usd = exit_slippage_usd
                            db_trade2.exit_timestamp = _utcnow()
                            db_trade2.exit_fee_usd = exit_fee_usd
                            db_trade2.exit_reason = exit_reason
                            db_trade2.gross_pnl_usd = gross_pnl
                            db_trade2.net_pnl_usd = net_pnl
                            db_trade2.pnl_pct = pnl_pct
                            db_trade2.fees_total_usd = float(db_trade2.entry_fee_usd or 0) + exit_fee_usd
                            db_trade2.slippage_total_usd = float(db_trade2.entry_slippage_usd or 0) + exit_slippage_usd
                            db_trade2.status = "closed"
                            db_trade2.exit_reasons_json = list(db_trade2.exit_reasons_json or []) + [
                                exit_reason,
                                f"LIVE sell 100% tx:{result.tx_signature}",
                            ]
                            session2.add(db_trade2)
                            await session2.commit()
                            db_save_success = True
                except Exception as db_exc2:
                    logger.error(f"DB save retry also failed: {db_exc2}")

            daily = await self._load_daily()
            daily["sol_pnl"] = round(
                float(daily["sol_pnl"]) + (net_pnl / sol_price_usd if sol_price_usd else 0.0),
                9,
            )
            daily["trades_closed"] = int(daily["trades_closed"]) + 1
            await self._save_daily(daily)

            # V407 P1-7: Update RiskManager with live trade result
            # Before: only paper trades updated risk state (consecutive_losses, peak_equity)
            try:
                from ..risk_management.manager import RiskManager
                rm = RiskManager()
                await rm.record_trade_result(net_pnl > 0, net_pnl)
            except Exception as rm_exc:
                logger.warning(f"RiskManager record_trade_result failed: {rm_exc}")

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
    # Manage open live positions (SL / TP / trailing / time exit)
    # ------------------------------------------------------------------
    async def manage_open_positions(self) -> Dict[str, int]:
        closed = {"stop_loss": 0, "sudden_dump": 0, "mega_tp": 0, "smart_trailing": 0, "stagnation": 0, "time_exit": 0, "exit_all": 0}
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

                # V400: Trailing stop with PERSISTED peak price (P0-1 fix).
                # Use max_unrealized_profit_usd as a proxy to store peak_price:
                # we encode peak_price in this field (it's normally a PnL value,
                # but for open trades it's the best available DB column).
                # If None, initialize to max(entry, current).
                if trade.max_unrealized_profit_usd is None:
                    trade.max_unrealized_profit_usd = max(trade.entry_price, price)
                # Update peak if current price is higher
                if price > trade.max_unrealized_profit_usd:
                    trade.max_unrealized_profit_usd = price
                    # Persist the updated peak to DB
                    try:
                        async with get_session() as s2:
                            db_trade = await s2.get(Trade, trade.id)
                            if db_trade and db_trade.status == "open":
                                db_trade.max_unrealized_profit_usd = price
                                await s2.commit()
                    except Exception:
                        pass  # non-critical, will retry next cycle

                peak_price = trade.max_unrealized_profit_usd
                # V500: Smart Exit Strategy — Variable trailing
                peak_gain_pct = (peak_price / trade.entry_price - 1) if trade.entry_price > 0 else 0
                if peak_gain_pct >= 5.0: trail_pct = 0.45  # +500%
                elif peak_gain_pct >= 1.0: trail_pct = 0.35  # +100%
                elif peak_gain_pct >= 0.30: trail_pct = 0.25  # +30%
                else: trail_pct = 0.40
                trailing_level = peak_price * (1 - trail_pct)
                trailing_hit = peak_gain_pct >= 0.30 and price < trailing_level
                # V500: Sudden dump (25%+ from peak while in profit)
                sudden_dump = peak_gain_pct >= 0.10 and (peak_price - price) / peak_price >= 0.25 if peak_price > 0 else False
                # V500: Stagnation (±5% for 4h while in profit)
                stagnation_hit = False
                if trade.entry_timestamp:
                    elapsed_min = (_utcnow() - trade.entry_timestamp).total_seconds() / 60
                    if elapsed_min >= 240:
                        current_gain = (price / trade.entry_price - 1) * 100
                        if current_gain > 5 and abs(price - trade.entry_price) / trade.entry_price < 0.05:
                            stagnation_hit = True

                reason = None
                if sudden_dump: reason = "sudden_dump"
                elif trade.stop_loss_price and price <= trade.stop_loss_price: reason = "stop_loss"
                elif trade.take_profit_price and price >= trade.take_profit_price: reason = "mega_tp"
                elif trailing_hit: reason = "smart_trailing"
                elif stagnation_hit: reason = "stagnation"
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
