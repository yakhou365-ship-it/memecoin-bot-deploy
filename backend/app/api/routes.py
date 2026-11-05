"""
FastAPI routes.

All endpoints return JSON. CORS is enabled for the React dashboard.
"""
from __future__ import annotations
import os

from datetime import datetime
from typing import Any, Dict, List, Optional

import fastapi
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, func, and_, desc, or_

from ..core.config import settings
from ..core.database import get_session, get_db
from ..core.logger import logger
from ..core.models import (
    Token, Trade, AnalysisSnapshot, Alert, Mistake, Learning,
    ModelVersion, ScanRun, StrategyVersion, TradingState,
)
from ..modules.paper_trading.engine import PaperTradingEngine
from ..modules.live_trading.executor import LiveTradingExecutor, is_live_trade
from ..modules.risk_management.manager import RiskManager
from ..modules.learning.system import (
    ContinuousImprover, MistakeMemory, LossAnalyzer, WinAnalyzer,
)
from ..modules.ai_research.researcher import AIResearcher
from ..modules.market_scanner.scanner import MarketScanner
from ..modules.market_scanner.ohlcv_fetcher import OHLCVFetcher
from ..modules.alerts.manager import AlertManager
from ..modules.backtesting.engine import Backtester


router = APIRouter(prefix="/api", tags=["main"])


# =========================================================================
# Account / Stats
# =========================================================================
@router.get("/account")
async def get_account():
    """Get paper trading account state."""
    engine = PaperTradingEngine()
    state = await engine.get_account_state()
    stats = await engine.get_stats()
    return {
        "starting_capital": state.starting_capital,
        "cash_balance": state.cash_balance,
        "equity": state.equity,
        "open_positions_value": state.open_positions_value,
        "unrealized_pnl": state.unrealized_pnl,
        "realized_pnl": state.realized_pnl,
        "total_pnl": state.total_pnl,
        "total_pnl_pct": state.total_pnl_pct,
        "max_drawdown_pct": state.max_drawdown_pct,
        "open_positions_count": state.open_positions_count,
        "win_rate": state.win_rate,
        "winning_trades": state.winning_trades_count,
        "losing_trades": state.losing_trades_count,
        "closed_trades": state.closed_trades_count,
        "total_fees_paid": state.total_fees_paid,
        "total_slippage_paid": state.total_slippage_paid,
        "stats": stats,
    }


# =========================================================================
# Tokens
# =========================================================================
@router.get("/tokens")
async def list_tokens(
    limit: int = Query(50, le=200),
    offset: int = 0,
    chain: Optional[str] = None,
    only_active: bool = True,
    only_blacklisted: bool = False,
    min_opportunity: Optional[float] = None,
    sort_by: str = "watch_priority",
):
    """List discovered tokens."""
    sort_col = {
        "watch_priority": Token.watch_priority,
        "opportunity": Token.latest_opportunity_score,
        "volume": Token.volume_24h_usd,
        "liquidity": Token.liquidity_usd,
        "updated": Token.updated_at,
    }.get(sort_by, Token.watch_priority)

    async with get_session() as s:
        stmt = select(Token)
        if only_active:
            stmt = stmt.where(Token.active == True)
        if only_blacklisted:
            stmt = stmt.where(Token.blacklisted == True)
        else:
            stmt = stmt.where(Token.blacklisted == False)
        if chain:
            stmt = stmt.where(Token.chain == chain)
        if min_opportunity:
            stmt = stmt.where(Token.latest_opportunity_score >= min_opportunity)
        stmt = stmt.order_by(desc(sort_col)).limit(limit).offset(offset)
        result = await s.execute(stmt)
        tokens = list(result.scalars().all())

    return [
        {
            "id": t.id,
            "address": t.address,
            "chain": t.chain,
            "symbol": t.symbol,
            "name": t.name,
            "dex": t.dex,
            "price_usd": t.price_usd,
            "market_cap_usd": t.market_cap_usd,
            "liquidity_usd": t.liquidity_usd,
            "volume_24h_usd": t.volume_24h_usd,
            "volume_5m_usd": t.volume_5m_usd,
            "txns_24h_buys": t.txns_24h_buys,
            "txns_24h_sells": t.txns_24h_sells,
            "price_change_24h_pct": t.price_change_24h_pct,
            "price_change_5m_pct": t.price_change_5m_pct,
            "fdv_usd": t.fdv_usd,
            "watch_priority": t.watch_priority,
            "latest_opportunity_score": t.latest_opportunity_score,
            "latest_risk_score": t.latest_risk_score,
            "latest_win_probability": t.latest_win_probability,
            "first_seen_at": t.first_seen_at.isoformat() if t.first_seen_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            "active": t.active,
            "blacklisted": t.blacklisted,
            "blacklist_reason": t.blacklist_reason,
        }
        for t in tokens
    ]


@router.get("/tokens/{token_id}")
async def get_token_detail(token_id: str):
    """Get full detail of a token, including latest analysis."""
    async with get_session() as s:
        token = await s.get(Token, token_id)
        if not token:
            raise HTTPException(404, "Token not found")

        # Latest snapshot
        snap_stmt = (
            select(AnalysisSnapshot)
            .where(AnalysisSnapshot.token_id == token_id)
            .order_by(desc(AnalysisSnapshot.timestamp))
            .limit(1)
        )
        snapshot = (await s.execute(snap_stmt)).scalar_one_or_none()

    return {
        "token": {
            "id": token.id,
            "address": token.address,
            "chain": token.chain,
            "symbol": token.symbol,
            "name": token.name,
            "dex": token.dex,
            "pair_address": token.pair_address,
            "price_usd": token.price_usd,
            "market_cap_usd": token.market_cap_usd,
            "liquidity_usd": token.liquidity_usd,
            "volume_24h_usd": token.volume_24h_usd,
            "volume_5m_usd": token.volume_5m_usd,
            "txns_24h_buys": token.txns_24h_buys,
            "txns_24h_sells": token.txns_24h_sells,
            "price_change_24h_pct": token.price_change_24h_pct,
            "price_change_5m_pct": token.price_change_5m_pct,
            "fdv_usd": token.fdv_usd,
            "watch_priority": token.watch_priority,
            "first_seen_at": token.first_seen_at.isoformat() if token.first_seen_at else None,
            "updated_at": token.updated_at.isoformat() if token.updated_at else None,
        },
        "latest_snapshot": {
            "id": snapshot.id,
            "timestamp": snapshot.timestamp.isoformat() if snapshot else None,
            "technical_score": snapshot.technical_score if snapshot else None,
            "liquidity_score": snapshot.liquidity_score if snapshot else None,
            "onchain_score": snapshot.onchain_score if snapshot else None,
            "security_score": snapshot.security_score if snapshot else None,
            "sentiment_score": snapshot.sentiment_score if snapshot else None,
            "momentum_score": snapshot.momentum_score if snapshot else None,
            "market_conditions_score": snapshot.market_conditions_score if snapshot else None,
            "volatility_score": snapshot.volatility_score if snapshot else None,
            "risk_score": snapshot.risk_score if snapshot else None,
            "opportunity_score": snapshot.opportunity_score if snapshot else None,
            "win_probability": snapshot.win_probability if snapshot else None,
            "win_probability_lower_ci": snapshot.win_probability_lower_ci if snapshot else None,
            "win_probability_upper_ci": snapshot.win_probability_upper_ci if snapshot else None,
            "market_regime": snapshot.market_regime if snapshot else None,
            "qualified": snapshot.qualified if snapshot else False,
            "rejection_reason": snapshot.rejection_reason if snapshot else None,
            "reasons": snapshot.reasons_json if snapshot else [],
            "reasoning_summary": snapshot.reasoning_summary if snapshot else None,
            "indicators": snapshot.indicators_json if snapshot else {},
            "flags": snapshot.flags_json if snapshot else {},
        } if snapshot else None,
    }


# =========================================================================
# Trades
# =========================================================================
@router.get("/trades")
async def list_trades(
    limit: int = Query(100, le=500),
    offset: int = 0,
    status: Optional[str] = None,
):
    """List paper trades."""
    async with get_session() as s:
        stmt = select(Trade, Token).join(Token, Trade.token_id == Token.id, isouter=True)
        if status:
            stmt = stmt.where(Trade.status == status)
        stmt = stmt.order_by(desc(Trade.created_at)).limit(limit).offset(offset)
        result = await s.execute(stmt)
        rows = result.all()

    return [
        {
            "id": t.id,
            "token_id": t.token_id,
            "token_symbol": tok.symbol if tok else None,
            "token_chain": tok.chain if tok else None,
            "side": t.side,
            "status": t.status,
            "entry_price": t.entry_price,
            "entry_size_usd": t.entry_size_usd,
            "entry_quantity": t.entry_quantity,
            "entry_timestamp": t.entry_timestamp.isoformat() if t.entry_timestamp else None,
            "exit_price": t.exit_price,
            "exit_timestamp": t.exit_timestamp.isoformat() if t.exit_timestamp else None,
            "exit_reason": t.exit_reason,
            "net_pnl_usd": t.net_pnl_usd,
            "pnl_pct": t.pnl_pct,
            "fees_total_usd": t.fees_total_usd,
            "slippage_total_usd": t.slippage_total_usd,
            "max_unrealized_profit_usd": t.max_unrealized_profit_usd,
            "max_unrealized_loss_usd": t.max_unrealized_loss_usd,
            "stop_loss_price": t.stop_loss_price,
            "take_profit_price": t.take_profit_price,
            "strategy_id": t.strategy_id,
            "entry_reasons": t.entry_reasons_json,
            "exit_reasons": t.exit_reasons_json,
        }
        for t, tok in rows
    ]


class CloseTradeRequest(BaseModel):
    exit_reason: str = "manual"


@router.post("/trades/{trade_id}/close")
async def close_trade(trade_id: str, req: CloseTradeRequest):
    """Manually close a trade. Live trades are sold on-chain via PumpPortal."""
    async with get_session() as s:
        trade = await s.get(Trade, trade_id)
        if not trade or trade.status != "open":
            raise HTTPException(400, "Cannot close trade")
        live = is_live_trade(trade)

    if live:
        if not settings.is_live_trading:
            raise HTTPException(400, "Live trading is not enabled; cannot sell on-chain")
        result = await LiveTradingExecutor().close_position(trade_id, req.exit_reason)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Cannot close live trade"))
        return {
            "status": "closed",
            "trade_id": trade_id,
            "live": True,
            "net_pnl": result.get("net_pnl_usd"),
            "tx_signature": result.get("tx_signature"),
        }

    engine = PaperTradingEngine()
    closed = await engine.close_position(trade_id, exit_reason=req.exit_reason)
    if not closed:
        raise HTTPException(400, "Cannot close trade")
    return {
        "status": "closed",
        "trade_id": closed.id,
        "live": False,
        "net_pnl": closed.net_pnl_usd,
    }


# =========================================================================
# Mistakes / Learning
# =========================================================================
@router.get("/learning/mistakes")
async def list_mistakes(limit: int = Query(50, le=200)):
    async with get_session() as s:
        stmt = select(Mistake).order_by(desc(Mistake.created_at)).limit(limit)
        result = await s.execute(stmt)
        return [
            {
                "id": m.id,
                "mistake_type": m.mistake_type,
                "description": m.description,
                "conditions": m.conditions_json,
                "evidence": m.evidence_json,
                "suggested_fix": m.suggested_fix_json,
                "fix_tested": m.fix_tested,
                "fix_test_result": m.fix_test_result,
                "fix_adopted": m.fix_adopted,
                "active_block": m.active_block,
                "occurrence_count": m.occurrence_count,
                "last_occurred_at": m.last_occurred_at.isoformat() if m.last_occurred_at else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in result.scalars().all()
        ]


@router.get("/learning/analysis")
async def get_learning_analysis():
    """Get continuous improvement analysis of recent trades."""
    improver = ContinuousImprover()
    return await improver.analyze_recent_trades(lookback_days=7)


@router.get("/learning/learnings")
async def list_learnings(limit: int = Query(50, le=200)):
    async with get_session() as s:
        stmt = select(Learning).order_by(desc(Learning.created_at)).limit(limit)
        result = await s.execute(stmt)
        return [
            {
                "id": l.id,
                "learning_type": l.learning_type,
                "pattern_name": l.pattern_name,
                "description": l.description,
                "conditions": l.conditions_json,
                "expected_outcome": l.expected_outcome_json,
                "supporting_trades": l.supporting_trades_count,
                "contradicting_trades": l.contradicting_trades_count,
                "confidence": l.confidence,
                "validated": l.validated,
                "adopted": l.adopted,
            }
            for l in result.scalars().all()
        ]


# =========================================================================
# Risk Manager
# =========================================================================
@router.get("/risk/state")
async def get_risk_state():
    rm = RiskManager()
    state = await rm.get_state()
    return state.to_dict()


@router.post("/risk/reset-circuit-breaker")
async def reset_circuit_breaker():
    rm = RiskManager()
    await rm.reset_circuit_breaker()
    return {"status": "ok"}


# =========================================================================
# Alerts
# =========================================================================
@router.get("/alerts")
async def list_alerts(limit: int = Query(50, le=200)):
    async with get_session() as s:
        stmt = select(Alert).order_by(desc(Alert.created_at)).limit(limit)
        result = await s.execute(stmt)
        return [
            {
                "id": a.id,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "title": a.title,
                "message": a.message,
                "data": a.data_json,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in result.scalars().all()
        ]


# =========================================================================
# Scanner Control
# =========================================================================
class ScanRequest(BaseModel):
    chain: Optional[str] = None


@router.post("/scanner/run")
async def run_scan(req: ScanRequest):
    """Trigger a manual scan."""
    scanner = MarketScanner()
    if req.chain:
        result = await scanner.scan_chain(req.chain)
    else:
        result = await scanner.scan_all_chains()
    await scanner.close()
    return {"status": "ok", "results": result}


@router.get("/scanner/runs")
async def list_scan_runs(limit: int = 20):
    async with get_session() as s:
        stmt = select(ScanRun).order_by(desc(ScanRun.started_at)).limit(limit)
        result = await s.execute(stmt)
        return [
            {
                "id": r.id,
                "chain": r.chain,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "tokens_discovered": r.tokens_discovered,
                "tokens_updated": r.tokens_updated,
                "tokens_blacklisted": r.tokens_blacklisted,
                "errors": r.errors,
                "status": r.status,
            }
            for r in result.scalars().all()
        ]


# =========================================================================
# ML Models
# =========================================================================
@router.get("/ml/models")
async def list_ml_models():
    async with get_session() as s:
        stmt = select(ModelVersion).order_by(desc(ModelVersion.created_at)).limit(20)
        result = await s.execute(stmt)
        return [
            {
                "id": m.id,
                "model_type": m.model_type,
                "purpose": m.purpose,
                "version": m.version,
                "train_samples": m.train_samples,
                "validation_samples": m.validation_samples,
                "test_samples": m.test_samples,
                "precision": m.precision,
                "recall": m.recall,
                "f1": m.f1,
                "roc_auc": m.roc_auc,
                "brier_score": m.brier_score,
                "calibration_error": m.calibration_error,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in result.scalars().all()
        ]




# =========================================================================
# Training Pipeline - generates data + trains ML to reach 70%+ win rate
# =========================================================================
@router.post("/ml/train")
async def train_ml_model():
    """Train a win-probability model on *real* closed trades only.

    Honest behaviour:
    * Requires at least ``ML_MIN_TRAIN_SAMPLES`` (default 30) closed trades that
      have a linked analysis snapshot.
    * Trains a small logistic-regression classifier with numpy (no heavy ML
      dependency required by the Railway image).
    * Reports metrics computed on a held-out test split — never random values.

    If there is not enough data, returns ``status="insufficient_data"`` and
    creates no model row.
    """
    import numpy as np

    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(Trade, AnalysisSnapshot)
                .join(AnalysisSnapshot, Trade.analysis_snapshot_id == AnalysisSnapshot.id)
                .where(Trade.status == "closed")
            )).all()

        feature_names = [
            "technical_score", "liquidity_score", "onchain_score",
            "security_score", "sentiment_score", "momentum_score",
            "market_conditions_score", "volatility_score", "risk_score",
            "opportunity_score",
        ]

        X: list[list[float]] = []
        y: list[int] = []
        for trade, snap in rows:
            if trade.net_pnl_usd is None:
                continue
            values = []
            ok = True
            for name in feature_names:
                value = getattr(snap, name, None)
                if value is None:
                    ok = False
                    break
                values.append(float(value))
            if not ok:
                continue
            X.append(values)
            y.append(1 if trade.net_pnl_usd > 0 else 0)

        total = len(y)
        min_samples = max(10, settings.ml_min_train_samples)
        if total < min_samples:
            return {
                "status": "insufficient_data",
                "message": (
                    f"Not enough real closed trades to train honestly: {total} "
                    f"labelled samples, {min_samples} required. Collect more "
                    "paper/live trades first."
                ),
                "labelled_samples": total,
                "required_samples": min_samples,
                "models_created": 0,
            }

        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)

        # Chronological-ish split: first 80% train, last 20% test.
        split = int(total * 0.8)
        X_train, X_test = X_arr[:split], X_arr[split:]
        y_train, y_test = y_arr[:split], y_arr[split:]

        mean = X_train.mean(axis=0)
        std = X_train.std(axis=0)
        std[std == 0] = 1.0
        X_train_n = (X_train - mean) / std
        X_test_n = (X_test - mean) / std

        # Logistic regression via gradient descent
        w = np.zeros(X_train_n.shape[1])
        b = 0.0
        lr = 0.1
        for _ in range(2000):
            z = X_train_n @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -50, 50)))
            grad_w = X_train_n.T @ (p - y_train) / max(len(y_train), 1)
            grad_b = float(np.mean(p - y_train))
            w -= lr * grad_w
            b -= lr * grad_b

        z_test = X_test_n @ w + b
        probs = 1.0 / (1.0 + np.exp(-np.clip(z_test, -50, 50)))
        preds = (probs >= 0.5).astype(int)

        tp = int(np.sum((preds == 1) & (y_test == 1)))
        fp = int(np.sum((preds == 1) & (y_test == 0)))
        fn = int(np.sum((preds == 0) & (y_test == 1)))
        tn = int(np.sum((preds == 0) & (y_test == 0)))

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        accuracy = (tp + tn) / max(len(y_test), 1)

        # ROC-AUC via pairwise ranking
        pos = probs[y_test == 1]
        neg = probs[y_test == 0]
        if len(pos) and len(neg):
            roc_auc = float(np.mean(pos[:, None] > neg[None, :]))
        else:
            roc_auc = 0.5

        brier = float(np.mean((probs - y_test) ** 2))

        version = int(datetime.utcnow().timestamp())
        model = ModelVersion(
            model_type="logistic_regression",
            purpose="win_probability",
            version=version,
            model_path=f"/data/models/logreg_{version}.json",
            features_json=feature_names,
            train_samples=len(y_train),
            validation_samples=len(y_test),
            test_samples=len(y_test),
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            roc_auc=round(roc_auc, 4),
            brier_score=round(brier, 4),
            calibration_error=round(abs(float(np.mean(probs)) - float(np.mean(y_test))), 4),
            status="trained",
        )
        async with get_session() as s:
            s.add(model)
            await s.flush()
            model_id = model.id

        return {
            "status": "success",
            "message": f"Trained on {total} real closed trades",
            "model_id": model_id,
            "labelled_samples": total,
            "train_samples": len(y_train),
            "test_samples": len(y_test),
            "metrics": {
                "accuracy": round(accuracy, 4),
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "roc_auc": round(roc_auc, 4),
                "brier_score": round(brier, 4),
            },
            "note": "Metrics are measured on a held-out test split, not simulated.",
        }
    except Exception as exc:
        logger.exception(f"ML training failed: {exc}")
        raise HTTPException(500, f"Training failed: {str(exc)[:200]}")


# =========================================================================
# Strategies
# =========================================================================
@router.get("/strategies")
async def list_strategies():
    async with get_session() as s:
        stmt = select(StrategyVersion).order_by(desc(StrategyVersion.version)).limit(20)
        result = await s.execute(stmt)
        return [
            {
                "id": sv.id,
                "version": sv.version,
                "name": sv.name,
                "description": sv.description,
                "config": sv.config_json,
                "backtest_win_rate": sv.backtest_win_rate,
                "backtest_profit_factor": sv.backtest_profit_factor,
                "backtest_expectancy": sv.backtest_expectancy,
                "backtest_max_drawdown_pct": sv.backtest_max_drawdown_pct,
                "backtest_sample_size": sv.backtest_sample_size,
                "status": sv.status,
                "created_at": sv.created_at.isoformat() if sv.created_at else None,
            }
            for sv in result.scalars().all()
        ]


# =========================================================================
# Backtest
# =========================================================================
class BacktestRequest(BaseModel):
    token_id: str
    timeframe: str = "5m"
    lookback_days: int = 7


@router.post("/backtest/run")
async def run_backtest(req: BacktestRequest):
    bt = Backtester()
    result = await bt.backtest_token(
        req.token_id, req.timeframe, req.lookback_days
    )
    return result.to_dict()


# =========================================================================
# AI Research
# =========================================================================
@router.get("/research/{token_id}")
async def get_research(token_id: str):
    """Generate AI research report for a token."""
    async with get_session() as s:
        token = await s.get(Token, token_id)
        if not token:
            raise HTTPException(404, "Token not found")
        snap_stmt = (
            select(AnalysisSnapshot)
            .where(AnalysisSnapshot.token_id == token_id)
            .order_by(desc(AnalysisSnapshot.timestamp))
            .limit(1)
        )
        snapshot = (await s.execute(snap_stmt)).scalar_one_or_none()

    researcher = AIResearcher()
    report = researcher.generate_token_report(token, snapshot)
    return {
        "confirmed_facts": report.confirmed_facts,
        "probable_inferences": report.probable_inferences,
        "hypotheses": report.hypotheses,
        "missing_data": report.missing_data,
        "recommendation": report.recommendation,
        "confidence_level": report.confidence_level,
        "summary_ar": report.summary_ar,
    }


# =========================================================================
# System
# =========================================================================
@router.get("/system/status")
async def system_status():
    """Get overall system status."""
    rm = RiskManager()
    risk_state = await rm.get_state()

    async with get_session() as s:
        tokens_count = (await s.execute(
            select(func.count(Token.id)).where(Token.active == True)
        )).scalar() or 0
        open_paper = (await s.execute(
            select(func.count(Trade.id)).where(
                Trade.status == "open",
                or_(Trade.strategy_id.is_(None), ~Trade.strategy_id.startswith("live")),
            )
        )).scalar() or 0
        open_live = (await s.execute(
            select(func.count(Trade.id)).where(
                Trade.status == "open",
                Trade.strategy_id.startswith("live"),
            )
        )).scalar() or 0
        closed_trades = (await s.execute(
            select(func.count(Trade.id)).where(Trade.status == "closed")
        )).scalar() or 0
        last_alert = (await s.execute(
            select(Alert).order_by(desc(Alert.created_at)).limit(1)
        )).scalar_one_or_none()
        trading_state = await s.get(TradingState, 1)

    return {
        "risk_state": risk_state.to_dict(),
        "tokens_active": tokens_count,
        "open_trades": open_paper + open_live,
        "open_paper_trades": open_paper,
        "open_live_trades": open_live,
        "closed_trades": closed_trades,
        "circuit_breaker_active": risk_state.circuit_breaker_active,
        "cooldown_active": risk_state.cooldown_active,
        "last_alert": last_alert.created_at.isoformat() if last_alert else None,
        "version": "0.3.0",
        "trading_mode": settings.trading_mode,
        "is_live_trading": settings.is_live_trading,
        "live_requirements_missing": settings.live_requirements,
        "trading_active": bool(trading_state.is_active) if trading_state else settings.auto_start_trading,
        "orchestrator_enabled": settings.enable_orchestrator,
        "ai_agents_enabled": {
            "technical": settings.ai_agent_technical_enabled,
            "security": settings.ai_agent_security_enabled,
            "liquidity": settings.ai_agent_liquidity_enabled,
            "sentiment": settings.ai_agent_sentiment_enabled,
            "risk": settings.ai_agent_risk_enabled,
        },
        "pumpfun_configured": bool(settings.solana_private_key and settings.solana_wallet_address),
    }


# =========================================================================
# AI Agents - 5 specialized AI agents
# =========================================================================
@router.get("/agents/status")
async def agents_status():
    """Get the latest agent consensus result."""
    from ..modules.ai_agents.orchestrator import AgentOrchestrator
    orch = AgentOrchestrator()
    latest = await orch.get_latest_consensus()
    return {
        "agents": [
            {"name": "technical_agent", "role": "Ultimate Technical Analyst", "enabled": settings.ai_agent_technical_enabled, "has_veto": False, "weight": 1.3},
            {"name": "security_agent", "role": "Ultimate Security Analyst", "enabled": settings.ai_agent_security_enabled, "has_veto": True, "weight": 2.0},
            {"name": "liquidity_agent", "role": "Ultimate Liquidity Analyst", "enabled": settings.ai_agent_liquidity_enabled, "has_veto": False, "weight": 1.2},
            {"name": "sentiment_agent", "role": "Ultimate Sentiment Analyst", "enabled": settings.ai_agent_sentiment_enabled, "has_veto": False, "weight": 1.0},
            {"name": "risk_agent", "role": "Ultimate Risk Manager", "enabled": settings.ai_agent_risk_enabled, "has_veto": True, "weight": 2.0},
        ],
        "require_unanimity": settings.ai_agent_require_unanimity,
        "latest_consensus": latest,
    }


class RunAgentsRequest(BaseModel):
    token_id: str


class RunAgentsByMintRequest(BaseModel):
    mint_address: str


@router.post("/agents/run")
async def run_agents(req: RunAgentsRequest):
    """Manually trigger AI agent consensus on a token (by DB token id)."""
    return await _run_agents_for_token(req.token_id)


@router.post("/agents/run-by-mint")
async def run_agents_by_mint(req: RunAgentsByMintRequest):
    """Run AI agent consensus by mint address (auto-creates placeholder row).

    Allows running the agents directly from the live token list even before
    the scanner has persisted the token in the DB.
    """
    mint = (req.mint_address or "").strip()
    if not mint:
        raise HTTPException(400, "mint_address is required")

    async with get_session() as s:
        token = (await s.execute(
            select(Token).where(Token.address == mint)
        )).scalar_one_or_none()
        if token is None:
            meta = await _lookup_token_meta(mint)
            token = Token(
                address=mint,
                chain="solana",
                symbol=(meta.get("symbol") or mint[:6])[:64],
                name=(meta.get("name") or "Manual agent run")[:256],
                dex=meta.get("dex"),
                price_usd=meta.get("price_usd"),
                market_cap_usd=meta.get("market_cap_usd"),
                liquidity_usd=meta.get("liquidity_usd"),
                active=True,
            )
            s.add(token)
            await s.flush()
        token_id = token.id

    return await _run_agents_for_token(token_id)


async def _run_agents_for_token(token_id: str):
    from ..modules.ai_agents.orchestrator import AgentOrchestrator
    from ..modules.decision_engine.engine import DecisionEngine
    from ..modules.market_scanner.ohlcv_fetcher import OHLCVFetcher
    from ..modules.market_regime.detector import MarketRegimeDetector
    import pandas as pd

    async with get_session() as s:
        token = await s.get(Token, token_id)
        if not token:
            raise HTTPException(404, "Token not found")

    # Fetch candles
    ohlcv = OHLCVFetcher()
    candles = await ohlcv.fetch_and_store(token.id, "1m", 200)
    if not candles:
        candles = await ohlcv.get_latest_candles(token.id, "1m", 200)
    df = pd.DataFrame([{
        "timestamp": c.timestamp, "open": c.open, "high": c.high,
        "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles]) if candles else None

    # Run decision engine to get analysis context
    engine = DecisionEngine()
    regime = MarketRegimeDetector().detect(broad_market_candles=df) if df is not None else None
    decision = await engine.analyze(token, candles_df=df, market_regime=regime)

    # Get risk state
    rm = RiskManager()
    risk_state = await rm.get_state()

    # Build a normalized analysis context (same shape the orchestrator uses)
    sec = decision.security_detail or {}
    onchain = decision.onchain_detail or {}
    honeypot_prob = float(sec.get("honeypot_probability", 0) or 0)
    mint_enabled = bool(sec.get("mint_authority_enabled", False))
    freeze_enabled = bool(sec.get("freeze_authority_enabled", False))
    analysis_context = {
        "price_usd": token.price_usd,
        "liquidity_usd": token.liquidity_usd,
        "volume_24h_usd": token.volume_24h_usd,
        "market_cap_usd": token.market_cap_usd,
        "price_change_24h_pct": token.price_change_24h_pct,
        "price_change_5m_pct": token.price_change_5m_pct,
        "volume_5m_usd": token.volume_5m_usd,
        "txns_24h_buys": token.txns_24h_buys,
        "txns_24h_sells": token.txns_24h_sells,
        "technical_score": decision.technical_score,
        "liquidity_score": decision.liquidity_score,
        "security_score": decision.security_score,
        "sentiment_score": decision.sentiment_score,
        "onchain_score": decision.onchain_score,
        "opportunity_score": decision.opportunity_score,
        "market_regime": decision.market_regime,
        "risk_state": risk_state.to_dict(),
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
    }

    # Run consensus
    orch = AgentOrchestrator()
    result = await orch.run_consensus(token, analysis_context)
    return result.to_dict()


# =========================================================================
# =========================================================================






# =========================================================================
# Database Export/Import (for GitHub sync)
# =========================================================================
@router.get("/database/export")
async def export_database():
    """Export the SQLite database as a downloadable file."""
    import os
    from fastapi.responses import FileResponse
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    if not os.path.exists(db_path):
        # Try absolute path
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "..", "data", "memecoin_bot.db")
        db_path = os.path.abspath(db_path)
    if os.path.exists(db_path):
        return FileResponse(
            db_path,
            media_type="application/octet-stream",
            filename="memecoin_bot_backup.db",
        )
    raise HTTPException(404, "Database file not found")


@router.get("/database/info")
async def database_info():
    """Get database file info."""
    import os
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "..", "data", "memecoin_bot.db")
        db_path = os.path.abspath(db_path)
    if os.path.exists(db_path):
        size = os.path.getsize(db_path)
        return {"path": db_path, "size_bytes": size, "size_mb": round(size / 1024 / 1024, 2)}
    return {"path": db_path, "exists": False}



# =========================================================================
# Start/Stop Trading Control
# =========================================================================
@router.get("/trading/state")
async def get_trading_state():
    """Get current trading state (active or stopped)."""
    async with get_session() as s:
        state = await s.get(TradingState, 1)
        if not state:
            state = TradingState(id=1, is_active=settings.auto_start_trading)
            s.add(state)
            await s.flush()
        return {
            "is_active": state.is_active,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "stopped_at": state.stopped_at.isoformat() if state.stopped_at else None,
            "trading_mode": settings.trading_mode,
            "is_live_trading": settings.is_live_trading,
            "live_requirements_missing": settings.live_requirements,
            "orchestrator_enabled": settings.enable_orchestrator,
            "max_trade_sol": settings.pump_max_trade_sol,
            "daily_cap_sol": settings.live_daily_cap_sol,
            "daily_loss_limit_sol": settings.live_daily_loss_limit_sol,
            "max_open_positions": (
                settings.live_max_open_positions
                if settings.is_live_trading
                else settings.paper_max_open_positions
            ),
        }


@router.post("/trading/start")
async def start_trading():
    """Start automated trading."""
    from ..core.models import TradingState
    from datetime import datetime
    async with get_session() as s:
        state = await s.get(TradingState, 1)
        if not state:
            state = TradingState(id=1)
            s.add(state)
            await s.flush()
        if state.is_active:
            return {"status": "already_running", "message": "Trading is already active"}
        state.is_active = True
        state.started_at = datetime.utcnow()
        state.stopped_at = None
    return {"status": "started", "message": "Trading started successfully"}


@router.post("/trading/stop")
async def stop_trading():
    """Stop automated trading."""
    from ..core.models import TradingState
    from datetime import datetime
    async with get_session() as s:
        state = await s.get(TradingState, 1)
        if not state:
            state = TradingState(id=1, is_active=False)
            s.add(state)
            await s.flush()
        if not state.is_active:
            return {"status": "already_stopped", "message": "Trading is already stopped"}
        state.is_active = False
        state.stopped_at = datetime.utcnow()
    return {"status": "stopped", "message": "Trading stopped successfully"}


# =========================================================================
# Daily Trading Budget System
# =========================================================================
@router.get("/budget/settings")
async def get_budget_settings():
    """Get daily budget settings."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    return await bm.get_settings()


@router.post("/budget/settings")
async def update_budget_settings(request: Request):
    """Update daily budget settings."""
    from ..modules.budget.budget_manager import BudgetManager
    try:
        body = await request.json()
    except Exception:
        body = {}
    bm = BudgetManager()
    return await bm.update_settings(body)


@router.get("/budget/daily-record")
async def get_daily_record():
    """Get today's daily trading record."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    return await bm.get_daily_record()


@router.get("/budget/can-trade")
async def check_can_trade(required_capital: float = 50.0, risk_amount: float = 10.0):
    """Check if a new trade can be opened."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    return await bm.check_can_trade(required_capital, risk_amount)


@router.post("/budget/stop")
async def stop_trading_budget():
    """Stop all new trades (budget control)."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    await bm.stop_trading("Manual stop by user")
    return {"status": "stopped", "message": "Trading stopped by user"}


@router.post("/budget/start")
async def start_trading_budget():
    """Resume trading."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    await bm.start_trading()
    return {"status": "started", "message": "Trading resumed by user"}


@router.get("/budget/daily-report")
async def get_daily_report(date: str = None):
    """Get daily performance report."""
    from ..modules.budget.budget_manager import BudgetManager
    bm = BudgetManager()
    return await bm.generate_daily_report(date)


@router.get("/budget/opportunities")
async def get_opportunities():
    """Get ranked trading opportunities."""
    from ..modules.budget.budget_manager import OpportunityDiscovery
    from sqlalchemy import select, desc
    
    od = OpportunityDiscovery()
    
    async with get_session() as s:
        stmt = select(Token).where(Token.active == True, Token.blacklisted == False).order_by(desc(Token.watch_priority)).limit(20)
        tokens = list((await s.execute(stmt)).scalars().all())
    
    # Build technical results from latest snapshots
    from ..core.models import AnalysisSnapshot
    technical_results = {}
    for token in tokens:
        async with get_session() as s:
            snap_stmt = select(AnalysisSnapshot).where(AnalysisSnapshot.token_id == token.id).order_by(desc(AnalysisSnapshot.timestamp)).limit(1)
            snap = (await s.execute(snap_stmt)).scalar_one_or_none()
            if snap:
                tech = (snap.indicators_json or {}).get("technical", {})
                technical_results[token.id] = {
                    "technical_score": snap.technical_score or 50,
                    "rsi": tech.get("rsi", 50),
                    "adx": tech.get("adx", 20),
                    "macd_hist": tech.get("macd_hist", 0),
                    "volatility_pct": tech.get("volatility_pct", 1),
                    "liquidity_score": snap.liquidity_score or 50,
                    "risk_score": snap.risk_score or 30,
                    "data_sufficient": snap.data_sufficient,
                    "market_regime_score": 60,
                }
    
    opportunities = await od.discover_opportunities(tokens, technical_results)
    return [o.to_dict() for o in opportunities[:20]]


# =========================================================================
# Multi-Agent System (18 agents) — new research modules
# =========================================================================
@router.get("/agents/analyze/{token_id}")
async def analyze_token_full(token_id: str):
    """Run full 18-agent analysis on a token."""
    from ..modules.agents.multi_agent_system import MultiAgentSystem
    from ..modules.decision_engine.engine import DecisionEngine
    from ..modules.market_scanner.ohlcv_fetcher import OHLCVFetcher
    from ..modules.market_regime.detector import MarketRegimeDetector
    import pandas as pd

    async with get_session() as s:
        token = await s.get(Token, token_id)
        if not token:
            raise HTTPException(404, "Token not found")

    # Fetch candles for technical analysis
    ohlcv = OHLCVFetcher()
    try:
        candles = await ohlcv.fetch_and_store(token.id, "1m", 200)
        if not candles:
            candles = await ohlcv.get_latest_candles(token.id, "1m", 200)
    except Exception as exc:
        logger.warning(f"analyze_token_full: OHLCV fetch failed: {exc}")
        candles = []

    df = pd.DataFrame([{
        "timestamp": c.timestamp, "open": c.open, "high": c.high,
        "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles]) if candles else None

    # Run decision engine to get the analysis context (indicators, scores, etc.)
    try:
        engine = DecisionEngine()
        regime = MarketRegimeDetector().detect(broad_market_candles=df) if df is not None else None
        decision = await engine.analyze(token, candles_df=df, market_regime=regime)
        rm = RiskManager()
        risk_state = await rm.get_state()
        analysis_context = {
            "indicators": {
                "technical": decision.technical_detail or {},
                "liquidity": decision.liquidity_detail or {},
                "onchain": decision.onchain_detail or {},
                "security": decision.security_detail or {},
                "sentiment": decision.sentiment_detail or {},
            },
            "opportunity_score": decision.opportunity_score,
            "market_regime": decision.market_regime or (regime.__dict__ if regime else {}),
            "risk_state": risk_state.to_dict(),
        }
    except Exception as exc:
        logger.warning(f"analyze_token_full: decision engine failed, using minimal ctx: {exc}")
        analysis_context = {"indicators": {}, "market_regime": {}}

    # Run 18-agent pipeline
    try:
        mas = MultiAgentSystem()
        result = await mas.run_full_analysis(token, analysis_context)
        return result
    except Exception as exc:
        logger.exception(f"Multi-agent analysis failed: {exc}")
        raise HTTPException(500, f"Multi-agent analysis failed: {str(exc)[:200]}")


@router.get("/agents/list")
async def list_agents():
    """List the 5 Ultimate AI agents with their roles, veto status, weights."""
    agents_meta = [
        {
            "name": "technical_agent",
            "role": "Ultimate Technical Analyst",
            "specialty": "Price action, momentum, volume, RSI, trend, bonding curve",
            "has_veto": False,
            "weight": 1.3,
            "enabled": settings.ai_agent_technical_enabled,
        },
        {
            "name": "security_agent",
            "role": "Ultimate Security Analyst",
            "specialty": "Honeypot, rug pull, contract safety, authority checks",
            "has_veto": True,
            "weight": 2.0,
            "enabled": settings.ai_agent_security_enabled,
        },
        {
            "name": "liquidity_agent",
            "role": "Ultimate Liquidity Analyst",
            "specialty": "Liquidity depth, volume, buy/sell pressure, mcap/liq ratio",
            "has_veto": False,
            "weight": 1.2,
            "enabled": settings.ai_agent_liquidity_enabled,
        },
        {
            "name": "sentiment_agent",
            "role": "Ultimate Sentiment Analyst",
            "specialty": "Social sentiment, community hype, trader behavior",
            "has_veto": False,
            "weight": 1.0,
            "enabled": settings.ai_agent_sentiment_enabled,
        },
        {
            "name": "risk_agent",
            "role": "Ultimate Risk Manager",
            "specialty": "Position sizing, circuit breaker, loss limits, exit strategy",
            "has_veto": True,
            "weight": 2.0,
            "enabled": settings.ai_agent_risk_enabled,
        },
    ]
    return {
        "total_agents": len(agents_meta),
        "require_unanimity": settings.ai_agent_require_unanimity,
        "agents": agents_meta,
    }


# =========================================================================
# Health Monitor
# =========================================================================
@router.get("/health-monitor")
async def get_health():
    """Get current system health status (runs full check)."""
    from ..modules.research.health_monitor import HealthMonitor

    try:
        monitor = HealthMonitor()
        status = await monitor.check_health()
        return status.to_dict()
    except Exception as exc:
        logger.exception(f"Health check failed: {exc}")
        raise HTTPException(500, f"Health check failed: {str(exc)[:200]}")


@router.get("/health-monitor/latest")
async def get_health_latest():
    """Get the most recent saved health status (no re-check)."""
    from ..modules.research.health_monitor import HealthMonitor

    try:
        monitor = HealthMonitor()
        status = await monitor.get_latest_status()
        return status.to_dict() if status else {"status": "no_data"}
    except Exception as exc:
        logger.warning(f"get_health_latest failed: {exc}")
        return {"status": "error", "error": str(exc)[:200]}


# =========================================================================
# Independent Auditor
# =========================================================================
@router.get("/audit")
async def run_audit(period_days: int = Query(7, ge=1, le=90)):
    """Run a full independent audit of recent trading performance."""
    from ..modules.research.independent_auditor import IndependentAuditor

    try:
        auditor = IndependentAuditor()
        report = await auditor.audit_performance(period_days=period_days)
        return report.to_dict()
    except Exception as exc:
        logger.exception(f"Audit failed: {exc}")
        raise HTTPException(500, f"Audit failed: {str(exc)[:200]}")


@router.get("/audit/latest")
async def get_audit_latest():
    """Get the most recent saved audit report."""
    from ..modules.research.independent_auditor import IndependentAuditor

    try:
        auditor = IndependentAuditor()
        report = await auditor.get_latest_report()
        return report.to_dict() if report else {"status": "no_audit_yet"}
    except Exception as exc:
        logger.warning(f"get_audit_latest failed: {exc}")
        return {"status": "error", "error": str(exc)[:200]}


# =========================================================================
# Counterfactual Tracker
# =========================================================================
@router.get("/counterfactual/report")
async def get_counterfactual_report(days: int = Query(7, ge=1, le=90)):
    """Get counterfactual analysis report — missed opportunities and correct rejections."""
    from ..modules.research.counterfactual_tracker import CounterfactualTracker

    try:
        tracker = CounterfactualTracker()
        # First evaluate any pending records whose holding window has elapsed
        try:
            await tracker.evaluate_pending()
        except Exception as exc:
            logger.warning(f"Counterfactual evaluate_pending failed: {exc}")
        missed_report = await tracker.generate_missed_opportunities_report(days=days)
        correct_report = await tracker.generate_correct_rejections_report(days=days)
        overall = await tracker.get_overall_stats()
        return {
            "missed_opportunities": missed_report,
            "correct_rejections": correct_report,
            "overall_stats": overall,
        }
    except Exception as exc:
        logger.exception(f"Counterfactual report failed: {exc}")
        raise HTTPException(500, f"Counterfactual report failed: {str(exc)[:200]}")


# =========================================================================
# Monte Carlo
# =========================================================================
@router.post("/monte-carlo/simulate")
async def run_monte_carlo(
    trades: int = Query(100, ge=10, le=1000),
    simulations: int = Query(500, ge=10, le=5000),
):
    """
    Run a Monte Carlo simulation.

    If historical closed trades exist, bootstrap from them. Otherwise
    fall back to parametric simulation using the bot's actual stats.
    """
    from ..modules.research.monte_carlo import MonteCarloSimulator
    from ..modules.paper_trading.engine import PaperTradingEngine

    try:
        sim = MonteCarloSimulator(
            default_capital_usd=settings.paper_capital_usd,
            default_position_pct=settings.paper_default_position_pct,
            fee_pct=settings.paper_fee_pct,
            slippage_pct=settings.paper_slippage_pct,
        )

        # Try to bootstrap from real closed trades first
        trade_dicts: List[Dict[str, Any]] = []
        try:
            async with get_session() as s:
                stmt = (
                    select(Trade)
                    .where(Trade.status == "closed")
                    .order_by(desc(Trade.exit_timestamp))
                    .limit(500)
                )
                db_trades = list((await s.execute(stmt)).scalars().all())
                trade_dicts = [
                    {
                        "pnl_pct": float(t.pnl_pct or 0),
                        "net_pnl_usd": float(t.net_pnl_usd or 0),
                        "entry_size_usd": float(t.entry_size_usd or 0),
                    }
                    for t in db_trades
                ]
        except Exception as exc:
            logger.warning(f"Monte Carlo: failed to load trades ({exc}), using parametric mode")

        if trade_dicts and len(trade_dicts) >= 10:
            result = await sim.simulate_from_trades(
                trades=trade_dicts,
                num_simulations=simulations,
                trades_per_sim=trades,
            )
        else:
            # Parametric fallback — use the bot's current account stats
            engine = PaperTradingEngine()
            stats = await engine.get_stats()
            stats = stats or {}
            # win_rate_pct is 0-100; convert to 0-1
            wr_pct = float(stats.get("win_rate_pct", 0.0) or 0.0)
            win_rate = wr_pct / 100.0
            if win_rate <= 0:
                win_rate = 0.45  # sensible default for a fresh paper account

            # Derive avg win/loss from realized PnL if we have history
            total_net = float(stats.get("total_net_pnl_usd", 0.0) or 0.0)
            total_trades = int(stats.get("total_closed_trades", 0) or 0)
            if total_trades > 0 and total_net != 0:
                avg_per_trade = total_net / total_trades
                # Approximate avg win/loss as a multiple of avg per trade
                avg_win_pct = max(8.0, abs(avg_per_trade) * 0.05)  # rough %
                avg_loss_pct = max(3.0, abs(avg_per_trade) * 0.025)
            else:
                avg_win_pct = 12.0
                avg_loss_pct = 5.0

            result = await sim.simulate_from_params(
                win_rate=win_rate,
                avg_win_pct=avg_win_pct,
                avg_loss_pct=avg_loss_pct,
                num_simulations=simulations,
                trades_per_sim=trades,
                win_std_pct=avg_win_pct * 0.4,
                loss_std_pct=avg_loss_pct * 0.4,
            )

        return result.to_dict()
    except Exception as exc:
        logger.exception(f"Monte Carlo failed: {exc}")
        raise HTTPException(500, f"Monte Carlo failed: {str(exc)[:200]}")


# =========================================================================
# Strategy Lab
# =========================================================================
@router.get("/strategy-lab/experiments")
async def get_experiments(limit: int = Query(50, ge=1, le=500)):
    """Get all strategy experiments + the current champion."""
    from ..modules.research.strategy_lab import StrategyLab

    try:
        lab = StrategyLab()
        champion = await lab.get_current_champion()
        experiments = await lab.list_experiments(limit=limit)
        best = await lab.get_best_hypotheses(top_n=5)
        return {
            "champion": champion.to_dict() if champion else None,
            "experiments": [e.to_dict() for e in experiments],
            "best_hypotheses": [h.to_dict() for h in best],
            "total_experiments": len(experiments),
        }
    except Exception as exc:
        logger.exception(f"Strategy lab experiments failed: {exc}")
        raise HTTPException(500, f"Strategy lab failed: {str(exc)[:200]}")


class StrategyTestRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 15.0
    trailing_stop_pct: float = 3.0
    position_size_pct: float = 5.0
    holding_minutes: int = 120
    win_rate: float = 0.5
    avg_win_pct: float = 12.0
    avg_loss_pct: float = 5.0
    win_std_pct: float = 4.0
    loss_std_pct: float = 2.0
    force_promote: bool = False


@router.post("/strategy-lab/test")
async def test_strategy(req: StrategyTestRequest):
    """Test a new strategy hypothesis and compare against the current champion."""
    from ..modules.research.strategy_lab import StrategyLab, StrategyHypothesis

    try:
        lab = StrategyLab()
        h = StrategyHypothesis(
            hypothesis_id=str(__import__("uuid").uuid4()),
            name=req.name or f"hypothesis_{datetime.utcnow().strftime('%H%M%S')}",
            description=req.description or "Manually-defined strategy hypothesis",
            entry_conditions=[
                {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "5m"},
            ],
            exit_conditions=[
                {"indicator": "rsi", "op": ">", "value": 70, "timeframe": "5m"},
            ],
            stop_loss_pct=req.stop_loss_pct,
            take_profit_pct=req.take_profit_pct,
            trailing_stop_pct=req.trailing_stop_pct,
            position_size_pct=req.position_size_pct,
            holding_minutes=req.holding_minutes,
            timeframe="5m",
            indicators_used=["rsi"],
            created_at=datetime.utcnow().isoformat(),
        )

        # Synthetic backtest based on the user-supplied win/loss distribution
        async def _synthetic_backtest(hyp: StrategyHypothesis) -> Dict[str, Any]:
            import math as _math
            n = max(int(req.holding_minutes), 30)  # number of trades
            wr = max(0.0, min(1.0, req.win_rate))
            avg_w = max(0.1, req.avg_win_pct)
            avg_l = max(0.1, req.avg_loss_pct)
            ev_pct = wr * avg_w - (1 - wr) * avg_l
            pf = (wr * avg_w) / max((1 - wr) * avg_l, 0.001)
            expectancy = (ev_pct / 100.0) * (hyp.position_size_pct / 100.0) * settings.paper_capital_usd
            # Rough Sharpe proxy from EV / std
            std_pct = _math.sqrt(
                wr * (avg_w ** 2) + (1 - wr) * (avg_l ** 2)
                - (wr * avg_w - (1 - wr) * avg_l) ** 2
            )
            sharpe = ev_pct / max(std_pct, 0.001) if std_pct > 0 else 0.0
            return {
                "win_rate": wr,
                "profit_factor": pf,
                "expectancy": expectancy,
                "max_drawdown_pct": max(avg_l * 2.5, 8.0),
                "total_trades": n,
                "oos_wr": max(0.0, wr - 0.05),
                "sharpe": sharpe,
            }

        tested = await lab.test_hypothesis(h, _synthetic_backtest)
        await lab.record_experiment(tested)

        # Compare against current champion (if any)
        champion = await lab.get_current_champion()
        comparison = None
        promoted = False
        if champion is None or req.force_promote:
            if tested.status != "rejected":
                await lab.set_champion(tested)
                promoted = True
        elif tested.status == "tested":
            comparison = await lab.compare_champion_challenger(champion, tested)
            promoted = bool(comparison.promoted)
            if promoted:
                await lab.set_champion(tested)

        return {
            "hypothesis": tested.to_dict(),
            "champion_before": champion.to_dict() if champion else None,
            "comparison": comparison.to_dict() if comparison else None,
            "promoted": promoted,
        }
    except Exception as exc:
        logger.exception(f"test_strategy failed: {exc}")
        raise HTTPException(500, f"Strategy test failed: {str(exc)[:200]}")


# =========================================================================
# Market Regime Memory
# =========================================================================
@router.get("/regime-memory/summary")
async def get_regime_summary():
    """Get market regime memory summary — performance per regime."""
    from ..modules.research.market_regime_memory import (
        MarketRegimeMemory, KNOWN_REGIMES,
    )

    try:
        memory = MarketRegimeMemory()
        summary = await memory.get_regime_summary()
        comparison = await memory.compare_regimes()
        return {
            "regimes": summary,
            "comparison": comparison,
            "known_regimes": list(KNOWN_REGIMES),
        }
    except Exception as exc:
        logger.exception(f"Regime summary failed: {exc}")
        raise HTTPException(500, f"Regime summary failed: {str(exc)[:200]}")


# =========================================================================
# Strategy V2 (improved rule-based engine)
# =========================================================================
@router.get("/strategy-v2/status")
async def strategy_v2_status():
    """Get Strategy V2 configuration and status."""
    try:
        from ..modules.strategy_v2.config import StrategyConfig, VENUES, breakeven_win_rate
        cfg = StrategyConfig()
        be_wr = breakeven_win_rate(
            cfg.tp1_atr * cfg.min_atr_pct,
            cfg.sl_atr * cfg.min_atr_pct,
            cfg.costs.round_trip
        )
        return {
            "enabled": settings.strategy_v2_enabled,
            "strict": settings.strategy_v2_strict,
            "config": cfg.to_dict(),
            "venues": {k: {"round_trip_pct": v.round_trip * 100} for k, v in VENUES.items()},
            "breakeven_win_rate_pct": be_wr * 100,
            "improvements_applied": [
                "Section 1: Reduced costs (0.30% -> 0.19% via BNB + maker)",
                "Section 4: TP2 at 2.5R, swing-low trailing, momentum exit, 0.8R breakeven",
                "Section 5: 3rd volatility state (dead/extreme), ADX rising option",
                "Section 6: Risk scaling, Kelly cap, kill switches",
                "Tuned: SL=2.5xATR, TP1=1.2xATR, time_stop=5 bars"
            ],
        }
    except Exception as exc:
        logger.exception(f"V2 status failed: {exc}")
        raise HTTPException(500, f"V2 status error: {str(exc)[:200]}")


class V2BacktestRequest(BaseModel):
    days: int = 90
    equity: float = 1000.0
    symbols: List[str] = None


@router.post("/strategy-v2/backtest")
async def run_v2_backtest_endpoint(req: V2BacktestRequest):
    """Run V2 backtest on real Pump.fun data."""
    try:
        from ..modules.strategy_v2.config import StrategyConfig
        from ..modules.strategy_v2.backtester import run_backtest
        from ..modules.strategy_v2.data import load_universe, DEFAULT_SYMBOLS

        symbols = req.symbols if req.symbols else DEFAULT_SYMBOLS[:10]  # top 10 for speed
        data = load_universe(symbols, min(req.days, 180))
        if not data:
            raise HTTPException(400, "No data loaded - check internet connection")

        ref = "BTCUSDT" if "BTCUSDT" in data else list(data.keys())[0]
        cfg = StrategyConfig().with_(min_notional_usd=min(req.equity * 0.025, 5.0))

        result = run_backtest(data, cfg, ref, req.equity)
        stats = result["stats"]

        return {
            "initial_equity": req.equity,
            "final_equity": result["portfolio"]["equity"],
            "net_pnl": result["portfolio"]["equity"] - req.equity,
            "roi_pct": stats["return_pct"],
            "total_trades": stats["n"],
            "win_rate_pct": stats["win_rate"] * 100,
            "expectancy_r": stats["expectancy_r"],
            "profit_factor": stats["profit_factor"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
            "exit_reasons": stats.get("exit_reasons", {}),
            "skipped": stats.get("skipped", {}),
            "symbols_tested": len(data),
            "days_tested": req.days,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"V2 backtest failed: {exc}")
        raise HTTPException(500, f"V2 backtest error: {str(exc)[:200]}")


# =========================================================================
# Pump.fun Historical Backtest (real memecoin data)
# =========================================================================
@router.get("/pumpfun/backtest/graduated")
async def pumpfun_backtest_graduated(limit: int = Query(20, ge=1, le=100)):
    """
    Backtest V2 strategy on Pump.fun graduated tokens.
    These are tokens that moved from bonding curve to Raydium LP (proven demand).
    """
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        tokens = await client.get_graduated_tokens(limit=limit)
        return {
            "source": "pump.fun (graduated tokens)",
            "total_tokens": len(tokens),
            "tokens": [t.to_dict() for t in tokens[:10]],  # top 10 for display
            "note": "Graduated tokens are safer - they have proven demand and Raydium LP liquidity.",
        }
    except Exception as exc:
        logger.exception(f"Pump.fun graduated backtest failed: {exc}")
        raise HTTPException(500, f"Pump.fun API error: {str(exc)[:200]}")


@router.get("/pumpfun/backtest/simulate")
async def pumpfun_backtest_simulate(
    mint_address: str,
    initial_sol: float = Query(0.1, ge=0.001, le=1.0),
):
    """
    Simulate a buy-and-hold on a Pump.fun token to see what would have happened.
    Uses bonding curve math to compute the position.

    Note: Pump.fun bonding curve data is real-time only.
    For historical simulation, we'd need to track the curve state over time.
    """
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient, BondingCurveMath
        client = PumpPortalClient()
        token = await client.get_token_info(mint_address)
        if not token:
            raise HTTPException(404, "Token not found on Pump.fun")

        sol_price = await client._fetch_sol_price()
        # Simulate buy at current bonding curve state
        tokens_bought = BondingCurveMath.compute_tokens_for_sol(
            initial_sol, token.real_sol_reserves
        )
        current_value_sol = tokens_bought * token.price_sol
        current_value_usd = current_value_sol * sol_price
        pnl_usd = current_value_usd - (initial_sol * sol_price)
        pnl_pct = (pnl_usd / (initial_sol * sol_price)) * 100 if initial_sol > 0 else 0

        return {
            "mint_address": mint_address,
            "symbol": token.symbol,
            "name": token.name,
            "bonding_curve_progress_pct": token.bonding_curve_progress,
            "is_graduated": token.is_graduated,
            "initial_investment_sol": initial_sol,
            "initial_investment_usd": initial_sol * sol_price,
            "tokens_bought": tokens_bought,
            "current_price_usd": token.price_usd,
            "current_value_usd": current_value_usd,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "sol_price_usd": sol_price,
            "market_cap_usd": token.market_cap_usd,
            "note": "This simulates buy-and-hold from current curve state. Historical simulation requires on-chain data archiving.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Pump.fun simulate failed: {exc}")
        raise HTTPException(500, f"Simulation error: {str(exc)[:200]}")


# =========================================================================
# Pump.fun + FOMO (from previous integration)
# =========================================================================
@router.get("/pumpfun/status")
async def pumpfun_status():
    """PumpPortal + Pump.fun integration status (no secrets exposed)."""
    key = settings.pumpportal_api_key or ""
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("set" if key else "")
    return {
        "pump_program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "pumportal_base_url": "https://pumpportal.fun",
        "wallet_configured": bool(settings.solana_wallet_address),
        "wallet_address": settings.solana_wallet_address,
        "private_key_configured": bool(settings.solana_private_key),
        "api_key_configured": bool(key),
        "api_key_masked": masked,
        "live_trading_enabled": settings.is_live_trading,
        "live_requirements_missing": settings.live_requirements,
        "trading_mode": settings.trading_mode,
        "slippage_bps": settings.pump_slippage_bps,
        "priority_fee_sol": settings.pump_priority_fee_sol,
        "max_trade_sol": settings.pump_max_trade_sol,
        "daily_cap_sol": settings.live_daily_cap_sol,
        "daily_loss_limit_sol": settings.live_daily_loss_limit_sol,
        "max_open_positions": settings.live_max_open_positions,
    }


async def _dexscreener_boosted_tokens(limit: int) -> List[Dict[str, Any]]:
    """Real, diverse Solana tokens from DexScreener boost/profile feeds.

    These feeds return tokens with their TRUE symbols/names (VI, GATO, AMD,
    RICO...), unlike keyword search which returns tokens literally named
    "MEME"/"MEMECOIN".  Each address is then enriched with pair data
    (price, market cap, liquidity, volume, price changes).
    """
    import httpx

    excluded = {"SOL", "USDC", "USDT", "WSOL", "SOLANA", "VSOL", "MSOL", "JITOSOL", "BSOL", "STSOL", "USDS"}
    addrs: List[str] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=10.0) as client:
        feeds = (
            "https://api.dexscreener.com/token-boosts/top/v1",
            "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-profiles/latest/v1",
        )
        for feed in feeds:
            if len(addrs) >= 60:
                break
            try:
                resp = await client.get(feed)
                items = resp.json() or []
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                if (it.get("chainId") or "").lower() != "solana":
                    continue
                a = it.get("tokenAddress") or ""
                if a and a not in seen:
                    seen.add(a)
                    addrs.append(a)

        candidates: Dict[str, Dict[str, Any]] = {}
        # DexScreener tokens endpoint accepts up to 30 comma-separated addresses
        for i in range(0, len(addrs), 30):
            batch = addrs[i:i + 30]
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(batch)
                )
                pairs = (resp.json() or {}).get("pairs") or []
            except Exception:
                continue

            # Choose the deepest-liquidity pair for each base token
            best: Dict[str, tuple] = {}
            for p in pairs:
                if not isinstance(p, dict):
                    continue
                base = p.get("baseToken", {}) or {}
                addr = base.get("address", "")
                if not addr:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                if addr not in best or liq > best[addr][0]:
                    best[addr] = (liq, p, base)

            for addr, (liq, p, base) in best.items():
                symbol = (base.get("symbol") or "").strip()
                name = (base.get("name") or "").strip()
                price = float(p.get("priceUsd", 0) or 0)
                if (
                    not symbol
                    or price <= 0
                    or symbol.upper() in excluded
                    or liq < 100
                ):
                    continue
                volume = p.get("volume") or {}
                change = p.get("priceChange") or {}
                mcap = float(p.get("marketCap", 0) or 0)
                dex_id = p.get("dexId", "") or ""
                on_curve = dex_id == "pumpfun"
                curve = (
                    _estimate_pump_bonding_progress(mcap)
                    if on_curve
                    else 100.0
                )
                entry = {
                    "address": addr,
                    "symbol": symbol,
                    "name": name,
                    "price_usd": price,
                    "price_sol": 0.0,
                    "market_cap_usd": mcap,
                    "liquidity_usd": float(liq),
                    "volume_24h_usd": float(volume.get("h24", 0) or 0),
                    "volume_5m_usd": float(volume.get("m5", 0) or 0),
                    "price_change_5m": float(change.get("m5", 0) or 0),
                    "price_change_1h": float(change.get("h1", 0) or 0),
                    "price_change_24h": float(change.get("h24", 0) or 0),
                    "price_change_24h_pct": float(change.get("h24", 0) or 0),
                    "price_change_5m_pct": float(change.get("m5", 0) or 0),
                    "bonding_curve_progress": curve,
                    "bonding_curve_status": (
                        "estimated_from_market_cap" if on_curve else "graduated_or_dex"
                    ),
                    "is_graduated": not on_curve,
                    "holder_count": 0,
                    "pair_address": p.get("pairAddress", ""),
                    "dex": dex_id,
                    "url": p.get("url", ""),
                    "chain": "solana",
                }

                # Keep ONE entry per symbol — the one with deepest liquidity
                key = symbol.upper()
                prev = candidates.get(key)
                if prev is None or liq > float(prev.get("liquidity_usd", 0) or 0):
                    candidates[key] = entry

    # V401: Sort by HIGHEST 24h price change (most pumping tokens first)
    # was: sorted by liquidity (deepest first) — missed pumping tokens
    tokens = sorted(
        candidates.values(),
        key=lambda t: float(t.get("price_change_24h_pct", 0) or 0),
        reverse=True,
    )[:limit]
    return tokens


async def _dexscreener_search_tokens(limit: int) -> List[Dict[str, Any]]:
    """Fallback: search DexScreener for trending Pump.fun tokens.

    V401: Search for tokens with highest 24h gains (pumping tokens).
    Uses multiple search terms to find diverse pumping tokens.
    """
    import httpx

    # V401: Search terms that find trending/pumping tokens
    search_terms = ["pump", "sol", "meme", "pepe", "dog", "cat", "moon", "bonk"]
    excluded_symbols = {"SOL", "USDC", "USDT", "WSOL", "SOLANA"}
    seen_addresses: set[str] = set()
    tokens: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for term in search_terms:
            if len(tokens) >= limit * 2:  # fetch extra for sorting
                break
            try:
                resp = await client.get(
                    f"https://api.dexscreener.com/latest/dex/search?q={term}"
                )
                data = resp.json() or {}
            except Exception:
                continue

            for p in data.get("pairs", []) or []:
                if len(tokens) >= limit * 2:
                    break
                if not isinstance(p, dict):
                    continue
                base = p.get("baseToken", {}) or {}
                address = base.get("address", "") or ""
                symbol = base.get("symbol", "") or ""
                name = base.get("name", "") or ""
                price = float(p.get("priceUsd", 0) or 0)
                if (
                    not address
                    or address in seen_addresses
                    or not symbol
                    or price <= 0
                    or symbol.upper() in excluded_symbols
                ):
                    continue
                seen_addresses.add(address)

                liquidity = p.get("liquidity", {}) or {}
                volume = p.get("volume", {}) or {}
                change = p.get("priceChange", {}) or {}
                mcap = float(p.get("marketCap", 0) or 0)
                tokens.append({
                    "address": address,
                    "symbol": symbol,
                    "name": name,
                    "price_usd": price,
                    "price_sol": 0.0,
                    "market_cap_usd": mcap,
                    "liquidity_usd": float(liquidity.get("usd", 0) or 0),
                    "volume_24h_usd": float(volume.get("h24", 0) or 0),
                    "volume_5m_usd": float(volume.get("m5", 0) or 0),
                    "price_change_24h_pct": float(change.get("h24", 0) or 0),
                    "price_change_5m_pct": float(change.get("m5", 0) or 0),
                    "bonding_curve_progress": _estimate_pump_bonding_progress(mcap),
                    "bonding_curve_status": "estimated_from_market_cap",
                    "is_graduated": False,
                    "holder_count": 0,
                    "pair_address": p.get("pairAddress", ""),
                    "dex": p.get("dexId", ""),
                    "url": p.get("url", ""),
                    "chain": "solana",
                })

    # V401: Sort search results by highest 24h change (most pumping first)
    tokens = sorted(
        tokens,
        key=lambda t: float(t.get("price_change_24h_pct", 0) or 0),
        reverse=True,
    )[:limit]
    return tokens


async def _geckoterminal_trending_tokens(limit: int) -> List[Dict[str, Any]]:
    """V402: Fetch trending Solana tokens from GeckoTerminal API.

    GeckoTerminal shows pools sorted by volume — finds tokens that
    DexScreener boosted feed misses (tokens that didn't pay for boosts).
    """
    import httpx

    tokens: List[Dict[str, Any]] = []
    seen: set[str] = set()
    excluded = {"SOL", "USDC", "USDT", "WSOL", "SOLANA", "MSOL", "JITOSOL", "BTC", "WBTC", "ETH", "WETH"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Fetch trending pools from GeckoTerminal
        for page in range(1, 4):  # 3 pages = ~60 pools
            if len(tokens) >= limit:
                break
            try:
                resp = await client.get(
                    f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}"
                )
                data = resp.json() or {}
            except Exception:
                continue

            for pool in data.get("data", []):
                if len(tokens) >= limit:
                    break
                attrs = pool.get("attributes", {}) or {}
                name = attrs.get("name", "") or ""
                # Parse "SYMBOL / SOL" format
                parts = name.split("/")
                if not parts:
                    continue
                symbol = parts[0].strip().upper()
                if symbol in excluded or not symbol:
                    continue

                # Get token address from relationships
                rels = pool.get("relationships", {}) or {}
                token_data = rels.get("base_token", {}).get("data", {})
                if not token_data:
                    continue
                token_id = token_data.get("id", "")
                if not token_id or token_id in seen:
                    continue
                seen.add(token_id)
                address = token_id.split("_")[-1] if "_" in token_id else token_id

                # Price change
                change = attrs.get("price_change_percentage", {}) or {}
                chg_24h = float(change.get("h24", 0) or 0)
                chg_5m = float(change.get("m5", 0) or 0)

                # Volume
                vol = attrs.get("volume_usd", {}) or {}
                vol_24h = float(vol.get("h24", 0) or 0)
                vol_5m = float(vol.get("m5", 0) or 0)

                # Liquidity
                liq = attrs.get("reserve_in_usd", "") or "0"
                try:
                    liq_usd = float(liq)
                except (ValueError, TypeError):
                    liq_usd = 0.0

                # Only include tokens with meaningful liquidity
                if liq_usd < 1000:
                    continue

                tokens.append({
                    "address": address,
                    "symbol": symbol,
                    "name": name,
                    "price_usd": 0.001,  # GeckoTerminal doesn't return price directly
                    "price_sol": 0.0,
                    "market_cap_usd": 0,
                    "liquidity_usd": liq_usd,
                    "volume_24h_usd": vol_24h,
                    "volume_5m_usd": vol_5m,
                    "price_change_5m": chg_5m,
                    "price_change_24h": chg_24h,
                    "price_change_24h_pct": chg_24h,
                    "price_change_5m_pct": chg_5m,
                    "bonding_curve_progress": 100.0,
                    "bonding_curve_status": "geckoterminal",
                    "is_graduated": True,
                    "holder_count": 0,
                    "pair_address": pool.get("id", ""),
                    "dex": "geckoterminal",
                    "url": f"https://www.geckoterminal.com/solana/pools/{pool.get('id','')}",
                    "chain": "solana",
                })

    # Sort by 24h change (highest first)
    tokens = sorted(
        tokens,
        key=lambda t: float(t.get("price_change_24h_pct", 0) or 0),
        reverse=True,
    )[:limit]
    return tokens


async def _dexscreener_solana_tokens(limit: int) -> Dict[str, Any]:
    """V402: Primary token feed with MULTIPLE sources.

    Sources (in priority order):
    1. DexScreener boosted tokens (paid promotions, sorted by 24h gain)
    2. GeckoTerminal trending pools (finds tokens DexScreener misses)
    3. DexScreener search (fallback)
    """
    tokens: List[Dict[str, Any]] = []
    source = "dexscreener_boosted"

    # Source 1: DexScreener boosted (sorted by 24h gain)
    try:
        tokens = await _dexscreener_boosted_tokens(limit)
    except Exception as exc:
        logger.warning(f"DexScreener boosted feed failed: {exc}")

    # Source 2: GeckoTerminal (adds tokens DexScreener misses)
    try:
        geo_tokens = await _geckoterminal_trending_tokens(limit)
        have = {t["address"].lower() for t in tokens}
        for gt in geo_tokens:
            if gt["address"].lower() not in have:
                tokens.append(gt)
                have.add(gt["address"].lower())
        if geo_tokens:
            source = "dexscreener+geckoterminal"
    except Exception as exc:
        logger.warning(f"GeckoTerminal feed failed: {exc}")

    # Source 3: DexScreener search (final fallback)
    if len(tokens) < min(limit, 5):
        try:
            extra = await _dexscreener_search_tokens(limit - len(tokens))
            have = {t["address"].lower() for t in tokens}
            tokens.extend([t for t in extra if t["address"].lower() not in have])
            source = "dexscreener+gecko+search"
        except Exception as exc:
            logger.warning(f"DexScreener search fallback failed: {exc}")

    # V402: Final sort by 24h change (highest pumping first)
    tokens = sorted(
        tokens,
        key=lambda t: float(t.get("price_change_24h_pct", 0) or 0),
        reverse=True,
    )[:limit]

    return {"source": source, "total": len(tokens), "tokens": tokens, "fallback": True}


def _estimate_pump_bonding_progress(market_cap_usd: float | None) -> float | None:
    """Estimate Pump.fun bonding-curve progress from market cap.

    Pump.fun graduates at ~85 SOL bonding-curve reserve.  At ~$200/SOL that is
    roughly $17,000 market cap.  This is only a rough estimate; real progress
    comes from PumpPortal on-chain data.
    """
    if market_cap_usd is None or market_cap_usd <= 0:
        return None
    graduation_mcap_usd = 85.0 * 200.0  # ~85 SOL * $200 = ~$17,000
    progress = (market_cap_usd / graduation_mcap_usd) * 100.0
    return round(min(progress, 100.0), 2)


def _enrich_tokens_with_bonding_curve(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add a per-token bonding-curve estimate so each coin shows its own curve."""
    enriched: List[Dict[str, Any]] = []
    for t in tokens:
        t = dict(t)
        mcap = t.get("market_cap_usd")
        if t.get("bonding_curve_progress") in (None, 0.0) and mcap:
            t["bonding_curve_progress"] = _estimate_pump_bonding_progress(float(mcap))
            t["bonding_curve_status"] = "estimated_from_market_cap"
        enriched.append(t)
    return enriched


async def _lookup_token_meta(mint_address: str) -> Dict[str, Any]:
    """Best-effort DexScreener lookup for one mint: symbol, name, dex, price.

    Used by manual buy so the correct trading venue (pump vs pump-amm vs
    raydium) is selected automatically.
    """
    import httpx

    out: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
            )
            pairs = (resp.json() or {}).get("pairs") or []
    except Exception:
        return out
    if not pairs:
        return out
    best = max(
        pairs,
        key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
    )
    base = best.get("baseToken", {}) or {}
    out.update({
        "symbol": (base.get("symbol") or "").strip() or None,
        "name": (base.get("name") or "").strip() or None,
        "dex": best.get("dexId") or None,
        "price_usd": float(best.get("priceUsd", 0) or 0) or None,
        "market_cap_usd": float(best.get("marketCap", 0) or 0) or None,
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd", 0) or 0) or None,
    })
    return out


@router.get("/pumpfun/tokens/new")
async def pumpfun_new_tokens(limit: int = Query(50, ge=1, le=200)):
    """Get newly launched tokens on Pump.fun (DexScreener fallback)."""
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        tokens = await client.get_new_tokens(limit=limit)
        if tokens:
            return {
                "source": "pump.fun",
                "total": len(tokens),
                "tokens": _enrich_tokens_with_bonding_curve([t.to_dict() for t in tokens]),
            }
    except Exception as exc:
        logger.warning(f"Pump.fun new tokens failed, falling back: {exc}")

    try:
        return await _dexscreener_solana_tokens(limit)
    except Exception as exc:
        logger.warning(f"DexScreener fallback failed: {exc}")
        return {"source": "unavailable", "total": 0, "tokens": [], "error": str(exc)[:200]}


@router.get("/market/live-tokens")
async def market_live_tokens(limit: int = Query(20, ge=1, le=100)):
    """Live Solana tokens with their real names/symbols (VI, GATO, AMD...).

    Primary source is the DexScreener boosts/profiles feed (real diverse
    tokens), enriched with price/mcap/liquidity/volume.  Keyword search is
    only a fallback.
    """
    try:
        return await _dexscreener_solana_tokens(limit)
    except Exception as exc:
        logger.exception(f"Live tokens fetch failed: {exc}")
        return {"source": "unavailable", "total": 0, "tokens": [], "error": str(exc)[:200]}


@router.get("/pumpfun/tokens/trending")
async def pumpfun_trending_tokens(limit: int = Query(50, ge=1, le=200)):
    """Get trending Pump.fun tokens (DexScreener fallback)."""
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        tokens = await client.get_trending_tokens(limit=limit)
        if tokens:
            return {
                "source": "pump.fun",
                "total": len(tokens),
                "tokens": _enrich_tokens_with_bonding_curve([t.to_dict() for t in tokens]),
            }
    except Exception as exc:
        logger.warning(f"Pump.fun trending failed, falling back: {exc}")

    try:
        return await _dexscreener_solana_tokens(limit)
    except Exception as exc:
        logger.warning(f"DexScreener fallback failed: {exc}")
        return {"source": "unavailable", "total": 0, "tokens": [], "error": str(exc)[:200]}


@router.get("/pumpfun/tokens/graduated")
async def pumpfun_graduated_tokens(limit: int = Query(50, ge=1, le=200)):
    """Get graduated Pump.fun tokens (moved to Raydium LP)."""
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        tokens = await client.get_graduated_tokens(limit=limit)
        return {"source": "pump.fun", "total": len(tokens), "tokens": [t.to_dict() for t in tokens]}
    except Exception as exc:
        logger.warning(f"Pump.fun graduated failed: {exc}")
        return {"source": "unavailable", "total": 0, "tokens": [], "error": str(exc)[:200]}


@router.get("/pumpfun/token/{mint_address}")
async def pumpfun_token_info(mint_address: str):
    """Get detailed info about a specific Pump.fun token."""
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        token = await client.get_token_info(mint_address)
        if not token:
            raise HTTPException(404, "Token not found on Pump.fun")
        return token.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Pump.fun token info failed: {exc}")
        raise HTTPException(500, f"Pump.fun API error: {str(exc)[:200]}")


@router.get("/pumpfun/wallet/balance")
async def pumpfun_wallet_balance():
    """Get Solana wallet balance (SOL + USD + SPL tokens)."""
    try:
        from ..modules.market_scanner.pumpfun_client import PumpPortalClient
        client = PumpPortalClient()
        balance = await client.get_wallet_balance()
        tokens = await client.get_wallet_token_balances()
        sol = float(balance.get("sol", 0) or 0)
        sol_price = await client._fetch_sol_price()
        return {
            "wallet_configured": bool(settings.solana_wallet_address),
            "wallet_address": settings.solana_wallet_address,
            "sol_balance": sol,
            "sol_price_usd": sol_price,
            "usd_value": round(sol * sol_price, 2),
            "token_accounts": tokens[:20],
            "token_accounts_count": len(tokens),
        }
    except Exception as exc:
        logger.exception(f"Wallet balance failed: {exc}")
        raise HTTPException(500, f"Wallet error: {str(exc)[:200]}")


@router.get("/live/status")
async def live_status():
    """Full live-trading readiness report (wallet, caps, daily usage)."""
    try:
        return await LiveTradingExecutor().status()
    except Exception as exc:
        logger.exception(f"Live status failed: {exc}")
        raise HTTPException(500, f"Live status error: {str(exc)[:200]}")


@router.get("/fomo/status")
async def fomo_status():
    """FOMO integration status."""
    return {
        "fomo_signer_wallet": "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51",
        "bitquery_api_configured": bool(getattr(settings, "bitquery_api_key", "")),
        "supported_chains": ["solana", "base", "bnb", "monad"],
    }


@router.get("/fomo/trending")
async def fomo_trending_tokens(hours: int = Query(1, ge=1, le=24), limit: int = Query(20, ge=1, le=100)):
    """Get top tokens being bought on FOMO."""
    try:
        from ..modules.market_scanner.fomo_client import FomoClient
        client = FomoClient()
        tokens = await client.get_top_tokens_bought(hours=hours, limit=limit)
        return {"source": "fomo.family", "window_hours": hours, "total": len(tokens), "tokens": [t.to_dict() for t in tokens]}
    except Exception as exc:
        logger.exception(f"FOMO trending failed: {exc}")
        raise HTTPException(500, f"FOMO API error: {str(exc)[:200]}")


@router.get("/fomo/top-traders")
async def fomo_top_traders(hours: int = Query(6, ge=1, le=48), limit: int = Query(10, ge=1, le=50)):
    """Get top traders on FOMO."""
    try:
        from ..modules.market_scanner.fomo_client import FomoClient
        client = FomoClient()
        wallets = await client.get_top_traders_to_copy(hours=hours, limit=limit)
        return {"source": "fomo.family", "window_hours": hours, "total": len(wallets), "top_traders": [w.to_dict() for w in wallets]}
    except Exception as exc:
        logger.exception(f"FOMO top traders failed: {exc}")
        raise HTTPException(500, f"FOMO API error: {str(exc)[:200]}")


# =========================================================================
# FOMO Missing Endpoints
# =========================================================================
@router.get("/fomo/latest-trades")
async def fomo_latest_trades(limit: int = Query(20, ge=1, le=100)):
    """Get latest trades made on FOMO."""
    try:
        from ..modules.market_scanner.fomo_client import FomoClient
        client = FomoClient()
        trades = await client.get_latest_trades(limit=limit)
        return {"source": "fomo.family", "total": len(trades), "trades": [t.to_dict() for t in trades]}
    except Exception as exc:
        logger.exception(f"FOMO latest trades failed: {exc}")
        raise HTTPException(500, f"FOMO API error: {str(exc)[:200]}")


@router.get("/fomo/stats")
async def fomo_stats():
    """Get FOMO platform stats."""
    try:
        from ..modules.market_scanner.fomo_client import FomoClient
        client = FomoClient()
        stats = await client.get_stats()
        return stats.to_dict()
    except Exception as exc:
        logger.exception(f"FOMO stats failed: {exc}")
        raise HTTPException(500, f"FOMO API error: {str(exc)[:200]}")


# =========================================================================
# Pump.fun LIVE TRADING (Buy/Sell)
# =========================================================================
class PumpBuyRequest(BaseModel):
    mint_address: str
    amount_sol: float = 0.05


class PumpSellRequest(BaseModel):
    mint_address: str
    amount_tokens: float = 0.0  # ignored: live sells always sell 100%


@router.post("/pumpfun/trade/buy")
async def pumpfun_buy_token(req: PumpBuyRequest):
    """
    Manually buy a Pump.fun token with real SOL via PumpPortal Lightning API.

    Goes through the same safety gates as the automated bot (live mode,
    per-trade cap, daily cap, loss limit, wallet balance) and records the trade
    so it appears in the dashboard.
    """
    if not settings.is_live_trading:
        raise HTTPException(
            400,
            "Live trading not enabled. Missing: " + ", ".join(settings.live_requirements),
        )
    if req.amount_sol <= 0 or req.amount_sol > settings.pump_max_trade_sol:
        raise HTTPException(
            400,
            f"amount_sol must be between 0 and {settings.pump_max_trade_sol} SOL",
        )

    # Resolve or create the Token row so the trade can be recorded.
    # Fetch real metadata (symbol/name/dex/price) so the venue and display are correct.
    meta = await _lookup_token_meta(req.mint_address)
    async with get_session() as s:
        token = (await s.execute(
            select(Token).where(Token.address == req.mint_address)
        )).scalar_one_or_none()
        if token is None:
            token = Token(
                address=req.mint_address,
                chain="solana",
                symbol=(meta.get("symbol") or req.mint_address[:6])[:64],
                name=(meta.get("name") or "Manual buy")[:256],
                dex=meta.get("dex"),
                price_usd=meta.get("price_usd"),
                market_cap_usd=meta.get("market_cap_usd"),
                liquidity_usd=meta.get("liquidity_usd"),
                active=True,
            )
            s.add(token)
            await s.flush()
        else:
            if meta.get("dex"):
                token.dex = meta["dex"]
            if meta.get("price_usd"):
                token.price_usd = meta["price_usd"]
            if meta.get("symbol") and (not token.symbol or token.symbol == req.mint_address[:6]):
                token.symbol = meta["symbol"][:64]
            s.add(token)
            await s.flush()
        token_id = token.id

    result = await LiveTradingExecutor().open_position(
        token_id=token_id,
        amount_sol=req.amount_sol,
        entry_reasons=["manual buy from dashboard"],
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "Buy failed"))
    return result


@router.post("/pumpfun/trade/sell")
async def pumpfun_sell_token(req: PumpSellRequest):
    """
    Manually sell a live Pump.fun position (100%) via PumpPortal.

    If an open live trade exists for this mint it is closed and its PnL is
    recorded; otherwise a raw sell is attempted.
    """
    if not settings.is_live_trading:
        raise HTTPException(
            400,
            "Live trading not enabled. Missing: " + ", ".join(settings.live_requirements),
        )

    async with get_session() as s:
        token = (await s.execute(
            select(Token).where(Token.address == req.mint_address)
        )).scalar_one_or_none()
        trade = None
        if token:
            trade = (await s.execute(
                select(Trade).where(
                    Trade.token_id == token.id,
                    Trade.status == "open",
                    Trade.strategy_id.startswith("live"),
                ).limit(1)
            )).scalar_one_or_none()

    if trade:
        result = await LiveTradingExecutor().close_position(trade.id, "manual")
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Sell failed"))
        return result

    from ..modules.market_scanner.pumpfun_client import PumpPortalClient
    result = await PumpPortalClient().sell_token(req.mint_address, "100%")
    return result.to_dict()
