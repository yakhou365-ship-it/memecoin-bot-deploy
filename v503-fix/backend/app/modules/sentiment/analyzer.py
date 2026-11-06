"""
Social Sentiment analyzer.

NOTE: Real Twitter/Reddit APIs require paid access. This module:
1. Attempts Reddit API (free, low rate limits) when credentials present
2. Falls back to activity-based heuristic sentiment from market data
3. NEVER fabricates social data

The market-data-derived sentiment is a transparent fallback, clearly marked.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import httpx

from ...core.config import settings
from ...core.logger import logger


@dataclass
class SentimentResult:
    mentions_count: int = 0
    mentions_growth_rate: float = 0.0  # % per hour
    positive_pct: float = 0.0
    negative_pct: float = 0.0
    neutral_pct: float = 0.0
    spread_velocity: float = 0.0  # 0-1
    discussion_quality: float = 0.0  # 0-1
    bot_activity_probability: float = 0.0  # 0-1
    artificial_engagement_probability: float = 0.0
    paid_promotion_signals: int = 0
    negative_news_detected: bool = False
    price_sentiment_divergence: float = 0.0  # +ve = price up but sentiment down

    data_source: str = "none"  # reddit | twitter | market_heuristic | none
    sentiment_score: float = 50.0
    reasons: List[str] = field(default_factory=list)
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class SentimentAnalyzer:
    """Multi-source sentiment analyzer."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)

    async def analyze(self, token) -> SentimentResult:
        result = SentimentResult()

        # Try Reddit first if creds present
        if settings.reddit_client_id and settings.reddit_client_secret:
            try:
                await self._fetch_reddit(token, result)
                if result.data_sufficient:
                    self._score(result)
                    return result
            except Exception as exc:
                logger.debug(f"Reddit sentiment failed for {token.symbol}: {exc}")

        # Fall back to market-derived heuristic sentiment
        await self._market_heuristic_sentiment(token, result)
        self._score(result)
        return result

    async def _fetch_reddit(self, token, result: SentimentResult) -> None:
        """Search Reddit for token mentions (free API, OAuth)."""
        try:
            # Get OAuth token
            auth_url = "https://www.reddit.com/api/v1/access_token"
            auth = httpx.BasicAuth(
                settings.reddit_client_id, settings.reddit_client_secret
            )
            data = {"grant_type": "client_credentials"}
            headers = {"User-Agent": "MemecoinAI/1.0"}
            resp = await self.client.post(
                auth_url, data=data, auth=auth, headers=headers
            )
            if resp.status_code != 200:
                result.data_source = "none"
                return
            token_resp = resp.json()
            access_token = token_resp.get("access_token")
            if not access_token:
                return

            # Search Reddit
            headers["Authorization"] = f"Bearer {access_token}"
            search_url = "https://oauth.reddit.com/search"
            params = {
                "q": f"{token.symbol}",
                "sort": "new",
                "limit": 50,
                "t": "day",
            }
            resp = await self.client.get(search_url, headers=headers, params=params)
            if resp.status_code != 200:
                return

            posts = resp.json().get("data", {}).get("children", [])
            if not posts:
                result.data_source = "reddit"
                result.data_sufficient = True
                result.mentions_count = 0
                return

            result.mentions_count = len(posts)
            result.data_source = "reddit"
            result.data_sufficient = True

            # Rough sentiment heuristic: title-based positive/negative word count
            positive_words = {"moon", "pump", "buy", "bullish", "hold", "gem", "alpha", "rocket", "🚀"}
            negative_words = {"scam", "rug", "dump", "sell", "bearish", "fraud", "rip", "loss"}

            pos = neg = neu = 0
            for post in posts:
                title = post.get("data", {}).get("title", "").lower()
                if any(w in title for w in positive_words):
                    pos += 1
                elif any(w in title for w in negative_words):
                    neg += 1
                else:
                    neu += 1
            total = max(pos + neg + neu, 1)
            result.positive_pct = pos / total
            result.negative_pct = neg / total
            result.neutral_pct = neu / total

            # Bot activity proxy: many posts with very low upvote ratios
            bot_signals = sum(
                1 for p in posts
                if p.get("data", {}).get("upvote_ratio", 1) < 0.3
            )
            result.bot_activity_probability = min(bot_signals / max(len(posts), 1), 1.0)

        except Exception as exc:
            logger.debug(f"Reddit fetch failed: {exc}")
            result.data_source = "none"

    async def _market_heuristic_sentiment(self, token, result: SentimentResult) -> None:
        """
        Fall-back sentiment derived purely from market activity.
        Clearly labeled so we don't pretend to have social data.
        """
        result.data_source = "market_heuristic"
        result.data_sufficient = True
        result.reasons.append(
            "التحليل الاجتماعي مبني على بيانات السوق فقط (لا تتوفر بيانات X/Reddit)"
        )

        buys = token.txns_24h_buys or 0
        sells = token.txns_24h_sells or 1
        chg_5m = token.price_change_5m_pct or 0
        chg_24h = token.price_change_24h_pct or 0
        vol_24h = token.volume_24h_usd or 0

        # Activity proxy
        result.mentions_count = buys + sells

        # Sentiment from price action
        if chg_24h > 20:
            result.positive_pct = 0.6
            result.negative_pct = 0.1
        elif chg_24h > 5:
            result.positive_pct = 0.5
            result.negative_pct = 0.2
        elif chg_24h > -5:
            result.positive_pct = 0.4
            result.negative_pct = 0.3
        elif chg_24h > -20:
            result.positive_pct = 0.2
            result.negative_pct = 0.5
        else:
            result.positive_pct = 0.1
            result.negative_pct = 0.7

        result.neutral_pct = 1 - result.positive_pct - result.negative_pct

        # Spread velocity: how fast is it growing
        if vol_24h > 1_000_000:
            result.spread_velocity = 0.8
        elif vol_24h > 100_000:
            result.spread_velocity = 0.5
        elif vol_24h > 10_000:
            result.spread_velocity = 0.3
        else:
            result.spread_velocity = 0.1

        # Discussion quality (lower for low liquidity)
        liq = token.liquidity_usd or 0
        result.discussion_quality = min(liq / 200_000, 1.0)

        # Price/sentiment divergence (impossible to compute without social, so 0)
        result.price_sentiment_divergence = 0.0

    def _score(self, r: SentimentResult) -> None:
        score = 50.0
        reasons: List[str] = list(r.reasons)

        if r.data_source == "market_heuristic":
            # Lower confidence
            if r.positive_pct > 0.5:
                score += 8
                reasons.append("اتجاه السوق إيجابي")
            elif r.negative_pct > 0.5:
                score -= 8
                reasons.append("اتجاه السوق سلبي")
            if r.spread_velocity > 0.7:
                score += 5
                reasons.append("زخم انتشار قوي")
            return

        # Real social data scoring
        if r.mentions_count < 5:
            score -= 5
            reasons.append("إشارات اجتماعية قليلة")
        elif r.mentions_count > 100:
            score += 10
            reasons.append(f"اهتمام اجتماعي مرتفع ({r.mentions_count} إشارة)")
        else:
            score += 5

        if r.positive_pct > 0.7:
            score += 15
        elif r.positive_pct > 0.5:
            score += 8
        elif r.negative_pct > 0.6:
            score -= 15
            reasons.append("مشاعر سلبية سائدة")

        if r.bot_activity_probability > 0.5:
            score -= 15
            reasons.append("⚠️ نشاط بوت مرتفع")
        elif r.bot_activity_probability > 0.3:
            score -= 5

        if r.negative_news_detected:
            score -= 20
            reasons.append("أخبار سلبية")

        if r.price_sentiment_divergence > 0.5:
            score -= 10
            reasons.append("تناقض: السعر يصعد لكن المشاعر سلبية")

        r.sentiment_score = max(0, min(100, score))
        r.reasons = reasons

    async def close(self):
        await self.client.aclose()
