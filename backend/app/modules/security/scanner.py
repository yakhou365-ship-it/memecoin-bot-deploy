"""
Security & Rug Pull Detection engine.

Combines on-chain flags with liquidity/contract heuristics to produce:
- Risk Score (0-100, higher = more dangerous)
- Critical flags (any critical flag → auto-disqualify)
- Honeypot probability
- Rug pull probability
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class SecurityResult:
    honeypot_probability: float = 0.0
    rug_pull_probability: float = 0.0
    sell_tax_estimate_pct: float = 0.0  # if derivable
    can_sell: Optional[bool] = None  # if we can verify sell function works
    mint_authority_enabled: bool = False
    freeze_authority_enabled: bool = False
    liquidity_locked: Optional[bool] = None
    liquidity_removal_risk: float = 0.0
    owner_concentration_risk: float = 0.0
    suspicious_wallets: int = 0
    contract_similar_to_known_scam: bool = False
    abnormal_volume_detected: bool = False
    dangerous_contract_changes: bool = False
    sudden_dump_detected: bool = False

    critical_flags: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    risk_score: int = 50  # 0-100, higher = more dangerous
    auto_disqualify: bool = False
    disqualify_reason: str = ""

    security_score: float = 50.0  # 0-100, higher = safer
    reasons: List[str] = field(default_factory=list)
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class SecurityScanner:
    """Main security analyzer."""

    def analyze(
        self,
        token,
        onchain_result=None,
        liquidity_result=None,
    ) -> SecurityResult:
        result = SecurityResult()
        result.data_sufficient = True

        # Inherit from on-chain findings
        if onchain_result and onchain_result.data_sufficient:
            result.mint_authority_enabled = onchain_result.mint_authority_enabled
            result.freeze_authority_enabled = onchain_result.freeze_authority_enabled
            result.liquidity_locked = onchain_result.liquidity_locked
            if onchain_result.top_10_pct > 80:
                result.owner_concentration_risk = 0.9
            elif onchain_result.top_10_pct > 60:
                result.owner_concentration_risk = 0.6
            elif onchain_result.top_10_pct > 40:
                result.owner_concentration_risk = 0.3
            result.suspicious_wallets = onchain_result.suspicious_wallet_count

        # Liquidity-based signals
        if liquidity_result and liquidity_result.data_sufficient:
            if liquidity_result.wash_trading_probability > 0.5:
                result.abnormal_volume_detected = True
                result.warnings.append("حجم تداول غير طبيعي - احتمال wash trading")
            if liquidity_result.fake_liquidity_probability > 0.5:
                result.liquidity_removal_risk = 0.7
                result.warnings.append("احتمال سيولة وهمية")
            if (token.price_change_24h_pct or 0) < -50:
                result.sudden_dump_detected = True
                result.warnings.append(f"هبوط حاد {token.price_change_24h_pct:.0f}% خلال 24 ساعة")

        # Honeypot heuristics (very rough - real honeypot detection needs simulation)
        honeypot_signals = 0
        if result.freeze_authority_enabled:
            honeypot_signals += 1  # could freeze buyers
        if result.mint_authority_enabled:
            honeypot_signals += 1  # could inflate supply
        if (token.liquidity_usd or 0) < 5000:
            honeypot_signals += 1
        if result.owner_concentration_risk > 0.8:
            honeypot_signals += 1
        result.honeypot_probability = min(honeypot_signals / 4.0, 1.0)

        # Rug pull probability
        rug_signals = 0
        if result.mint_authority_enabled:
            rug_signals += 2  # double weight
        if not result.liquidity_locked:
            rug_signals += 1
        if result.owner_concentration_risk > 0.7:
            rug_signals += 1
        if (token.liquidity_usd or 0) < 20_000:
            rug_signals += 1
        if result.sudden_dump_detected:
            rug_signals += 2
        result.rug_pull_probability = min(rug_signals / 6.0, 1.0)

        # Determine critical flags
        if result.mint_authority_enabled:
            result.critical_flags.append("MINT_AUTHORITY_ENABLED")
        if result.freeze_authority_enabled:
            result.critical_flags.append("FREEZE_AUTHORITY_ENABLED")
        if result.honeypot_probability > 0.7:
            result.critical_flags.append("HONEYPOT_HIGH_RISK")
        if result.rug_pull_probability > 0.7:
            result.critical_flags.append("RUG_PULL_HIGH_RISK")
        if result.owner_concentration_risk > 0.9:
            result.critical_flags.append("EXTREME_OWNER_CONCENTRATION")
        if result.suspicious_wallets >= 5:
            result.critical_flags.append("MULTIPLE_SUSPICIOUS_WALLETS")

        # Auto-disqualify on any critical flag
        if result.critical_flags:
            result.auto_disqualify = True
            result.disqualify_reason = " / ".join(result.critical_flags)

        # Score
        self._score(result, token)
        return result

    def _score(self, r: SecurityResult, token) -> None:
        risk = 30  # start neutral-low
        reasons: List[str] = []

        # Honeypot
        if r.honeypot_probability > 0.7:
            risk += 30
            reasons.append("احتمال Honeypot عالٍ جداً")
        elif r.honeypot_probability > 0.4:
            risk += 15
            reasons.append("احتمال Honeypot متوسط")
        elif r.honeypot_probability < 0.2:
            reasons.append("احتمال Honeypot منخفض")

        # Rug pull
        if r.rug_pull_probability > 0.7:
            risk += 35
            reasons.append("احتمال Rug Pull عالٍ جداً")
        elif r.rug_pull_probability > 0.4:
            risk += 18
            reasons.append("احتمال Rug Pull متوسط")
        elif r.rug_pull_probability < 0.2:
            reasons.append("احتمال Rug Pull منخفض")

        # Owner concentration
        if r.owner_concentration_risk > 0.8:
            risk += 20
            reasons.append("تركز ملكية خطر")
        elif r.owner_concentration_risk > 0.5:
            risk += 10

        # Liquidity lock status
        if r.liquidity_locked is False:
            risk += 15
            reasons.append("السيولة غير مقفلة - يمكن سحبها")

        # Volume anomalies
        if r.abnormal_volume_detected:
            risk += 8
        if r.liquidity_removal_risk > 0.5:
            risk += 10

        # Sudden dump
        if r.sudden_dump_detected:
            risk += 12

        # Suspicious wallets
        if r.suspicious_wallets > 5:
            risk += 8

        # Liquidity tier (low liquidity = higher risk)
        liq = token.liquidity_usd or 0
        if liq < 5000:
            risk += 15
            reasons.append("سيولة منخفضة جداً - مخاطرة عالية")
        elif liq < 20_000:
            risk += 8
        elif liq >= 100_000:
            risk -= 5

        # Age of token (younger = riskier) - using first_seen_at
        # Could add: if token.age < 1 hour → risk += 15
        # Done at scanner level instead

        # Cap to 0-100
        r.risk_score = max(0, min(100, int(risk)))
        # Security score is inverse (safer = higher)
        r.security_score = 100 - r.risk_score
        r.reasons = reasons
