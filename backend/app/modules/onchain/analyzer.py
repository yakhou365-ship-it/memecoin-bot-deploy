"""
On-chain analysis engine.

Combines Solana RPC data with market data to assess:
- Holder distribution
- Whale concentration
- Insider / bundling indicators
- Token authority status (mint / freeze)
- Liquidity lock status (when derivable)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from ...core.config import settings
from ...core.logger import logger
from .solana_rpc import SolanaRPC


@dataclass
class OnChainResult:
    chain: str = "solana"
    total_holders: int = 0
    top_10_pct: float = 0.0  # percentage held by top 10 wallets
    top_20_pct: float = 0.0
    whale_activity_detected: bool = False
    whale_concentration: str = "low"  # low | moderate | high | extreme
    early_wallet_activity: bool = False  # wallets that bought very early
    insider_activity_probability: float = 0.0
    token_bundling_probability: float = 0.0
    mint_authority_enabled: bool = False
    freeze_authority_enabled: bool = False
    liquidity_locked: Optional[bool] = None  # True if we can verify lock
    recent_buys_count: int = 0
    recent_sells_count: int = 0
    suspicious_wallet_count: int = 0
    related_wallet_clusters: int = 0

    onchain_score: float = 50.0
    reasons: List[str] = field(default_factory=list)
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class OnChainAnalyzer:
    """Main on-chain analyzer."""

    def __init__(self):
        self.rpc = SolanaRPC()

    async def analyze(self, token) -> OnChainResult:
        result = OnChainResult(chain=token.chain)

        if token.chain == "solana":
            await self._analyze_solana(token, result)
        else:
            # TODO: implement for other chains
            result.data_sufficient = False
            result.reasons.append(f"تحليل on-chain لشبكة {token.chain} غير مدعوم بعد")
            return result

        if not result.data_sufficient:
            return result

        self._score(result)
        return result

    async def _analyze_solana(self, token, result: OnChainResult) -> None:
        try:
            # Get largest holders
            largest = await self.rpc.get_token_largest_accounts(token.address)
            supply_info = await self.rpc.get_token_supply(token.address)

            if not largest or not supply_info:
                result.data_sufficient = False
                result.reasons.append("تعذر جلب بيانات حاملي العملة من RPC")
                return

            supply_raw = supply_info.get("value", {}).get("amount", "0")
            supply = float(supply_raw)
            if supply <= 0:
                result.data_sufficient = False
                return

            result.data_sufficient = True
            result.total_holders = len(largest)

            # Top 10 / 20 concentration
            amounts = []
            for holder in largest[:20]:
                amt = float(holder.get("amount", {}).get("amount", "0"))
                amounts.append(amt)

            if amounts:
                result.top_10_pct = (sum(amounts[:10]) / supply) * 100
                result.top_20_pct = (sum(amounts[:20]) / supply) * 100

            # Whale concentration classification
            if result.top_10_pct > 80:
                result.whale_concentration = "extreme"
                result.reasons.append(f"تركيز شديد: أعلى 10 محافظ تملك {result.top_10_pct:.1f}%")
            elif result.top_10_pct > 60:
                result.whale_concentration = "high"
                result.reasons.append(f"تركيز عالٍ: أعلى 10 محافظ تملك {result.top_10_pct:.1f}%")
            elif result.top_10_pct > 40:
                result.whale_concentration = "moderate"
            else:
                result.whale_concentration = "low"
                result.reasons.append(f"توزيع جيد: أعلى 10 محافظ تملك {result.top_10_pct:.1f}%")

            # Detect very large single wallet (>20%)
            for amt in amounts[:5]:
                pct = (amt / supply) * 100
                if pct > 20:
                    result.whale_activity_detected = True
                    result.reasons.append(f"محفظة واحدة تملك {pct:.1f}% - مخاطرة dump")
                    break

            # Token bundling: top 5 wallets each holding 5-15% (typical bundle pattern)
            top5_pcts = [(amt / supply) * 100 for amt in amounts[:5]]
            bundle_like = sum(1 for p in top5_pcts if 5 <= p <= 20)
            if bundle_like >= 4:
                result.token_bundling_probability = min(bundle_like / 5.0 + 0.2, 1.0)
                result.reasons.append("نمط يشبه Token Bundling بين أكبر 5 محافظ")

            # Insider activity: very recent wallets with very large holdings
            if amounts and (amounts[0] / supply) > 0.10:
                result.insider_activity_probability = 0.4

            # Ultimate-bot: Mint authority check (SPL layout)
            try:
                account_info = await self.rpc.get_account_info(token.address)
                if account_info and account_info.get("data"):
                    b64data = account_info["data"][0]
                    raw = base64_decode_safe(b64data)
                    if raw and len(raw) >= 46:
                        if int.from_bytes(raw[0:4], "little") != 0:
                            result.mint_authority_enabled = True
                            result.reasons.append("⚠️ Mint Authority مفعّل - يمكن إنشاء عملات جديدة!")
                        if len(raw) >= 50 and int.from_bytes(raw[46:50], "little") != 0:
                            result.freeze_authority_enabled = True
                            result.reasons.append("⚠️ Freeze Authority مفعّل - يمكن تجميد الحسابات!")
            except Exception as exc:
                logger.debug(f"Token account parse failed for {token.symbol}: {exc}")

            # Ultimate-bot: Tradability check (honeypot detection)
            try:
                tradable, tradable_reason = await self.rpc.check_token_tradable(token.address)
                if not tradable:
                    result.honeypot_probability = max(result.honeypot_probability, 0.8)
                    result.reasons.append(f"🔴 غير قابل للتداول: {tradable_reason}")
                    logger.warning(f"Honeypot suspected for {token.symbol}: {tradable_reason}")
            except Exception as exc:
                logger.debug(f"Tradability check failed for {token.symbol}: {exc}")

            # Ultimate-bot: Token age check
            try:
                age_seconds = await self.rpc.get_token_age_seconds(token.address)
                if age_seconds is not None:
                    if age_seconds < 60:  # less than 1 minute old
                        result.insider_activity_probability = max(
                            result.insider_activity_probability, 0.7
                        )
                        result.reasons.append(f"🔴 توكن جديد جداً (عمره {age_seconds}s) - مخاطرة عالية")
                    elif age_seconds < 300:  # less than 5 minutes
                        result.insider_activity_probability = max(
                            result.insider_activity_probability, 0.3
                        )
                        result.reasons.append(f"⚠️ توكن حديث (عمره {age_seconds//60}min)")
            except Exception as exc:
                logger.debug(f"Token age check failed for {token.symbol}: {exc}")

            # Ultimate-bot: LP lock check
            try:
                lp_locked = await self.rpc.check_lp_locked(token.address)
                if lp_locked is True:
                    result.liquidity_locked = True
                    result.reasons.append("✅ السيولة مقفلة (LP Locked)")
                elif lp_locked is False:
                    result.liquidity_locked = False
                    result.reasons.append("⚠️ السيولة غير مقفلة - مخاطرة سحب")
            except Exception as exc:
                logger.debug(f"LP lock check failed for {token.symbol}: {exc}")

        except Exception as exc:
            logger.warning(f"On-chain analysis failed for {token.symbol if token else '?'}: {exc}")
            result.data_sufficient = False
            result.reasons.append(f"خطأ في تحليل on-chain: {str(exc)[:100]}")

    def _score(self, r: OnChainResult) -> None:
        score = 50.0

        # Holder concentration (lower is better)
        if r.top_10_pct < 30:
            score += 20
        elif r.top_10_pct < 50:
            score += 10
        elif r.top_10_pct < 70:
            score -= 5
        elif r.top_10_pct < 85:
            score -= 15
        else:
            score -= 30

        # Whale concentration
        if r.whale_concentration == "extreme":
            score -= 20
        elif r.whale_concentration == "high":
            score -= 10
        elif r.whale_concentration == "low":
            score += 10

        # Mint authority (CRITICAL)
        if r.mint_authority_enabled:
            score -= 30
            r.reasons.append("🔴 خطر حرج: Mint Authority مفعّل")

        # Freeze authority (CRITICAL)
        if r.freeze_authority_enabled:
            score -= 30
            r.reasons.append("🔴 خطر حرج: Freeze Authority مفعّل")

        # Bundling
        if r.token_bundling_probability > 0.7:
            score -= 20
            r.reasons.append("نمط bundling واضح")
        elif r.token_bundling_probability > 0.4:
            score -= 10

        # Insider
        if r.insider_activity_probability > 0.5:
            score -= 15

        # Liquidity lock (unknown = neutral)
        if r.liquidity_locked is True:
            score += 10
            r.reasons.append("السيولة مقفلة")
        elif r.liquidity_locked is False:
            score -= 15
            r.reasons.append("⚠️ السيولة غير مقفلة")

        r.onchain_score = max(0, min(100, score))

    async def close(self):
        await self.rpc.close()


def base64_decode_safe(s: str) -> Optional[bytes]:
    try:
        import base64
        return base64.b64decode(s)
    except Exception:
        return None
