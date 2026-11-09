"""
HTTP client layer with built-in retry, rate-limit handling, and audit logging.
ALL external API calls in the system MUST go through this layer.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import httpx
from tenacity import (
    retry, stop_after_attempt, wait_exponential, retry_if_exception_type,
)

from ..core.config import settings
from ..core.logger import logger


class RateLimitError(Exception):
    """Raised when an API returns 429 / explicit rate-limit signal."""


class DataSourceError(Exception):
    """Raised when a data source returns unexpected response."""


class HttpClient:
    """Wrapper around httpx.AsyncClient with retry, rate-limit, and audit logging."""

    def __init__(
        self,
        source_name: str,
        base_url: str = "",
        default_headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
        max_concurrency: int = 5,
    ):
        self.source_name = source_name
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self.timeout = timeout
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url or None,
                timeout=self.timeout,
                headers=self.default_headers,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    @retry(
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RateLimitError)
        ),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        async with self._sem:
            client = await self._get_client()
            start = time.monotonic()
            req_headers = {**self.default_headers, **(headers or {})}

            try:
                response = await client.request(
                    method, url, params=params, json=json, headers=req_headers,
                )
                latency_ms = int((time.monotonic() - start) * 1000)

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "2"))
                    logger.warning(
                        f"[{self.source_name}] rate limited, sleeping {retry_after}s"
                    )
                    await asyncio.sleep(retry_after)
                    raise RateLimitError(f"{self.source_name} returned 429")

                response.raise_for_status()

                # Audit log (best-effort, never block on DB failure)
                await self._audit(url, latency_ms, response.status_code, True, None)

                return response.json()

            except Exception as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                status = getattr(exc, "response", None) and getattr(
                    exc.response, "status_code", None
                )
                await self._audit(url, latency_ms, status, False, str(exc)[:200])
                logger.debug(f"[{self.source_name}] {method} {url} failed: {exc}")
                raise

    async def _audit(
        self,
        endpoint: str,
        latency_ms: int,
        status_code: Optional[int],
        success: bool,
        error: Optional[str],
    ) -> None:
        """Persist a row to data_source_calls table. Best-effort."""
        try:
            from ..core.database import get_session
            from ..core.models import DataSourceCall

            async with get_session() as session:
                call = DataSourceCall(
                    source=self.source_name,
                    endpoint=endpoint[:256],
                    latency_ms=latency_ms,
                    status_code=status_code,
                    success=success,
                    error=error,
                )
                session.add(call)
        except Exception as audit_exc:
            logger.debug(f"Audit log failed (non-blocking): {audit_exc}")

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
