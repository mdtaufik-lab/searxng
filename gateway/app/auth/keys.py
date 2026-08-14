from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

KEY_PREFIX = "sk_live_"


def generate_key() -> tuple[str, str, str]:
    """-> (raw_key, sha256_hash, display_prefix). The raw key is shown once."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[: len(KEY_PREFIX) + 6]


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class KeyRecord:
    id: str
    name: str | None
    cx_allowlist: tuple[str, ...] | None
    rate_limit_rpm: int
    rate_limit_burst: int
    quota_period: str
    quota_limit: int | None
    active: bool
    expires_at: datetime | None

    def usable(self) -> bool:
        if not self.active:
            return False
        if self.expires_at and self.expires_at < datetime.now(timezone.utc):
            return False
        return True


class KeyStore(Protocol):
    async def lookup(self, key_hash: str) -> KeyRecord | None: ...
    async def touch(self, key_id: str) -> None: ...


class DevKeyStore:
    """Single hard-coded key from DEV_API_KEY, no database. Local testing only:
    there are no quotas and no usage records. Never run this on a public host."""

    def __init__(self, raw_key: str) -> None:
        self._hash = hash_key(raw_key)
        self._rec = KeyRecord(
            id="00000000-0000-0000-0000-000000000000",
            name="dev",
            cx_allowlist=None,
            rate_limit_rpm=600,
            rate_limit_burst=100,
            quota_period="month",
            quota_limit=None,
            active=True,
            expires_at=None,
        )

    async def lookup(self, key_hash: str) -> KeyRecord | None:
        return self._rec if secrets.compare_digest(key_hash, self._hash) else None

    async def touch(self, key_id: str) -> None:
        return None


class PgKeyStore:
    """Postgres-backed, with a short TTL cache in front.

    Consequence worth knowing: revocation takes effect within `ttl` seconds, not
    instantly. POST /admin/purge-key-cache forces it immediately.
    """

    def __init__(self, pool, ttl: float = 30.0, negative_ttl: float = 10.0) -> None:
        self._pool = pool
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._cache: dict[str, tuple[KeyRecord | None, float]] = {}

    def purge(self) -> None:
        self._cache.clear()

    async def lookup(self, key_hash: str) -> KeyRecord | None:
        now = time.monotonic()
        hit = self._cache.get(key_hash)
        if hit and now < hit[1]:
            return hit[0]

        row = await self._pool.fetchrow(
            """
            select id, name, cx_allowlist, rate_limit_rpm, rate_limit_burst,
                   quota_period, quota_limit, active, expires_at
              from api_keys
             where key_hash = $1
            """,
            key_hash,
        )

        rec = (
            KeyRecord(
                id=str(row["id"]),
                name=row["name"],
                cx_allowlist=tuple(row["cx_allowlist"]) if row["cx_allowlist"] else None,
                rate_limit_rpm=row["rate_limit_rpm"],
                rate_limit_burst=row["rate_limit_burst"],
                quota_period=row["quota_period"],
                quota_limit=row["quota_limit"],
                active=row["active"],
                expires_at=row["expires_at"],
            )
            if row
            else None
        )

        # Negative results are cached too, and deliberately: otherwise a bot
        # spraying random keys gets a free database query per guess.
        ttl = self._ttl if rec else self._negative_ttl
        self._cache[key_hash] = (rec, now + ttl)
        return rec

    async def touch(self, key_id: str) -> None:
        await self._pool.execute(
            "update api_keys set last_used_at = now() where id = $1::uuid", key_id
        )
