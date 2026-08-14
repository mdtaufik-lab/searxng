from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..auth.keys import KeyRecord, hash_key
from ..errors import GatewayError


@dataclass
class Auth:
    key: KeyRecord
    rate_remaining: int


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Google CSE clients put the key in the query string -- supporting this is
    # what makes the shim a genuine drop-in.
    if k := request.query_params.get("key"):
        return k.strip()
    if k := request.headers.get("x-api-key"):
        return k.strip()
    return None


async def authenticate(request: Request) -> Auth:
    app = request.app.state
    raw = _extract_key(request)
    if not raw:
        raise GatewayError(
            401,
            "API key missing. Pass it as ?key=, Authorization: Bearer, or X-API-Key.",
            "required",
        )

    record = await app.keystore.lookup(hash_key(raw))
    if record is None or not record.usable():
        # One message for unknown/revoked/expired alike: distinguishing them
        # tells an attacker which of their guessed keys are real.
        raise GatewayError(401, "Invalid API key.", "authError")

    cx = request.query_params.get("cx")
    if record.cx_allowlist and cx and cx not in record.cx_allowlist:
        raise GatewayError(403, f"This key may not use cx={cx!r}.", "forbidden")

    decision = await app.limiter.check(
        record.id, record.rate_limit_rpm, record.rate_limit_burst
    )
    if not decision.allowed:
        raise GatewayError(
            429,
            "Rate limit exceeded.",
            "rateLimitExceeded",
            headers={
                "Retry-After": str(decision.retry_after),
                "X-RateLimit-Limit": str(record.rate_limit_rpm),
                "X-RateLimit-Remaining": "0",
            },
        )

    if app.quota is not None:
        allowed, used = await app.quota.consume(
            record.id, record.quota_period, record.quota_limit
        )
        if not allowed:
            raise GatewayError(
                429,
                f"Quota exceeded ({record.quota_limit} requests per {record.quota_period}).",
                "quotaExceeded",
                headers={"X-Quota-Limit": str(record.quota_limit), "X-Quota-Used": str(used)},
            )

    return Auth(key=record, rate_remaining=decision.remaining)


async def require_admin(request: Request) -> None:
    token = request.app.state.settings.admin_token
    if not token:
        raise GatewayError(403, "Admin API is disabled (set ADMIN_TOKEN).", "forbidden")
    supplied = request.headers.get("x-admin-token", "")
    if supplied != token:
        raise GatewayError(403, "Bad admin token.", "forbidden")
