from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth.usage import UsageRow
from ..core.normalize import query_hash
from ..core.service import SearchService
from ..google import response as gresponse
from ..google.params import ParamError, map_params
from ..errors import GatewayError
from ..searxng.client import UpstreamBadRequest, UpstreamError
from .deps import Auth, authenticate

router = APIRouter()


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def make_postprocess(m) -> Any:
    """siteSearch and sort=date are applied to the whole accumulation, not to a
    single page -- otherwise the window boundaries would be wrong."""
    if not m.site_filter and not m.sort_by_date:
        return None

    def post(results: list[dict]) -> list[dict]:
        out = results
        if m.site_filter:
            host, mode = m.site_filter
            # `site:` is only a hint to the upstream engines and not all of them
            # honour it, so the merged set leaks other domains. Every result has
            # a URL, so filtering here is safe and exact.
            def matches(r: dict) -> bool:
                h = _host(r.get("url", ""))
                return h == host or h.endswith("." + host)

            out = [r for r in out if matches(r) == (mode == "i")]
        if m.sort_by_date:
            # Sorts the retrieved candidates, NOT the whole web. Undated results
            # sink to the bottom rather than being dropped: most general web
            # results carry no publishedDate at all.
            out = sorted(
                out, key=lambda r: (r.get("publishedDate") or "", ), reverse=True
            )
        return out

    return post


@router.get("/customsearch/v1")
async def customsearch(request: Request, auth: Auth = Depends(authenticate)) -> JSONResponse:
    st = request.app.state
    settings = st.settings
    args = dict(request.query_params)

    profile = st.profiles.resolve(args.get("cx"))
    if profile is None:
        raise GatewayError(
            400,
            f"Unknown cx {args.get('cx')!r}. Add it to profiles.yml (as a profile or an alias).",
            "invalid",
        )

    try:
        m = map_params(
            args,
            profile,
            st.catalog,
            strict=settings.strict_params,
            default_safesearch=0,
        )
    except ParamError as exc:
        raise GatewayError(400, exc.reason, "invalidParameter") from exc

    service: SearchService = st.service
    try:
        served = await service.search(
            m.params, m.start, m.num, postprocess=make_postprocess(m)
        )
    except UpstreamBadRequest as exc:
        raise GatewayError(502, f"Upstream rejected the query: {exc}", "backendError") from exc
    except UpstreamError as exc:
        raise GatewayError(503, "Search backend unavailable.", "backendError") from exc

    body = gresponse.build(
        served,
        m,
        args,
        profile,
        total_results_mode=settings.total_results_mode,
        public_base_url=settings.public_base_url,
    )

    headers = {
        "X-Cache": served.cache_state,
        "X-Upstream-Pages": str(served.upstream_pages),
        "X-RateLimit-Remaining": str(auth.rate_remaining),
    }
    if served.degraded:
        # Degradation is signalled in HEADERS, never in the body: injecting
        # non-standard keys into a CSE response can break strict clients. A
        # rising count here is the early warning that the host IP is being
        # blocked by upstream engines -- before results go to zero.
        headers["X-Search-Degraded"] = "1"
        headers["X-Search-Unresponsive-Engines"] = ",".join(
            str(e[0]) for e in served.entry.unresponsive_engines if e
        )[:200]
    if m.unsupported:
        headers["X-Unsupported-Params"] = ",".join(m.unsupported)

    if st.usage is not None:
        st.usage.record(
            UsageRow(
                api_key_id=auth.key.id,
                endpoint="customsearch",
                cx=args.get("cx"),
                query_hash=query_hash(m.params),
                query_text=args.get("q") if settings.log_raw_queries else None,
                status=200,
                cache_hit=served.cache_state == "HIT",
                upstream_pages=served.upstream_pages,
                latency_ms=served.took_ms,
                degraded=served.degraded,
            )
        )

    return JSONResponse(body, headers=headers)
