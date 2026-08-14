from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.core.cache import MemoryCache
from app.core.service import SearchService
from app.searxng.qbuilder import EngineCatalog

FIXTURES = Path(__file__).parent / "fixtures"


def load_page(n: int) -> dict:
    return json.loads((FIXTURES / f"searxng_page{n}.json").read_text())


class FakeClient:
    """Replays recorded REAL SearXNG pages and counts fetches.

    The count is the whole point: it is what proves the cache stops us scraping
    the upstream engines once per client page.
    """

    def __init__(self, pages: list[dict] | None = None, fail_after: int | None = None) -> None:
        self.pages = pages if pages is not None else [load_page(1), load_page(2), load_page(3)]
        self.calls = 0
        self.fail_after = fail_after

    async def search(self, q, pageno, **kw):
        from app.searxng.client import UpstreamError

        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise UpstreamError("simulated upstream failure")
        if pageno > len(self.pages):
            return {"results": [], "answers": [], "infoboxes": [], "suggestions": [],
                    "corrections": [], "unresponsive_engines": []}
        return self.pages[pageno - 1]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        dev_api_key="sk_live_test",
        max_upstream_pages=3,
        max_results=100,
        cache_ttl_fresh=600,
        cache_ttl_max=21600,
        upstream_time_budget=30.0,
    )


@pytest.fixture
def make_service(settings):
    def _make(client=None):
        client = client or FakeClient()
        return SearchService(settings, client, MemoryCache()), client

    return _make


@pytest.fixture
def catalog() -> EngineCatalog:
    # Mirrors the real instance: there is NO engine literally named "google"
    # (it is "google cse"), which is why !google is not a bang here.
    return EngineCatalog(
        engines={"google cse", "duckduckgo", "startpage", "brave", "wikipedia"},
        shortcuts={"gcse", "ddg", "sp", "br", "wp"},
        categories={"general", "images", "news", "it", "science"},
    )
