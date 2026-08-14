from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from .api import customsearch, native
from .api.deps import require_admin
from .auth.keys import DevKeyStore, PgKeyStore
from .auth.quota import Quota
from .auth.ratelimit import build_rate_limiter
from .auth.usage import UsageLogger
from .config import get_settings
from .core.cache import build_cache
from .core.service import SearchService
from .db.pool import create_pool
from .errors import GatewayError, gateway_error_handler
from .google.profiles import Profiles
from .searxng.client import SearxngClient
from .searxng.qbuilder import EngineCatalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.settings = s
    app.state.profiles = Profiles.load(s.profiles_path)
    app.state.client = SearxngClient(s)
    app.state.cache = build_cache(s.cache_backend, s.redis_url, s.cache_max_entries)
    app.state.service = SearchService(s, app.state.client, app.state.cache)
    app.state.limiter = build_rate_limiter(s.rate_limit_backend, s.redis_url)

    # The guard needs the exact engine/shortcut/category names this instance
    # will consume, so we take them from SearXNG itself rather than guessing.
    try:
        app.state.catalog = EngineCatalog.from_config(await app.state.client.config())
        log.info(
            "engine catalog: %d engines, %d shortcuts, %d categories",
            len(app.state.catalog.engines),
            len(app.state.catalog.shortcuts),
            len(app.state.catalog.categories),
        )
    except Exception:
        log.exception("could not read SearXNG /config -- query guard will be inert")
        app.state.catalog = EngineCatalog()

    app.state.pool = None
    app.state.quota = None
    app.state.usage = None

    if s.database_url:
        app.state.pool = await create_pool(s.database_url)
        app.state.keystore = PgKeyStore(
            app.state.pool, ttl=s.key_cache_ttl, negative_ttl=s.key_negative_cache_ttl
        )
        app.state.quota = Quota(app.state.pool)
        app.state.usage = UsageLogger(
            app.state.pool, s.usage_queue_size, s.usage_flush_seconds, s.usage_flush_rows
        )
        app.state.usage.start()
        log.info("auth: postgres key store")
    elif s.dev_api_key:
        app.state.keystore = DevKeyStore(s.dev_api_key)
        log.warning(
            "auth: DEV MODE -- single key from DEV_API_KEY, no quota, no usage log. "
            "Do not expose this to a network."
        )
    else:
        raise RuntimeError("Set DATABASE_URL (production) or DEV_API_KEY (local testing).")

    try:
        yield
    finally:
        if app.state.usage:
            await app.state.usage.stop()
        if app.state.pool:
            await app.state.pool.close()
        await app.state.client.aclose()
        await app.state.cache.close()


app = FastAPI(
    title="Search Gateway",
    version="0.1.0",
    description="Authenticated, cached search API over a private SearXNG instance.",
    lifespan=lifespan,
)
app.add_exception_handler(GatewayError, gateway_error_handler)
app.include_router(customsearch.router, tags=["google-compat"])
app.include_router(native.router, tags=["native"])


@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"ok": True}


@app.get("/readyz", tags=["ops"])
async def readyz():
    """Ready means SearXNG is actually reachable, not merely that we booted."""
    try:
        await app.state.client.config()
    except Exception as exc:
        raise GatewayError(503, f"SearXNG unreachable: {exc}", "backendError") from exc
    return {"ok": True, "searxng": app.state.settings.searxng_base_url}


@app.post("/admin/purge-key-cache", tags=["ops"], dependencies=[Depends(require_admin)])
async def purge_key_cache():
    """Key lookups are cached for KEY_CACHE_TTL seconds, so a revoked key keeps
    working until it expires. This makes revocation take effect immediately."""
    ks = app.state.keystore
    if hasattr(ks, "purge"):
        ks.purge()
    return {"purged": True}
