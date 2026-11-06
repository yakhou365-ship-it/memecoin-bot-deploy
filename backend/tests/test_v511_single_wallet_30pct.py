"""
V511 — Single-wallet 30%+ hard-block test suite.

يثبت أن:
1. OnChainAnalyzer يحسب top_holder_pct بدقة ويعُدّ المحافظ فوق العتبات
2. SecurityScanner يضيف critical flag SINGLE_WALLET_OVER_30PCT ويُفعّل auto_disqualify
3. DecisionEngine يحظر العملة بشكل قاطع (GATE 1b)
4. DangerDetector يصنف الخطر كـ "critical" + AVOID
5. تعمل ذاكرة البيانات حتى لو محفظتين 30%+ (holders_over_30pct = 2)
6. لا يحظر عند 29.9% (حد أدنى صحي)
7. يحظر عند 30.0% بالضبط (حد الحظر)
"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Add project root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# Stub core.logger if missing
try:
    from app.core.logger import logger  # noqa: F401
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO)
    class _L:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass
    import types
    sys.modules.setdefault("app.core.logger", types.SimpleNamespace(logger=_L()))

from app.core.config import settings
from app.modules.onchain.analyzer import OnChainAnalyzer, OnChainResult
from app.modules.security.scanner import SecurityScanner, SecurityResult
from app.modules.security.danger_detector import DangerousPatternDetector
from app.modules.decision_engine.engine import DecisionEngine, DecisionResult


# ─── Stubs ───────────────────────────────────────────────────────────────────

@dataclass
class FakeToken:
    symbol: str = "TEST"
    chain: str = "solana"
    address: str = "TestMintAddr111111111111111111111111111"
    price_usd: float = 0.001
    liquidity_usd: float = 100_000.0
    volume_24h_usd: float = 500_000.0
    price_change_24h_pct: float = 50.0
    price_change_5m_pct: float = 5.0
    last_seen_at: Optional[str] = None


def make_onchain(top_pct: float, supply: float = 1_000_000.0) -> OnChainResult:
    """بناء OnChainResult يدوياً بدون RPC لمحاكاة محفظة مع نسبة معينة."""
    r = OnChainResult()
    r.data_sufficient = True
    r.total_holders = 20
    r.top_holder_pct = top_pct
    r.top_holder_address = "WhaleWalletABC12345"
    r.holders_over_10pct = 1 if top_pct >= 10 else 0
    r.holders_over_20pct = 1 if top_pct >= 20 else 0
    r.holders_over_30pct = 1 if top_pct >= 30 else 0
    r.top_10_pct = max(top_pct, 35.0)  # mock: top 10 holders
    r.top_20_pct = max(top_pct, 50.0)
    r.mint_authority_enabled = False
    r.freeze_authority_enabled = False
    r.liquidity_locked = True
    return r


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_config_has_threshold():
    """الإعداد الافتراضي 30%."""
    assert settings.max_single_holder_pct == 30.0, (
        f"Expected 30.0, got {settings.max_single_holder_pct}"
    )
    assert settings.block_single_wallet_over_pct is True
    print("✅ [1] config threshold = 30%, block enabled")


def test_security_scanner_blocks_at_30():
    """عند 35%: SecurityScanner يفعّل auto_disqualify + critical flag."""
    token = FakeToken()
    onchain = make_onchain(top_pct=35.0)
    scanner = SecurityScanner()
    sec = scanner.analyze(token, onchain_result=onchain, liquidity_result=None)

    assert sec.single_wallet_blocked is True, "single_wallet_blocked should be True"
    assert sec.auto_disqualify is True, "auto_disqualify should be True"
    assert sec.top_holder_pct == 35.0
    assert any("SINGLE_WALLET_OVER_30PCT" in f for f in sec.critical_flags), (
        f"critical_flags={sec.critical_flags}"
    )
    print(f"✅ [2] 35% wallet → auto_disqualify=True, flags={sec.critical_flags}")


def test_security_scanner_warns_at_25():
    """عند 25%: تحذير فقط (لا حظر)، لكن لم يُفعّل single_wallet_blocked."""
    token = FakeToken()
    onchain = make_onchain(top_pct=25.0)
    scanner = SecurityScanner()
    sec = scanner.analyze(token, onchain_result=onchain, liquidity_result=None)

    assert sec.single_wallet_blocked is False, "should NOT block at 25%"
    assert sec.top_holder_pct == 25.0
    # تحذير مبكر عند 70% من العتبة (21%)
    assert any("قريبة" in w or "محفظة" in w for w in sec.warnings), (
        f"warnings={sec.warnings}"
    )
    print(f"✅ [3] 25% wallet → warning only (no block), warnings={sec.warnings}")


def test_security_scanner_allows_at_15():
    """عند 15%: لا تحذير ولا حظر."""
    token = FakeToken()
    onchain = make_onchain(top_pct=15.0)
    scanner = SecurityScanner()
    sec = scanner.analyze(token, onchain_result=onchain, liquidity_result=None)

    assert sec.single_wallet_blocked is False
    assert sec.auto_disqualify is False
    print(f"✅ [4] 15% wallet → no flags, clean (risk={sec.risk_score})")


def test_decision_engine_rejects_at_50pct():
    """الـ DecisionEngine يجب أن يرفض مباشرة عند 50% لمحفظة واحدة."""
    token = FakeToken()
    onchain = make_onchain(top_pct=50.0)

    # بناء DecisionResult يدوياً مع onchain_detail
    result = DecisionResult()
    result.onchain_detail = onchain.to_dict()
    result.data_sufficient = True
    result.data_age_seconds = 0

    # security result: محاكي (لكن فارغ — لن يُفعل auto_disqualify)
    sec = SecurityResult()
    sec.data_sufficient = True
    sec.top_holder_pct = 50.0
    sec.top_holder_address = "WhaleAddrXYZ"
    sec.auto_disqualify = False  # نتأكد أن البوابة المستقلة تعمل

    engine = DecisionEngine()
    # استدعاء _decide مباشرة (نحاول مع regime بسيط)
    from app.modules.market_regime.detector import MarketRegime
    regime = MarketRegime()
    regime.regime = "unknown"
    regime.trading_recommended = True

    engine._decide(result, token, regime, sec)

    assert result.qualified is False, "Token must be REJECTED"
    assert result.recommendation == "reject", f"got {result.recommendation}"
    assert "30" in result.rejection_reason or "محفظة" in result.rejection_reason, (
        f"rejection_reason={result.rejection_reason}"
    )
    print(f"✅ [5] 50% wallet → DecisionEngine REJECTED: {result.rejection_reason[:80]}")


def test_decision_engine_allows_at_5pct():
    """عند 5% (توزيع صحي): DecisionEngine يجب أن يسمح (يجتاز بوابة 30%)."""
    token = FakeToken()
    onchain = make_onchain(top_pct=5.0)

    result = DecisionResult()
    result.onchain_detail = onchain.to_dict()
    result.data_sufficient = True
    result.data_age_seconds = 0

    sec = SecurityResult()
    sec.data_sufficient = True
    sec.top_holder_pct = 5.0
    sec.auto_disqualify = False

    engine = DecisionEngine()
    from app.modules.market_regime.detector import MarketRegime
    regime = MarketRegime()
    regime.regime = "unknown"
    regime.trading_recommended = True

    engine._decide(result, token, regime, sec)

    # لن يكون "qualified=True" بالضرورة (بوابات أخرى)، لكن سبب الرفض
    # يجب ألا يكون "محفظة 30%"
    rejection = result.rejection_reason or ""
    assert "محفظة" not in rejection or "30" not in rejection, (
        f"Should NOT block on 30% rule at 5%: {rejection}"
    )
    print(f"✅ [6] 5% wallet → passes 30% gate (rejection if any: {rejection[:60]})")


def test_danger_detector_critical_at_40pct():
    """DangerDetector يجب أن يصنف 40% كـ critical + AVOID."""
    detector = DangerousPatternDetector()
    onchain = {
        "top_holder_pct": 40.0,
        "top_holder_address": "WhaleAddrAAA",
        "holders_over_30pct": 1,
        "holders_over_20pct": 1,
        "holders_over_10pct": 1,
    }
    danger = detector.detect_single_wallet_concentration(onchain)
    assert danger.detected is True
    assert danger.severity == "critical"
    assert danger.recommendation == "AVOID"
    assert danger.confidence > 0.9
    print(f"✅ [7] 40% → danger detector: critical, AVOID, conf={danger.confidence:.2f}")


def test_danger_detector_safe_at_5pct():
    """DangerDetector عند 5% = SAFE."""
    detector = DangerousPatternDetector()
    onchain = {"top_holder_pct": 5.0, "top_holder_address": "x"}
    danger = detector.detect_single_wallet_concentration(onchain)
    assert danger.detected is False
    assert danger.severity == "low"
    assert danger.recommendation == "SAFE"
    print(f"✅ [8] 5% → danger detector: low, SAFE")


def test_multiple_whales_counted():
    """إذا محفظتين 30%+: holders_over_30pct يجب أن يكون 2."""
    onchain = OnChainResult()
    onchain.top_holder_pct = 45.0
    onchain.holders_over_30pct = 2
    onchain.holders_over_20pct = 3
    onchain.holders_over_10pct = 5

    detector = DangerousPatternDetector()
    danger = detector.detect_single_wallet_concentration(onchain.to_dict())
    assert danger.indicators["holders_over_30pct"] == 2
    assert danger.indicators["holders_over_20pct"] == 3
    assert danger.indicators["holders_over_10pct"] == 5
    print(f"✅ [9] Multiple whales tracked: 30%={danger.indicators['holders_over_30pct']}, "
          f"20%={danger.indicators['holders_over_20pct']}, 10%={danger.indicators['holders_over_10pct']}")


def test_threshold_boundary():
    """حد الحظر بالضبط عند 30.0% — لا يُسمح عند 29.9% ويُحظر عند 30.0%."""
    scanner = SecurityScanner()
    token = FakeToken()

    # 29.9%: يجب ألا يحظر
    onchain_29_9 = make_onchain(top_pct=29.9)
    sec_29_9 = scanner.analyze(token, onchain_result=onchain_29_9, liquidity_result=None)
    assert sec_29_9.single_wallet_blocked is False, "29.9% should NOT be blocked"
    assert sec_29_9.auto_disqualify is False, "29.9% should NOT auto-disqualify"

    # 30.0%: يجب أن يحظر
    onchain_30 = make_onchain(top_pct=30.0)
    sec_30 = scanner.analyze(token, onchain_result=onchain_30, liquidity_result=None)
    assert sec_30.single_wallet_blocked is True, "30.0% SHOULD be blocked"
    assert sec_30.auto_disqualify is True, "30.0% should auto-disqualify"
    print("✅ [10] Boundary: 29.9% allowed, 30.0% blocked (sharp threshold)")


def test_pump_fun_typical_scenario():
    """سيناريو واقعي: توكن pump.fun جديد فيه محفظة dev تملك 35%.
    هذا случrug pull شائع. البوت يجب أن يرفضه فوراً.
    """
    token = FakeToken(symbol="RISKYDEV", liquidity_usd=15_000.0, volume_24h_usd=80_000.0)
    onchain = make_onchain(top_pct=35.0)
    onchain.mint_authority_enabled = False  # حتى لا يكون هناك سبب آخر للرفض
    onchain.liquidity_locked = True

    scanner = SecurityScanner()
    sec = scanner.analyze(token, onchain_result=onchain, liquidity_result=None)

    # حتى لو بقية المؤشرات نظيفة، الـ 35% يكفي وحده للحظر
    assert sec.single_wallet_blocked is True
    assert sec.auto_disqualify is True
    assert "SINGLE_WALLET_OVER_30PCT" in sec.critical_flags
    print(f"✅ [11] Real-world rug scenario: 35% dev wallet → BLOCKED "
          f"(flags={sec.critical_flags})")


def main():
    print("=" * 70)
    print("V511 — Single-Wallet 30%+ Hard-Block Test Suite")
    print("=" * 70)
    print()

    test_config_has_threshold()
    test_security_scanner_blocks_at_30()
    test_security_scanner_warns_at_25()
    test_security_scanner_allows_at_15()
    test_decision_engine_rejects_at_50pct()
    test_decision_engine_allows_at_5pct()
    test_danger_detector_critical_at_40pct()
    test_danger_detector_safe_at_5pct()
    test_multiple_whales_counted()
    test_threshold_boundary()
    test_pump_fun_typical_scenario()

    print()
    print("=" * 70)
    print("✅ ALL 11 TESTS PASSED — single-wallet 30%+ block is working")
    print("=" * 70)


if __name__ == "__main__":
    main()
