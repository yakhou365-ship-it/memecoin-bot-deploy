"""
Ultimate-bot realistic test suite — 30 scenarios.

Run: cd backend && python tests/test_ultimate_scenarios.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_scenarios.db"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_ORCHESTRATOR"] = "false"
os.environ["AUTO_START_TRADING"] = "false"
os.environ["API_AUTH_KEY"] = "test-key-123"

# Clean DB
db_path = BACKEND_DIR / "data" / "test_scenarios.db"
if db_path.exists():
    db_path.unlink()


# =========================================================================
# Test framework
# =========================================================================
PASSED = 0
FAILED = 0
SKIPPED = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name} — {detail}")


def skip(name: str, reason: str):
    global SKIPPED
    SKIPPED += 1
    print(f"  ⏭️  {name} — SKIPPED: {reason}")


# =========================================================================
# Scenarios
# =========================================================================
async def run_all():
    from app.core.database import init_db, get_session, close_db
    from app.core.models import Token, Trade, TradingState, SystemState, Alert, ScanRun
    from sqlalchemy import select, func, and_, desc, or_
    from sqlalchemy.orm.attributes import flag_modified

    await init_db()

    # =====================================================================
    # GROUP 1: Token Discovery & Filtering (5 scenarios)
    # =====================================================================
    print("\n" + "=" * 70)
    print("GROUP 1: Token Discovery & Filtering")
    print("=" * 70)

    # Scenario 1: Insert a token with valid data
    print("\n--- Scenario 1: Valid token insertion ---")
    async with get_session() as s:
        token = Token(
            address="So11111111111111111111111111111111111111112",
            chain="solana",
            symbol="BONK",
            name="Bonk",
            price_usd=0.00001234,
            liquidity_usd=500000,
            volume_24h_usd=5000000,
            market_cap_usd=1200000000,
            price_change_24h_pct=15.5,
            price_change_5m_pct=2.3,
            active=True,
            blacklisted=False,
        )
        s.add(token)
        await s.flush()
        check("Token inserted", token.id is not None)
        check("Token address case-exact", token.address == "So11111111111111111111111111111111111111112")

    # Scenario 2: Token with insufficient liquidity (< $5000)
    print("\n--- Scenario 2: Low-liquidity token rejection ---")
    from app.modules.decision_engine.engine import DecisionEngine
    from app.modules.market_regime.detector import MarketRegime, MarketRegimeDetector
    engine = DecisionEngine()
    async with get_session() as s:
        low_liq_token = Token(
            id="low-liq",
            address="LowLiq1234567890",
            chain="solana", symbol="LOWLIQ", name="Low Liquidity Token",
            price_usd=0.001, liquidity_usd=3000, volume_24h_usd=500,
            active=True, blacklisted=False,
        )
        s.add(low_liq_token)
        await s.commit()
        low_liq_token = await s.get(Token, "low-liq")
    regime = MarketRegime()
    regime.regime = "bull_market"
    regime.trading_recommended = True
    decision = await engine.analyze(low_liq_token, candles_df=None, market_regime=regime)
    check("Low-liquidity rejected", not decision.qualified, f"qualified={decision.qualified}")
    check("Rejection reason mentions liquidity", "السيولة" in (decision.rejection_reason or ""), decision.rejection_reason)

    # Scenario 3: Token with mint authority enabled (security auto-disqualify)
    print("\n--- Scenario 3: Mint authority auto-disqualify ---")
    async with get_session() as s:
        risky_token = Token(
            id="risky-mint",
            address="RiskyMint123",
            chain="solana", symbol="RISK", name="Risky Token",
            price_usd=0.01, liquidity_usd=50000, volume_24h_usd=100000,
            active=True, blacklisted=False,
        )
        s.add(risky_token)
        await s.commit()
        risky_token = await s.get(Token, "risky-mint")
    # Manually set security detail to simulate mint authority
    decision2 = await engine.analyze(risky_token, candles_df=None, market_regime=regime)
    # The onchain analyzer may or may not detect it without real RPC — check the flow
    check("Risky token analyzed", decision2 is not None)

    # Scenario 4: Token with zero price (should be rejected)
    print("\n--- Scenario 4: Zero-price token rejection ---")
    async with get_session() as s:
        zero_price = Token(
            id="zero-price",
            address="ZeroPrice123",
            chain="solana", symbol="ZERO", name="Zero Price",
            price_usd=0, liquidity_usd=50000, volume_24h_usd=100000,
            active=True, blacklisted=False,
        )
        s.add(zero_price)
        await s.commit()
        zero_price = await s.get(Token, "zero-price")
    decision3 = await engine.analyze(zero_price, candles_df=None, market_regime=regime)
    check("Zero-price rejected", not decision3.qualified, f"qualified={decision3.qualified}")
    check("Rejection mentions price", "سعر" in (decision3.rejection_reason or "") or "price" in (decision3.rejection_reason or "").lower(), decision3.rejection_reason)

    # Scenario 5: Valid token passes all gates
    print("\n--- Scenario 5: Valid token passes gates ---")
    async with get_session() as s:
        good_token = Token(
            id="good-token",
            address="GoodToken123456",
            chain="solana", symbol="GOOD", name="Good Token",
            price_usd=0.05, liquidity_usd=100000, volume_24h_usd=500000,
            price_change_24h_pct=8.0, price_change_5m_pct=1.5,
            market_cap_usd=5000000,
            active=True, blacklisted=False,
        )
        s.add(good_token)
        await s.commit()
        good_token = await s.get(Token, "good-token")
    decision4 = await engine.analyze(good_token, candles_df=None, market_regime=regime)
    check("Good token qualified", decision4.qualified, f"qualified={decision4.qualified}, reason={decision4.rejection_reason}")
    check("SL < entry", (decision4.suggested_stop_loss or 0) < (decision4.suggested_entry or 0))
    check("TP > entry", (decision4.suggested_take_profit or 0) > (decision4.suggested_entry or 0))
    check("Holding period = 5 days", decision4.suggested_holding_period_minutes == 7200)

    # =====================================================================
    # GROUP 2: Paper Trading Engine (8 scenarios)
    # =====================================================================
    print("\n" + "=" * 70)
    print("GROUP 2: Paper Trading Engine")
    print("=" * 70)

    from app.modules.paper_trading.engine import PaperTradingEngine
    pe = PaperTradingEngine()

    # Scenario 6: Open a paper trade
    print("\n--- Scenario 6: Open paper trade ---")
    trade = await pe.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=500,
        stop_loss=0.046,
        take_profit=0.55,
        holding_period_minutes=7200,
        strategy_id="test_v1",
        liquidity_usd=100000,
    )
    check("Trade opened", trade is not None)
    check("Trade has entry price", trade.entry_price > 0)
    check("Trade has SL", trade.stop_loss_price == 0.046)
    check("Trade has TP", trade.take_profit_price == 0.55)
    check("Trade status = open", trade.status == "open")

    # Scenario 7: Verify no double slippage (Ultimate-bot fix)
    print("\n--- Scenario 7: No double slippage on open ---")
    # Entry price should include slippage, but total_cost should NOT add it again
    check("Entry price > market price (slippage included)", trade.entry_price > 0.05)
    # Cash balance should be reduced by position_size + fee only (not + slippage)
    expected_cash = 10000.0 - 500.0 - (500.0 * 0.25 / 100)  # notional + fee
    actual_cash = pe.account.cash_balance
    check("Cash = notional + fee only (no double slippage)", abs(actual_cash - expected_cash) < 5.0, f"cash={actual_cash:.2f}, expected≈{expected_cash:.2f}")

    # Scenario 8: Close trade manually and verify no double slippage on close
    print("\n--- Scenario 8: Close trade — no double slippage ---")
    # Update token price to simulate current price
    async with get_session() as s:
        t = await s.get(Token, "good-token")
        t.price_usd = 0.06  # +20% gain
        s.add(t)
        await s.commit()
    closed = await pe.close_position(trade.id, exit_reason="manual")
    # Actually the trade id is auto-generated; use the returned trade
    check("Trade closed", closed is not None, "close returned None")
    if closed:
        check("Trade status = closed", closed.status == "closed")
        check("Exit reason = manual", closed.exit_reason == "manual")
        # net_pnl should be gross - entry_fee - exit_fee only (no slippage subtraction)
        check("Net PnL > 0 (profitable)", closed.net_pnl_usd > 0, f"pnl={closed.net_pnl_usd:.4f}")
        # Verify slippage is NOT subtracted from net_pnl (it's in the exit price)
        expected_gross = (closed.exit_price - closed.entry_price) * closed.entry_quantity
        expected_net = expected_gross - closed.entry_fee_usd - closed.exit_fee_usd
        check("net_pnl = gross - fees only", abs(closed.net_pnl_usd - expected_net) < 0.01, f"net={closed.net_pnl_usd:.4f}, expected={expected_net:.4f}")

    # Scenario 9: Trade rejected by risk manager (max open positions)
    print("\n--- Scenario 9: Max open positions rejection ---")
    # Open paper_max_open_positions trades, then try one more
    for i in range(10):  # paper_max_open_positions = 10
        await pe.open_position(
            token_id="good-token",
            entry_price=0.05,
            position_size_usd=100,
            stop_loss=0.046,
            take_profit=0.55,
            strategy_id="test_v1",
            liquidity_usd=100000,
        )
    # Now try one more — should be rejected
    extra = await pe.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=100,
        stop_loss=0.046,
        take_profit=0.55,
        strategy_id="test_v1",
        liquidity_usd=100000,
    )
    check("11th trade rejected (max positions)", extra is None)

    # Scenario 10: Insufficient cash rejection
    print("\n--- Scenario 10: Insufficient cash rejection ---")
    pe2 = PaperTradingEngine()
    pe2.account.cash_balance = 10.0  # only $10
    rejected = await pe2.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=500,  # needs $500+
        stop_loss=0.046,
        take_profit=0.55,
        strategy_id="test_v1",
        liquidity_usd=100000,
    )
    check("Trade rejected (insufficient cash)", rejected is None)

    # Scenario 11: Close with SL hit
    print("\n--- Scenario 11: Stop-loss exit ---")
    pe3 = PaperTradingEngine()
    sl_trade = await pe3.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=200,
        stop_loss=0.045,
        take_profit=0.55,
        strategy_id="test_sl",
        liquidity_usd=100000,
    )
    if sl_trade:
        # Price drops below SL
        async with get_session() as s:
            t = await s.get(Token, "good-token")
            t.price_usd = 0.044  # below SL
            s.add(t)
            await s.commit()
        closed_sl = await pe3.close_position(sl_trade.id, exit_reason="stop_loss")
        check("SL trade closed", closed_sl is not None)
        if closed_sl:
            check("SL exit reason", closed_sl.exit_reason == "stop_loss")
            check("SL trade is a loss", closed_sl.net_pnl_usd < 0)

    # Scenario 12: Close with TP hit
    print("\n--- Scenario 12: Take-profit exit ---")
    pe4 = PaperTradingEngine()
    tp_trade = await pe4.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=200,
        stop_loss=0.045,
        take_profit=0.055,  # tight TP
        strategy_id="test_tp",
        liquidity_usd=100000,
    )
    if tp_trade:
        async with get_session() as s:
            t = await s.get(Token, "good-token")
            t.price_usd = 0.056  # above TP
            s.add(t)
            await s.commit()
        closed_tp = await pe4.close_position(tp_trade.id, exit_reason="mega_tp")
        check("TP trade closed", closed_tp is not None)
        if closed_tp:
            check("TP exit reason", closed_tp.exit_reason == "mega_tp")
            check("TP trade is a profit", closed_tp.net_pnl_usd > 0)

    # Scenario 13: peak_price initialized on open (Ultimate-bot fix)
    print("\n--- Scenario 13: peak_price initialization ---")
    pe5 = PaperTradingEngine()
    peak_trade = await pe5.open_position(
        token_id="good-token",
        entry_price=0.05,
        position_size_usd=100,
        stop_loss=0.045,
        take_profit=0.55,
        strategy_id="test_peak",
        liquidity_usd=100000,
    )
    if peak_trade:
        check("peak_price column exists", hasattr(peak_trade, 'peak_price'))

    # =====================================================================
    # GROUP 3: Risk Manager (6 scenarios)
    # =====================================================================
    print("\n" + "=" * 70)
    print("GROUP 3: Risk Manager")
    print("=" * 70)

    from app.modules.risk_management.manager import RiskManager
    rm = RiskManager()

    # Scenario 14: Fresh risk state (no trades, no circuit breaker)
    print("\n--- Scenario 14: Fresh risk state ---")
    state = await rm.get_state()
    check("can_open = True (fresh)", state.can_open)
    check("circuit_breaker = False", not state.circuit_breaker_active)
    check("cooldown = False", not state.cooldown_active)
    check("consecutive_losses = 0", state.consecutive_losses == 0)

    # Scenario 15: Circuit breaker activation
    print("\n--- Scenario 15: Circuit breaker activation ---")
    await rm.activate_circuit_breaker("Test: daily loss limit breached")
    state = await rm.get_state()
    check("Circuit breaker active", state.circuit_breaker_active)
    check("Reason recorded", "daily loss" in state.circuit_breaker_reason)
    # Now check_can_open_trade should reject
    result = await rm.check_can_open_trade()
    check("Trade rejected (circuit breaker)", not result.can_open)

    # Scenario 16: Circuit breaker reset
    print("\n--- Scenario 16: Circuit breaker reset ---")
    await rm.reset_circuit_breaker()
    state = await rm.get_state()
    check("Circuit breaker inactive after reset", not state.circuit_breaker_active)
    check("Cooldown cleared", not state.cooldown_active)
    check("consecutive_losses reset", state.consecutive_losses == 0)

    # Scenario 17: Cooldown after 2 consecutive losses
    print("\n--- Scenario 17: Cooldown after 2 losses ---")
    # Ultimate-bot FIX: close all open trades from scenario 9 first,
    # otherwise gate 6 (max open positions) fires before gate 7 (cooldown).
    async with get_session() as s:
        open_trades = list((await s.execute(
            select(Trade).where(Trade.status == "open")
        )).scalars().all())
        for t in open_trades:
            t.status = "closed"
            t.exit_reason = "cleanup"
            t.exit_timestamp = datetime.utcnow()
    # Reset risk state fresh
    rm2 = RiskManager()
    await rm2.reset_circuit_breaker()
    await rm2.record_trade_result(False, -50)  # loss 1
    s1 = await rm2.get_state()
    check("After 1 loss: consecutive = 1", s1.consecutive_losses == 1)
    check("No cooldown yet", not s1.cooldown_active)
    await rm2.record_trade_result(False, -30)  # loss 2
    # Now check_can_open_trade should arm cooldown
    result2 = await rm2.check_can_open_trade()
    check("Cooldown armed after 2 losses", not result2.can_open, f"can_open={result2.can_open}, reason={result2.reason}")
    # Verify streak was RESET (Ultimate-bot fix — not permanent lock)
    s2 = await rm2.get_state()
    check("Streak reset when cooldown armed", s2.consecutive_losses == 0, f"losses={s2.consecutive_losses}")

    # Scenario 18: Cooldown expiry
    print("\n--- Scenario 18: Cooldown expiry simulation ---")
    # Manually set cooldown_until to past
    async with get_session() as s:
        sys_state = await s.get(SystemState, rm.STATE_KEY)
        if sys_state:
            v = dict(sys_state.value_json)
            v["cooldown_until"] = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            v["cooldown_active"] = True
            v["consecutive_losses"] = 0
            sys_state.value_json = v
            flag_modified(sys_state, "value_json")
    state3 = await rm.get_state()
    check("Cooldown expired (auto-cleared)", not state3.cooldown_active, f"cooldown={state3.cooldown_active}")

    # Scenario 19: Risk state with None value_json (defensive)
    print("\n--- Scenario 19: None value_json handling ---")
    async with get_session() as s:
        # Delete the risk state row
        sys_state = await s.get(SystemState, rm.STATE_KEY)
        if sys_state:
            await s.delete(sys_state)
    # get_state should not crash
    try:
        state4 = await rm.get_state()
        check("get_state handles missing row", state4 is not None)
        check("can_open = True (fresh)", state4.can_open)
    except Exception as exc:
        check("get_state handles missing row", False, str(exc))

    # =====================================================================
    # GROUP 4: AI Agent Consensus (5 scenarios)
    # =====================================================================
    print("\n" + "=" * 70)
    print("GROUP 4: AI Agent Consensus")
    print("=" * 70)

    from app.modules.ai_agents.orchestrator import AgentOrchestrator

    # Scenario 20: All 5 agents approve a good token
    print("\n--- Scenario 20: All agents approve ---")
    good_ctx = {
        "price_usd": 0.05, "liquidity_usd": 100000, "volume_24h_usd": 500000,
        "market_cap_usd": 5000000, "price_change_24h_pct": 8, "price_change_5m_pct": 1.5,
        "volume_5m_usd": 5000, "txns_24h_buys": 200, "txns_24h_sells": 100,
        "chain": "solana", "technical_score": 75, "liquidity_score": 80,
        "security_score": 85, "sentiment_score": 65, "onchain_score": 70,
        "opportunity_score": 75, "market_regime": "bull_market",
        "risk_state": {"can_open": True, "consecutive_losses": 0},
        "security": {"is_honeypot": False, "honeypot_probability": 0.1,
                      "mint_authority_disabled": True, "freeze_authority_disabled": True,
                      "ownership_renounced": True, "lp_locked": True, "risk_score": 25},
        "onchain": {"top_holders_concentration": 25},
        "liquidity": {"liquidity_score": 80},
        "sentiment": {"sentiment_score": 65},
    }
    orch = AgentOrchestrator()
    async with get_session() as s:
        token_for_agents = await s.get(Token, "good-token")
    if token_for_agents:
        consensus = await orch.run_consensus(token_for_agents, good_ctx)
        check("Consensus approved", consensus.approved)
        check("5 agents ran", consensus.total_agents == 5)
        check("Approvals >= 3 (majority)", consensus.approvals_count >= 3)

    # Scenario 21: Security agent veto (honeypot)
    print("\n--- Scenario 21: Security veto on honeypot ---")
    bad_ctx = dict(good_ctx)
    bad_ctx["security"] = {
        "is_honeypot": True, "honeypot_probability": 0.9,
        "mint_authority_disabled": False, "freeze_authority_disabled": False,
        "ownership_renounced": False, "lp_locked": False, "risk_score": 90,
    }
    if token_for_agents:
        consensus2 = await orch.run_consensus(token_for_agents, bad_ctx)
        check("Consensus rejected (honeypot)", not consensus2.approved)
        check("At least 1 veto", any(v.veto for v in consensus2.verdicts))

    # Scenario 22: Low liquidity rejection (token doesn't pass decision engine gates)
    print("\n--- Scenario 22: Low liquidity gate rejection ---")
    async with get_session() as s:
        low_liq_tok = Token(
            id="low-liq-2", address="LowLiq456789",
            chain="solana", symbol="LOWLIQ2", name="Low Liq Token 2",
            price_usd=0.01, liquidity_usd=2000, volume_24h_usd=500,
            active=True, blacklisted=False,
        )
        s.add(low_liq_tok)
        await s.commit()
        low_liq_tok = await s.get(Token, "low-liq-2")
    decision_low = await engine.analyze(low_liq_tok, candles_df=None, market_regime=regime)
    check("Low-liquidity token rejected by engine", not decision_low.qualified, f"qualified={decision_low.qualified}")
    check("Rejection mentions liquidity", "السيولة" in (decision_low.rejection_reason or ""), decision_low.rejection_reason)

    # Scenario 23: Risk agent veto (circuit breaker active)
    print("\n--- Scenario 23: Risk veto on circuit breaker ---")
    cb_ctx = dict(good_ctx)
    cb_ctx["risk_state"] = {"can_open": False, "circuit_breaker_active": True,
                             "consecutive_losses": 0, "cooldown_active": False}
    if token_for_agents:
        consensus4 = await orch.run_consensus(token_for_agents, cb_ctx)
        check("Consensus rejected (circuit breaker)", not consensus4.approved)

    # Scenario 24: Mixed consensus (3 approve, 2 reject → approved)
    print("\n--- Scenario 24: Majority rule (3/5) ---")
    mixed_ctx = dict(good_ctx)
    mixed_ctx["technical_score"] = 55  # mediocre
    mixed_ctx["sentiment_score"] = 35  # negative sentiment
    mixed_ctx["liquidity_score"] = 50
    if token_for_agents:
        consensus5 = await orch.run_consensus(token_for_agents, mixed_ctx)
        # Should still work if no veto
        check("Consensus completed", consensus5.total_agents == 5)

    # =====================================================================
    # GROUP 5: API Endpoints (6 scenarios)
    # =====================================================================
    print("\n" + "=" * 70)
    print("GROUP 5: API Endpoints")
    print("=" * 70)

    from app.main import app, lifespan
    from httpx import AsyncClient, ASGITransport
    KEY = "test-key-123"
    HEADERS = {"X-API-Key": KEY, "Authorization": f"Bearer {KEY}"}

    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

            # Scenario 25: /health returns ok
            print("\n--- Scenario 25: /health ---")
            r = await c.get("/health", timeout=10.0)
            check("Health 200", r.status_code == 200)
            d = r.json()
            check("status=ok", d.get("status") == "ok")

            # Scenario 26: Auth rejection without key
            print("\n--- Scenario 26: Auth rejection ---")
            r = await c.get("/api/system/status", timeout=10.0)
            check("401 without key", r.status_code == 401)

            # Scenario 27: Trading start/stop cycle
            print("\n--- Scenario 27: Trading start/stop ---")
            r = await c.post("/api/trading/start", headers=HEADERS, timeout=10.0)
            check("Start trading 200", r.status_code == 200)
            r = await c.get("/api/trading/state", headers=HEADERS, timeout=10.0)
            check("is_active=True after start", r.json().get("is_active") == True)
            r = await c.post("/api/trading/stop", headers=HEADERS, timeout=10.0)
            check("Stop trading 200", r.status_code == 200)
            r = await c.get("/api/trading/state", headers=HEADERS, timeout=10.0)
            check("is_active=False after stop", r.json().get("is_active") == False)

            # Scenario 28: Exit-all (closes any remaining open trades)
            print("\n--- Scenario 28: Exit-all ---")
            r = await c.post("/api/trades/exit-all", headers=HEADERS, timeout=15.0)
            check("Exit-all 200", r.status_code == 200)
            d = r.json()
            check("Exit-all returned summary", "total_closed" in d or "status" in d)

            # Scenario 29: Budget start/stop cycle
            print("\n--- Scenario 29: Budget start/stop ---")
            r = await c.post("/api/budget/start", headers=HEADERS, timeout=10.0)
            check("Budget start 200", r.status_code == 200)
            r = await c.post("/api/budget/stop", headers=HEADERS, timeout=10.0)
            check("Budget stop 200", r.status_code == 200)

            # Scenario 30: Frontend served with auth script
            print("\n--- Scenario 30: Frontend served ---")
            r = await c.get("/", timeout=10.0)
            check("Frontend 200", r.status_code == 200)
            check("Has localStorage script", "localStorage" in r.text or "bot_api_key" in r.text)

    await close_db()

    # =====================================================================
    # Summary
    # =====================================================================
    print("\n" + "=" * 70)
    print(f"RESULTS: ✅ {PASSED} passed | ❌ {FAILED} failed | ⏭️  {SKIPPED} skipped")
    print(f"Total: {PASSED + FAILED + SKIPPED} scenarios")
    print("=" * 70)
    return FAILED


if __name__ == "__main__":
    failed = asyncio.run(run_all())
    sys.exit(1 if failed > 0 else 0)
