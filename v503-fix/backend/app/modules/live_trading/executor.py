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
# V504: shared with RiskManager — start-of-day wallet USD value used for
# %-based daily-loss / weekly-loss / drawdown gates. Without this the risk
# manager falls back to paper_capital_usd = $10000 (a small live wallet
# could lose 100% without ever tripping a 5% daily-loss limit).
LIVE_CAPITAL_KEY = "live_capital_base"
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
        self._sold_pending_db: set = set()  # V503: sold on-chain but DB close failed

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
    # V505: real-slippage measurement from wallet balance diff.
    # PumpPortal doesn't return fill details, so we measure the actual
    # SOL spent / tokens received by snapshotting the wallet before and
    # after each trade. This replaces the 3% buy / 5% sell estimates
    # with the true on-chain cost, which is essential for accurate PnL.
    # ------------------------------------------------------------------
    async def _snapshot_wallet(self, mint: Optional[str] = None) -> Dict[str, Any]:
        """Capture SOL balance + token balance for ``mint`` (if provided).

        Returns ``{"sol": float, "token_balance": float, "ts": datetime}``.
        Best-effort: on RPC failure returns the SOL balance (or 0.0) with
        token_balance = None so the caller can decide whether to proceed.
        """
        snap: Dict[str, Any] = {"sol": 0.0, "token_balance": None, "ts": _utcnow()}
        if not self.client.wallet_address:
            return snap
        try:
            wallet = await self.client.get_wallet_balance()
            snap["sol"] = float(wallet.get("sol", 0.0) or 0.0)
        except Exception as exc:
            logger.debug(f"snapshot: SOL balance read failed: {exc}")
        if mint:
            try:
                token_bal = await self._get_token_balance(mint)
                snap["token_balance"] = token_bal
            except Exception as exc:
                logger.debug(f"snapshot: token balance read failed for {mint}: {exc}")
        return snap

    async def _get_token_balance(self, mint: str) -> Optional[float]:
        """Return the wallet's SPL token balance for ``mint`` (raw units), or None."""
        if not self.client.wallet_address or not mint:
            return None
        client = await self.client._get_client()
        try:
            resp = await client.post(
                settings.solana_rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.client.wallet_address,
                        {"mint": mint},
                        {"encoding": "jsonParsed"},
                    ],
                },
                timeout=5.0,
            )
            if resp.status_code != 200:
                return None
            accounts = (resp.json() or {}).get("result", {}).get("value", [])
            if not accounts:
                return 0.0
            # Sum across all ATAs (rare to have >1, but possible).
            total = 0.0
            for acc in accounts:
                info = (
                    acc.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                )
                amount_str = info.get("tokenAmount", {}).get("uiAmountString") or "0"
                try:
                    total += float(amount_str)
                except (TypeError, ValueError):
                    pass
            return total
        except Exception as exc:
            logger.debug(f"get_token_balance failed for {mint}: {exc}")
            return None

    def _compute_actual_slippage(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
        notional_usd: float,
        sol_price_usd: float,
        side: str,  # "buy" | "sell"
        market_price_usd: Optional[float] = None,
        token_quantity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute actual fill price + slippage from wallet diff.

        For BUY: sol_spent = before.sol - after.sol; tokens_received = after.token_balance - before.token_balance
        For SELL: tokens_sold = before.token_balance - after.token_balance; sol_received = after.sol - before.sol

        Returns dict with ``actual_slippage_pct``, ``actual_fill_price``, ``actual_fee_and_slippage_sol``,
        or empty values when the data isn't usable (RPC issues, ATA rent eating the diff, etc.).
        """
        out: Dict[str, Any] = {
            "actual_slippage_pct": None,
            "actual_fill_price": None,
            "actual_cost_sol": None,
            "measured": False,
            "reason": "",
        }
        if not sol_price_usd or sol_price_usd <= 0:
            out["reason"] = "no sol price"
            return out
        try:
            sol_diff = float(before.get("sol", 0.0)) - float(after.get("sol", 0.0))
            tok_before = before.get("token_balance")
            tok_after = after.get("token_balance")
            if side == "buy":
                sol_spent = sol_diff  # positive = spent
                if tok_before is None or tok_after is None:
                    out["reason"] = "token balance unavailable"
                    return out
                tokens_received = float(tok_after) - float(tok_before)
                if sol_spent <= 0 or tokens_received <= 0:
                    out["reason"] = f"invalid diff: sol_spent={sol_spent}, tokens={tokens_received}"
                    return out
                actual_fill_price = (sol_spent * sol_price_usd) / tokens_received
                actual_cost_sol = sol_spent
                if market_price_usd and market_price_usd > 0:
                    actual_slippage_pct = (actual_fill_price - market_price_usd) / market_price_usd * 100
                else:
                    actual_slippage_pct = None
            elif side == "sell":
                sol_received = -sol_diff  # before - after is negative on sell
                if tok_before is None or tok_after is None:
                    out["reason"] = "token balance unavailable"
                    return out
                tokens_sold = float(tok_before) - float(tok_after)
                if sol_received <= 0 or tokens_sold <= 0:
                    out["reason"] = f"invalid diff: sol_received={sol_received}, tokens={tokens_sold}"
                    return out
                actual_fill_price = (sol_received * sol_price_usd) / tokens_sold
                actual_cost_sol = sol_received
                if market_price_usd and market_price_usd > 0:
                    actual_slippage_pct = (market_price_usd - actual_fill_price) / market_price_usd * 100
                else:
                    actual_slippage_pct = None
            else:
                out["reason"] = f"unknown side: {side}"
                return out
            out["actual_fill_price"] = actual_fill_price
            out["actual_slippage_pct"] = actual_slippage_pct
            out["actual_cost_sol"] = actual_cost_sol
            out["measured"] = True
            return out
        except Exception as exc:
            out["reason"] = f"compute error: {exc}"
            return out

    async def _record_live_capital_base(
        self, wallet_sol: float, sol_price_usd: float
    ) -> None:
        """V504: persist start-of-day wallet USD for the RiskManager.

        The RiskManager's %-based daily-loss / weekly-loss / max-drawdown gates
        need a capital base to divide by.  Without this row they fall back to
        settings.paper_capital_usd = $10000, so a small live wallet ($20) could
        lose everything without ever tripping a 5% daily-loss limit.

        We record once per UTC day (or refresh if the price has moved >20% —
        e.g. a deposit / withdrawal / large PnL swing).  This is best-effort:
        any failure just logs a warning and the RiskManager keeps using the
        previous value or paper_capital_usd.
        """
        if wallet_sol <= 0 or sol_price_usd <= 0:
            return
        try:
            from sqlalchemy.orm.attributes import flag_modified
            today = _today()
            capital_usd = float(wallet_sol) * float(sol_price_usd)
            async with get_session() as session:
                row = await session.get(SystemState, LIVE_CAPITAL_KEY)
                v = dict(row.value_json) if row else {}
                needs_update = (
                    not v
                    or v.get("date") != today
                    or not v.get("usd")
                    # Refresh if recorded value has drifted > 20% from current
                    # (covers deposits / withdrawals / large PnL swings).
                    or abs(float(v.get("usd", 0)) - capital_usd) / max(capital_usd, 1e-9) > 0.20
                )
                if not needs_update:
                    return
                v = {
                    "date": today,
                    "usd": capital_usd,
                    "wallet_sol": float(wallet_sol),
                    "sol_price_usd": float(sol_price_usd),
                    "recorded_at": _utcnow().isoformat(),
                }
                if row is None:
                    row = SystemState(key=LIVE_CAPITAL_KEY, value_json=v)
                    session.add(row)
                else:
                    row.value_json = v
                    flag_modified(row, "value_json")
                await session.commit()
                logger.info(
                    f"V504: live capital base recorded: ${capital_usd:.2f} "
                    f"(wallet {wallet_sol:.4f} SOL × ${sol_price_usd:.2f})"
                )
        except Exception as exc:
            logger.warning(f"Could not record live capital base: {exc}")

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
            # V503 FIX: effective daily loss limit = min(absolute SOL limit,
            # X% of the start-of-day wallet). With a ~0.1 SOL wallet the 0.3 SOL
            # absolute limit could never trigger, i.e. there was no loss limit.
            effective_loss_limit = float(settings.live_daily_loss_limit_sol)
            try:
                _w = await self.client.get_wallet_balance()
                _start_wallet = float(_w.get("sol", 0.0)) + max(0.0, -float(daily["sol_pnl"]))
                if _start_wallet > 0:
                    effective_loss_limit = min(
                        effective_loss_limit,
                        _start_wallet * settings.live_daily_loss_limit_pct_of_wallet / 100.0,
                    )
            except Exception as _exc:
                logger.warning(f"Could not read wallet for loss-limit check: {_exc}")
            if daily["sol_pnl"] <= -effective_loss_limit:
                try:
                    from ..risk_management.manager import RiskManager
                    await RiskManager().activate_circuit_breaker(
                        f"Live daily loss limit reached: {daily['sol_pnl']:.4f} SOL "
                        f"(limit {effective_loss_limit:.4f} SOL)"
                    )
                except Exception as _exc:
                    logger.warning(f"Could not activate circuit breaker: {_exc}")
                return self._reject(
                    "daily_loss_limit_reached",
                    f"Daily loss limit {effective_loss_limit:.4f} SOL reached "
                    f"(pnl {daily['sol_pnl']:.4f} SOL)",
                )

            # V503: enforce circuit breaker + cooldown-after-losses in LIVE mode
            try:
                from ..risk_management.manager import RiskManager
                _rc = await RiskManager().check_live_can_open()
                if not _rc.can_open:
                    return self._reject("risk_blocked", _rc.reason or "Risk manager blocked the trade")
            except Exception as _exc:
                # Fail closed: if the risk state cannot be read, do not trade.
                logger.error(f"Live risk check failed — blocking trade: {_exc}")
                return self._reject("risk_check_failed", f"Risk check failed: {str(_exc)[:150]}")

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
            # V504 FIX: skip the trade if no real SOL price is available.
            # Previously this fell back to a $200 constant, producing wildly
            # wrong notional/PnL math (a $20 wallet would look like a $200 trade).
            if not sol_price_usd or sol_price_usd <= 0:
                return self._reject(
                    "sol_price_unavailable",
                    "SOL price unavailable from all sources (Jupiter, Binance, "
                    "Coinbase, DexScreener); cannot size the position safely.",
                )

            # V504: record the start-of-day live capital base for the RiskManager
            # so %-based daily-loss / weekly-loss / drawdown gates use the real
            # wallet USD value (not paper_capital_usd = $10000).
            await self._record_live_capital_base(sol_balance, sol_price_usd)

            # V505: snapshot wallet BEFORE the buy so we can compute actual
            # slippage from the on-chain balance diff after the trade lands.
            # (PumpPortal doesn't return fill details; this is the only way to
            # know the true fill price + cost.)
            wallet_before = await self._snapshot_wallet(mint=mint)

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
                    # V503 FIX: kwarg is timeout_seconds (was `timeout=` -> TypeError,
                    # silently swallowed, so confirmation NEVER ran) and the result
                    # is a dict, not a bool.
                    conf = await self.client.confirm_signature(result.tx_signature, timeout_seconds=15)
                    confirmed = bool(conf.get("confirmed"))
                    conf_err = str(conf.get("error") or "")
                    if not confirmed and conf_err and not conf_err.startswith("timeout"):
                        # The tx LANDED on-chain but FAILED (slippage, funds...).
                        # No tokens were bought -> do not record a phantom position.
                        logger.error(
                            f"❌ Buy tx {result.tx_signature} for {symbol} failed on-chain: {conf_err}"
                        )
                        return self._reject(
                            "buy_tx_failed_onchain",
                            f"Buy transaction failed on-chain: {conf_err[:200]}",
                            {"tx_signature": result.tx_signature},
                        )
                except Exception as confirm_exc:
                    logger.warning(f"Buy confirm failed for {symbol}: {confirm_exc}")
            if not confirmed:
                # Status unknown (timeout / RPC trouble): PumpPortal accepted the
                # order, so keep tracking it but flag it for the operator.
                logger.warning(
                    f"⚠️ Buy tx {result.tx_signature} for {symbol} NOT confirmed within 15s. "
                    f"Recording anyway — monitor for stuck position."
                )

            # V505: REAL slippage measurement — snapshot the wallet AFTER the
            # buy lands and compute the actual fill price + slippage from the
            # SOL spent vs tokens received. Falls back to the V400 estimate
            # (3% slippage) only when the wallet diff isn't usable (RPC issues,
            # brand-new ATA, etc.). This is the only accurate way to know what
            # we actually paid — PumpPortal Lightning API returns no fill details.
            wallet_after = await self._snapshot_wallet(mint=mint)
            notional_usd = amount * sol_price_usd
            market_price_for_slip = float(price_usd) if price_usd and price_usd > 0 else None
            real_slip = self._compute_actual_slippage(
                wallet_before, wallet_after, notional_usd,
                sol_price_usd, side="buy",
                market_price_usd=market_price_for_slip,
            )
            if real_slip["measured"] and real_slip["actual_fill_price"]:
                fill_price = float(real_slip["actual_fill_price"])
                # tokens_received is implied by wallet diff (don't use the
                # estimated 3% slippage path).
                if wallet_before.get("token_balance") is not None and wallet_after.get("token_balance") is not None:
                    quantity = float(wallet_after["token_balance"]) - float(wallet_before["token_balance"])
                else:
                    quantity = notional_usd / fill_price if fill_price > 0 else 0.0
                # entry slippage = (fill_price - market) * qty — fees are
                # reported separately (PumpPortal fee = 1% of notional).
                actual_slip_pct = real_slip.get("actual_slippage_pct") or 0.0
                entry_slippage_usd = notional_usd * max(0.0, actual_slip_pct) / 100
                # Recompute fee from actual notional (it's still 1% of intended).
                entry_fee_usd = notional_usd * PUMPPORTAL_FEE_PCT
                actual_cost_sol = real_slip.get("actual_cost_sol")
                logger.info(
                    f"📐 V505 real buy slippage: {actual_slip_pct:+.2f}% "
                    f"(fill=${fill_price:.10f}, market=${market_price_for_slip or 0:.10f}, "
                    f"sol_spent={actual_cost_sol:.6f}, tokens={quantity:.2f})"
                )
                reasons = list(entry_reasons or [])
                reasons.append(f"LIVE buy {amount} SOL via PumpPortal")
                reasons.append(f"tx:{result.tx_signature}")
                reasons.append(
                    f"V505 measured slippage {actual_slip_pct:+.2f}% "
                    f"(fill=${fill_price:.10f})"
                )
                if not confirmed:
                    reasons.append("⚠️ tx not confirmed on-chain")
            else:
                # V400 P1-2/P1-3 fallback: 3% slippage estimate when wallet
                # diff isn't usable (rare — usually means RPC failed).
                estimated_slippage_pct = 3.0
                if price_usd and price_usd > 0:
                    fill_price = price_usd * (1 + estimated_slippage_pct / 100)
                else:
                    fill_price = notional_usd  # fallback (quantity = 1.0)
                quantity = notional_usd / fill_price if fill_price > 0 else 0.0
                entry_fee_usd = notional_usd * PUMPPORTAL_FEE_PCT
                entry_slippage_usd = notional_usd * estimated_slippage_pct / 100
                reasons = list(entry_reasons or [])
                reasons.append(f"LIVE buy {amount} SOL via PumpPortal")
                reasons.append(f"tx:{result.tx_signature}")
                reasons.append(
                    f"V505 slippage estimate {estimated_slippage_pct}% "
                    f"(wallet-diff measurement failed: {real_slip.get('reason', 'unknown')})"
                )
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
                max_unrealized_profit_usd=fill_price,  # V400 P0-1: init peak = entry (legacy field)
                peak_price=fill_price,  # V506: dedicated peak_price column
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

            # V505: snapshot wallet BEFORE sell so we can compute the actual
            # exit price from the SOL received vs tokens sold.
            wallet_before_sell = await self._snapshot_wallet(mint=mint)

            result = await self.client.sell_token(mint_address=mint, amount_tokens="100%", pool=pool)
            if not result.success:
                logger.warning(f"Live sell failed for {symbol}: {result.error}")
                return self._reject("sell_failed", result.error or "PumpPortal rejected the sell")

            # V400 P0-3 FIX: Confirm sell on-chain (V503: correct kwarg + dict result)
            sell_confirmed = False
            if result.tx_signature:
                try:
                    sconf = await self.client.confirm_signature(result.tx_signature, timeout_seconds=15)
                    sell_confirmed = bool(sconf.get("confirmed"))
                    serr = str(sconf.get("error") or "")
                    if not sell_confirmed and serr and not serr.startswith("timeout"):
                        # Sell failed on-chain: tokens are still in the wallet.
                        # Keep the trade OPEN so the next cycle retries the exit.
                        logger.error(
                            f"❌ Sell tx {result.tx_signature} for {symbol} failed on-chain: {serr}"
                        )
                        return self._reject(
                            "sell_tx_failed_onchain",
                            f"Sell transaction failed on-chain: {serr[:200]}",
                            {"tx_signature": result.tx_signature},
                        )
                    if not sell_confirmed:
                        logger.warning(f"⚠️ Sell tx {result.tx_signature} for {symbol} NOT confirmed in 15s")
                except Exception as confirm_exc:
                    logger.warning(f"Sell confirm failed: {confirm_exc}")

            sol_price_usd = await self.client._fetch_sol_price()
            # V504 FIX: if SOL price is unavailable, still close the position
            # (it may be hitting stop-loss / circuit breaker — urgent liquidation),
            # but skip the SOL-denominated pnl tracking this cycle to avoid
            # corrupting the daily PnL counter with a $0 / fallback value.
            if not sol_price_usd or sol_price_usd <= 0:
                logger.warning(
                    f"SOL price unavailable during close of trade {trade_id} "
                    f"({symbol}); closing anyway (urgent exit), SOL-denominated "
                    f"daily pnl tracking will be skipped this cycle."
                )
                sol_price_usd = 0.0
            # V505: REAL exit-slippage measurement — snapshot AFTER sell and
            # compute actual exit price from SOL received vs tokens sold.
            # Falls back to the 5% estimate only when the wallet diff fails.
            wallet_after_sell = await self._snapshot_wallet(mint=mint)
            exit_market_price = float(exit_price) if exit_price else None
            real_sell_slip = self._compute_actual_slippage(
                wallet_before_sell, wallet_after_sell,
                0.0,  # notional not used for sell
                sol_price_usd, side="sell",
                market_price_usd=exit_market_price,
            )
            if real_sell_slip["measured"] and real_sell_slip["actual_fill_price"]:
                actual_exit_price = float(real_sell_slip["actual_fill_price"])
                actual_exit_slip_pct = real_sell_slip.get("actual_slippage_pct") or 0.0
                # tokens actually sold — use wallet diff if both snapshots have it
                if (
                    wallet_before_sell.get("token_balance") is not None
                    and wallet_after_sell.get("token_balance") is not None
                ):
                    tokens_sold = float(wallet_before_sell["token_balance"]) - float(wallet_after_sell["token_balance"])
                else:
                    tokens_sold = float(trade.entry_quantity or 0.0)
                proceeds_usd = tokens_sold * actual_exit_price
                exit_fee_usd = proceeds_usd * PUMPPORTAL_FEE_PCT
                exit_slippage_usd = tokens_sold * exit_price * max(0.0, actual_exit_slip_pct) / 100
                logger.info(
                    f"📐 V505 real sell slippage: {actual_exit_slip_pct:+.2f}% "
                    f"(exit=${actual_exit_price:.10f}, market=${exit_market_price or 0:.10f}, "
                    f"tokens_sold={tokens_sold:.2f}, sol_received={real_sell_slip.get('actual_cost_sol'):.6f})"
                )
                # Update exit_price variable used downstream in the log line.
                exit_price = actual_exit_price
            else:
                # V400 P1-2 fallback: 5% slippage estimate when wallet diff
                # isn't usable (rare — usually means RPC failed).
                estimated_exit_slippage_pct = 5.0
                actual_exit_price = exit_price * (1 - estimated_exit_slippage_pct / 100)
                tokens_sold = float(trade.entry_quantity or 0.0)
                proceeds_usd = tokens_sold * actual_exit_price
                exit_fee_usd = proceeds_usd * PUMPPORTAL_FEE_PCT
                exit_slippage_usd = tokens_sold * exit_price * estimated_exit_slippage_pct / 100
                logger.warning(
                    f"V505 sell slippage estimate 5% (wallet-diff measurement failed: "
                    f"{real_sell_slip.get('reason', 'unknown')})"
                )
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

            if not db_save_success:
                # V503: tokens are sold but the DB still says "open". Remember it so
                # manage_open_positions does not try to sell it again every cycle.
                self._sold_pending_db.add(trade_id)
                logger.critical(
                    f"🚨 Trade {trade_id} SOLD on-chain (tx={result.tx_signature}) but DB close "
                    f"failed twice. Excluded from auto-exit; fix the DB row manually."
                )

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
        closed = {"stop_loss": 0, "sudden_dump": 0, "mega_tp": 0, "smart_trailing": 0, "stagnation": 0, "time_exit": 0, "exit_all": 0, "circuit_breaker": 0}
        async with get_session() as session:
            trades = (
                await session.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id == LIVE_STRATEGY_ID,
                    )
                )
            ).scalars().all()

        # V502: circuit breaker active -> liquidate everything.
        circuit_breaker_on = False
        try:
            from ..risk_management.manager import RiskManager
            _st = (await RiskManager().get_state()).to_dict()
            circuit_breaker_on = bool(_st.get("circuit_breaker_active", False))
        except Exception as exc:
            logger.warning(f"Could not read circuit breaker state: {exc}")

        for trade in trades:
            if trade.id in self._sold_pending_db:
                continue
            try:
                async with get_session() as session:
                    token = await session.get(Token, trade.token_id)
                    price = float(token.price_usd) if token and token.price_usd else None
                if price is None or price <= 0:
                    continue

                # V506: trailing stop with PERSISTED peak_price (was overloaded
                # onto max_unrealized_profit_usd — wrong field semantically and
                # meant max USD PnL, not max price). Now uses the dedicated
                # peak_price column. Falls back to max_unrealized_profit_usd
                # for trades opened before V506 (legacy compatibility).
                legacy_peak = float(trade.max_unrealized_profit_usd or 0)
                current_peak = float(trade.peak_price or 0)
                # Migrate legacy values on first read
                if current_peak <= 0 and legacy_peak > 0:
                    current_peak = legacy_peak
                if current_peak <= 0:
                    current_peak = max(float(trade.entry_price or 0), price)
                # Update peak if current price is higher
                if price > current_peak:
                    current_peak = price
                    try:
                        async with get_session() as s2:
                            db_trade = await s2.get(Trade, trade.id)
                            if db_trade and db_trade.status == "open":
                                db_trade.peak_price = price
                                await s2.commit()
                    except Exception:
                        pass  # non-critical, will retry next cycle

                peak_price = current_peak
                # V506: Smart exit — TIGHTER variable trailing tiers.
                # V502 had 25/35/45% which gave back too much profit on memecoins
                # (a "winning" trailing exit could be -10% net after 8% round-trip
                # slippage). V506 tightens to 15/20/30% so we lock more profit.
                peak_gain_pct = (peak_price / trade.entry_price - 1) if trade.entry_price > 0 else 0
                if peak_gain_pct >= 5.0:
                    trail_pct = 0.30   # +500%: ride mega pumps but lock 70% of gain
                elif peak_gain_pct >= 1.0:
                    trail_pct = 0.20   # +100%: lock 80% of gain
                elif peak_gain_pct >= 0.15:
                    trail_pct = 0.15   # +15%: tight, lock small wins early
                else:
                    trail_pct = None   # trailing not armed yet (below +15%)
                drop_from_peak = (peak_price - price) / peak_price if peak_price > 0 else 0
                trailing_hit = trail_pct is not None and drop_from_peak >= trail_pct
                # V506: sudden-dump rule — escape an obvious rug-pull in the
                # +10%..+15% pre-trailing zone. When peak_gain >= +10% but price
                # has now dropped -20% from that peak (e.g. peak +15% → now -8%
                # from entry, still above SL of -15%), we exit early to lock
                # small wins and avoid the SL taking a -15% loss.
                sudden_dump = (
                    trail_pct is None and peak_gain_pct >= 0.10 and drop_from_peak >= 0.20
                )
                # V502 FIX: stagnation was impossible (gain>5% AND within 5%).
                # V506: lowered stagnation wait from 4h to 3h (memecoins move
                # fast — if a position hasn't moved in 3h, it's likely dead).
                # Still requires: held >=3h, in profit, never above +5%.
                stagnation_hit = False
                age_min = 0.0
                if trade.entry_timestamp:
                    age_min = (_utcnow() - trade.entry_timestamp).total_seconds() / 60
                    if age_min >= 180 and trade.entry_price > 0:
                        current_gain = price / trade.entry_price - 1
                        if 0 < current_gain <= 0.05 and peak_gain_pct < 0.05:
                            stagnation_hit = True

                reason = None
                if circuit_breaker_on: reason = "circuit_breaker"
                elif sudden_dump: reason = "sudden_dump"
                elif trade.stop_loss_price and price <= trade.stop_loss_price: reason = "stop_loss"
                elif trade.take_profit_price and price >= trade.take_profit_price: reason = "mega_tp"
                elif trailing_hit: reason = "smart_trailing"
                elif stagnation_hit: reason = "stagnation"
                elif trade.planned_holding_period_minutes and trade.entry_timestamp:
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
