"""
Database models - the data backbone of the entire system.
All tables are designed for PostgreSQL compatibility but work with SQLite for dev.

Design principles:
- Every table has created_at / updated_at timestamps
- All monetary values stored as float (acceptable precision for paper trading)
- Soft-delete via 'active' flag instead of hard deletes (preserves history)
- Hypertable-eligible tables (ohlcv, trades, scores) marked for TimescaleDB upgrade
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, Index, BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base."""


# ============================================================================
# 1. TOKENS - Discovered memecoins
# ============================================================================
class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    address: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    chain: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=True)
    pair_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    dex: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Market data (latest snapshot)
    price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_5m_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    txns_24h_buys: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    txns_24h_sells: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price_change_24h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_change_5m_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fdv_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Scanner state
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    discovery_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    watch_priority: Mapped[int] = mapped_column(Integer, default=50)  # 0-100

    # Status
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False)
    blacklist_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_dead: Mapped[bool] = mapped_column(Boolean, default=False)  # rug / abandoned

    # Latest scores (denormalized for fast filtering)
    latest_opportunity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    latest_risk_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latest_win_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_tokens_chain_address", "chain", "address", unique=True),
        Index("ix_tokens_updated", "updated_at"),
        Index("ix_tokens_opportunity", "latest_opportunity_score"),
    )


# ============================================================================
# 2. OHLCV - Time-series price data
# ============================================================================
class OHLCV(Base):
    """Hypertable candidate (TimescaleDB)."""
    __tablename__ = "ohlcv"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_id: Mapped[str] = mapped_column(String(36), ForeignKey("tokens.id"), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), index=True)  # 1m, 5m, 15m, 1h, 4h, 1d
    timestamp: Mapped[int] = mapped_column(BigInteger, index=True)  # unix seconds
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ohlcv_token_tf_ts", "token_id", "timeframe", "timestamp", unique=True),
    )


# ============================================================================
# 3. ANALYSIS SNAPSHOTS - Full per-token analysis result
# ============================================================================
class AnalysisSnapshot(Base):
    """A frozen analysis result for a token at a point in time."""
    __tablename__ = "analysis_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_id: Mapped[str] = mapped_column(String(36), ForeignKey("tokens.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    # Individual scores (0-100 unless noted)
    technical_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    onchain_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    security_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_conditions_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volatility_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0-100, higher = worse
    opportunity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Win probability - the calibrated probability that a paper trade would be net-profitable
    win_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    win_probability_lower_ci: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # lower bound of CI
    win_probability_upper_ci: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Market regime at time of analysis
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Detailed indicators (JSON blob for full reproducibility)
    indicators_json: Mapped[dict] = mapped_column(JSON, default=dict)
    flags_json: Mapped[dict] = mapped_column(JSON, default=dict)  # red flags, critical issues
    reasons_json: Mapped[list] = mapped_column(JSON, default=list)  # human-readable reasons (Arabic)

    # AI reasoning summary
    reasoning_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Data freshness check
    data_age_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_sufficient: Mapped[bool] = mapped_column(Boolean, default=False)

    # Decision
    qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# 4. TRADES - Paper trading positions
# ============================================================================
class Trade(Base):
    """A paper-trade position. One row per round-trip trade."""
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_id: Mapped[str] = mapped_column(String(36), ForeignKey("tokens.id"), index=True)
    analysis_snapshot_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Entry
    side: Mapped[str] = mapped_column(String(8), default="long")  # long / short (only long for memecoins)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_size_usd: Mapped[float] = mapped_column(Float)
    entry_quantity: Mapped[float] = mapped_column(Float)
    entry_fee_usd: Mapped[float] = mapped_column(Float, default=0)
    entry_slippage_usd: Mapped[float] = mapped_column(Float, default=0)
    entry_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Plan
    stop_loss_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    planned_holding_period_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strategy_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Exit (nullable while open)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_fee_usd: Mapped[float] = mapped_column(Float, default=0)
    exit_slippage_usd: Mapped[float] = mapped_column(Float, default=0)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Possible exit reasons: take_profit | stop_loss | trailing_stop | time_exit | manual | circuit_breaker

    # PnL
    gross_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees_total_usd: Mapped[float] = mapped_column(Float, default=0)
    slippage_total_usd: Mapped[float] = mapped_column(Float, default=0)

    # Extrema during the trade
    max_unrealized_profit_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_unrealized_loss_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # open | closed | cancelled | force_closed

    # Strategy conformance (filled in at close)
    strategy_compliant: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Reasoning trail
    entry_reasons_json: Mapped[list] = mapped_column(JSON, default=list)
    exit_reasons_json: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_trades_status_token", "status", "token_id"),
        Index("ix_trades_entry_ts", "entry_timestamp"),
    )


# ============================================================================
# 5. MISTAKES - Learning database
# ============================================================================
class Mistake(Base):
    """A recorded mistake pattern with conditions for repetition-prevention."""
    __tablename__ = "mistakes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trade_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    mistake_type: Mapped[str] = mapped_column(String(64), index=True)
    # Examples: late_entry | fake_breakout | weak_liquidity | stale_data | bad_stop_loss |
    #           overconfidence | suspicious_activity | model_overestimate | bad_market_regime

    description: Mapped[str] = mapped_column(Text)

    # Conditions under which this mistake tends to happen (for matching)
    conditions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # e.g. {"market_regime": "high_volatility", "entry_after_surge_pct": 30, "liquidity_below": 100000}

    evidence_json: Mapped[list] = mapped_column(JSON, default=list)  # list of supporting facts
    suggested_fix_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Test status for the suggested fix
    fix_tested: Mapped[bool] = mapped_column(Boolean, default=False)
    fix_test_result: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # improved | neutral | worse
    fix_adopted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Repetition-prevention
    active_block: Mapped[bool] = mapped_column(Boolean, default=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# 6. LEARNINGS - Lessons extracted (successes AND failures)
# ============================================================================
class Learning(Base):
    """Discovered patterns that improve future decisions."""
    __tablename__ = "learnings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    learning_type: Mapped[str] = mapped_column(String(32))  # success_factor | failure_factor | pattern | refinement
    pattern_name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text)

    conditions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_outcome_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Validation
    supporting_trades_count: Mapped[int] = mapped_column(Integer, default=0)
    contradicting_trades_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0-1

    # Status
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    adopted: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================================
# 7. STRATEGY VERSIONS - Immutable strategy snapshots
# ============================================================================
class StrategyVersion(Base):
    """Versioned strategy configuration. Never mutated, only superseded."""
    __tablename__ = "strategy_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, index=True)
    parent_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)

    # Configuration (entry/exit rules, indicator weights, thresholds)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Backtest results (validated against historical data)
    backtest_win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    backtest_profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    backtest_expectancy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    backtest_max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    backtest_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    backtest_out_of_sample_win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), default="draft")
    # draft | testing | validated | adopted | retired

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# 8. ML MODEL VERSIONS - Trained model registry
# ============================================================================
class ModelVersion(Base):
    """Registry of trained ML models with calibration metrics."""
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_type: Mapped[str] = mapped_column(String(32), index=True)
    # xgboost | lightgbm | logistic | random_forest | ensemble
    purpose: Mapped[str] = mapped_column(String(32))
    # win_probability | market_regime | anomaly_detection | volatility_forecast

    version: Mapped[int] = mapped_column(Integer, index=True)
    parent_model_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # File path
    model_path: Mapped[str] = mapped_column(String(256))
    features_json: Mapped[list] = mapped_column(JSON, default=list)

    # Training data
    train_samples: Mapped[int] = mapped_column(Integer, default=0)
    validation_samples: Mapped[int] = mapped_column(Integer, default=0)
    test_samples: Mapped[int] = mapped_column(Integer, default=0)
    train_period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    train_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Metrics (computed on holdout)
    precision: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    f1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roc_auc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    brier_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calibration_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), default="trained")
    # trained | validated | adopted | retired | failed

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# 9. ALERTS - Telegram & dashboard notifications log
# ============================================================================
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    alert_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | warning | critical
    title: Mapped[str] = mapped_column(String(256))
    message: Mapped[str] = mapped_column(Text)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Delivery
    sent_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_dashboard: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# ============================================================================
# 10. SYSTEM STATE - Internal counters & circuit breakers
# ============================================================================
class SystemState(Base):
    """Singleton-ish table holding runtime state (one row per key)."""
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Add a simple TradingState model for Start/Stop
class TradingState(Base):
    """Tracks whether the bot is actively trading or stopped."""
    __tablename__ = "trading_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================================
# 11. SCAN RUNS - Audit log of scanner invocations
# ============================================================================
class ScanRun(Base):
    """Each scanner cycle creates one row for auditing."""
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    chain: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    tokens_discovered: Mapped[int] = mapped_column(Integer, default=0)
    tokens_updated: Mapped[int] = mapped_column(Integer, default=0)
    tokens_blacklisted: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | failed


# ============================================================================
# 12. DATA SOURCES - API call audit
# ============================================================================
class DataSourceCall(Base):
    """Tracks every external API call: latency, status, rate-limit remaining."""
    __tablename__ = "data_source_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)  # dexscreener | geckoterminal | solana_rpc
    endpoint: Mapped[str] = mapped_column(String(256))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rate_limit_remaining: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
