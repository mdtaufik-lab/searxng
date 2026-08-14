from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int  # seconds


class RateLimiter(Protocol):
    async def check(self, key_id: str, rpm: int, burst: int) -> Decision: ...


class MemoryRateLimiter:
    """Token bucket, in-process.

    CORRECT ONLY IN A SINGLE PROCESS. With `uvicorn --workers N` each worker
    keeps its own buckets, so the effective limit becomes N x what you
    configured. Use the Redis limiter whenever you run more than one worker.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = asyncio.Lock()

    async def check(self, key_id: str, rpm: int, burst: int) -> Decision:
        rate = rpm / 60.0
        async with self._lock:  # refill-check-decrement must be indivisible
            now = time.monotonic()
            tokens, last = self._buckets.get(key_id, (float(burst), now))
            tokens = min(burst, tokens + (now - last) * rate)

            if tokens < 1.0:
                self._buckets[key_id] = (tokens, now)
                return Decision(False, 0, max(1, int((1.0 - tokens) / rate) + 1))

            tokens -= 1.0
            self._buckets[key_id] = (tokens, now)
            return Decision(True, int(tokens), 0)


# Redis executes a script atomically (it is single-threaded), so refill, check
# and decrement happen as one indivisible step. A naive GET -> compute -> SET
# races badly: N concurrent requests all read the same token count and all pass
# a limit of 1. The Lua is the entire point of this backend.
_LUA = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now   = tonumber(ARGV[3])

local b = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts     = tonumber(b[2])
if tokens == nil then tokens = burst; ts = now end

tokens = math.min(burst, tokens + (now - ts) * rate)

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
return {allowed, math.floor(tokens)}
"""


class RedisRateLimiter:
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)
        self._script = self._r.register_script(_LUA)

    async def check(self, key_id: str, rpm: int, burst: int) -> Decision:
        rate = rpm / 60.0
        allowed, remaining = await self._script(
            keys=[f"rl:{key_id}"], args=[rate, burst, time.time()]
        )
        if int(allowed) == 1:
            return Decision(True, int(remaining), 0)
        return Decision(False, 0, max(1, int(1.0 / rate) + 1))


def build_rate_limiter(backend: str, redis_url: str) -> RateLimiter:
    return RedisRateLimiter(redis_url) if backend == "redis" else MemoryRateLimiter()
