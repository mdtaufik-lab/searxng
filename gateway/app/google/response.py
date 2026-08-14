from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from ..core.models import Served
from .highlight import bold, extract_terms
from .params import MAX_INDEX, Mapped
from .profiles import Profile

_RES = re.compile(r"(\d+)\s*[x×]\s*(\d+)")
_TAGS = re.compile(r"<[^>]+>")


def _display_link(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _formatted_url(url: str, limit: int = 110) -> str:
    pretty = unquote(url)
    if len(pretty) <= limit:
        return pretty
    return f"{pretty[: limit - 30]}...{pretty[-27:]}"


def _total_results(served: Served, mode: str) -> int:
    """SearXNG has no result-count field. Anything here is an estimate.

    `lower_bound` (the default) reports what we actually hold -- honest and
    monotonic. `inflated` fabricates a Google-shaped number for clients that
    hard-depend on one; it is off by default because fake numbers have a way of
    ending up in someone's analytics as though they were real.
    """
    n = served.total_available
    if mode == "inflated" and n:
        return n * 12_345 if served.has_more else n
    if served.has_more:
        return n + 10
    return n


def _item(r: dict[str, Any], terms: list[str], image_mode: bool) -> dict[str, Any]:
    url = r.get("url") or ""
    title = r.get("title") or ""
    snippet = _TAGS.sub("", r.get("content") or "").strip()

    item: dict[str, Any] = {
        "kind": "customsearch#result",
        "title": title,
        "htmlTitle": bold(title, terms),
        "link": url,
        "displayLink": _display_link(url),
        "snippet": snippet,
        "htmlSnippet": bold(snippet, terms),
        "formattedUrl": _formatted_url(url),
        "htmlFormattedUrl": bold(_formatted_url(url), terms),
    }

    pagemap: dict[str, Any] = {}
    if thumb := r.get("thumbnail"):
        pagemap["cse_thumbnail"] = [{"src": thumb}]
    if img := r.get("img_src"):
        pagemap["cse_image"] = [{"src": img}]

    # Namespaced provenance. Unknown pagemap keys are ignored by every client,
    # so this is free information rather than a compatibility risk.
    pagemap["searxng"] = [
        {
            "engines": ", ".join(r.get("engines") or []),
            "score": str(r.get("score", "")),
            "category": r.get("category", ""),
            "publishedDate": r.get("publishedDate") or "",
        }
    ]
    # Deliberately NO `metatags`: SearXNG never fetches the target page, so there
    # is no structured data to report. An empty [{}] would be worse than absence.
    item["pagemap"] = pagemap

    if image_mode and (img := r.get("img_src")):
        item["link"] = img
        item["mime"] = r.get("img_format") or ""
        image: dict[str, Any] = {
            "contextLink": url,
            "thumbnailLink": r.get("thumbnail") or "",
        }
        if m := _RES.search(str(r.get("resolution") or "")):
            image["width"] = int(m.group(1))
            image["height"] = int(m.group(2))
        # width/height/byteSize are omitted when unknown -- emitting 0 makes
        # clients render broken 0x0 images.
        item["image"] = image

    return item


def _request_block(
    m: Mapped, args: dict[str, str], profile: Profile, total: int, start: int, count: int
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "title": f"Google Custom Search - {args.get('q', '')}",
        "totalResults": str(total),
        "searchTerms": args.get("q", ""),
        "count": count,
        "startIndex": start,
        "inputEncoding": "utf8",
        "outputEncoding": "utf8",
        "safe": args.get("safe", "off"),
        "cx": args.get("cx", profile.name),
    }
    for p in ("lr", "hl", "gl", "cr", "siteSearch", "siteSearchFilter", "fileType",
              "exactTerms", "excludeTerms", "orTerms", "dateRestrict", "sort", "searchType"):
        if v := args.get(p):
            block[p] = v
    return block


def build(
    served: Served,
    m: Mapped,
    args: dict[str, str],
    profile: Profile,
    *,
    total_results_mode: str,
    public_base_url: str,
) -> dict[str, Any]:
    image_mode = args.get("searchType") == "image"
    terms = extract_terms(m.bold_terms_source)
    total = _total_results(served, total_results_mode)

    queries: dict[str, Any] = {
        "request": [_request_block(m, args, profile, total, m.start, m.num)]
    }

    # nextPage is the authoritative "is there more" signal -- it is what real
    # clients actually branch on, and unlike totalResults we can compute it
    # truthfully (the accumulator fetches one result past the window).
    next_start = m.start + m.num
    if served.has_more and next_start + m.num - 1 <= MAX_INDEX:
        queries["nextPage"] = [
            _request_block(m, args, profile, total, next_start, m.num)
        ]
    if m.start > 1:
        prev_start = max(1, m.start - m.num)
        queries["previousPage"] = [
            _request_block(m, args, profile, total, prev_start, m.num)
        ]

    body: dict[str, Any] = {
        "kind": "customsearch#search",
        "url": {
            "type": "application/json",
            "template": (
                f"{public_base_url.rstrip('/')}/customsearch/v1"
                "?q={searchTerms}&num={count?}&start={startIndex?}&cx={cx?}"
            ),
        },
        "queries": queries,
        "context": {"title": profile.title},
        "searchInformation": {
            # The time to serve THIS request (~0 on a cache hit). Honest.
            # Upstream fetch time goes in the X-Upstream-Time-Ms header rather
            # than being laundered into the body.
            "searchTime": round(served.took_ms / 1000, 3),
            "formattedSearchTime": f"{served.took_ms / 1000:.2f}",
            "totalResults": str(total),
            "formattedTotalResults": f"{total:,}",
        },
        "items": [_item(r, terms, image_mode) for r in served.items],
    }

    if served.entry.corrections:
        corrected = served.entry.corrections[0]
        body["spelling"] = {
            "correctedQuery": corrected,
            "htmlCorrectedQuery": bold(corrected, terms),
        }

    return body
