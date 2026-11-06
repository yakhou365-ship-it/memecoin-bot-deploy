"""
Smart Rebalancing Strategy (Ultimate-bot).

When the bot holds multiple positions and one is significantly outperforming
the others, it closes the smaller winners and reallocates that capital into
the star position. This concentrates capital where momentum is strongest.

Rules:
- Never closes a losing trade (SL handles those)
- Never closes a big winner (> +30%) — let it ride via trailing
- Only closes small winners (+5% to +30%) when a star position exists (+10%+)
- Never exceeds 5% of wallet in a single position (risk cap)
- Only triggers when >= 2 open positions exist
- Cooldown: max 1 rebalance per 30 minutes (prevents churn)
- Only in LIVE mode (real SOL)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, Trade, SystemState

STAR_MIN_GAIN_PCT = 10.0
SMALL_WINNER_MIN_PCT = 5.0
SMALL_WINNER_MAX_PCT = 30.0
MAX_POSITION_PCT_OF_WALLET = 15.0  # Ultimate-bot: allow star to grow to 15% (was 5%)
REBALANCE_COOLDOWN_MINUTES = 30
REBALANCE_STATE_KEY = "rebalance_state"


class RebalanceManager:
    """Manages smart capital reallocation among open positions."""

    def __init__(self):
        self._last_rebalance: Optional[datetime] = None

    async def should_rebalance(self) -> Tuple[bool, str]:
        async with get_session() as s:
            count = len(list((
                await s.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id == "live_pumpfun",
                    )
                )
            ).scalars().all()))
        if count < 2:
            return False, f"Only {count} open positions (need >= 2)"

        state = await self._load_state()
        last = state.get("last_rebalance_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                elapsed = datetime.utcnow() - last_dt
                if elapsed < timedelta(minutes=REBALANCE_COOLDOWN_MINUTES):
                    remaining = REBALANCE_COOLDOWN_MINUTES - elapsed.total_seconds() / 60
                    return False, f"Cooldown: {remaining:.0f}min remaining"
            except Exception:
                pass
        return True, "OK"

    async def find_rebalance_opportunity(
        self, wallet_sol: float, sol_price_usd: float
    ) -> Optional[Dict[str, Any]]:
        trades_data = []
        async with get_session() as s:
            trades = list((
                await s.execute(
                    select(Trade).where(
                        Trade.status == "open",
                        Trade.strategy_id == "live_pumpfun",
                    )
                )
            ).scalars().all())
            for trade in trades:
                token = await s.get(Token, trade.token_id)
                price = float(token.price_usd) if token and token.price_usd else None
                if price is None or price <= 0:
                    continue
                gain_pct = (price / float(trade.entry_price) - 1) * 100 if trade.entry_price > 0 else 0
                trades_data.append({
                    "trade_id": trade.id,
                    "token_id": trade.token_id,
                    "symbol": token.symbol if token else "?",
                    "mint": token.address if token else "",
                    "dex": token.dex if token else None,
                    "entry_price": float(trade.entry_price),
                    "current_price": price,
                    "gain_pct": gain_pct,
                    "entry_size_usd": float(trade.entry_size_usd or 0),
                    "entry_quantity": float(trade.entry_quantity or 0),
                })

        if len(trades_data) < 2:
            return None

        trades_data.sort(key=lambda t: t["gain_pct"], reverse=True)
        star = trades_data[0]
        others = trades_data[1:]

        if star["gain_pct"] < STAR_MIN_GAIN_PCT:
            return None

        closables = [t for t in others if SMALL_WINNER_MIN_PCT <= t["gain_pct"] <= SMALL_WINNER_MAX_PCT]
        if not closables:
            return None

        total_release_usd = sum(t["entry_size_usd"] * (1 + t["gain_pct"] / 100) for t in closables)
        total_release_sol = total_release_usd / sol_price_usd if sol_price_usd > 0 else 0

        max_position_sol = wallet_sol * MAX_POSITION_PCT_OF_WALLET / 100
        star_current_sol = star["entry_size_usd"] / sol_price_usd if sol_price_usd > 0 else 0
        additional_cap = max(0, max_position_sol - star_current_sol)
        additional_buy_sol = min(total_release_sol * 0.8, additional_cap)

        if additional_buy_sol < 0.005:
            return None

        return {
            "star_trade_id": star["trade_id"],
            "star_token_id": star["token_id"],
            "star_symbol": star["symbol"],
            "star_mint": star["mint"],
            "star_dex": star["dex"],
            "star_gain_pct": star["gain_pct"],
            "star_entry_price": star["entry_price"],
            "star_current_price": star["current_price"],
            "close_trade_ids": [t["trade_id"] for t in closables],
            "close_symbols": [t["symbol"] for t in closables],
            "close_gains": [t["gain_pct"] for t in closables],
            "total_release_sol": round(total_release_sol, 6),
            "additional_buy_sol": round(additional_buy_sol, 6),
            "wallet_sol": wallet_sol,
            "sol_price_usd": sol_price_usd,
        }

    async def execute_rebalance(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        from ..live_trading.executor import LiveTradingExecutor
        executor = LiveTradingExecutor()
        results = {"closed": [], "failed_closes": [], "buy_result": None,
                    "total_pnl_usd": 0.0, "additional_sol_bought": 0.0}

        for trade_id, symbol, gain in zip(
            opportunity["close_trade_ids"],
            opportunity["close_symbols"],
            opportunity["close_gains"],
        ):
            try:
                close_result = await executor.close_position(trade_id, "smart_rebalance")
                if close_result.get("success"):
                    results["closed"].append({"trade_id": trade_id, "symbol": symbol,
                        "pnl": close_result.get("net_pnl_usd", 0), "tx": close_result.get("tx_signature", "")})
                    results["total_pnl_usd"] += close_result.get("net_pnl_usd", 0)
                    logger.info(f"🔄 Rebalance: closed {symbol} (+{gain:.1f}%) pnl=${close_result.get('net_pnl_usd', 0):.4f}")
                else:
                    results["failed_closes"].append({"trade_id": trade_id, "symbol": symbol,
                        "error": close_result.get("error", "unknown")})
            except Exception as exc:
                results["failed_closes"].append({"trade_id": trade_id, "symbol": symbol, "error": str(exc)[:150]})

        additional_sol = opportunity["additional_buy_sol"]
        if additional_sol >= 0.005 and results["closed"]:
            try:
                buy_result = await executor.open_position(
                    token_id=opportunity["star_token_id"],
                    amount_sol=additional_sol,
                    entry_price=opportunity["star_current_price"],
                    entry_reasons=[f"Smart Rebalance: adding to {opportunity['star_symbol']} (+{opportunity['star_gain_pct']:.1f}% gain)"],
                )
                results["buy_result"] = buy_result
                if buy_result.get("success"):
                    results["additional_sol_bought"] = additional_sol
                    logger.info(f"🔄 Rebalance: bought {additional_sol:.4f} SOL more of {opportunity['star_symbol']}")
            except Exception as exc:
                results["buy_result"] = {"success": False, "error": str(exc)[:150]}

        await self._save_state({"last_rebalance_at": datetime.utcnow().isoformat()})
        return results

    async def _load_state(self) -> Dict[str, Any]:
        async with get_session() as s:
            row = await s.get(SystemState, REBALANCE_STATE_KEY)
            return dict(row.value_json) if row else {}

    async def _save_state(self, data: Dict[str, Any]) -> None:
        async with get_session() as s:
            row = await s.get(SystemState, REBALANCE_STATE_KEY)
            if row is None:
                row = SystemState(key=REBALANCE_STATE_KEY, value_json=dict(data))
                s.add(row)
            else:
                v = dict(row.value_json or {})
                v.update(data)
                row.value_json = v
                flag_modified(row, "value_json")
