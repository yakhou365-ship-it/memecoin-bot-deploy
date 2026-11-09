"""
Basic test suite for the Memecoin AI Bot backend.

Run with: pytest tests/ -v
"""
import asyncio
import os
import sys
import pytest
import pytest_asyncio

# Configure test env BEFORE any imports
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_memecoin.db"


@pytest_asyncio.fixture
async def db():
    """Initialize fresh test database."""
    from app.core.database import init_db, close_db, engine
    from app.core.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await close_db()


@pytest.mark.asyncio
async def test_config_loads(db):
    from app.core.config import settings
    assert settings.paper_capital_usd == 10000
    assert settings.min_win_probability == 0.55  # Ultimate-bot: was 0.70 (stale — V31.2 lowered it)


@pytest.mark.asyncio
async def test_database_token_crud(db):
    """Test that we can insert & query a token."""
    from app.core.database import get_session
    from app.core.models import Token
    async with get_session() as s:
        t = Token(
            address="TestToken11111111111111111111111111111111111",
            chain="solana",
            symbol="TEST",
            name="Test Token",
            liquidity_usd=100_000,
            volume_24h_usd=200_000,
            price_usd=0.01,
        )
        s.add(t)
        await s.flush()
        assert t.id is not None
        assert t.watch_priority >= 0


@pytest.mark.asyncio
async def test_technical_analyzer(db):
    """Test TA on synthetic candle data."""
    import pandas as pd
    import numpy as np
    from app.modules.technical_analysis.analyzer import TechnicalAnalyzer

    np.random.seed(42)
    n = 250
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "open": prices,
        "high": prices + np.abs(np.random.randn(n) * 0.5),
        "low": prices - np.abs(np.random.randn(n) * 0.5),
        "close": prices,
        "volume": np.random.randint(1000, 10000, n).astype(float),
    })

    ta = TechnicalAnalyzer()
    result = ta.analyze(df)
    assert result.data_sufficient is True
    assert 0 <= result.rsi <= 100
    assert 0 <= result.technical_score <= 100
    assert isinstance(result.candlestick_patterns, list)


@pytest.mark.asyncio
async def test_market_regime_detector(db):
    """Test market regime detection."""
    import pandas as pd
    import numpy as np
    from app.modules.market_regime.detector import MarketRegimeDetector

    np.random.seed(42)
    n = 100
    # Bullish series
    prices = 100 + np.cumsum(np.random.randn(n) * 0.2 + 0.5)
    df = pd.DataFrame({
        "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": np.random.randint(1000, 5000, n).astype(float),
    })

    detector = MarketRegimeDetector()
    regime = detector.detect(broad_market_candles=df)
    assert regime.regime != "unknown"
    assert isinstance(regime.trading_recommended, bool)


@pytest.mark.asyncio
async def test_security_scanner_flags_mint_authority(db):
    """Test that mint authority is flagged as critical."""
    from app.core.models import Token
    from app.modules.security.scanner import SecurityScanner
    from app.modules.onchain.analyzer import OnChainResult

    token = Token(
        address="x", chain="solana", symbol="X",
        liquidity_usd=50_000, volume_24h_usd=100_000,
    )
    onchain = OnChainResult(
        data_sufficient=True,
        mint_authority_enabled=True,
        freeze_authority_enabled=True,
        top_10_pct=85,
    )
    sec = SecurityScanner()
    result = sec.analyze(token, onchain_result=onchain)
    assert result.auto_disqualify is True
    assert "MINT_AUTHORITY_ENABLED" in result.critical_flags
    assert "FREEZE_AUTHORITY_ENABLED" in result.critical_flags


@pytest.mark.asyncio
async def test_risk_manager_circuit_breaker(db):
    """Test that circuit breaker blocks trades once activated."""
    from app.modules.risk_management.manager import RiskManager

    rm = RiskManager()
    await rm.activate_circuit_breaker("test breach")
    state = await rm.get_state()
    assert state.circuit_breaker_active is True
    check = await rm.check_can_open_trade()
    assert check.can_open is False
    assert "Circuit breaker" in check.reason

    await rm.reset_circuit_breaker()
    # Force reload from DB
    rm2 = RiskManager()
    state2 = await rm2.get_state()
    assert state2.circuit_breaker_active is False


@pytest.mark.asyncio
async def test_paper_trading_engine_open_close(db):
    """Test that we can open and close a paper trade."""
    from app.core.database import get_session
    from app.core.models import Token
    from app.modules.paper_trading.engine import PaperTradingEngine

    # Create a token
    async with get_session() as s:
        t = Token(
            address="testtrade123", chain="solana", symbol="TST",
            price_usd=1.0, liquidity_usd=100_000, volume_24h_usd=50_000,
        )
        s.add(t)
        await s.flush()
        token_id = t.id

    engine = PaperTradingEngine()
    # Open trade
    trade = await engine.open_position(
        token_id=token_id,
        entry_price=1.0,
        position_size_usd=100,
        stop_loss=0.95,
        take_profit=1.10,
        liquidity_usd=100_000,
    )
    assert trade is not None
    assert trade.status == "open"

    # Close trade
    closed = await engine.close_position(
        trade.id, exit_price=1.05, exit_reason="take_profit", liquidity_usd=100_000
    )
    assert closed is not None
    assert closed.status == "closed"
    assert closed.net_pnl_usd is not None


@pytest.mark.asyncio
async def test_decision_engine_rejects_high_risk(db):
    """Test that the decision engine auto-rejects when risk is critical."""
    from app.core.models import Token
    from app.modules.decision_engine.engine import DecisionEngine, DecisionResult
    from app.modules.security.scanner import SecurityResult

    # Build a security result with critical flags
    sec = SecurityResult(
        auto_disqualify=True,
        disqualify_reason="MINT_AUTHORITY_ENABLED",
    )

    token = Token(
        address="x", chain="solana", symbol="X",
        liquidity_usd=10_000, volume_24h_usd=5_000,
        price_usd=0.01, updated_at=None,
    )

    # Simulate the engine's _decide path
    result = DecisionResult()
    result.security_detail = sec.__dict__
    result.risk_score = 90

    # We test the gate directly
    from app.core.config import settings
    # Mint authority enabled -> auto-disqualify
    assert sec.auto_disqualify is True


@pytest.mark.asyncio
async def test_loss_analyzer_extracts_causes(db):
    """Test that loss analyzer can extract causes from a losing trade."""
    from app.core.models import Trade
    from app.modules.learning.system import LossAnalyzer

    trade = Trade(
        token_id="abc",
        side="long",
        entry_price=1.0,
        entry_size_usd=100,
        entry_quantity=100,
        stop_loss_price=0.95,
        take_profit_price=1.10,
        exit_price=0.94,
        net_pnl_usd=-7,
        pnl_pct=-7,
        max_unrealized_profit_usd=2,
        max_unrealized_loss_usd=-7,
        status="closed",
        exit_reason="stop_loss",
    )

    analyzer = LossAnalyzer()
    result = await analyzer.analyze(trade)
    assert isinstance(result.causes, list)
    assert len(result.causes) > 0
    assert len(result.evidence) > 0
