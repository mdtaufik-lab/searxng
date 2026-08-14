from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class UpstreamError(RuntimeError):
    """SearXNG could not be reached or failed. Callers may serve stale cache."""


class UpstreamBadRequest(RuntimeError):
    """SearXNG rejected our params (400/403). This is our bug, not a blip --
    retrying only doubles the damage, so it is a distinct exception type."""


class CircuitOpen(UpstreamError):
    pass


class _Breaker:
    def __init__(self, threshold: int, reset_seconds: float) -> None:
        self._threshold = threshold
        self._reset = reset_seconds
        self._fails = 0
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self._fails < self._threshold:
            return False
        if time.monotonic() - self._opened_at >= self._reset:
            self._fails = self._threshold - 1  # half-open: let one probe through
            return False
        return True

    def record_success(self) -> None:
        self._fails = 0

    def record_failure(self) -> None:
        self._fails += 1
        if self._fails >= self._threshold:
            self._opened_at = time.monotonic()


class SearxngClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._sem = asyncio.Semaphore(settings.upstream_concurrency)
        self._breaker = _Breaker(settings.breaker_fail_threshold, settings.breaker_reset_seconds)
        self._http = httpx.AsyncClient(
            base_url=settings.searxng_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.upstream_timeout),
            follow_redirects=False,
            # A fixed, clean header set. We never forward the caller's cookies,
            # Accept-Language or X-Forwarded-For: SearXNG derives behaviour from
            # them (see the `language` note in search()) and that would make one
            # cache key mean different things for different callers.
            headers={
                "User-Agent": "search-gateway/0.1",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def config(self) -> dict[str, Any]:
        r = await self._http.get("/config")
        r.raise_for_status()
        return r.json()

    async def search(
        self,
        q: str,
        pageno: int,
        *,
        categories: tuple[str, ...] = (),
        engines: tuple[str, ...] = (),
        language: str = "auto",
        safesearch: int = 0,
        time_range: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": q,
            "format": "json",
            "pageno": pageno,
            "safesearch": safesearch,
            # ALWAYS send language explicitly. SearXNG falls back to the request's
            # Accept-Language header when it is absent (searx/webapp.py:498), which
            # would silently make identical cache keys return different results.
            "language": language or "auto",
        }
        if categories:
            params["categories"] = ",".join(categories)
        if engines:
            params["engines"] = ",".join(engines)
        if time_range:
            params["time_range"] = time_range

        if self._breaker.is_open:
            raise CircuitOpen("searxng circuit is open")

        last: Exception | None = None
        for attempt in range(3):
            try:
                async with self._sem:
                    r = await self._http.get("/search", params=params)
            except httpx.HTTPError as exc:  # transport: worth a retry
                last = exc
            else:
                if r.status_code < 400:
                    self._breaker.record_success()
                    return r.json()

                if r.status_code in (400, 403):
                    # 400 = we sent a bad param; 403 = `json` is missing from
                    # search.formats in settings.yml. Neither is transient.
                    self._breaker.record_success()
                    raise UpstreamBadRequest(
                        f"searxng rejected the request ({r.status_code}): {r.text[:200]}"
                    )
                last = UpstreamError(f"searxng returned {r.status_code}")

            if attempt < 2:  # exponential backoff with jitter
                await asyncio.sleep((0.25 * 2**attempt) + random.uniform(0, 0.1))

        self._breaker.record_failure()
        log.warning("searxng fetch failed after retries: %s", last)
        raise UpstreamError(str(last))
