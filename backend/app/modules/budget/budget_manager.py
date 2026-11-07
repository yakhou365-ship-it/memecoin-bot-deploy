"""
Daily Trading Budget System.

Implements:
A. Daily Budget Control Panel (user-configurable settings)
B. Daily Budget Enforcement (strict checks before every trade)
C. Automatic Daily Stop System (stops when limits breached)
D. Intelligent Opportunity Discovery
E. Opportunity Prioritization Engine
F. Automatic Paper-Trade Participation
G. Smart Capital Allocation
H. Opportunity Monitoring
J. Daily Performance Report
K. Non-Negotiable Safety Rules
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
from enum import Enum

from sqlalchemy import select, desc, and_, func
from sqlalchemy.orm.attributes import flag_modified

from ...core.config import settings
from ...core.database import get_session
from ...core.logger import logger
from ...core.models import Token, Trade, SystemState


DEFAULT_BUDGET = {
    "daily_trading_budget": 1000.0,
    "max_capital_per_trade": 100.0,
    "max_daily_trades": 20,
    "max_simultaneous_positions": 5,
    "max_daily_loss": 50.0,
    "max_daily_drawdown_pct": 5.0,
    "daily_risk_exposure_limit": 200.0,
    "daily_reset_hour": 0,
    "daily_reset_timezone": "UTC",
    "is_active": True,
    # Ultimate-bot: auto-sizing percentages controlled from the dashboard.
    # When the user changes max_capital_per_trade / max_daily_loss /
    # max_simultaneous_positions in the budget page, these percentages update
    # and the orchestrator reads them to compute SOL limits from the wallet.
    # V515 SNIPER: defaults مناسبة للنمط الجديد (الأفضل في سوق نوفمبر 2026)
    "auto_trade_pct": 10.0,      # V515 SNIPER: 10% من المحفظة لكل صفقة
    "auto_loss_pct": 20.0,       # V515 SNIPER: 20% حد خسارة يومي
    "auto_max_positions": 8,     # V515 SNIPER: 8 صفقات متزامنة
}

BUDGET_KEY = "daily_budget_settings"
DAILY_RECORD_KEY_PREFIX = "daily_record_"


class OpportunityStatus(str, Enum):
    QUALIFIED_ENTERED = "qualified_entered"
    QUALIFIED_WAITING = "qualified_waiting"
    REJECTED_RISK = "rejected_risk"
    REJECTED_PROBABILITY = "rejected_probability"
    REJECTED_DATA = "rejected_data"
    REJECTED_RETURN = "rejected_return"
    REJECTED_LIQUIDITY = "rejected_liquidity"
    REJECTED_DAILY_LIMIT = "rejected_daily_limit"


@dataclass
class Opportunity:
    token_symbol: str
    token_address: str
    chain: str
    current_price: float
    predicted_win_probability: float = 0.0
    confidence_interval_lower: float = 0.0
    confidence_interval_upper: float = 0.0
    expected_net_return: float = 0.0
    expected_loss: float = 0.0
    risk_reward_ratio: float = 0.0
    liquidity_score: float = 0.0
    security_score: float = 0.0
    market_regime_compatibility: float = 0.0
    execution_feasibility: float = 0.0
    data_quality: float = 0.0
    overall_score: float = 0.0
    required_capital: float = 0.0
    suggested_position_size: float = 0.0
    status: str = "pending"
    rejection_reason: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


class BudgetManager:
    async def get_settings(self) -> Dict:
        async with get_session() as s:
            state = await s.get(SystemState, BUDGET_KEY)
            if not state:
                state = SystemState(key=BUDGET_KEY, value_json=DEFAULT_BUDGET)
                s.add(state)
                await s.flush()
            return dict(state.value_json)

    async def update_settings(self, updates: Dict) -> Dict:
        current = await self.get_settings()

        # Ultimate-bot: sync budget page values to auto-sizing percentages.
        # When the user changes a USD value in the dashboard, convert it
        # to a percentage of daily_trading_budget so the orchestrator can
        # apply it to the actual wallet.
        # FIX: read budget from updates FIRST (it may be changing in the
        # same request), then fall back to current.
        budget = float(updates.get("daily_trading_budget", current.get("daily_trading_budget", 1000.0)))

        if "max_capital_per_trade" in updates:
            max_trade = float(updates["max_capital_per_trade"])
            if budget > 0:
                # Convert USD → percentage (e.g. $50 trade on $1000 budget = 5%)
                current["auto_trade_pct"] = round((max_trade / budget) * 100.0, 2)
                current["auto_trade_pct"] = max(0.5, min(50.0, current["auto_trade_pct"]))

        if "max_daily_loss" in updates:
            max_loss = float(updates["max_daily_loss"])
            if budget > 0:
                current["auto_loss_pct"] = round((max_loss / budget) * 100.0, 2)
                current["auto_loss_pct"] = max(1.0, min(50.0, current["auto_loss_pct"]))

        if "max_simultaneous_positions" in updates:
            positions = int(updates["max_simultaneous_positions"])
            current["auto_max_positions"] = max(1, min(10, positions))

        current.update(updates)
        async with get_session() as s:
            state = await s.get(SystemState, BUDGET_KEY)
            if not state:
                state = SystemState(key=BUDGET_KEY, value_json=current)
                s.add(state)
            else:
                state.value_json = current
                flag_modified(state, "value_json")
        logger.info(f"Budget settings updated: {updates} → auto_trade_pct={current.get('auto_trade_pct')}%, auto_loss_pct={current.get('auto_loss_pct')}%, auto_max_positions={current.get('auto_max_positions')}")
        return current

    async def get_daily_record(self, date: Optional[str] = None) -> Dict:
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        key = f"{DAILY_RECORD_KEY_PREFIX}{date}"
        async with get_session() as s:
            state = await s.get(SystemState, key)
            if not state:
                settings_dict = await self.get_settings()
                record = {
                    "date": date,
                    "starting_budget": settings_dict["daily_trading_budget"],
                    "remaining_budget": settings_dict["daily_trading_budget"],
                    "allocated_capital": 0.0,
                    "available_capital": settings_dict["daily_trading_budget"],
                    "realized_profit": 0.0,
                    "realized_loss": 0.0,
                    "unrealized_profit": 0.0,
                    "unrealized_loss": 0.0,
                    "trades_opened": 0,
                    "trades_won": 0,
                    "trades_lost": 0,
                    "max_drawdown_pct": 0.0,
                    "peak_equity": settings_dict["daily_trading_budget"],
                    "stop_reason": "",
                    "is_stopped": False,
                    "opportunities_discovered": 0,
                    "opportunities_qualified": 0,
                    "opportunities_rejected": 0,
                    "rejection_reasons": {},
                }
                state = SystemState(key=key, value_json=record)
                s.add(state)
                await s.flush()
            return dict(state.value_json)

    async def update_daily_record(self, updates: Dict) -> Dict:
        date = datetime.utcnow().strftime("%Y-%m-%d")
        key = f"{DAILY_RECORD_KEY_PREFIX}{date}"
        async with get_session() as s:
            state = await s.get(SystemState, key)
            if not state:
                await self.get_daily_record(date)
                state = await s.get(SystemState, key)
            current = dict(state.value_json)
            current.update(updates)
            state.value_json = current
            flag_modified(state, "value_json")
        return current

    async def check_can_trade(self, required_capital: float, risk_amount: float) -> Dict:
        settings_dict = await self.get_settings()
        record = await self.get_daily_record()

        if not settings_dict.get("is_active", True):
            return {"allowed": False, "reason": "Trading stopped by user"}
        if record.get("is_stopped", False):
            return {"allowed": False, "reason": f"Daily stop: {record.get('stop_reason', '')}"}
        if record["remaining_budget"] < required_capital:
            return {"allowed": False, "reason": f"Budget: ${record['remaining_budget']:.2f} < ${required_capital:.2f}"}
        if required_capital > settings_dict["max_capital_per_trade"]:
            return {"allowed": False, "reason": f"Max per trade: ${required_capital:.2f} > ${settings_dict['max_capital_per_trade']:.2f}"}
        if record["trades_opened"] >= settings_dict["max_daily_trades"]:
            return {"allowed": False, "reason": f"Max daily trades: {record['trades_opened']}/{settings_dict['max_daily_trades']}"}

        async with get_session() as s:
            open_count = (await s.execute(
                select(func.count(Trade.id)).where(Trade.status == "open")
            )).scalar() or 0
        if open_count >= settings_dict["max_simultaneous_positions"]:
            return {"allowed": False, "reason": f"Max positions: {open_count}/{settings_dict['max_simultaneous_positions']}"}

        total_loss = record.get("realized_loss", 0) + record.get("unrealized_loss", 0)
        if total_loss >= settings_dict["max_daily_loss"]:
            return {"allowed": False, "reason": f"Max daily loss: ${total_loss:.2f} >= ${settings_dict['max_daily_loss']:.2f}"}

        if record.get("max_drawdown_pct", 0) >= settings_dict["max_daily_drawdown_pct"]:
            return {"allowed": False, "reason": f"Max drawdown: {record['max_drawdown_pct']:.2f}%"}

        current_exposure = record.get("allocated_capital", 0) + risk_amount
        if current_exposure > settings_dict["daily_risk_exposure_limit"]:
            return {"allowed": False, "reason": f"Risk exposure: ${current_exposure:.2f} > ${settings_dict['daily_risk_exposure_limit']:.2f}"}

        return {"allowed": True, "reason": ""}

    async def allocate_capital(self, amount: float, trade_id: str) -> None:
        record = await self.get_daily_record()
        record["remaining_budget"] -= amount
        record["allocated_capital"] += amount
        record["available_capital"] = record["remaining_budget"]  # Ultimate-bot FIX: no double-subtract
        record["trades_opened"] += 1
        await self.update_daily_record(record)
        logger.info(f"Allocated ${amount:.2f} for trade {trade_id}")

    async def release_capital(self, amount: float, pnl: float, trade_id: str) -> None:
        record = await self.get_daily_record()
        record["allocated_capital"] -= amount
        if pnl >= 0:
            record["realized_profit"] += pnl
            record["trades_won"] += 1
        else:
            record["realized_loss"] += abs(pnl)
            record["trades_lost"] += 1
        record["remaining_budget"] += amount + pnl
        record["available_capital"] = record["remaining_budget"]
        # Ultimate-bot FIX: was `remaining_budget - allocated_capital` which
        # double-subtracts (allocated_capital is already part of the budget
        # that's been "spent" from remaining).

    async def check_daily_limits(self) -> Optional[str]:
        settings_dict = await self.get_settings()
        record = await self.get_daily_record()
        if record["remaining_budget"] <= 0:
            return "Daily budget exhausted"
        total_loss = record.get("realized_loss", 0) + record.get("unrealized_loss", 0)
        if total_loss >= settings_dict["max_daily_loss"]:
            return f"Max daily loss: ${total_loss:.2f}"
        if record.get("max_drawdown_pct", 0) >= settings_dict["max_daily_drawdown_pct"]:
            return f"Max drawdown: {record['max_drawdown_pct']:.2f}%"
        if record["trades_opened"] >= settings_dict["max_daily_trades"]:
            return f"Max daily trades: {record['trades_opened']}"
        return None

    async def stop_trading(self, reason: str) -> None:
        record = await self.get_daily_record()
        record["is_stopped"] = True
        record["stop_reason"] = reason
        await self.update_daily_record(record)
        # Also mark the budget settings inactive so /api/budget/can-trade
        # returns allowed=false immediately.
        settings_dict = await self.get_settings()
        settings_dict["is_active"] = False
        await self.update_settings(settings_dict)
        logger.warning(f"Trading stopped: {reason}")

    async def start_trading(self) -> None:
        """Counterpart to stop_trading — clears the daily-stop flag AND
        marks the budget settings active again. Without this method the
        /api/budget/start endpoint crashes with AttributeError → HTTP 500."""
        record = await self.get_daily_record()
        record["is_stopped"] = False
        record["stop_reason"] = ""
        await self.update_daily_record(record)
        settings_dict = await self.get_settings()
        settings_dict["is_active"] = True
        await self.update_settings(settings_dict)
        logger.info("Trading resumed by user")

    async def generate_daily_report(self, date: Optional[str] = None) -> Dict:
        if date is None:
            date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        key = f"{DAILY_RECORD_KEY_PREFIX}{date}"
        async with get_session() as s:
            state = await s.get(SystemState, key)
            if not state:
                return {"error": "No record for this date"}
            record = dict(state.value_json)
        total_trades = record.get("trades_won", 0) + record.get("trades_lost", 0)
        win_rate = (record["trades_won"] / total_trades * 100) if total_trades > 0 else 0
        net_pnl = record.get("realized_profit", 0) - record.get("realized_loss", 0)
        budget_util = ((record.get("starting_budget", 1) - record.get("remaining_budget", 0)) / max(record.get("starting_budget", 1), 1)) * 100
        return {
            "date": date,
            "total_opportunities": record.get("opportunities_discovered", 0),
            "qualified_opportunities": record.get("opportunities_qualified", 0),
            "rejected_opportunities": record.get("opportunities_rejected", 0),
            "trades_opened": record.get("trades_opened", 0),
            "winning_trades": record.get("trades_won", 0),
            "losing_trades": record.get("trades_lost", 0),
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "budget_utilization_pct": budget_util,
            "max_drawdown_pct": record.get("max_drawdown_pct", 0),
            "stop_reason": record.get("stop_reason", ""),
            "rejection_reasons": record.get("rejection_reasons", {}),
        }


class OpportunityDiscovery:
    def __init__(self):
        self.budget_manager = BudgetManager()

    async def discover_opportunities(self, tokens: List, technical_results: Dict) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        settings_dict = await self.budget_manager.get_settings()

        for token in tokens:
            tech = technical_results.get(token.id, {})
            if not tech:
                continue
            opp = self._evaluate_token(token, tech, settings_dict)
            if opp:
                opportunities.append(opp)

        opportunities.sort(key=lambda x: x.overall_score, reverse=True)

        record = await self.budget_manager.get_daily_record()
        record["opportunities_discovered"] += len(opportunities)
        qualified = [o for o in opportunities if "qualified" in o.status]
        rejected = [o for o in opportunities if "rejected" in o.status]
        record["opportunities_qualified"] += len(qualified)
        record["opportunities_rejected"] += len(rejected)
        for o in rejected:
            reason = o.status.replace("rejected_", "")
            record["rejection_reasons"][reason] = record["rejection_reasons"].get(reason, 0) + 1
        await self.budget_manager.update_daily_record(record)

        return opportunities

    def _evaluate_token(self, token, tech: Dict, settings_dict: Dict) -> Optional[Opportunity]:
        try:
            opp = Opportunity(
                token_symbol=token.symbol,
                token_address=token.address,
                chain=token.chain,
                current_price=float(token.price_usd or 0),
            )

            tech_score = tech.get("technical_score", 50)
            rsi = tech.get("rsi", 50)
            adx = tech.get("adx", 20)
            macd_hist = tech.get("macd_hist", 0)
            volatility = tech.get("volatility_pct", 1)
            liquidity_score = tech.get("liquidity_score", 50)
            risk_score = tech.get("risk_score", 50)

            win_prob = 0.45
            if tech_score > 65: win_prob += 0.15
            if 40 <= rsi <= 55: win_prob += 0.10
            if adx > 20: win_prob += 0.08
            if macd_hist > 0: win_prob += 0.07
            if volatility < 2: win_prob += 0.05
            win_prob = min(win_prob, 0.85)

            opp.predicted_win_probability = win_prob
            opp.confidence_interval_lower = max(0.3, win_prob - 0.15)
            opp.confidence_interval_upper = min(0.95, win_prob + 0.10)

            tp_pct = 0.8
            sl_pct = 0.8
            expected_return = (win_prob * tp_pct) - ((1 - win_prob) * sl_pct) - 0.2
            opp.expected_net_return = expected_return
            opp.expected_loss = sl_pct
            opp.risk_reward_ratio = tp_pct / sl_pct if sl_pct > 0 else 0

            liq = float(token.liquidity_usd or 0)
            opp.liquidity_score = min(liq / 100_000, 100) if liq > 0 else 0
            opp.security_score = 100 - risk_score
            opp.market_regime_compatibility = tech.get("market_regime_score", 50)
            slippage = 200 * 100 / max(liq, 1) if liq > 0 else 100
            opp.execution_feasibility = max(0, 100 - slippage * 10)
            opp.data_quality = 80 if tech.get("data_sufficient", False) else 20

            opp.overall_score = (
                win_prob * 30 + (1 if expected_return > 0 else 0) * 20 +
                opp.liquidity_score / 100 * 15 + opp.security_score / 100 * 15 +
                opp.data_quality / 100 * 10 + opp.execution_feasibility / 100 * 10
            )

            # Qualification checks
            min_prob = float(settings.min_win_probability)
            min_ci = float(settings.min_confidence_interval)

            if risk_score > 70:
                opp.status = OpportunityStatus.REJECTED_RISK.value
                opp.rejection_reason = f"Risk score {risk_score} > 70"
                return opp
            if win_prob < min_prob:
                opp.status = OpportunityStatus.REJECTED_PROBABILITY.value
                opp.rejection_reason = f"Win prob {win_prob:.1%} < {min_prob:.0%}"
                return opp
            if opp.confidence_interval_lower < min_ci:
                opp.status = OpportunityStatus.REJECTED_PROBABILITY.value
                opp.rejection_reason = f"CI {opp.confidence_interval_lower:.1%} < {min_ci:.0%}"
                return opp
            if expected_return <= 0:
                opp.status = OpportunityStatus.REJECTED_RETURN.value
                opp.rejection_reason = f"Expected return {expected_return:.2f}% <= 0"
                return opp
            if liq < float(settings.min_liquidity_usd):
                opp.status = OpportunityStatus.REJECTED_LIQUIDITY.value
                opp.rejection_reason = f"Liquidity ${liq:,.0f} < ${settings.min_liquidity_usd}"
                return opp
            if not tech.get("data_sufficient", False):
                opp.status = OpportunityStatus.REJECTED_DATA.value
                opp.rejection_reason = "Insufficient data"
                return opp

            # Smart Capital Allocation
            max_per_trade = settings_dict.get("max_capital_per_trade", 100)
            daily_budget = settings_dict.get("daily_trading_budget", 1000)
            base = max_per_trade * 0.5 * win_prob
            if opp.expected_net_return > 0:
                base *= min(1 + opp.expected_net_return / 10, 1.5)
            base = min(base, max_per_trade, daily_budget * 0.25)
            opp.suggested_position_size = round(base, 2)
            opp.required_capital = opp.suggested_position_size

            opp.status = OpportunityStatus.QUALIFIED_WAITING.value
            return opp
        except Exception as e:
            logger.debug(f"Opportunity eval failed for {token.symbol}: {e}")
            return None
