"""
Comprehensive Dangerous Pattern Detection.

Detects 20+ types of dangerous trading patterns/scenarios:
1. Honeypot (can't sell)
2. Rug Pull (liquidity removal)
3. Pump and Dump
4. Wash Trading (fake volume)
5. Front Running
6. MEV/Sandwich Attacks
7. Flash Loan Attacks
8. Spoofing (fake orders)
9. Layering (multiple fake orders)
10. Stop Hunting
11. Bear Trap
12. Bull Trap
13. Dead Cat Bounce
14. Blow-off Top (parabolic then crash)
15. Capitulation (panic selling)
16. Whale Manipulation
17. Insider Trading
18. Token Bundling
19. Liquidity Migration
20. Mint Authority Abuse
21. Freeze Authority Abuse
22. Smart Contract Exploit
23. Fake Breakout
24. Low Liquidity Trap
25. Slippage Trap
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


@dataclass
class DangerDetection:
    """Result of dangerous pattern detection."""
    pattern_name: str
    detected: bool
    severity: str = "low"  # low | medium | high | critical
    confidence: float = 0.0  # 0-1
    description: str = ""
    indicators: Dict = field(default_factory=dict)
    recommendation: str = ""  # AVOID | CAUTION | MONITOR | SAFE

    def to_dict(self) -> Dict:
        return asdict(self)


class DangerousPatternDetector:
    """Detects 25+ dangerous trading patterns."""

    def detect_all(self, df: pd.DataFrame, token=None, onchain_data: Dict = None) -> List[DangerDetection]:
        """Run all detectors and return list of detected patterns."""
        results = []
        onchain_data = onchain_data or {}

        # === Contract-level dangers (from on-chain data) ===
        results.append(self.detect_honeypot(onchain_data, token))
        results.append(self.detect_rug_pull(onchain_data, token))
        results.append(self.detect_mint_authority_abuse(onchain_data))
        results.append(self.detect_freeze_authority_abuse(onchain_data))
        results.append(self.detect_token_bundling(onchain_data))
        results.append(self.detect_liquidity_migration(onchain_data))
        results.append(self.detect_smart_contract_exploit(onchain_data))
        # V511 — single-wallet concentration (CRITICAL)
        results.append(self.detect_single_wallet_concentration(onchain_data))

        # === Volume/Trading dangers ===
        results.append(self.detect_wash_trading(df, token))
        results.append(self.detect_fake_volume(df, token))
        results.append(self.detect_spoofing(df))
        results.append(self.detect_layering(df))
        results.append(self.detect_front_running(df))
        results.append(self.detect_mev_sandwich(df))

        # === Price action dangers ===
        results.append(self.detect_pump_and_dump(df))
        results.append(self.detect_blow_off_top(df))
        results.append(self.detect_capitulation(df))
        results.append(self.detect_dead_cat_bounce(df))
        results.append(self.detect_bear_trap(df))
        results.append(self.detect_bull_trap(df))
        results.append(self.detect_stop_hunting(df))
        results.append(self.detect_fake_breakout(df))

        # === Liquidity dangers ===
        results.append(self.detect_low_liquidity_trap(token))
        results.append(self.detect_slippage_trap(token))
        results.append(self.detect_whale_manipulation(onchain_data, df))

        # === Insider dangers ===
        results.append(self.detect_insider_trading(onchain_data))
        results.append(self.detect_flash_loan_attack(df, onchain_data))

        return [r for r in results if r is not None]

    # =====================================================================
    # Contract-level dangers
    # =====================================================================

    def detect_honeypot(self, onchain: Dict, token) -> DangerDetection:
        """Honeypot: you can buy but can't sell."""
        if not onchain or not token:
            return DangerDetection("honeypot", False, "low", 0, "No on-chain data")

        # Heuristics
        can_sell = onchain.get("can_sell", True)
        sell_tax = onchain.get("sell_tax_estimate_pct", 0)
        freeze_auth = onchain.get("freeze_authority_enabled", False)

        score = 0
        if not can_sell: score += 50
        if sell_tax > 25: score += 30  # high sell tax
        if freeze_auth: score += 20

        detected = score >= 50
        severity = "critical" if score >= 80 else ("high" if score >= 50 else "low")
        return DangerDetection(
            "honeypot", detected, severity, score / 100,
            "Honeypot: يمكن الشراء لكن لا يمكن البيع",
            {"can_sell": can_sell, "sell_tax": sell_tax, "freeze_auth": freeze_auth},
            "AVOID" if detected else "SAFE"
        )

    def detect_rug_pull(self, onchain: Dict, token) -> DangerDetection:
        """Rug Pull: liquidity removal by devs."""
        if not onchain or not token:
            return DangerDetection("rug_pull", False, "low", 0, "No on-chain data")

        liq_locked = onchain.get("liquidity_locked", False)
        owner_concentration = onchain.get("owner_concentration_risk", 0)
        top_10_pct = onchain.get("top_10_pct", 0)
        mint_auth = onchain.get("mint_authority_enabled", False)

        score = 0
        if not liq_locked: score += 30
        if owner_concentration > 0.7: score += 30
        if top_10_pct > 80: score += 25
        if mint_auth: score += 15

        detected = score >= 50
        severity = "critical" if score >= 80 else ("high" if score >= 50 else "medium")
        return DangerDetection(
            "rug_pull", detected, severity, score / 100,
            "Rug Pull: يمكن للمطورين سحب السيولة",
            {"liq_locked": liq_locked, "owner_conc": owner_concentration,
             "top_10_pct": top_10_pct, "mint_auth": mint_auth},
            "AVOID" if detected else "CAUTION"
        )

    def detect_mint_authority_abuse(self, onchain: Dict) -> DangerDetection:
        """Mint Authority: devs can mint unlimited tokens."""
        if not onchain:
            return DangerDetection("mint_authority_abuse", False)
        mint_enabled = onchain.get("mint_authority_enabled", False)
        detected = mint_enabled
        return DangerDetection(
            "mint_authority_abuse", detected,
            "critical" if detected else "low",
            1.0 if detected else 0,
            "Mint Authority مفعّل - يمكن إنشاء عملات جديدة",
            {"mint_authority_enabled": mint_enabled},
            "AVOID" if detected else "SAFE"
        )

    def detect_freeze_authority_abuse(self, onchain: Dict) -> DangerDetection:
        """Freeze Authority: devs can freeze your wallet."""
        if not onchain:
            return DangerDetection("freeze_authority_abuse", False)
        freeze_enabled = onchain.get("freeze_authority_enabled", False)
        detected = freeze_enabled
        return DangerDetection(
            "freeze_authority_abuse", detected,
            "critical" if detected else "low",
            1.0 if detected else 0,
            "Freeze Authority مفعّل - يمكن تجميد محفظتك",
            {"freeze_authority_enabled": freeze_enabled},
            "AVOID" if detected else "SAFE"
        )

    def detect_token_bundling(self, onchain: Dict) -> DangerDetection:
        """Token Bundling: multiple wallets holding similar percentages."""
        if not onchain:
            return DangerDetection("token_bundling", False)
        bundle_prob = onchain.get("token_bundling_probability", 0)
        detected = bundle_prob > 0.5
        return DangerDetection(
            "token_bundling", detected,
            "high" if detected else "low",
            bundle_prob,
            "نمط Token Bundling - محافظ متعددة بنسب متشابهة",
            {"bundle_prob": bundle_prob},
            "AVOID" if detected else "CAUTION"
        )

    def detect_liquidity_migration(self, onchain: Dict) -> DangerDetection:
        """Liquidity Migration: dev moves LP tokens."""
        if not onchain:
            return DangerDetection("liquidity_migration", False)
        liq_removed_recently = onchain.get("liquidity_removed_recently", False)
        detected = liq_removed_recently
        return DangerDetection(
            "liquidity_migration", detected,
            "critical" if detected else "low",
            1.0 if detected else 0,
            "تم سحب السيولة مؤخراً",
            {"liq_removed_recently": liq_removed_recently},
            "AVOID" if detected else "SAFE"
        )

    def detect_smart_contract_exploit(self, onchain: Dict) -> DangerDetection:
        """Smart Contract Exploit: known vulnerable patterns."""
        if not onchain:
            return DangerDetection("smart_contract_exploit", False)
        similar_to_scam = onchain.get("contract_similar_to_known_scam", False)
        dangerous_changes = onchain.get("dangerous_contract_changes", False)
        detected = similar_to_scam or dangerous_changes
        return DangerDetection(
            "smart_contract_exploit", detected,
            "critical" if detected else "low",
            1.0 if detected else 0,
            "العقد مشابه لعقود احتيالية معروفة",
            {"similar_to_scam": similar_to_scam, "dangerous_changes": dangerous_changes},
            "AVOID" if detected else "SAFE"
        )

    # =====================================================================
    # V511 — Single-wallet concentration
    # =====================================================================

    def detect_single_wallet_concentration(self, onchain: Dict) -> DangerDetection:
        """Single wallet owning a large fraction of supply = guaranteed dump risk.

        Thresholds (configurable via settings.max_single_holder_pct, default 30):
          - >= 30%  → critical, AVOID (hard-block in SecurityScanner & DecisionEngine)
          - >= 20%  → high, AVOID
          - >= 10%  → medium, CAUTION
          - <  10%  → low, SAFE
        """
        if not onchain:
            return DangerDetection("single_wallet_concentration", False)

        top_holder_pct = float(onchain.get("top_holder_pct", 0) or 0)
        top_holder_address = onchain.get("top_holder_address", "")
        holders_over_30 = int(onchain.get("holders_over_30pct", 0) or 0)
        holders_over_20 = int(onchain.get("holders_over_20pct", 0) or 0)
        holders_over_10 = int(onchain.get("holders_over_10pct", 0) or 0)

        # Default thresholds (kept in sync with settings.max_single_holder_pct)
        try:
            from ...core.config import settings as _settings
            crit_threshold = float(getattr(_settings, "max_single_holder_pct", 30.0) or 30.0)
        except Exception:
            crit_threshold = 30.0

        if top_holder_pct >= crit_threshold:
            severity = "critical"
            detected = True
            # عند مستوى الحظر: الثقة عالية جداً (ليس هناك شك في الخطر)
            confidence = min(1.0, 0.6 + (top_holder_pct - crit_threshold) / 100.0 + 0.35)
            recommendation = "AVOID"
            desc = (
                f"🔴🔴 محفظة واحدة تملك {top_holder_pct:.2f}% من العرض "
                f"({(top_holder_address or 'unknown')[:10]}...) — "
                f"يمكنها dump كامل في صفقة واحدة"
            )
        elif top_holder_pct >= 20.0:
            severity = "high"
            detected = True
            confidence = 0.85
            recommendation = "AVOID"
            desc = f"محفظة واحدة تملك {top_holder_pct:.2f}% — مخاطرة dump عالية"
        elif top_holder_pct >= 10.0:
            severity = "medium"
            detected = True
            confidence = 0.6
            recommendation = "CAUTION"
            desc = f"محفظة واحدة تملك {top_holder_pct:.2f}% — رصد مستمر مطلوب"
        else:
            severity = "low"
            detected = False
            confidence = 0.2
            recommendation = "SAFE"
            desc = f"أكبر محفظة تملك {top_holder_pct:.2f}% — توزيع صحي"

        return DangerDetection(
            "single_wallet_concentration",
            detected,
            severity,
            confidence,
            desc,
            {
                "top_holder_pct": top_holder_pct,
                "top_holder_address": top_holder_address,
                "holders_over_30pct": holders_over_30,
                "holders_over_20pct": holders_over_20,
                "holders_over_10pct": holders_over_10,
                "critical_threshold_pct": crit_threshold,
            },
            recommendation
        )

    # =====================================================================
    # Volume/Trading dangers
    # =====================================================================

    def detect_wash_trading(self, df: pd.DataFrame, token=None) -> DangerDetection:
        """Wash Trading: same wallet buying and selling to fake volume."""
        if df is None or len(df) < 20:
            return DangerDetection("wash_trading", False)
        vols = df["volume"].astype(float).tail(20)
        cv = vols.std() / max(vols.mean(), 1e-9)  # coefficient of variation
        # Suspiciously uniform volume = wash trading
        detected = cv < 0.15
        confidence = max(0, 1 - cv / 0.15) if cv < 0.15 else 0
        return DangerDetection(
            "wash_trading", detected,
            "high" if detected else "low",
            confidence,
            f"حجم تداول منتظم بشكل مريب (CV={cv:.3f})",
            {"cv": cv, "avg_vol": float(vols.mean())},
            "AVOID" if detected else "MONITOR"
        )

    def detect_fake_volume(self, df: pd.DataFrame, token=None) -> DangerDetection:
        """Fake Volume: high volume but no price movement."""
        if df is None or len(df) < 30 or token is None:
            return DangerDetection("fake_volume", False)
        recent = df.tail(30)
        vol = float(recent["volume"].mean())
        price_range = (recent["high"].max() - recent["low"].min()) / recent["close"].mean()
        # High volume but low price movement = fake
        vol_to_movement_ratio = vol / max(price_range, 1e-9)
        detected = vol_to_movement_ratio > 100000 and price_range < 0.05
        return DangerDetection(
            "fake_volume", detected,
            "medium" if detected else "low",
            0.7 if detected else 0,
            "حجم مرتفع بدون حركة سعر حقيقية",
            {"vol": vol, "price_range": price_range},
            "CAUTION" if detected else "SAFE"
        )

    def detect_spoofing(self, df: pd.DataFrame) -> DangerDetection:
        """Spoofing: large orders placed then cancelled."""
        # Can't detect without order book data, but can infer from volume spikes
        # followed by immediate price reversals
        if df is None or len(df) < 20:
            return DangerDetection("spoofing", False)
        vols = df["volume"].astype(float).tail(20)
        avg_vol = vols.mean()
        spikes = (vols > avg_vol * 3).sum()
        # Many volume spikes without sustained price movement = spoofing
        detected = spikes >= 3
        return DangerDetection(
            "spoofing", detected,
            "medium" if detected else "low",
            min(spikes / 5, 1.0),
            f"{spikes} ارتفاعات حجم مشبوهة في آخر 20 شمعة",
            {"spikes": spikes, "avg_vol": avg_vol},
            "CAUTION" if detected else "SAFE"
        )

    def detect_layering(self, df: pd.DataFrame) -> DangerDetection:
        """Layering: multiple fake orders at different price levels."""
        if df is None or len(df) < 20:
            return DangerDetection("layering", False)
        # Detect: high volume but price stays in tight range
        recent = df.tail(20)
        price_range_pct = (recent["high"].max() - recent["low"].min()) / recent["close"].mean()
        avg_vol = recent["volume"].mean()
        # High vol + tight range = layering
        detected = price_range_pct < 0.02 and avg_vol > 50000
        return DangerDetection(
            "layering", detected,
            "medium" if detected else "low",
            0.6 if detected else 0,
            f"نطاق سعري ضيق ({price_range_pct:.3f}) مع حجم مرتفع",
            {"price_range_pct": price_range_pct, "avg_vol": avg_vol},
            "CAUTION" if detected else "SAFE"
        )

    def detect_front_running(self, df: pd.DataFrame) -> DangerDetection:
        """Front Running: bots buying before large orders."""
        if df is None or len(df) < 30:
            return DangerDetection("front_running", False)
        # Detect: sudden price spike + immediate reversal
        recent = df.tail(10)
        max_gain = (recent["high"].max() - recent["open"].iloc[0]) / recent["open"].iloc[0]
        final_change = (recent["close"].iloc[-1] - recent["open"].iloc[0]) / recent["open"].iloc[0]
        # Spike then reversal = front running
        detected = max_gain > 0.05 and final_change < 0
        return DangerDetection(
            "front_running", detected,
            "medium" if detected else "low",
            0.7 if detected else 0,
            f"ارتفاع {max_gain:.1%} ثم انعكاس",
            {"max_gain": max_gain, "final_change": final_change},
            "CAUTION" if detected else "SAFE"
        )

    def detect_mev_sandwich(self, df: pd.DataFrame) -> DangerDetection:
        """MEV Sandwich: bot buys before you, sells after."""
        if df is None or len(df) < 10:
            return DangerDetection("mev_sandwich", False)
        # Detect: rapid price spike (1-2 candles) then immediate drop
        recent = df.tail(5)
        spike = (recent["high"].max() - recent["open"].iloc[0]) / recent["open"].iloc[0]
        drop = (recent["high"].max() - recent["close"].iloc[-1]) / recent["high"].max()
        detected = spike > 0.03 and drop > 0.05  # Ultimate-bot: was 0.5 (50%!), should be 5%
        return DangerDetection(
            "mev_sandwich", detected,
            "high" if detected else "low",
            0.8 if detected else 0,
            f"هجوم MEV: ارتفاع {spike:.1%} ثم انخفاض {drop:.1%}",
            {"spike": spike, "drop": drop},
            "AVOID" if detected else "MONITOR"
        )

    # =====================================================================
    # Price action dangers
    # =====================================================================

    def detect_pump_and_dump(self, df: pd.DataFrame) -> DangerDetection:
        """Pump and Dump: rapid rise then crash."""
        if df is None or len(df) < 50:
            return DangerDetection("pump_and_dump", False)
        recent = df.tail(50)
        prices = recent["close"].astype(float)
        max_price = prices.max()
        current_price = prices.iloc[-1]
        max_idx = prices.values.tolist().index(max_price)
        # If peak was in middle and we're now 30%+ below
        drop_from_peak = (max_price - current_price) / max_price
        detected = drop_from_peak > 0.30 and 10 < max_idx < 40
        return DangerDetection(
            "pump_and_dump", detected,
            "high" if detected else "low",
            drop_from_peak if detected else 0,
            f"هبوط {drop_from_peak:.1%} من القمة",
            {"drop_from_peak": drop_from_peak, "peak_position": max_idx},
            "AVOID" if detected else "MONITOR"
        )

    def detect_blow_off_top(self, df: pd.DataFrame) -> DangerDetection:
        """Blow-off Top: parabolic rise then immediate reversal."""
        if df is None or len(df) < 30:
            return DangerDetection("blow_off_top", False)
        recent = df.tail(30)
        prices = recent["close"].astype(float)
        # Parabolic: price increased 100%+ then dropped 20%+ from peak
        total_change = (prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0]
        max_price = prices.max()
        drop_from_peak = (max_price - prices.iloc[-1]) / max_price
        detected = total_change > 1.0 and drop_from_peak > 0.20
        return DangerDetection(
            "blow_off_top", detected,
            "high" if detected else "low",
            0.8 if detected else 0,
            f"صعود Parabolic ({total_change:.0%}) ثم هبوط {drop_from_peak:.0%}",
            {"total_change": total_change, "drop_from_peak": drop_from_peak},
            "AVOID" if detected else "CAUTION"
        )

    def detect_capitulation(self, df: pd.DataFrame) -> DangerDetection:
        """Capitulation: panic selling with high volume."""
        if df is None or len(df) < 20:
            return DangerDetection("capitulation", False)
        recent = df.tail(20)
        price_change = (recent["close"].iloc[-1] - recent["open"].iloc[0]) / recent["open"].iloc[0]
        vol = recent["volume"].astype(float)
        vol_spike = vol.iloc[-5:].mean() / max(vol.iloc[-20:-5].mean(), 1e-9)
        detected = price_change < -0.20 and vol_spike > 2
        return DangerDetection(
            "capitulation", detected,
            "high" if detected else "low",
            0.7 if detected else 0,
            f"بيع ذعرّي: {price_change:.1%} مع حجم {vol_spike:.1f}x",
            {"price_change": price_change, "vol_spike": vol_spike},
            "AVOID" if detected else "CAUTION"
        )

    def detect_dead_cat_bounce(self, df: pd.DataFrame) -> DangerDetection:
        """Dead Cat Bounce: brief recovery in downtrend."""
        if df is None or len(df) < 100:
            return DangerDetection("dead_cat_bounce", False)
        # Long-term downtrend + brief recovery
        long_trend = (df["close"].iloc[-1] - df["close"].iloc[-100]) / df["close"].iloc[-100]
        short_trend = (df["close"].iloc[-1] - df["close"].iloc[-10]) / df["close"].iloc[-10]
        detected = long_trend < -0.20 and short_trend > 0.05
        return DangerDetection(
            "dead_cat_bounce", detected,
            "medium" if detected else "low",
            0.6 if detected else 0,
            f"اتجاه هابط طويل ({long_trend:.1%}) مع ارتفاع مؤقت ({short_trend:.1%})",
            {"long_trend": long_trend, "short_trend": short_trend},
            "CAUTION" if detected else "SAFE"
        )

    def detect_bear_trap(self, df: pd.DataFrame) -> DangerDetection:
        """Bear Trap: price drops then immediately reverses up."""
        if df is None or len(df) < 10:
            return DangerDetection("bear_trap", False)
        recent = df.tail(10)
        # Quick drop then recovery
        min_price = recent["low"].min()
        current = recent["close"].iloc[-1]
        drop = (recent["open"].iloc[0] - min_price) / recent["open"].iloc[0]
        recovery = (current - min_price) / min_price
        detected = drop > 0.05 and recovery > 0.05  # Ultimate-bot: was 0.5 (50%!), should be 5%
        return DangerDetection(
            "bear_trap", detected,
            "medium" if detected else "low",
            0.6 if detected else 0,
            f"فخ هابط: انخفاض {drop:.1%} ثم تعافي {recovery:.1%}",
            {"drop": drop, "recovery": recovery},
            "MONITOR" if detected else "SAFE"
        )

    def detect_bull_trap(self, df: pd.DataFrame) -> DangerDetection:
        """Bull Trap: price spikes then immediately reverses down."""
        if df is None or len(df) < 10:
            return DangerDetection("bull_trap", False)
        recent = df.tail(10)
        max_price = recent["high"].max()
        current = recent["close"].iloc[-1]
        spike = (max_price - recent["open"].iloc[0]) / recent["open"].iloc[0]
        drop = (max_price - current) / max_price
        detected = spike > 0.05 and drop > 0.05  # Ultimate-bot: was 0.5 (50%!), should be 5%
        return DangerDetection(
            "bull_trap", detected,
            "high" if detected else "low",
            0.7 if detected else 0,
            f"فخ صاعد: ارتفاع {spike:.1%} ثم هبوط {drop:.1%}",
            {"spike": spike, "drop": drop},
            "AVOID" if detected else "CAUTION"
        )

    def detect_stop_hunting(self, df: pd.DataFrame) -> DangerDetection:
        """Stop Hunting: price spikes to trigger stops then reverses."""
        if df is None or len(df) < 30:
            return DangerDetection("stop_hunting", False)
        recent = df.tail(30)
        # Detect: wick below recent low, then close back above
        recent_low = recent["low"].iloc[:-1].min()
        last_low = recent["low"].iloc[-1]
        last_close = recent["close"].iloc[-1]
        # Wick below support then close above
        detected = last_low < recent_low * 0.98 and last_close > recent_low
        return DangerDetection(
            "stop_hunting", detected,
            "medium" if detected else "low",
            0.6 if detected else 0,
            "صيد أوامر الإيقاف: اختراق أسفل الدعم ثم إغلاق فوقه",
            {"recent_low": recent_low, "last_low": last_low, "last_close": last_close},
            "CAUTION" if detected else "SAFE"
        )

    def detect_fake_breakout(self, df: pd.DataFrame) -> DangerDetection:
        """Fake Breakout: price breaks resistance then immediately reverses."""
        if df is None or len(df) < 30:
            return DangerDetection("fake_breakout", False)
        recent = df.tail(30)
        resistance = recent["high"].iloc[:-5].max()
        # Check if we broke resistance in last 5 candles but closed below
        last_5 = recent.tail(5)
        broke = (last_5["high"] > resistance).any()
        closed_below = last_5["close"].iloc[-1] < resistance
        detected = broke and closed_below
        return DangerDetection(
            "fake_breakout", detected,
            "high" if detected else "low",
            0.8 if detected else 0,
            "اختراق وهمي: تجاوز المقاومة ثم إغلاق دونها",
            {"resistance": resistance, "broke": broke, "closed_below": closed_below},
            "AVOID" if detected else "CAUTION"
        )

    # =====================================================================
    # Liquidity dangers
    # =====================================================================

    def detect_low_liquidity_trap(self, token) -> DangerDetection:
        """Low Liquidity Trap: high slippage expected."""
        if not token:
            return DangerDetection("low_liquidity_trap", False)
        liq = float(token.liquidity_usd or 0) if hasattr(token, 'liquidity_usd') else 0
        if liq == 0:
            return DangerDetection("low_liquidity_trap", False)
        detected = liq < 50000
        severity = "critical" if liq < 10000 else ("high" if liq < 30000 else "medium")
        return DangerDetection(
            "low_liquidity_trap", detected,
            severity if detected else "low",
            1 - (liq / 50000) if detected else 0,
            f"سيولة منخفضة (${liq:,.0f}) - انزلاق سعري متوقع",
            {"liquidity_usd": liq},
            "AVOID" if detected else "SAFE"
        )

    def detect_slippage_trap(self, token) -> DangerDetection:
        """Slippage Trap: trade size vs liquidity ratio bad."""
        if not token:
            return DangerDetection("slippage_trap", False)
        liq = float(token.liquidity_usd or 0) if hasattr(token, 'liquidity_usd') else 0
        vol_24h = float(token.volume_24h_usd or 0) if hasattr(token, 'volume_24h_usd') else 0
        if liq == 0:
            return DangerDetection("slippage_trap", False)
        # Estimate slippage for $1000 trade
        slippage_pct = 200 * 1000 / liq  # rough estimate
        detected = slippage_pct > 2
        return DangerDetection(
            "slippage_trap", detected,
            "high" if slippage_pct > 5 else ("medium" if detected else "low"),
            min(slippage_pct / 10, 1.0),
            f"انزلاق سعري متوقع {slippage_pct:.2f}% لصفقة $1000",
            {"slippage_pct": slippage_pct, "liquidity": liq},
            "AVOID" if detected else "CAUTION"
        )

    def detect_whale_manipulation(self, onchain: Dict, df: pd.DataFrame) -> DangerDetection:
        """Whale Manipulation: large holders can move price."""
        if not onchain:
            return DangerDetection("whale_manipulation", False)
        top_10_pct = onchain.get("top_10_pct", 0)
        whale_concentration = onchain.get("whale_concentration", "low")
        detected = top_10_pct > 60 or whale_concentration == "high"
        severity = "high" if top_10_pct > 80 else ("medium" if detected else "low")
        return DangerDetection(
            "whale_manipulation", detected,
            severity,
            top_10_pct / 100 if detected else 0,
            f"تركيز الحيازة: top 10 = {top_10_pct:.1f}%",
            {"top_10_pct": top_10_pct, "whale_concentration": whale_concentration},
            "AVOID" if detected else "CAUTION"
        )

    # =====================================================================
    # Insider dangers
    # =====================================================================

    def detect_insider_trading(self, onchain: Dict) -> DangerDetection:
        """Insider Trading: wallets bought very early."""
        if not onchain:
            return DangerDetection("insider_trading", False)
        insider_prob = onchain.get("insider_activity_probability", 0)
        early_wallet = onchain.get("early_wallet_activity", False)
        detected = insider_prob > 0.5 or early_wallet
        return DangerDetection(
            "insider_trading", detected,
            "high" if detected else "low",
            insider_prob,
            "نشاط insider محتمل - محافظ اشترت مبكراً",
            {"insider_prob": insider_prob, "early_wallet": early_wallet},
            "AVOID" if detected else "CAUTION"
        )

    def detect_flash_loan_attack(self, df: pd.DataFrame, onchain: Dict) -> DangerDetection:
        """Flash Loan Attack: borrowed funds used to manipulate price."""
        if df is None or len(df) < 5:
            return DangerDetection("flash_loan_attack", False)
        # Detect: massive volume spike in 1-2 candles with extreme price movement
        recent = df.tail(5)
        vols = recent["volume"].astype(float)
        avg_vol = vols.mean()
        max_vol = vols.max()
        price_change = abs((recent["close"].iloc[-1] - recent["close"].iloc[0]) / recent["close"].iloc[0])
        detected = max_vol > avg_vol * 5 and price_change > 0.20
        return DangerDetection(
            "flash_loan_attack", detected,
            "critical" if detected else "low",
            0.9 if detected else 0,
            f"هجوم Flash Loan محتمل: حجم {max_vol/avg_vol:.1f}x وحركة {price_change:.1%}",
            {"max_vol": max_vol, "avg_vol": avg_vol, "price_change": price_change},
            "AVOID" if detected else "MONITOR"
        )


# Convenience function to get summary
def get_danger_summary(detections: List[DangerDetection]) -> Dict:
    """Get summary of all detected dangers."""
    detected = [d for d in detections if d.detected]
    critical = [d for d in detected if d.severity == "critical"]
    high = [d for d in detected if d.severity == "high"]
    medium = [d for d in detected if d.severity == "medium"]

    # Overall recommendation
    if critical:
        recommendation = "AVOID"
    elif len(high) >= 2:
        recommendation = "AVOID"
    elif high or len(medium) >= 3:
        recommendation = "CAUTION"
    elif medium:
        recommendation = "MONITOR"
    else:
        recommendation = "SAFE"

    return {
        "total_dangers": len(detected),
        "critical_count": len(critical),
        "high_count": len(high),
        "medium_count": len(medium),
        "detected_patterns": [d.pattern_name for d in detected],
        "critical_patterns": [d.pattern_name for d in critical],
        "recommendation": recommendation,
        "should_avoid": recommendation == "AVOID",
    }
