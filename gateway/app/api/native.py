"""The API we actually recommend. Nothing here is fabricated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..auth.usage import UsageRow
from ..core.models import SearchParams
from ..core.normalize import normalize_q, query_hash
from ..errors import GatewayError
from ..searxng.client import UpstreamBadRequest, UpstreamError
from ..searxng.qbuilder import guard
from .deps import Auth, authenticate

router = APIRouter()

TIME_RANGES = {"day", "week", "month", "year"}


@router.get("/v1/search")
async def search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    cx: str = Query("web", description="Engine profile from profiles.yml"),
    language: str = Query("auto"),
    safesearch: int = Query(0, ge=0, le=2),
    time_range: str | None = Query(None),
    auth: Auth = Depends(authenticate),
) -> JSONResponse:
    st = request.app.state
    settings = st.settings

    if time_range and time_range not in TIME_RANGES:
        raise GatewayError(400, f"time_range must be one of {sorted(TIME_RANGES)}", "invalid")

    profile = st.profiles.resolve(cx)
    if profile is None:
        raise GatewayError(400, f"Unknown cx {cx!r}.", "invalid")

    if offset + limit > settings.max_results:
        raise GatewayError(
            400,
            f"offset + limit must be <= {settings.max_results}. Deep pagination is not "
            "available: upstream engines cap how far back their own pages go.",
            "invalid",
        )

    params = SearchParams(
        q=guard(normalize_q(q), st.catalog),
        categories=tuple(profile.categories),
        engines=tuple(profile.engines),
        language=language,
        safesearch=safesearch,
        time_range=time_range,
    )

    try:
        served = await st.service.search(params, offset + 1, limit)
    except UpstreamBadRequest as exc:
        raise GatewayError(502, f"Upstream rejected the query: {exc}", "backendError") from exc
    except UpstreamError:
        raise GatewayError(503, "Search backend unavailable.", "backendError") from None

    body = {
        "query": q,
        "results": [
            {
                "url": r.get("url"),
                "title": r.get("title"),
                "snippet": r.get("content"),
                "engines": r.get("engines") or [],
                "score": r.get("score"),
                "category": r.get("category"),
                "published_date": r.get("publishedDate"),
                "thumbnail": r.get("thumbnail") or None,
                "image": r.get("img_src") or None,
            }
            for r in served.items
        ],
        "answers": [
            a.get("answer") if isinstance(a, dict) else a for a in served.entry.answers
        ],
        "suggestions": served.entry.suggestions,
        "corrections": served.entry.corrections,
        "infoboxes": served.entry.infoboxes,
        # No total-result count: SearXNG does not have one, so we do not invent
        # one. `has_more` is the honest signal, and it is exact.
        "has_more": served.has_more,
        "offset": offset,
        "limit": limit,
        "returned": len(served.items),
        "meta": {
            "cached": served.cache_state == "HIT",
            "cache_state": served.cache_state,
            "upstream_pages": served.upstream_pages,
            "took_ms": served.took_ms,
            "degraded": served.degraded,
            "unresponsive_engines": served.entry.unresponsive_engines,
        },
    }

    if st.usage is not None:
        st.usage.record(
            UsageRow(
                api_key_id=auth.key.id,
                endpoint="native",
                cx=cx,
                query_hash=query_hash(params),
                query_text=q if settings.log_raw_queries else None,
                status=200,
                cache_hit=served.cache_state == "HIT",
                upstream_pages=served.upstream_pages,
                latency_ms=served.took_ms,
                degraded=served.degraded,
            )
        )

    return JSONResponse(
        body,
        headers={
            "X-Cache": served.cache_state,
            "X-Upstream-Pages": str(served.upstream_pages),
            "X-RateLimit-Remaining": str(auth.rate_remaining),
            **({"X-Search-Degraded": "1"} if served.degraded else {}),
        },
    )
