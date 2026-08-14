"""The accumulator. If anything here breaks, the gateway starts scraping the
upstream engines once per client page and the host IP gets captcha'd."""

from __future__ import annotations

import pytest

from app.core.models import SearchParams
from app.searxng.client import UpstreamError
from tests.conftest import FakeClient, load_page

P = SearchParams(q="python packaging")


async def test_paging_through_a_result_set_costs_one_fetch(make_service):
    """THE test. Four Google pages must not mean four scrapes."""
    svc, client = make_service()

    for start in (1, 11, 21):
        served = await svc.search(P, start, 10)
        assert len(served.items) == 10

    # Page 1 of SearXNG alone holds 25 results, which covers client pages 1-2
    # and most of page 3. A naive pass-through would have fetched 3 times.
    assert client.calls <= 2, f"expected <=2 upstream fetches, got {client.calls}"


async def test_second_identical_request_is_a_pure_cache_hit(make_service):
    svc, client = make_service()
    await svc.search(P, 1, 10)
    calls_after_first = client.calls

    served = await svc.search(P, 1, 10)
    assert served.cache_state == "HIT"
    assert served.upstream_pages == 0
    assert client.calls == calls_after_first  # not one extra scrape


async def test_windows_are_contiguous_and_non_overlapping(make_service):
    """Client pages must tile the result set exactly -- no gaps, no repeats."""
    svc, _ = make_service()
    page1 = await svc.search(P, 1, 10)
    page2 = await svc.search(P, 11, 10)
    page3 = await svc.search(P, 21, 10)

    urls = [r["url"] for r in page1.items + page2.items + page3.items]
    assert len(urls) == 30
    assert len(set(urls)) == 30, "the same result appeared on two client pages"


async def test_order_is_stable_as_the_client_pages_forward(make_service):
    """Re-sorting the accumulation by score would make the item at start=1
    CHANGE once page 2 is fetched. Pagination would be incoherent."""
    svc, _ = make_service()
    first_before = (await svc.search(P, 1, 10)).items[0]["url"]
    await svc.search(P, 31, 10)  # forces a deeper fetch
    first_after = (await svc.search(P, 1, 10)).items[0]["url"]

    assert first_before == first_after


async def test_cross_page_duplicates_are_removed(make_service):
    """SearXNG dedupes only WITHIN one page; pages 1 and 2 are independent
    searches, so removing cross-page duplicates is our job."""
    svc, _ = make_service()
    served = await svc.search(P, 1, 100)

    from app.core.normalize import dedupe_url

    idents = [dedupe_url(r["url"]) for r in served.entry.results]
    assert len(idents) == len(set(idents))


async def test_deepening_stops_at_max_upstream_pages(make_service):
    """The scrape budget is a hard stop, not a suggestion."""
    svc, client = make_service()
    await svc.search(P, 91, 10)
    assert client.calls <= 3


async def test_has_more_is_false_at_the_end_of_an_exhausted_set(make_service):
    svc, _ = make_service(FakeClient(pages=[load_page(1)]))  # only one page exists
    served = await svc.search(P, 21, 10)
    assert served.entry.exhausted
    assert served.has_more is False


async def test_window_past_the_end_returns_empty_not_an_error(make_service):
    svc, _ = make_service(FakeClient(pages=[load_page(1)]))
    served = await svc.search(P, 91, 10)
    assert served.items == []  # Google returns 200 + no items here; so do we


async def test_upstream_failure_serves_stale_cache_rather_than_erroring(make_service):
    svc, client = make_service(FakeClient(fail_after=1))
    warm = await svc.search(P, 1, 10)
    assert warm.cache_state == "MISS"

    # Expire the entry so the next call must re-fetch, and make that fetch fail.
    svc._cache._d[list(svc._cache._d)[0]][0].fetched_at = 0.0

    served = await svc.search(P, 1, 10)
    assert served.cache_state == "STALE"
    assert len(served.items) == 10  # users get results, not a 503


async def test_a_failed_search_is_never_cached(make_service):
    """Caching an empty/all-failed result would pin one transient captcha into
    the cache for the whole TTL, turning a blip into an outage."""
    svc, client = make_service(FakeClient(pages=[{"results": []}]))
    served = await svc.search(P, 1, 10)
    assert served.items == []

    await svc.search(P, 1, 10)
    assert client.calls == 2, "an empty result was cached and served back"


async def test_upstream_failure_with_no_cache_raises(make_service):
    svc, _ = make_service(FakeClient(fail_after=0))
    with pytest.raises(UpstreamError):
        await svc.search(P, 1, 10)


async def test_concurrent_identical_misses_collapse_into_one_fetch(make_service):
    """The stampede case: a cold cache plus a burst on the same query."""
    import asyncio

    svc, client = make_service()
    await asyncio.gather(*(svc.search(P, 1, 10) for _ in range(8)))

    assert client.calls <= 2, f"8 concurrent misses caused {client.calls} scrapes"
