"""Google Custom Search params -> SearXNG params.

Where a Google param has no SearXNG equivalent we say so, rather than quietly
pretending. Unsupported params are collected and reported back in the
X-Unsupported-Params response header (or rejected outright when STRICT_PARAMS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.models import SearchParams
from ..searxng.qbuilder import EngineCatalog, build_query
from .profiles import Profile

MAX_INDEX = 100  # Google's own ceiling: start + num - 1 <= 100

# Params Google accepts that we cannot honour. Ignored leniently by default
# (Google itself is lenient); STRICT_PARAMS turns them into a 400.
UNSUPPORTED = (
    "c2coff", "googlehost", "hq", "imgSize", "imgType", "imgColorType",
    "imgDominantColor", "linkSite", "relatedSite", "rights", "lowRange",
    "highRange", "filter", "hl",
)

_LR = re.compile(r"^lang_([A-Za-z]{2,3}(?:-[A-Za-z]{2})?)$")
_DATE_RESTRICT = re.compile(r"^([dwmy])(\d*)$", re.I)

_SAFE = {"off": 0, "medium": 1, "active": 2, "high": 2}


class ParamError(ValueError):
    def __init__(self, param: str, reason: str) -> None:
        super().__init__(reason)
        self.param = param
        self.reason = reason


@dataclass
class Mapped:
    params: SearchParams
    start: int
    num: int
    sort_by_date: bool = False
    site_filter: tuple[str, str] | None = None  # (host, "i"|"e")
    unsupported: list[str] = field(default_factory=list)
    bold_terms_source: str = ""


def _date_restrict_to_time_range(v: str) -> str | None:
    """Lossy, and deliberately so.

    Google takes a count (`d3` = last 3 days). SearXNG has four fixed buckets.
    We round UP to the nearest bucket that still contains the requested window:
    under-returning results would be the worse failure. The count is discarded.
    """
    m = _DATE_RESTRICT.match(v.strip())
    if not m:
        raise ParamError("dateRestrict", f"malformed dateRestrict: {v!r}")
    unit, n_raw = m.group(1).lower(), m.group(2)
    n = int(n_raw) if n_raw else 1

    if unit == "d":
        return "day" if n <= 1 else ("week" if n <= 7 else ("month" if n <= 31 else "year"))
    if unit == "w":
        return "week" if n <= 1 else ("month" if n <= 4 else "year")
    if unit == "m":
        return "month" if n <= 1 else ("year" if n <= 12 else None)
    return "year" if n <= 1 else None  # y2+ is wider than SearXNG can express


def map_params(
    args: dict[str, str],
    profile: Profile,
    catalog: EngineCatalog,
    *,
    strict: bool = False,
    default_safesearch: int = 0,
) -> Mapped:
    q = (args.get("q") or "").strip()
    if not q:
        raise ParamError("q", "Required parameter: q")

    try:
        num = int(args.get("num", 10))
    except ValueError:
        raise ParamError("num", "num must be an integer") from None
    try:
        start = int(args.get("start", 1))
    except ValueError:
        raise ParamError("start", "start must be an integer") from None

    num = max(1, min(10, num))  # Google's range; clamp rather than reject
    start = max(1, start)
    if start > MAX_INDEX:
        raise ParamError("start", f"start must be <= {MAX_INDEX}")
    if start + num - 1 > MAX_INDEX:
        num = MAX_INDEX - start + 1

    unsupported = [p for p in UNSUPPORTED if args.get(p)]
    if strict and unsupported:
        raise ParamError(unsupported[0], f"unsupported parameter: {unsupported[0]}")

    # --- language: lr is authoritative; gl/cr may only refine it to a locale ---
    language = "auto"
    if lr := args.get("lr"):
        m = _LR.match(lr.strip())
        if not m:
            raise ParamError("lr", f"malformed lr: {lr!r}")
        language = m.group(1)

    region = args.get("gl") or ""
    if not region and (cr := args.get("cr", "")).startswith("country"):
        if "|" in cr or "." in cr:
            # Boolean expressions like countryUS|countryGB have no SearXNG
            # equivalent -- there is no country dimension separate from locale.
            unsupported.append("cr")
        else:
            region = cr[len("country") :]
    if region and "-" not in language and language != "auto":
        language = f"{language}-{region.upper()}"

    # --- safesearch ---
    safesearch = default_safesearch
    if safe := args.get("safe"):
        if safe.lower() not in _SAFE:
            raise ParamError("safe", f"safe must be one of {sorted(_SAFE)}")
        safesearch = _SAFE[safe.lower()]

    time_range = None
    if dr := args.get("dateRestrict"):
        time_range = _date_restrict_to_time_range(dr)

    # --- sort ---
    sort_by_date = False
    if sort := args.get("sort"):
        if sort.strip().lower() in ("date", "date:d", "date:a"):
            sort_by_date = True
        else:
            # Structured sorts (review-rating:d:s) rely on Google's own indexed
            # metadata. Nothing here can approximate them.
            raise ParamError("sort", f"unsupported sort expression: {sort!r}")

    # --- categories: searchType=image overrides the cx profile ---
    categories = profile.categories
    if args.get("searchType") == "image":
        categories = ("images",)

    site_search = args.get("siteSearch")
    site_filter_mode = (args.get("siteSearchFilter") or "i").lower()
    if site_filter_mode not in ("i", "e"):
        raise ParamError("siteSearchFilter", "siteSearchFilter must be 'i' or 'e'")

    q_final = build_query(
        q,
        catalog,
        exact_terms=args.get("exactTerms"),
        exclude_terms=args.get("excludeTerms"),
        or_terms=args.get("orTerms"),
        site_search=site_search,
        site_search_filter=site_filter_mode,
        file_type=args.get("fileType"),
    )

    site_filter = None
    if site_search:
        host = site_search.strip().lstrip("*.").lower()
        if host:
            site_filter = (host, site_filter_mode)

    return Mapped(
        params=SearchParams(
            q=q_final,
            categories=tuple(categories),
            engines=tuple(profile.engines),
            language=language,
            safesearch=safesearch,
            time_range=time_range,
        ),
        start=start,
        num=num,
        sort_by_date=sort_by_date,
        site_filter=site_filter,
        unsupported=unsupported,
        bold_terms_source=q,
    )
