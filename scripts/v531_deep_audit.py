"""
V531 — DEEP BOT AUDIT: فحص كأنني أصنع البوت لأول مرة
يفحص كل جزء ويتحقق من التناسق بين الأجزاء.
"""
from __future__ import annotations
import os, sys, ast, importlib, inspect, json
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

def section(t):
    print(f"\n{'━' * 90}\n  {t}\n{'━' * 90}")

def check(label, ok, detail=""):
    symbol = "✅" if ok else "❌"
    print(f"  {symbol} {label}" + (f" → {detail}" if detail else ""))
    return ok

issues = []

# ============================================================
section("1️⃣  PROFILE SYSTEM CONSISTENCY — هل كل جزء يحترم النمط النشط؟")
# ============================================================

from app.core.config import settings

# اختبر كل نمط وتحقق أن active_* properties تتطابق
profiles = ['conservative','aggressive','extreme','sniper','brutal','ultimate',
            'survivor','rugsurfer','flash','micro','balanced','adaptive','sniper_v2']

all_ok = True
for p in profiles:
    settings.trading_profile = p
    # تحقق أن كل property تعطي قيمة (ليس None أو 0)
    issues_p = []
    if settings.active_trade_pct <= 0: issues_p.append("trade_pct=0")
    if settings.active_daily_cap_pct <= 0: issues_p.append("cap=0")
    if settings.active_loss_pct <= 0: issues_p.append("loss=0")
    if settings.active_max_positions <= 0: issues_p.append("pos=0")
    if settings.active_min_liquidity_usd <= 0: issues_p.append("liq=0")
    if issues_p:
        all_ok = False
        issues.append(f"Profile '{p}': {', '.join(issues_p)}")
        check(f"{p}", False, ", ".join(issues_p))
    else:
        check(f"{p}", True, f"trade={settings.active_trade_pct}% loss={settings.active_loss_pct}% pos={settings.active_max_positions}")

settings.trading_profile = 'ultimate'

# ============================================================
section("2️⃣  DECISION ENGINE — هل SL/TP/position_size متناسقة مع النمط؟")
# ============================================================

from app.modules.decision_engine.engine import DecisionEngine, DecisionResult
from app.modules.security.scanner import SecurityResult
from app.modules.market_regime.detector import MarketRegime

@dataclass
class FakeToken:
    symbol: str = "TEST"
    chain: str = "solana"
    address: str = "TestMint"
    price_usd: float = 0.001
    liquidity_usd: float = 50000.0
    volume_24h_usd: float = 200000.0
    price_change_24h_pct: float = 30.0
    price_change_5m_pct: float = 15.0
    price_change_1h_pct: float = 20.0
    price_change_6h_pct: float = 25.0
    last_seen_at: Any = None
    first_seen_at: Any = None

# اختبر SL/TP لكل نمط
for p in ['ultimate', 'survivor', 'rugsurfer', 'flash', 'micro', 'balanced', 'adaptive', 'sniper_v2']:
    settings.trading_profile = p
    token = FakeToken()
    result = DecisionResult()
    result.qualified = True
    result.data_sufficient = True
    result.onchain_detail = {"top_holder_pct": 5.0}
    result.technical_detail = {"data_sufficient": True}
    result.liquidity_detail = {"data_sufficient": True}
    sec = SecurityResult()
    sec.auto_disqualify = False
    sec.top_holder_pct = 5.0
    regime = MarketRegime()
    regime.regime = "unknown"
    regime.trading_recommended = True

    engine = DecisionEngine()
    engine._decide(result, token, regime, sec)

    sl_pct = (1 - (result.suggested_stop_loss or token.price_usd * 0.92) / token.price_usd) * 100
    tp_mult = (result.suggested_take_profit or token.price_usd * 1.5) / token.price_usd
    pos = result.suggested_position_size_pct
    hold = result.suggested_holding_period_minutes

    # القيم المتوقعة
    expected = {
        'ultimate': (5, 1.6, 10, 60),
        'survivor': (3, 1.05, 5, 30),
        'rugsurfer': (5, 1.20, 8, 0.33),
        'flash': (2, 1.03, 6, 0.25),
        'micro': (2, 1.02, 5, 0.17),
        'balanced': (4, 1.08, 7, 1.0),
        'adaptive': (5, 1.05, 8, 0.75),
        'sniper_v2': (5, 1.12, 9, 1.0),
    }
    exp_sl, exp_tp, exp_pos, exp_hold = expected[p]
    ok_sl = abs(sl_pct - exp_sl) < 0.5
    ok_tp = abs(tp_mult - exp_tp) < 0.05
    ok_pos = abs(pos - exp_pos) < 0.5
    ok_hold = abs(hold - exp_hold) < 0.1 if hold < 5 else abs(hold - exp_hold) < 1

    all_match = ok_sl and ok_tp and ok_pos and ok_hold
    if not all_match:
        issues.append(f"DecisionEngine '{p}': SL={sl_pct:.1f}% (exp {exp_sl}%), TP={tp_mult:.2f}x (exp {exp_tp}x), pos={pos}% (exp {exp_pos}%), hold={hold}min (exp {exp_hold})")
    check(f"{p} SL/TP/pos/hold", all_match,
          f"SL=-{sl_pct:.1f}% TP={tp_mult:.2f}x pos={pos}% hold={hold}min")

settings.trading_profile = 'ultimate'

# ============================================================
section("3️⃣  EXECUTOR EXIT LOGIC — هل تطبّق SL/TP بشكل صحيح؟")
# ============================================================

from app.modules.live_trading.executor import LiveTradingExecutor
src = inspect.getsource(LiveTradingExecutor.manage_open_positions)

# تحقق من كل أنواع الخروج
exit_checks = [
    ("circuit_breaker", "circuit breaker check"),
    ("sudden_dump", "sudden dump detection"),
    ("stop_loss_price", "stop loss execution"),
    ("take_profit_price", "take profit execution"),
    ("trailing_hit", "trailing stop"),
    ("stagnation_hit", "stagnation exit"),
    ("time_exit", "time-based exit"),
    ("is_quick_exit_mode", "quick exit profiles"),
    ("is_ultimate_mode", "ultimate profile"),
]
for keyword, desc in exit_checks:
    ok = keyword in src
    if not ok:
        issues.append(f"Executor missing: {keyword}")
    check(f"{desc} ({keyword})", ok)

# ============================================================
section("4️⃣  EXECUTOR BUY GATES — هل كل البوابات بالترتيب الصحيح؟")
# ============================================================

src_buy = inspect.getsource(LiveTradingExecutor.open_position)
gates = [
    "live_trading_not_configured",
    "invalid_amount",
    "daily_cap_reached",
    "daily_loss_limit_reached",
    "risk_blocked",
    "max_open_positions",
    "token_not_found",
    "missing_mint",
    "insufficient_wallet_balance",
    "buy_failed",
    "buy_tx_failed_onchain",
]
for g in gates:
    ok = g in src_buy
    if not ok:
        issues.append(f"Buy gate missing: {g}")
    check(f"Gate: {g}", ok)

# ============================================================
section("5️⃣  EXECUTOR SELL FLOW — هل البيع يعمل بشكل صحيح؟")
# ============================================================

src_sell = inspect.getsource(LiveTradingExecutor.close_position)
sell_checks = [
    ("not_an_open_live_trade", "verify trade is open"),
    ("sell_failed", "sell error handling"),
    ("sell_tx_failed_onchain", "sell on-chain confirmation"),
    ("already_closed", "double-sell prevention"),
    ("exit_slippage_usd", "exit slippage tracking"),
    ("net_pnl_usd", "net PnL computation"),
    ("pnl_pct", "PnL % computation"),
    ("record_trade_result", "risk manager update"),
    ("exit_timestamp", "exit timestamp recording"),
    ("exit_reason", "exit reason recording"),
]
for kw, desc in sell_checks:
    ok = kw in src_sell
    if not ok:
        issues.append(f"Sell flow missing: {kw}")
    check(f"{desc}", ok)

# ============================================================
section("6️⃣  API ENDPOINTS — هل كل الـ endpoints مسجلة؟")
# ============================================================

from app.main import create_app
app = create_app()
paths = sorted([r.path for r in app.routes if hasattr(r, 'path')])

required_endpoints = [
    # Trading control
    "/api/trading/start", "/api/trading/stop", "/api/trading/state",
    # Token & trades
    "/api/tokens", "/api/trades",
    # Budget
    "/api/budget/settings", "/api/budget/can-trade",
    # Live trading
    "/api/live/status",
    # Profile switching (V529)
    "/api/trading/profile", "/api/market/regime",
    # Capital (V529)
    "/api/capital/balance", "/api/capital/withdraw",
    # Manual buy/sell
    "/api/pumpfun/trade/buy", "/api/pumpfun/trade/sell",
    # Agents
    "/api/agents/list", "/api/agents/status",
    # System
    "/api/system/status", "/health", "/docs",
]
for ep in required_endpoints:
    ok = ep in paths
    if not ok:
        issues.append(f"Endpoint missing: {ep}")
    check(f"GET/POST {ep}", ok)

# ============================================================
section("7️⃣  AGENTS — هل AI agents فعلاً مستوردة ومنطقها موجود؟")
# ============================================================

agent_modules = [
    'app.modules.ai_agents.orchestrator',
    'app.modules.ai_agents.base_agent',
    'app.modules.ai_agents.security_agent',
    'app.modules.ai_agents.risk_agent',
    'app.modules.ai_agents.technical_agent',
    'app.modules.ai_agents.sentiment_agent',
    'app.modules.ai_agents.liquidity_agent',
    'app.modules.ai_agents.llm_provider',
    'app.modules.ai_agents.prompts',
    'app.modules.agents.multi_agent_system',
]
for m in agent_modules:
    try:
        importlib.import_module(m)
        check(f"Import {m}", True)
    except Exception as e:
        issues.append(f"Agent import failed: {m}: {e}")
        check(f"Import {m}", False, str(e)[:100])

# تحقق من class names في multi_agent_system
try:
    from app.modules.agents.multi_agent_system import MultiAgentSystem
    check("MultiAgentSystem class exists", True)
except Exception as e:
    check("MultiAgentSystem class exists", False, str(e)[:100])
    issues.append(f"MultiAgentSystem: {e}")

# ============================================================
section("8️⃣  SMART PROFILE SWITCHER — هل يعمل في كل الحالات؟")
# ============================================================

from app.modules.market_regime.smart_switcher import SmartProfileSwitcher
switcher = SmartProfileSwitcher()

# تحقق من الدوال الموجودة
switcher_methods = ['detect_market_state', 'auto_switch_if_needed', '_update_open_positions_for_new_profile', '_classify_market']
for m in switcher_methods:
    ok = hasattr(switcher, m)
    if not ok:
        issues.append(f"Switcher missing method: {m}")
    check(f"SmartProfileSwitcher.{m}", ok)

# تحقق من المنطق
src_sw = inspect.getsource(SmartProfileSwitcher._classify_market)
# هل يوصي بـ ULTIMATE لكل السيناريوهات؟
has_ultimate = "ultimate" in src_sw
# هل override لـ SURVIVOR عند الخسارة؟
has_override = "survivor" in src_sw and "pnl_24h" in src_sw
check("Recommends ULTIMATE for all scenarios", has_ultimate)
check("Override to SURVIVOR on losses", has_override)
if not has_ultimate: issues.append("Switcher doesn't recommend ultimate")
if not has_override: issues.append("Switcher doesn't override to survivor on losses")

# ============================================================
section("9️⃣  ORCHESTRATOR — هل SmartProfileSwitcher يُستدعى كل دورة؟")
# ============================================================

from app.jobs.orchestrator import MainOrchestrator
src_orch = inspect.getsource(MainOrchestrator.run_cycle)

orch_checks = [
    ("SmartProfileSwitcher", "switcher is called"),
    ("auto_switch_if_needed", "auto switch runs"),
    ("scan_all_chains", "market scan"),
    ("get_watch_list", "watch list fetch"),
    ("manage_open_positions", "position management"),
    ("_refresh_live_token_prices", "DexScreener price refresh"),
]
for kw, desc in orch_checks:
    ok = kw in src_orch
    if not ok:
        issues.append(f"Orchestrator missing: {kw}")
    check(f"{desc}", ok)

# ============================================================
section("🔟  DATA FLOW CONSISTENCY — هل البيانات تتدفق بشكل صحيح؟")
# ============================================================

# تحقق أن scanner يحفظ price_change_1h_pct
from app.modules.market_scanner.scanner import MarketScanner
src_scanner = inspect.getsource(MarketScanner)
flow_checks = [
    ("price_change_1h_pct", "1h change is parsed"),
    ("price_change_6h_pct", "6h change is parsed"),
    ("price_change_24h_pct", "24h change is parsed"),
]
for kw, desc in flow_checks:
    ok = kw in src_scanner
    if not ok:
        issues.append(f"Scanner missing: {kw}")
    check(f"{desc}", ok)

# تحقق أن routes ترجع كل الحقول
routes_src = Path(ROOT / "backend" / "app" / "api" / "routes.py").read_text()
api_checks = [
    ("price_change_1h_pct", "1h in API response"),
    ("price_change_6h_pct", "6h in API response"),
    ("price_change_24h_pct", "24h in API response"),
]
for kw, desc in api_checks:
    ok = kw in routes_src
    if not ok:
        issues.append(f"API missing field: {kw}")
    check(f"{desc}", ok)

# ============================================================
section("1️⃣1️⃣  V511 PROTECTION — هل ما زالت تعمل؟")
# ============================================================

from app.modules.security.scanner import SecurityScanner, SecurityResult
from app.modules.onchain.analyzer import OnChainResult

@dataclass
class TestToken:
    symbol: str = "TEST"
    chain: str = "solana"
    address: str = "Test"
    price_usd: float = 0.001
    liquidity_usd: float = 50000.0
    volume_24h_usd: float = 200000.0
    price_change_24h_pct: float = 30.0
    price_change_5m_pct: float = 15.0
    last_seen_at: Any = None

scanner = SecurityScanner()
for holder_pct, should_block in [(30.0, True), (29.9, False), (50.0, True), (15.0, False)]:
    token = TestToken()
    onchain = OnChainResult()
    onchain.data_sufficient = True
    onchain.top_holder_pct = holder_pct
    onchain.top_10_pct = 30.0
    sec = scanner.analyze(token, onchain_result=onchain, liquidity_result=None)
    actual = sec.single_wallet_blocked
    ok = actual == should_block
    if not ok:
        issues.append(f"V511 {holder_pct}%: expected {should_block}, got {actual}")
    check(f"V511 {holder_pct}% wallet → block={should_block}", ok, f"actual={actual}")

# ============================================================
section("1️⃣2️⃣  BUDGET MANAGER — هل auto-sizing متناسق؟")
# ============================================================

from app.modules.budget.budget_manager import DEFAULT_BUDGET
check("auto_trade_pct > 0", DEFAULT_BUDGET["auto_trade_pct"] > 0, f"{DEFAULT_BUDGET['auto_trade_pct']}%")
check("auto_loss_pct > 0", DEFAULT_BUDGET["auto_loss_pct"] > 0, f"{DEFAULT_BUDGET['auto_loss_pct']}%")
check("auto_max_positions > 0", DEFAULT_BUDGET["auto_max_positions"] > 0, f"{DEFAULT_BUDGET['auto_max_positions']}")
check("auto_trade_pct matches ULTIMATE", DEFAULT_BUDGET["auto_trade_pct"] == settings.ultimate_trade_pct,
      f"budget={DEFAULT_BUDGET['auto_trade_pct']}% vs ultimate={settings.ultimate_trade_pct}%")
check("auto_loss_pct matches ULTIMATE", DEFAULT_BUDGET["auto_loss_pct"] == settings.ultimate_loss_pct,
      f"budget={DEFAULT_BUDGET['auto_loss_pct']}% vs ultimate={settings.ultimate_loss_pct}%")
check("auto_max_positions matches ULTIMATE", DEFAULT_BUDGET["auto_max_positions"] == settings.ultimate_max_positions,
      f"budget={DEFAULT_BUDGET['auto_max_positions']} vs ultimate={settings.ultimate_max_positions}")

# ============================================================
section("🏆 FINAL DEEP AUDIT SUMMARY")
# ============================================================

print(f"\n  Total issues found: {len(issues)}")
if issues:
    print(f"\n  ❌ ISSUES:")
    for issue in issues:
        print(f"     • {issue}")
else:
    print(f"\n  ✅ NO ISSUES FOUND — bot is fully consistent")

print(f"\n  {'OVERALL':<30} {'✅ ALL CONSISTENT' if not issues else '❌ ISSUES FOUND'}")
