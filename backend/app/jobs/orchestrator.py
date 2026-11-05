"""
Main orchestrator: ties together scanner → analyzer → decision → trading.

Runs the main loop:
1. Scan market for tokens
2. Fetch OHLCV for top candidates
3. Run full analysis on each
4. Persist AnalysisSnapshot
5. For qualified tokens: open paper trade
6. Manage open positions (SL/TP/time exits)
7. Periodically: train ML model, run learning analysis, send alerts
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import select, desc

from ..core.config import settings
from ..core.database import get_session
from ..core.logger import logger
from ..core.models import Token, AnalysisSnapshot, Trade, TradingState
from ..modules.alerts.manager import AlertManager
from ..modules.decision_engine.engine import DecisionEngine
from ..modules.learning.system import (
    AntiRepetitionChecker, LossAnalyzer, MistakeMemory, WinAnalyzer,
)
from ..modules.live_trading.executor import LiveTradingExecutor, is_live_trade
from ..modules.market_regime.detector import MarketRegimeDetector
from ..modules.market_scanner.scanner import MarketScanner
from ..modules.market_scanner.ohlcv_fetcher import OHLCVFetcher
from ..modules.ml.trainer import MLModelLoader, MLTrainer
from ..modules.paper_trading.engine import PaperTradingEngine


class MainOrchestrator:
    """Runs the autonomous bot loop."""

    def __init__(self):
        self.scanner = MarketScanner()
        self.ohlcv_fetcher = OHLCVFetcher()
        self.alerts = AlertManager()
        self.engine = PaperTradingEngine()
        self.live = LiveTradingExecutor()
        self.regime_detector = MarketRegimeDetector()
        self.loss_analyzer = LossAnalyzer()
        self.win_analyzer = WinAnalyzer()
        self.mistake_memory = MistakeMemory()
        self.anti_repetition = AntiRepetitionChecker()
        self.decision_engine: Optional[DecisionEngine] = None  # lazy init
        self._running = False
        self._last_market_regime = None

    async def _init_decision_engine(self):
        """Initialize the decision engine (rule-based, no ML needed)."""
        self.decision_engine = DecisionEngine(ml_model=None)
        logger.info("🧠 Decision engine initialized (Smart Rule-Based)")

    async def _trading_active(self) -> bool:
        """Read the dashboard Start/Stop switch. Defaults to inactive.

        V400 P1-8 FIX: On DB read failure OR missing TradingState row,
        we DEFAULT TO FALSE (safer). Previously this returned True on
        missing row (auto-created is_active=True), which meant the bot
        could start live trading without explicit user approval.
        """
        try:
            async with get_session() as s:
                state = await s.get(TradingState, 1)
                if state is None:
                    # V400: create with is_active=False (was True)
                    # User must explicitly start trading via /api/trading/start
                    state = TradingState(id=1, is_active=False)
                    s.add(state)
                    await s.flush()
                return bool(state.is_active)
        except Exception as exc:
            logger.warning(
                f"TradingState read failed - defaulting to INACTIVE for safety: {exc}"
            )
            return False

    async def run_cycle(self) -> dict:
        """Run one complete analysis cycle. Returns summary."""
        if self.decision_engine is None:
            await self._init_decision_engine()

        trading_active = await self._trading_active()

        summary = {
            "scanned": 0,
            "analyzed": 0,
            "qualified": 0,
            "trades_opened": 0,
            "live_trades_opened": 0,
            "trades_rejected": 0,
            "trades_closed": 0,
            "alerts_sent": 0,
            "trading_active": trading_active,
            "live_mode": settings.is_live_trading,
        }

        # === 1. Scan market ===
        try:
            scan_results = await self.scanner.scan_all_chains()
            for chain, r in scan_results.items():
                summary["scanned"] += r.get("discovered", 0) + r.get("updated", 0)
        except Exception as exc:
            logger.exception(f"Scanner failed: {exc}")
            await self.alerts.send(
                "scanner_error", "Scanner failed", str(exc)[:200],
                {"error": str(exc)},
            )

        # === 2. Get watch list ===
        watch_list = await self.scanner.get_watch_list(limit=20)
        if not watch_list:
            logger.info("Watch list empty, skipping analysis cycle")
            return summary

        # === 3. Detect market regime (from broad basket) ===
        # Use SOL candles as proxy for broad market (when available)
        broad_candles = await self._fetch_broad_market_candles()
        regime = self.regime_detector.detect(broad_market_candles=broad_candles)
        if self._last_market_regime != regime.regime and regime.regime != "unknown":
            await self.alerts.send(
                "market_regime_changed",
                f"تغير حالة السوق: {regime.regime}",
                regime.reasons[0] if regime.reasons else regime.regime,
                {"regime": regime.to_dict()},
            )
            self._last_market_regime = regime.regime

        # === 4. Analyze each token ===
        for token in watch_list[:10]:  # limit to top 10 to keep cycle fast
            try:
                # Fetch OHLCV
                candles = await self.ohlcv_fetcher.fetch_and_store(
                    token.id, timeframe="1m", limit=200
                )
                if not candles:
                    # Try DB cached
                    db_candles = await self.ohlcv_fetcher.get_latest_candles(
                        token.id, "1m", 200
                    )
                    candles = db_candles
                if candles:
                    df = pd.DataFrame([{
                        "timestamp": c.timestamp,
                        "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume,
                    } for c in candles])
                else:
                    df = None

                # Run full analysis
                decision = await self.decision_engine.analyze(
                    token, candles_df=df, market_regime=regime
                )

                # V200: Removed V101's bear token filter and P&D filter.
                # Backtest proved these filters reduce profits significantly
                # (V101 avg ROI = +17% vs V57 avg ROI = +270%). The 5 AI agents
                # already filter bad tokens effectively. No extra filters needed.

                # V31.2: AI agent consensus (3/5 + veto) as a SECOND OPINION.
                # - If the rule engine rejects but 3/5 agents approve → override.
                # - If the rule engine approves but agents give <3/5 → veto.
                if any((
                    settings.ai_agent_technical_enabled,
                    settings.ai_agent_security_enabled,
                    settings.ai_agent_liquidity_enabled,
                    settings.ai_agent_sentiment_enabled,
                    settings.ai_agent_risk_enabled,
                )):
                    try:
                        from ..modules.ai_agents.orchestrator import AgentOrchestrator
                        if not hasattr(self, '_agent_orchestrator'):
                            self._agent_orchestrator = AgentOrchestrator()
                        # Build a rich, normalized analysis context for the agents
                        sec = decision.security_detail or {}
                        onchain = decision.onchain_detail or {}
                        try:
                            from ..modules.risk_management.manager import RiskManager
                            risk_state_ctx = (await RiskManager().get_state()).to_dict()
                        except Exception:
                            risk_state_ctx = {}
                        honeypot_prob = float(sec.get("honeypot_probability", 0) or 0)
                        mint_enabled = bool(sec.get("mint_authority_enabled", False))
                        freeze_enabled = bool(sec.get("freeze_authority_enabled", False))
                        analysis_context = {
                            "price_usd": token.price_usd,
                            "liquidity_usd": token.liquidity_usd,
                            "volume_24h_usd": token.volume_24h_usd,
                            "market_cap_usd": getattr(token, 'market_cap_usd', 0),
                            "price_change_24h_pct": getattr(token, 'price_change_24h_pct', 0),
                            "price_change_5m_pct": getattr(token, 'price_change_5m_pct', 0),
                            "volume_5m_usd": getattr(token, 'volume_5m_usd', 0),
                            "txns_24h_buys": getattr(token, 'txns_24h_buys', 0),
                            "txns_24h_sells": getattr(token, 'txns_24h_sells', 0),
                            "chain": token.chain,
                            # Agent scores from the rule engine
                            "technical_score": decision.technical_score,
                            "liquidity_score": decision.liquidity_score,
                            "security_score": decision.security_score,
                            "sentiment_score": decision.sentiment_score,
                            "onchain_score": decision.onchain_score,
                            # Normalized security facts (map scanner field names)
                            "security": {
                                "is_honeypot": honeypot_prob >= 0.6,
                                "honeypot_probability": honeypot_prob,
                                "mint_authority_disabled": not mint_enabled,
                                "freeze_authority_disabled": not freeze_enabled,
                                "ownership_renounced": not mint_enabled,
                                "lp_locked": sec.get("liquidity_locked"),
                                "is_verified": False,
                                "risk_score": sec.get("risk_score"),
                            },
                            "onchain": {
                                "top_holders_concentration": float(onchain.get("top_10_pct", 0) or 0),
                            },
                            "liquidity": decision.liquidity_detail or {},
                            "sentiment": decision.sentiment_detail or {},
                            "risk_state": risk_state_ctx,
                        }
                        consensus = await self._agent_orchestrator.run_consensus(token, analysis_context)

                        # V31.4: NEVER override a security auto-disqualify.
                        # If the SecurityScanner flagged the token as a
                        # honeypot / rug / mint-authority-enabled, no
                        # amount of agent consensus can bring it back.
                        sec_detail = decision.security_detail or {}
                        auto_disqualified = bool(sec_detail.get("auto_disqualify", False))

                        # V31.4: detect any agent veto (SecurityAgent,
                        # LiquidityAgent, RiskAgent can all veto now).
                        has_veto = any(
                            v.veto for v in (consensus.verdicts or [])
                        )

                        if auto_disqualified:
                            # Hard lock: security auto-disqualify is final.
                            decision.qualified = False
                            decision.recommendation = "reject"
                            if not decision.rejection_reason:
                                decision.rejection_reason = (
                                    f"Security auto-disqualify: "
                                    f"{sec_detail.get('disqualify_reason', 'unknown')}"
                                )
                            logger.info(
                                f"🔒 {token.symbol} locked: security auto-disqualify "
                                f"({sec_detail.get('disqualify_reason', 'unknown')[:80]})"
                            )
                        elif has_veto:
                            # Any agent veto is final (P0-1 fix).
                            decision.qualified = False
                            decision.recommendation = "reject"
                            decision.rejection_reason = (
                                f"AI veto: an agent invoked veto power"
                            )
                            decision.reasons.append(decision.rejection_reason)
                            logger.info(f"🛑 AI VETO (agent): {token.symbol}")
                        elif not decision.qualified and consensus.approved:
                            # AI agents approved! Override the decision.
                            # V31.4: cap at 5% (matches RiskAgent hard cap).
                            # V31.4: do NOT inflate win_probability (P0-9).
                            decision.qualified = True
                            decision.recommendation = "buy"
                            decision.suggested_position_size_pct = min(
                                consensus.final_position_size_pct, 5.0
                            )
                            # V400 P0-2 FIX: Ensure SL/TP/entry are always set.
                            # If _decide() rejected early (before _suggest_trade_params),
                            # suggested_stop_loss/take_profit/entry will be None.
                            # This caused ~20% of trades to have NO protection!
                            token_price = float(token.price_usd or 0)
                            if token_price <= 0:
                                # Skip — no valid price (P1-9 fix)
                                logger.warning(f"Skip {token.symbol}: no valid price")
                                continue
                            if not decision.suggested_entry or decision.suggested_entry <= 0:
                                decision.suggested_entry = token_price
                            if not decision.suggested_stop_loss or decision.suggested_stop_loss <= 0:
                                # V57: -8% SL (ATR fallback)
                                decision.suggested_stop_loss = token_price * 0.92
                            if not decision.suggested_take_profit or decision.suggested_take_profit <= 0:
                                # V57: +50% TP (ATR fallback)
                                decision.suggested_take_profit = token_price * 1.50
                            if not decision.suggested_holding_period_minutes:
                                decision.suggested_holding_period_minutes = 3600  # V57 60h
                            decision.reasons.append(
                                f"AI consensus: {consensus.approvals_count}/"
                                f"{consensus.total_agents} approved"
                            )
                            logger.info(
                                f"🤖 AI OVERRIDE: {token.symbol} approved by "
                                f"{consensus.approvals_count}/{consensus.total_agents} agents "
                                f"(SL=${decision.suggested_stop_loss:.8f}, "
                                f"TP=${decision.suggested_take_profit:.8f})"
                            )
                        elif decision.qualified and not consensus.approved:
                            # V31.4: any rejection (not just < 3) now vetoes.
                            # Previously `approvals_count < 3` would let a
                            # 3-approve / 2-reject consensus bypass the veto
                            # even when a veto-capable agent rejected.
                            decision.qualified = False
                            decision.recommendation = "reject"
                            decision.rejection_reason = (
                                f"AI veto: only {consensus.approvals_count}/"
                                f"{consensus.total_agents} agents approved"
                            )
                            decision.reasons.append(decision.rejection_reason)
                            logger.info(f"🛑 AI VETO: {token.symbol} - {decision.rejection_reason}")
                    except Exception as exc:
                        logger.warning(f"AI orchestrator failed for {token.symbol}: {exc}")

                # Persist snapshot
                snapshot = self.decision_engine.to_snapshot(token.id, decision)
                async with get_session() as s:
                    s.add(snapshot)
                    await s.flush()
                    snapshot_id = snapshot.id

                # Update token latest scores
                token.latest_opportunity_score = decision.opportunity_score
                token.latest_risk_score = decision.risk_score
                token.latest_win_probability = decision.win_probability
                async with get_session() as s:
                    s.add(token)

                summary["analyzed"] += 1

                # Check anti-repetition
                anti_check = await self.anti_repetition.check(decision, snapshot)

                # Decision
                if decision.qualified and not anti_check["blocked"]:
                    summary["qualified"] += 1
                    if not trading_active:
                        logger.info(f"⏸️ Trading stopped - not opening {token.symbol}")
                    elif settings.is_live_trading:
                        # === REAL MONEY PATH ===
                        # V300: AUTO-STRATEGY SELECTION based on wallet size.
                        # Small capital (< $50 = ~0.3 SOL): use V300-SMALL-CAP
                        #   - Higher risk per trade (5-10%)
                        #   - R:R = 1:10 (asymmetric, capture big pumps)
                        #   - 2 positions max (concentration)
                        #   - 3-day hold (wait for big pump)
                        # Large capital (≥ $50): use V200 (balanced)
                        #   - Lower risk per trade (1-5%)
                        #   - R:R = 1:5
                        #   - 3 positions max
                        sol_price_usd = await self.live.client._fetch_sol_price()
                        wallet_balance = await self.live.client.get_wallet_balance()
                        wallet_sol = float(wallet_balance.get("sol", 0.0)) if wallet_balance else 0.0

                        # Use real wallet balance (in USD) as the capital base
                        if sol_price_usd and sol_price_usd > 0:
                            real_capital_usd = wallet_sol * sol_price_usd
                        else:
                            real_capital_usd = 50.0  # conservative default

                        # V300: Auto-detect strategy based on wallet size
                        SMALL_CAP_THRESHOLD_USD = 50.0  # < $50 = small cap mode
                        is_small_cap = real_capital_usd < SMALL_CAP_THRESHOLD_USD

                        position_pct = float(decision.suggested_position_size_pct or 0)

                        if is_small_cap:
                            # V300-SMALL-CAP strategy
                            # V400 P1-10 FIX: Don't blindly multiply AI pct × 2.
                            # Instead, use a fixed 5% for small cap (in [2.5%, 10%] range).
                            # This is safer — the AI's confidence level is preserved.
                            position_pct = min(max(position_pct, 2.5), 10.0)
                            # If AI suggested very low (< 2.5%), use the floor 2.5%
                            # If AI suggested high (5%), use 5% (not 10%)
                            max_safe_sol = wallet_sol * 0.50 if wallet_sol > 0 else 0
                            v300_max_trade_sol = min(0.1, settings.pump_max_trade_sol)
                            v300_min_trade_sol = 0.005  # lower min for small cap
                            logger.info(
                                f"🐜 V300-SMALL-CAP mode: wallet=${real_capital_usd:.0f} "
                                f"({wallet_sol:.4f} SOL) - concentrated strategy"
                            )
                        else:
                            # V200 strategy (large capital)
                            position_pct = min(max(position_pct, 0.0), 5.0)
                            max_safe_sol = wallet_sol * 0.50 if wallet_sol > 0 else 0
                            v300_max_trade_sol = settings.pump_max_trade_sol
                            v300_min_trade_sol = 0.01

                        # Convert % of REAL capital -> USD -> SOL
                        if sol_price_usd and sol_price_usd > 0 and real_capital_usd > 0:
                            target_usd = real_capital_usd * position_pct / 100.0
                            target_sol = target_usd / sol_price_usd
                        else:
                            target_sol = 0.02

                        # Safety floor: never trade more than 50% of wallet
                        if target_sol > max_safe_sol and max_safe_sol > 0:
                            target_sol = max_safe_sol

                        # Final safety cap
                        target_sol = min(target_sol, v300_max_trade_sol)

                        # Floor: skip if too small
                        if target_sol < v300_min_trade_sol:
                            logger.info(
                                f"Skip {token.symbol}: target {target_sol:.4f} SOL "
                                f"too small (wallet={wallet_sol:.4f} SOL, "
                                f"pct={position_pct:.1f}%, min={v300_min_trade_sol})"
                            )
                            continue
                        target_sol = max(target_sol, v300_min_trade_sol)

                        logger.info(
                            f"Live trade {token.symbol}: {target_sol:.4f} SOL "
                            f"(~${target_sol * (sol_price_usd or 0):.2f}) "
                            f"= {position_pct:.1f}% of ${real_capital_usd:.0f} wallet "
                            f"[{'V300-SMALL' if is_small_cap else 'V200'}]"
                        )
                        result = await self.live.open_position(
                            token_id=token.id,
                            amount_sol=target_sol,
                            entry_price=decision.suggested_entry or token.price_usd or 0,
                            stop_loss=decision.suggested_stop_loss,
                            take_profit=decision.suggested_take_profit,
                            holding_period_minutes=decision.suggested_holding_period_minutes or 60,
                            analysis_snapshot_id=snapshot_id,
                            entry_reasons=decision.reasons,
                        )
                        if result.get("success"):
                            summary["trades_opened"] += 1
                            summary["live_trades_opened"] += 1
                            await self.alerts.send(
                                "live_trade_opened",
                                f"🟢 صفقة حقيقية: {token.symbol}",
                                f"دخول @ ~${result['fill_price']:.10f}\n"
                                f"الحجم: {result['amount_sol']} SOL (~${result['notional_usd']:.2f})\n"
                                f"TX: {result['tx_signature']}",
                                {
                                    "trade_id": result["trade_id"],
                                    "token": token.symbol,
                                    "tx": result["tx_signature"],
                                },
                            )
                        else:
                            summary["trades_rejected"] += 1
                            logger.warning(
                                f"Live trade rejected for {token.symbol}: "
                                f"{result.get('code')} - {result.get('error')}"
                            )
                    else:
                        # === PAPER PATH ===
                        trade = await self.engine.open_position(
                            token_id=token.id,
                            entry_price=decision.suggested_entry or token.price_usd or 0,
                            position_size_usd=settings.paper_capital_usd * decision.suggested_position_size_pct / 100,
                            stop_loss=decision.suggested_stop_loss,
                            take_profit=decision.suggested_take_profit,
                            holding_period_minutes=decision.suggested_holding_period_minutes,
                            strategy_id="default_v1",
                            analysis_snapshot_id=snapshot_id,
                            entry_reasons=decision.reasons,
                            liquidity_usd=token.liquidity_usd or 0,
                        )
                        if trade:
                            summary["trades_opened"] += 1
                            await self.alerts.send(
                                "trade_opened",
                                f"صفقة افتراضية: {token.symbol}",
                                f"دخول @ ${trade.entry_price:.8f}\n"
                                f"الحجم: ${trade.entry_size_usd:.2f}\n"
                                f"Stop Loss: ${trade.stop_loss_price:.8f}\n"
                                f"Take Profit: ${trade.take_profit_price:.8f}\n"
                                f"احتمال النجاح: {decision.win_probability:.1%}",
                                {
                                    "trade_id": trade.id,
                                    "token": token.symbol,
                                    "win_probability": decision.win_probability,
                                },
                            )
                elif anti_check["blocked"]:
                    await self.alerts.send(
                        "opportunity_rejected",
                        f"رفض فرصة: {token.symbol}",
                        f"سبب: {anti_check['reason']}",
                        {"token": token.symbol, "reason": anti_check["reason"]},
                    )
                elif decision.recommendation == "reject":
                    if decision.risk_score > 80:
                        await self.alerts.send(
                            "scam_detected",
                            f"احتيال محتمل: {token.symbol}",
                            decision.rejection_reason,
                            {"token": token.symbol, "risk_score": decision.risk_score},
                        )

            except Exception as exc:
                logger.exception(f"Analysis failed for token {token.id}: {exc}")

        # === 5. Manage open positions ===
        try:
            closed = await self.engine.manage_open_positions()
            for reason, count in closed.items():
                summary["trades_closed"] += count
        except Exception as exc:
            logger.exception(f"Paper position management failed: {exc}")

        # === 5b. Manage live positions (real money) ===
        if settings.is_live_trading:
            try:
                live_closed = await self.live.manage_open_positions()
                for reason, count in live_closed.items():
                    summary["trades_closed"] += count
            except Exception as exc:
                logger.exception(f"Live position management failed: {exc}")

        # === 6. Analyze newly closed trades (learning) ===
        await self._analyze_recently_closed_trades()

        logger.info(
            f"✅ Cycle done | scanned={summary['scanned']} analyzed={summary['analyzed']} "
            f"qualified={summary['qualified']} opened={summary['trades_opened']} "
            f"closed={summary['trades_closed']}"
        )
        return summary

    async def _fetch_broad_market_candles(self):
        """Fetch SOL/ETH candles as broad market proxy."""
        # Try to find SOL token in DB
        async with get_session() as s:
            stmt = select(Token).where(
                Token.symbol.in_(["SOL", "WETH", "ETH"])
            ).order_by(desc(Token.liquidity_usd)).limit(1)
            token = (await s.execute(stmt)).scalar_one_or_none()
            if not token:
                return None

        candles = await self.ohlcv_fetcher.get_latest_candles(token.id, "1m", 200)
        if not candles:
            candles = await self.ohlcv_fetcher.fetch_and_store(token.id, "1m", 200)
        if not candles:
            return None

        return pd.DataFrame([{
            "timestamp": c.timestamp,
            "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "volume": c.volume,
        } for c in candles])

    async def _analyze_recently_closed_trades(self):
        """Find trades that closed since last cycle and analyze them for learning."""
        async with get_session() as s:
            stmt = (
                select(Trade)
                .where(Trade.status == "closed")
                .order_by(desc(Trade.exit_timestamp))
                .limit(5)
            )
            recent = (await s.execute(stmt)).scalars().all()

        # We can't tell which were analyzed already without a flag,
        # so we check if any Mistake record references this trade_id
        for trade in recent:
            async with get_session() as s:
                from ..core.models import Mistake
                existing = await s.execute(
                    select(Mistake).where(Mistake.trade_id == trade.id).limit(1)
                )
                if existing.scalar_one_or_none():
                    continue  # already analyzed

            if (trade.net_pnl_usd or 0) > 0:
                win_result = await self.win_analyzer.analyze(trade)
                logger.info(f"📊 Win analyzed: {win_result.summary}")
                if win_result.repeatable:
                    # Save as learning
                    from ..core.models import Learning
                    learning = Learning(
                        learning_type="success_factor",
                        pattern_name=f"win_pattern_{trade.strategy_id}",
                        description=win_result.summary,
                        conditions_json={"factors": win_result.success_factors},
                        expected_outcome_json={"pnl": trade.net_pnl_usd},
                        supporting_trades_count=1,
                        confidence=0.5,
                    )
                    async with get_session() as s:
                        s.add(learning)
            else:
                loss_result = await self.loss_analyzer.analyze(trade)
                logger.info(f"📉 Loss analyzed: {loss_result.summary}")
                await self.mistake_memory.record_mistake(trade, loss_result)
                await self.alerts.send(
                    "loss_realized",
                    f"خسارة افتراضية: ${trade.net_pnl_usd:.2f}",
                    loss_result.summary,
                    {
                        "trade_id": trade.id,
                        "causes": loss_result.causes,
                        "evidence": loss_result.evidence,
                    },
                )

    async def run_forever(self, interval_seconds: Optional[int] = None):
        """Run the orchestrator in a continuous loop."""
        interval = interval_seconds or settings.scan_interval_seconds
        timeout = max(60, int(settings.live_cycle_timeout_seconds))
        self._running = True
        logger.info(f"🚀 Main orchestrator started (cycle every {interval}s, timeout {timeout}s)")
        while self._running:
            try:
                await asyncio.wait_for(self.run_cycle(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Orchestrator cycle timed out after {timeout}s, continuing...")
            except Exception as exc:
                logger.warning(f"Orchestrator cycle failed: {exc}")
                await asyncio.sleep(30)
            await asyncio.sleep(interval)

    async def fast_monitor_forever(self, interval_seconds: float = 1.0):
        """
        Fast exit monitor.

        The full analysis cycle is slow (scanning + LLM agents).  Stop-loss and
        take-profit must react quickly, so this lightweight loop checks open
        positions every second using the latest prices already stored by the
        scanner.
        """
        logger.info(f"⚡ Fast position monitor started (every {interval_seconds}s)")
        while self._running:
            try:
                await self.engine.manage_open_positions()
                if settings.is_live_trading:
                    await self.live.manage_open_positions()
            except Exception as exc:
                logger.debug(f"Fast monitor error: {exc}")
            await asyncio.sleep(interval_seconds)

    async def stop(self):
        self._running = False
        await self.scanner.close()
        await self.ohlcv_fetcher.close()
        await self.alerts.close()
        if self.decision_engine:
            await self.decision_engine.close()
        logger.info("Orchestrator stopped")


# Periodic ML training job (run separately)
async def periodic_ml_training():
    """ML training disabled - using rule-based engine."""
    logger.info("ML training disabled - using SmartDecisionEngine")
    # Just sleep forever (keeps the task alive without errors)
    while True:
        await asyncio.sleep(3600)
