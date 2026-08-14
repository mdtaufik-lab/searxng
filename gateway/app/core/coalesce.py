from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class KeyedLock:
    """One lock per cache key, refcounted so the dict cannot grow without bound.

    This is what collapses concurrent misses. A cold cache plus a burst of
    traffic on the same query is exactly the shape that gets an IP captcha'd:
    every request misses at the same instant and every one of them scrapes.
    Holding the lock means the first request fetches and the rest find the
    result already in the cache when they wake up.
    """

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def acquire(self, key: str):
        lock, refs = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock, refs = self._locks[key]
            if refs <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, refs - 1)
