from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchParams:
    """A search, normalized. Deliberately excludes start/num: the window is a
    property of the *request*, not of the result set we cache."""

    q: str
    categories: tuple[str, ...] = ("general",)
    engines: tuple[str, ...] = ()
    language: str = "auto"
    safesearch: int = 0
    time_range: str | None = None

    def as_key_dict(self) -> dict[str, Any]:
        return {
            "q": self.q,
            "categories": list(self.categories),
            "engines": list(self.engines),
            "language": self.language,
            "safesearch": self.safesearch,
            "time_range": self.time_range,
        }


@dataclass
class Entry:
    """The accumulator. Append-only, capped, cached without start/num."""

    results: list[dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    exhausted: bool = False
    answers: list[Any] = field(default_factory=list)
    infoboxes: list[Any] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    unresponsive_engines: list[Any] = field(default_factory=list)
    upstream_ms: int = 0
    fetched_at: float = 0.0


@dataclass
class Served:
    """One answered request: the window, plus how we got it."""

    items: list[dict[str, Any]]
    entry: Entry
    total_available: int
    has_more: bool
    cache_state: str  # HIT | MISS | STALE
    upstream_pages: int  # pages actually fetched while serving THIS request
    took_ms: int
    degraded: bool
