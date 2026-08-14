"""The accumulator.

Google clients paginate with `start` (absolute index) and `num` (page size).
SearXNG has NEITHER -- it offers only a page-based `pageno` whose result count is
*variable and unpredictable*: it is however many results the selected engines
happened to return, merged and deduplicated. Measured on this instance for one
query: page 1 -> 25 results, page 2 -> 35, page 3 -> 35. It drifts with the
query, the engine mix, and which engines are currently rate-limited, so no fixed
page size can be assumed in either direction.

So we cache an append-only result list keyed on the query WITHOUT start/num, and
serve any window by slicing it. This is the single most important idea here:
without it, a client paging through 10 result pages triggers 10-30 real scrapes
of Google/DDG/Brave and gets the host IP captcha'd. With it, the same traffic
costs at most `max_upstream_pages` fetches -- usually one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeAlias

from ..config import Settings
from ..searxng.client import SearxngClient, UpstreamBadRequest, UpstreamError
from .cache import CacheBackend
from .coalesce import KeyedLock
from .models import Entry, SearchParams, Served
from .normalize import cache_key, dedupe_url

log = logging.getLogger(__name__)

# Filter/sort applied to the full accumulation before the window is sliced.
Postprocess: TypeAlias = Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None


class SearchService:
    def __init__(self, settings: Settings, client: SearxngClient, cache: CacheBackend) -> None:
        self._s = settings
        self._client = client
        self._cache = cache
        self._locks = KeyedLock()

    def _ttl_fresh(self, params: SearchParams) -> int:
        if params.time_range == "day" or "news" in params.categories:
            return self._s.cache_ttl_fresh_recent
        return self._s.cache_ttl_fresh

    def _satisfied(self, entry: Entry, target: int, view_len: int) -> bool:
        """True when no amount of further fetching would help this window."""
        return (
            view_len >= target
            or entry.exhausted
            or entry.pages_fetched >= self._s.max_upstream_pages
            or len(entry.results) >= self._s.max_results
        )

    @staticmethod
    def _view(entry: Entry, postprocess: Postprocess) -> list[dict[str, Any]]:
        """What the caller actually sees.

        Applied to the WHOLE accumulation before the window is sliced -- filter
        or sort after slicing and the page boundaries would be wrong. The cache
        always holds the unfiltered list, so one entry serves every variant.
        """
        return postprocess(entry.results) if postprocess else entry.results

    async def search(
        self,
        params: SearchParams,
        start: int,
        num: int,
        postprocess: Postprocess = None,
    ) -> Served:
        t0 = time.monotonic()
        key = cache_key(params)
        # Fetch one result PAST the window: that tells us has_more for free,
        # with no extra upstream page.
        target = min(start + num, self._s.max_results)
        ttl_fresh = self._ttl_fresh(params)

        entry = await self._cache.get(key)
        if entry and self._is_fresh(entry, ttl_fresh):
            view = self._view(entry, postprocess)
            if self._satisfied(entry, target, len(view)):
                return self._serve(entry, view, start, num, "HIT", 0, t0)

        stale = entry  # keep it: if upstream is down, this beats an error

        async with self._locks.acquire(key):
            # Re-read under the lock. While we waited, the request that held the
            # lock may have already filled this exact entry.
            entry = await self._cache.get(key)
            if entry and self._is_fresh(entry, ttl_fresh):
                view = self._view(entry, postprocess)
                if self._satisfied(entry, target, len(view)):
                    return self._serve(entry, view, start, num, "HIT", 0, t0)

            # Stale entries are rebuilt from page 1 rather than extended: their
            # older pages may no longer reflect what the engines return today.
            work = entry if (entry and self._is_fresh(entry, ttl_fresh)) else Entry()

            fetched = 0
            deadline = t0 + self._s.upstream_time_budget
            view = self._view(work, postprocess)
            try:
                while (
                    not self._satisfied(work, target, len(view))
                    and time.monotonic() < deadline
                ):
                    page = await self._client.search(
                        params.q,
                        work.pages_fetched + 1,
                        categories=params.categories,
                        engines=params.engines,
                        language=params.language,
                        safesearch=params.safesearch,
                        time_range=params.time_range,
                    )
                    fetched += 1
                    self._absorb(work, page)
                    view = self._view(work, postprocess)
            except UpstreamBadRequest:
                raise  # our own bad params; surfacing it beats hiding it
            except UpstreamError:
                if stale is not None and stale.results:
                    log.warning("upstream failed; serving stale cache for %s", key)
                    stale_view = self._view(stale, postprocess)
                    return self._serve(stale, stale_view, start, num, "STALE", fetched, t0)
                raise

            if not work.results:
                # NEVER cache an empty or all-engines-failed result. Doing so
                # pins one transient captcha into the cache for the whole TTL
                # and turns a blip into an outage.
                return self._serve(work, view, start, num, "MISS", fetched, t0)

            if work.fetched_at == 0.0:
                work.fetched_at = time.time()
            age = time.time() - work.fetched_at
            await self._cache.set(key, work, max(60, int(self._s.cache_ttl_max - age)))
            return self._serve(work, view, start, num, "MISS", fetched, t0)

    @staticmethod
    def _is_fresh(entry: Entry, ttl_fresh: int) -> bool:
        return (time.time() - entry.fetched_at) < ttl_fresh

    def _absorb(self, work: Entry, page: dict[str, Any]) -> None:
        """Append one SearXNG page, deduping against everything already held."""
        results = page.get("results") or []

        # SearXNG dedupes within a single ResultContainer -- i.e. within one
        # page. Pages 1 and 2 are independent searches, so cross-page duplicates
        # are routine and removing them is our job.
        seen = {dedupe_url(r.get("url", "")) for r in work.results}
        added = 0
        for r in results:
            ident = dedupe_url(r.get("url", ""))
            if not ident or ident in seen:
                continue
            seen.add(ident)
            work.results.append(r)
            added += 1
            if len(work.results) >= self._s.max_results:
                break

        work.pages_fetched += 1

        # No new information came back, so deeper pages will not help either.
        if added == 0:
            work.exhausted = True

        if work.pages_fetched == 1:
            work.answers = page.get("answers") or []
            work.infoboxes = page.get("infoboxes") or []
            work.suggestions = list(page.get("suggestions") or [])
            work.corrections = list(page.get("corrections") or [])

        for ue in page.get("unresponsive_engines") or []:
            if ue not in work.unresponsive_engines:
                work.unresponsive_engines.append(ue)

    def _serve(
        self,
        entry: Entry,
        view: list[dict[str, Any]],
        start: int,
        num: int,
        state: str,
        fetched: int,
        t0: float,
    ) -> Served:
        lo = start - 1
        # NOTE: fetch order is preserved and the list is never re-sorted by score.
        # SearXNG's `score` is computed per-search, so page-1 and page-2 scores
        # are not comparable; sorting the merged list would make the result at
        # start=1 CHANGE as a client pages forward, which breaks pagination.
        items = view[lo : lo + num]

        has_more = len(view) > lo + num or (
            not entry.exhausted
            and entry.pages_fetched < self._s.max_upstream_pages
            and lo + num < self._s.max_results
        )

        return Served(
            items=items,
            entry=entry,
            total_available=len(view),
            has_more=has_more,
            cache_state=state,
            upstream_pages=fetched,
            took_ms=int((time.monotonic() - t0) * 1000),
            degraded=bool(entry.unresponsive_engines),
        )
