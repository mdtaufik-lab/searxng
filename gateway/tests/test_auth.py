from __future__ import annotations

from app.auth.keys import DevKeyStore, generate_key, hash_key
from app.auth.ratelimit import MemoryRateLimiter


def test_generated_keys_are_high_entropy_and_only_the_hash_is_kept():
    raw, h, prefix = generate_key()
    assert raw.startswith("sk_live_")
    assert len(raw) > 40  # 256 bits of randomness -- no dictionary to attack,
    assert h == hash_key(raw)  # which is why sha256 (not bcrypt) is right here
    assert raw not in h and prefix in raw


def test_two_keys_are_never_the_same():
    assert generate_key()[0] != generate_key()[0]


async def test_dev_keystore_only_accepts_its_own_key():
    store = DevKeyStore("sk_live_right")
    assert await store.lookup(hash_key("sk_live_right")) is not None
    assert await store.lookup(hash_key("sk_live_wrong")) is None


async def test_rate_limiter_allows_up_to_burst_then_blocks():
    rl = MemoryRateLimiter()
    allowed = 0
    for _ in range(15):
        if (await rl.check("k", rpm=60, burst=10)).allowed:
            allowed += 1
    assert allowed == 10  # the burst, and not one more

    decision = await rl.check("k", rpm=60, burst=10)
    assert decision.allowed is False
    assert decision.retry_after >= 1  # clients get a usable Retry-After


async def test_rate_limits_are_per_key():
    rl = MemoryRateLimiter()
    for _ in range(10):
        await rl.check("noisy", rpm=60, burst=10)

    assert (await rl.check("noisy", rpm=60, burst=10)).allowed is False
    # One abusive key must not exhaust anyone else's budget.
    assert (await rl.check("quiet", rpm=60, burst=10)).allowed is True
