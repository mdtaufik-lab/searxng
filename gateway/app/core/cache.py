from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Protocol

from .models import Entry


class CacheBackend(Protocol):
    async def get(self, key: str) -> Entry | None: ...
    async def set(self, key: str, entry: Entry, ttl: int) -> None: ...
    async def close(self) -> None: ...


class MemoryCache:
    """Local-dev default. Per-process, so DO NOT run multiple uvicorn workers
    with this backend -- N workers means N caches and N times the upstream
    scraping, which is exactly what we built the cache to avoid."""

    def __init__(self, max_entries: int = 5000) -> None:
        self._d: OrderedDict[str, tuple[Entry, float]] = OrderedDict()
        self._max = max_entries
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Entry | None:
        async with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            entry, expires = hit
            if time.time() > expires:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)
            return entry

    async def set(self, key: str, entry: Entry, ttl: int) -> None:
        async with self._lock:
            self._d[key] = (entry, time.time() + ttl)
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    async def close(self) -> None:
        self._d.clear()


class RedisCache:
    """VPS backend. Shared across workers/hosts, so the cache and the upstream
    protection it provides survive horizontal scaling."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Entry | None:
        raw = await self._r.get(key)
        if not raw:
            return None
        try:
            return Entry(**json.loads(raw))
        except (ValueError, TypeError):
            return None  # schema drift across a deploy: treat as a miss

    async def set(self, key: str, entry: Entry, ttl: int) -> None:
        await self._r.set(key, json.dumps(asdict(entry)), ex=ttl)

    async def close(self) -> None:
        await self._r.aclose()


def build_cache(backend: str, redis_url: str, max_entries: int) -> CacheBackend:
    return RedisCache(redis_url) if backend == "redis" else MemoryCache(max_entries)
