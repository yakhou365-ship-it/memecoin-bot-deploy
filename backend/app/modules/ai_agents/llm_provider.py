"""
LLM Provider - multi-provider AI with automatic failover.

Priority order (first configured key wins, then falls through on error):
1. NVIDIA NIM            (NVIDIA_API_KEY / NVIDIA_API_KEY_2, model
                          ``deepseek-ai/deepseek-v4.1-flash``)
2. OpenRouter            (OPENROUTER_API_KEY, model ``openai/gpt-4o-mini``)
3. Zhipu / BigModel      (ZHIPU_API_KEY, model ``glm-4.5-flash``)
4. Google Gemini         (GEMINI_API_KEY)
5. Groq                  (GROQ_API_KEY)
6. Deterministic rule-based fallback (no network)

Design constraints:
* Hard per-call timeout (default 8s) so the trading loop never stalls.
* Responses cached for ``LLM_CACHE_SECONDS`` (default 60s) because the five
  agents often ask near-identical questions about the same token.
* Never raises: always returns text / a JSON dict.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ...core.config import settings
from ...core.logger import logger

DEFAULT_NVIDIA_MODEL = "deepseek-ai/deepseek-v4.1-flash"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
DEFAULT_ZHIPU_MODEL = "glm-4.5-flash"


class LLMProvider:
    """Multi-provider LLM that auto-detects which API key is available."""

    def __init__(self):
        self.timeout = float(getattr(settings, "llm_timeout_seconds", 8.0) or 8.0)
        self.cache_seconds = int(getattr(settings, "llm_cache_seconds", 60) or 0)
        self._cache: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self.last_provider: str = ""

    # ------------------------------------------------------------------
    # Provider selection
    # ------------------------------------------------------------------
    def _providers(self) -> List[str]:
        """Return providers in priority order for the configured keys."""
        forced = (getattr(settings, "llm_provider", "auto") or "auto").strip().lower()
        available: List[str] = []
        if settings.nvidia_api_key or settings.nvidia_api_key_2:
            available.append("nvidia")
        if settings.openrouter_api_key:
            available.append("openrouter")
        if settings.zhipu_api_key:
            available.append("zhipu")
        if settings.gemini_api_key:
            available.append("gemini")
        if settings.groq_api_key:
            available.append("groq")
        if forced in available:
            return [forced] + [p for p in available if p != forced]
        return available

    def _get_provider(self) -> str:
        providers = self._providers()
        return providers[0] if providers else "fallback"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        """Get an AI response from the best available provider."""
        cache_key = (system_prompt, user_prompt)
        if self.cache_seconds > 0:
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached[0]) < self.cache_seconds:
                return cached[1]

        for provider in self._providers():
            try:
                if provider == "nvidia":
                    text = await self._call_nvidia(system_prompt, user_prompt)
                elif provider == "openrouter":
                    text = await self._call_openrouter(system_prompt, user_prompt)
                elif provider == "zhipu":
                    text = await self._call_zhipu(system_prompt, user_prompt)
                elif provider == "gemini":
                    text = await self._call_gemini(system_prompt, user_prompt)
                elif provider == "groq":
                    text = await self._call_groq(system_prompt, user_prompt)
                else:
                    continue
                if text and text.strip():
                    self.last_provider = provider
                    if self.cache_seconds > 0:
                        self._cache[cache_key] = (time.time(), text)
                    return text
            except Exception as exc:
                logger.warning(f"LLM provider '{provider}' failed: {str(exc)[:200]}")

        self.last_provider = "fallback"
        return self._fallback_response(user_prompt)

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Get an AI response parsed as JSON (never raises)."""
        full_prompt = (
            system_prompt
            + "\n\nIMPORTANT: Respond with valid JSON only. No markdown, no code fences."
        )
        text = await self.chat(full_prompt, user_prompt)
        return self._parse_json(text)

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------
    async def _call_openai_compatible(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system: str,
        user: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": float(getattr(settings, "ai_agent_temperature", 0.3) or 0.3),
                    "max_tokens": int(getattr(settings, "ai_agent_max_tokens", 800) or 800),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

    async def _call_nvidia(self, system: str, user: str) -> str:
        """NVIDIA NIM API with key rotation."""
        model = getattr(settings, "ai_agent_model", "") or DEFAULT_NVIDIA_MODEL
        keys = [k for k in (settings.nvidia_api_key, settings.nvidia_api_key_2) if k]
        last_error: Optional[Exception] = None
        for key in keys:
            try:
                return await self._call_openai_compatible(
                    settings.nvidia_base_url, key, model, system, user
                )
            except Exception as exc:
                last_error = exc
                logger.debug(f"NVIDIA key ...{key[-4:]} failed: {str(exc)[:120]}")
        raise last_error or RuntimeError("No NVIDIA key configured")

    async def _call_openrouter(self, system: str, user: str) -> str:
        model = getattr(settings, "llm_model", "") or DEFAULT_OPENROUTER_MODEL
        return await self._call_openai_compatible(
            "https://openrouter.ai/api/v1",
            settings.openrouter_api_key,
            model,
            system,
            user,
            extra_headers={
                "HTTP-Referer": "https://memecoin-bot-deploy-production.up.railway.app",
                "X-Title": "Memecoin AI Bot",
            },
        )

    async def _call_zhipu(self, system: str, user: str) -> str:
        return await self._call_openai_compatible(
            settings.zhipu_base_url,
            settings.zhipu_api_key,
            DEFAULT_ZHIPU_MODEL,
            system,
            user,
        )

    async def _call_gemini(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={settings.gemini_api_key}",
                json={
                    "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 800},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    async def _call_groq(self, system: str, user: str) -> str:
        return await self._call_openai_compatible(
            "https://api.groq.com/openai/v1",
            settings.groq_api_key,
            "llama-3.3-70b-versatile",
            system,
            user,
        )

    # ------------------------------------------------------------------
    # Parsing / fallback
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if text.startswith("```"):
            lines = [line for line in text.split("\n") if not line.startswith("```")]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
        logger.warning(f"LLM JSON parse failed: {text[:200]}")
        return {
            "decision": "WAIT",
            "confidence": 0.5,
            "score": 50,
            "reasoning": text[:500],
            "key_factors": [],
            "concerns": [],
            "suggested_position_size_pct": 0,
        }

    def _fallback_response(self, prompt: str) -> str:
        """Deterministic rule-based fallback when no AI provider is reachable."""
        p = (prompt or "").lower()
        if any(k in p for k in ["رصيد", "balance", "حساب"]):
            return "رصيدك الحالي: 0 SOL. أرسل SOL لمحفظتك لبدء التداول الحقيقي على Pump.fun."
        if any(k in p for k in ["عملة", "token", "coin"]):
            return "تم اكتشاف عملات Solana حقيقية. راجع صفحة العملات للتفاصيل."
        if any(k in p for k in ["وكيل", "agent"]):
            return "5 وكلاء AI جاهزون. أضف مفتاح NVIDIA أو OpenRouter لتفعيل التحليل الكامل."
        if any(k in p for k in ["صفقة", "trade"]):
            return "لا توجد صفقات مفتوحة حالياً. النظام يراقب السوق."
        if "decision" in p or "json" in p:
            return json.dumps({
                "decision": "WAIT",
                "confidence": 0.5,
                "score": 50,
                "reasoning": "تحليل أساسي - لا يوجد مزود AI متاح حالياً",
                "key_factors": [],
                "concerns": ["الذكاء الاصطناعي غير مُفعّل"],
                "suggested_position_size_pct": 0,
            })
        return "مرحباً! أنا مساعد Memecoin AI Bot. أضف مفتاح AI لتفعيل الذكاء الكامل."


# Singleton
_provider: Optional[LLMProvider] = None


def get_llm() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = LLMProvider()
    return _provider
