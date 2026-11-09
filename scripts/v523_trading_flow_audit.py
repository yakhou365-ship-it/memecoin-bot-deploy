"""
V523 — TRADING FLOW INTEGRITY TEST
فحص شامل لتدفق التداول الكامل:
1. DecisionEngine ينتج SL/TP متوافقة مع ULTIMATE profile
2. PumpPortal client methods (buy/sell/confirm) signature صحيحة
3. LiveTradingExecutor gates تعمل بالترتيب الصحيح
4. manage_open_positions يطبّق كل قواعد ULTIMATE
5. Auto-sizing يحترم max_position_usd
6. Risk manager يتكامل مع close_position
7. API endpoints /trading/start /trading/stop تعمل
"""
from __future__ import annotations

import os, sys, asyncio, json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def section(t: str):
    print(f"\n{'═' * 80}\n  {t}\n{'═' * 80}")


def check_decision_sl_tp():
    """1. DecisionEngine يجب أن ينتج SL/TP متوافقة مع ULTIMATE."""
    section("1️⃣  DECISION ENGINE — SL/TP/POSITION_SIZE per profile")
    from app.core.config import settings
    from app.modules.decision_engine.engine import DecisionEngine, DecisionResult
    from app.modules.security.scanner import SecurityResult

    @dataclass
    class FakeToken:
        symbol: str = "TEST"
        chain: str = "solana"
        address: str = "TestMint"
        price_usd: float = 0.001
        liquidity_usd: float = 100_000.0
        volume_24h_usd: float = 500_000.0
        price_change_24h_pct: float = 50.0
        price_change_5m_pct: float = 15.0
        price_change_1h_pct: float = 30.0
        last_seen_at: Optional[str] = None
        first_seen_at: Optional[str] = None

    profiles = ['conservative', 'aggressive', 'extreme', 'sniper', 'brutal', 'ultimate']
    all_ok = True
    for p in profiles:
        settings.trading_profile = p
        token = FakeToken()
        result = DecisionResult()
        result.qualified = True
        result.data_sufficient = True
        result.onchain_detail = {"top_holder_pct": 5.0, "top_holder_address": "x"}
        result.technical_detail = {"data_sufficient": True}
        result.liquidity_detail = {"data_sufficient": True}

        sec = SecurityResult()
        sec.auto_disqualify = False
        sec.top_holder_pct = 5.0

        from app.modules.market_regime.detector import MarketRegime
        regime = MarketRegime()
        regime.regime = "unknown"
        regime.trading_recommended = True

        engine = DecisionEngine()
        engine._decide(result, token, regime, sec)

        sl = result.suggested_stop_loss
        tp = result.suggested_take_profit
        pos = result.suggested_position_size_pct

        # حساب النسب المتوقعة
        if p == 'ultimate':
            expected_sl_pct = settings.ultimate_stop_loss_pct * 100  # 5%
            expected_tp_mult = 1.0 + settings.ultimate_medium_take_profit_pct  # 1.6
            expected_pos = settings.ultimate_trade_pct  # 10.0
        elif p == 'brutal':
            expected_sl_pct = settings.brutal_stop_loss_pct * 100
            expected_tp_mult = 1.0 + settings.brutal_medium_take_profit_pct
            expected_pos = settings.brutal_trade_pct
        elif p == 'sniper':
            expected_sl_pct = settings.sniper_stop_loss_pct * 100
            expected_tp_mult = 1.0 + settings.sniper_moonshot_threshold
            expected_pos = settings.sniper_trade_pct
        else:
            expected_sl_pct = 8.0
            expected_tp_mult = 11.0
            expected_pos = settings.active_trade_pct

        actual_sl_pct = (1 - sl / token.price_usd) * 100
        actual_tp_mult = tp / token.price_usd

        sl_ok = abs(actual_sl_pct - expected_sl_pct) < 0.1
        tp_ok = abs(actual_tp_mult - expected_tp_mult) < 0.01
        pos_ok = abs(pos - expected_pos) < 0.1

        status = "✅" if (sl_ok and tp_ok and pos_ok) else "❌"
        if not (sl_ok and tp_ok and pos_ok):
            all_ok = False
        print(f"  {status} {p:<13} | pos={pos:>4.1f}% (exp {expected_pos:>4.1f}%) | "
              f"SL=-{actual_sl_pct:>4.1f}% (exp -{expected_sl_pct:>4.1f}%) | "
              f"TP={actual_tp_mult:>5.2f}x (exp {expected_tp_mult:>5.2f}x)")

    settings.trading_profile = 'ultimate'  # restore
    return all_ok


def check_pumpportal_client():
    """2. PumpPortal client methods signature."""
    section("2️⃣  PUMP PORTAL CLIENT — method signatures")
    from app.modules.market_scanner.pumpfun_client import PumpPortalClient
    from app.modules.market_scanner.pumpportal_ws import PumpPortalWebSocket
    import inspect

    methods_to_check = ['buy_token', 'sell_token', 'confirm_signature', 'get_wallet_balance', '_fetch_sol_price']
    all_ok = True
    for m in methods_to_check:
        if hasattr(PumpPortalClient, m):
            sig = inspect.signature(getattr(PumpPortalClient, m))
            print(f"  ✅ PumpPortalClient.{m}{sig}")
        else:
            print(f"  ❌ PumpPortalClient.{m} NOT FOUND")
            all_ok = False

    # PumpPortalWebSocket
    ws_methods = ['start', 'stop', '_handle_message', 'on_new_token', 'on_migration', 'is_connected']
    for m in ws_methods:
        if hasattr(PumpPortalWebSocket, m):
            print(f"  ✅ PumpPortalWebSocket.{m}")
        else:
            print(f"  ❌ PumpPortalWebSocket.{m} NOT FOUND")
            all_ok = False
    return all_ok


def check_executor_gates():
    """3. LiveTradingExecutor gates order."""
    section("3️⃣  EXECUTOR — open_position gate order")
    from app.modules.live_trading.executor import LiveTradingExecutor
    import inspect

    src = inspect.getsource(LiveTradingExecutor.open_position)
    gates_in_order = [
        ("live_trading_not_configured", "configured check"),
        ("invalid_amount", "amount > 0"),
        ("daily_cap_reached", "daily cap"),
        ("daily_loss_limit_reached", "daily loss"),
        ("risk_blocked", "circuit breaker"),
        ("max_open_positions", "max positions"),
        ("token_not_found", "token exists"),
        ("missing_mint", "mint address"),
        ("insufficient_wallet_balance", "wallet SOL"),
        ("buy_failed", "buy tx"),
        ("buy_tx_failed_onchain", "buy confirm"),
    ]
    all_ok = True
    for reject_code, desc in gates_in_order:
        if reject_code in src:
            print(f"  ✅ {reject_code} ({desc})")
        else:
            print(f"  ❌ {reject_code} NOT in open_position source")
            all_ok = False
    return all_ok


def check_exit_logic():
    """4. manage_open_positions يطبّق كل قواعد ULTIMATE."""
    section("4️⃣  EXIT LOGIC — manage_open_positions rules")
    from app.modules.live_trading.executor import LiveTradingExecutor
    import inspect

    src = inspect.getsource(LiveTradingExecutor.manage_open_positions)

    rules_to_check = [
        ("peak_price", "tracking peak price"),
        ("peak_gain_pct", "computing peak gain"),
        ("is_ultimate_mode", "ULTIMATE branch"),
        ("is_brutal_mode", "BRUTAL branch"),
        ("is_sniper_mode", "SNIPER branch"),
        ("ultimate_quick_take_profit_pct", "ULTIMATE quick_tp +10%"),
        ("ultimate_medium_take_profit_pct", "ULTIMATE medium_tp +60%"),
        ("ultimate_moonshot_threshold", "ULTIMATE moonshot +200%"),
        ("ultimate_moonshot_trail_pct", "ULTIMATE trail 25%"),
        ("sudden_dump_min_gain", "sudden dump gain threshold"),
        ("sudden_dump_drop_threshold", "sudden dump drop threshold"),
        ("circuit_breaker_on", "circuit breaker check"),
        ("stop_loss_price", "stop loss from trade"),
        ("take_profit_price", "take profit from trade"),
        ("stagnation_hit", "stagnation"),
        ("planned_holding_period_minutes", "time exit"),
    ]
    all_ok = True
    for rule, desc in rules_to_check:
        if rule in src:
            print(f"  ✅ {rule} ({desc})")
        else:
            print(f"  ❌ {rule} NOT in manage_open_positions")
            all_ok = False
    return all_ok


def check_auto_sizing():
    """5. Auto-sizing يحترم max_position_usd في orchestrator."""
    section("5️⃣  AUTO-SIZING — orchestrator logic")
    from app.jobs.orchestrator import MainOrchestrator
    import inspect

    src = inspect.getsource(MainOrchestrator)
    items = [
        ("auto_sizing", "auto_sizing flag"),
        ("auto_trade_pct", "trade_pct from budget"),
        ("auto_loss_pct", "loss_pct from budget"),
        ("auto_max_positions", "max_positions from budget"),
        ("effective_max_trade_sol", "max trade SOL"),
        ("effective_daily_cap", "daily cap"),
        ("effective_loss_limit", "loss limit"),
        ("SMALL_CAP_THRESHOLD_USD", "small cap detection"),
    ]
    all_ok = True
    for keyword, desc in items:
        if keyword in src:
            print(f"  ✅ {keyword} ({desc})")
        else:
            print(f"  ❌ {keyword} NOT in orchestrator")
            all_ok = False
    return all_ok


def check_risk_manager():
    """6. RiskManager integration."""
    section("6️⃣  RISK MANAGER — integration with close_position")
    from app.modules.live_trading.executor import LiveTradingExecutor
    from app.modules.risk_management.manager import RiskManager
    import inspect

    src = inspect.getsource(LiveTradingExecutor.close_position)

    checks = [
        ("record_trade_result", "RM record_trade_result called"),
        ("already_closed", "double-sell prevention"),
        ("exit_slippage_usd", "exit slippage tracked"),
        ("net_pnl_usd", "net PnL computed"),
        ("pnl_pct", "PnL % computed"),
        ("daily[\"sol_pnl\"]", "daily PnL updated"),
        ("daily[\"trades_closed\"]", "daily closed count"),
    ]
    all_ok = True
    for keyword, desc in checks:
        if keyword in src:
            print(f"  ✅ {keyword} ({desc})")
        else:
            print(f"  ❌ {keyword} NOT in close_position")
            all_ok = False
    return all_ok


def check_api_endpoints():
    """7. API endpoints trading start/stop."""
    section("7️⃣  API ENDPOINTS — trading control")
    from app.main import create_app
    app = create_app()

    expected_trading_endpoints = [
        "/api/trading/start",
        "/api/trading/stop",
        "/api/live/status",
        "/api/budget/settings",
        "/api/budget/can-trade",
        "/api/budget/start",
        "/api/budget/stop",
        "/api/tokens",
        "/api/trades",
    ]
    actual_paths = []
    for route in app.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            methods = route.methods - {'HEAD', 'OPTIONS'}
            if methods:
                actual_paths.append(route.path)

    all_ok = True
    for ep in expected_trading_endpoints:
        present = ep in actual_paths or any(p.startswith(ep + "/") for p in actual_paths)
        status = "✅" if present else "❌"
        if not present:
            all_ok = False
        print(f"  {status} {ep}")
    return all_ok


def check_api_response_format():
    """8. API /api/tokens response format."""
    section("8️⃣  API RESPONSE FORMAT — /api/tokens fields")
    # فحص مباشرة في routes.py للحقول المطلوبة
    routes_path = ROOT / "backend" / "app" / "api" / "routes.py"
    src = routes_path.read_text()

    required_fields = [
        '"price_change_24h_pct"',
        '"price_change_6h_pct"',
        '"price_change_1h_pct"',
        '"price_change_5m_pct"',
        '"liquidity_usd"',
        '"volume_24h_usd"',
        '"volume_5m_usd"',
        '"txns_24h_buys"',
        '"txns_24h_sells"',
        '"fdv_usd"',
        '"watch_priority"',
    ]
    all_ok = True
    for f in required_fields:
        if f in src:
            print(f"  ✅ {f.strip('\"')} is serialized")
        else:
            print(f"  ❌ {f.strip('\"')} NOT serialized")
            all_ok = False
    return all_ok


def check_db_persistence():
    """9. Trade persistence with new columns."""
    section("9️⃣  DB PERSISTENCE — Trade fields")
    from app.core.models import Trade, Token
    from sqlalchemy import inspect as sa_inspect

    # تحقق Token columns
    token_cols = {c.name for c in sa_inspect(Token).columns}
    required_token = {
        "price_change_24h_pct", "price_change_6h_pct", "price_change_1h_pct", "price_change_5m_pct"
    }
    missing_token = required_token - token_cols
    if not missing_token:
        print(f"  ✅ Token model has all 4 price_change columns")
    else:
        print(f"  ❌ Token missing: {missing_token}")

    # تحقق Trade columns
    trade_cols = {c.name for c in sa_inspect(Trade).columns}
    required_trade = {
        "entry_price", "exit_price", "entry_size_usd", "entry_quantity",
        "stop_loss_price", "take_profit_price",
        "entry_fee_usd", "exit_fee_usd", "fees_total_usd",
        "entry_slippage_usd", "exit_slippage_usd", "slippage_total_usd",
        "gross_pnl_usd", "net_pnl_usd", "pnl_pct",
        "max_unrealized_profit_usd", "status",
        "entry_timestamp", "exit_timestamp", "exit_reason",
        "strategy_id",
    }
    missing_trade = required_trade - trade_cols
    if not missing_trade:
        print(f"  ✅ Trade model has all {len(required_trade)} required columns")
    else:
        print(f"  ❌ Trade missing: {missing_trade}")
    return not missing_token and not missing_trade


def check_pumpportal_ws():
    """10. PumpPortal WebSocket connection."""
    section("🔟  PUMP PORTAL WEBSOCKET — discovery flow")
    from app.modules.market_scanner.pumpportal_ws import PumpPortalWebSocket
    import inspect

    src = inspect.getsource(PumpPortalWebSocket)
    items = [
        ("start", "WS start method"),
        ("_handle_message", "message handler"),
        ("on_new_token", "new token callback"),
        ("on_migration", "graduation/migration callback"),
        ("is_connected", "connection status"),
        ("stats", "stats method"),
    ]
    all_ok = True
    for keyword, desc in items:
        if keyword in src:
            print(f"  ✅ {keyword} ({desc})")
        else:
            print(f"  ❌ {keyword} NOT in PumpPortalWebSocket")
            all_ok = False
    return all_ok


def main():
    print("╔" + "═" * 78 + "╗")
    print("║" + "  V523 — TRADING FLOW INTEGRITY AUDIT".center(78) + "║")
    print("╚" + "═" * 78 + "╝")

    results = {
        "1. Decision SL/TP": check_decision_sl_tp(),
        "2. PumpPortal client": check_pumpportal_client(),
        "3. Executor gates": check_executor_gates(),
        "4. Exit logic": check_exit_logic(),
        "5. Auto-sizing": check_auto_sizing(),
        "6. Risk manager": check_risk_manager(),
        "7. API endpoints": check_api_endpoints(),
        "8. API response format": check_api_response_format(),
        "9. DB persistence": check_db_persistence(),
        "10. WebSocket discovery": check_pumpportal_ws(),
    }

    section("🏆 FINAL TRADING FLOW AUDIT")
    print(f"\n  {'Check':<30} {'Status':<10}")
    print(f"  {'-'*45}")
    for name, ok in results.items():
        print(f"  {name:<30} {'✅ PASS' if ok else '❌ FAIL'}")

    all_pass = all(results.values())
    print(f"\n  {'OVERALL':<30} {'✅ ALL PASS' if all_pass else '❌ ISSUES FOUND'}")
    return all_pass


if __name__ == "__main__":
    main()
