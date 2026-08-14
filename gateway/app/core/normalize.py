from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SearchParams

CACHE_VERSION = "v1"

_WS = re.compile(r"\s+")

# Params that identify a campaign, not a document. Two URLs differing only in
# these are the same result and must dedupe together.
_TRACKING = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|gbraid|wbraid|msclkid|mc_[ce]id|igshid|"
    r"ref|ref_src|source|spm|_hsenc|_hsmi|yclid|dclid|twclid|si)$",
    re.I,
)


def normalize_q(q: str) -> str:
    """Collapse whitespace. Case is preserved -- some engines are case-sensitive
    for operators like OR -- but the cache key casefolds separately."""
    return _WS.sub(" ", q).strip()


def cache_key(params: SearchParams) -> str:
    payload = params.as_key_dict()
    payload["q"] = payload["q"].casefold()  # cache-only: raises the hit rate
    payload["categories"] = sorted(payload["categories"])
    payload["engines"] = sorted(payload["engines"])
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sx:{CACHE_VERSION}:{hashlib.sha256(blob.encode()).hexdigest()}"


def query_hash(params: SearchParams) -> str:
    """For the usage log: identifies a query without storing its text."""
    return hashlib.sha256(
        json.dumps(params.as_key_dict(), sort_keys=True).encode()
    ).hexdigest()


def dedupe_url(url: str) -> str:
    """Identity of a result for cross-page dedup.

    SearXNG dedupes within one page only; pages 1 and 2 are independent searches,
    so duplicates across them are routine and removing them is our job.
    """
    if not url:
        return ""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip().casefold()

    host = (s.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    if s.port and not (
        (s.scheme == "http" and s.port == 80) or (s.scheme == "https" and s.port == 443)
    ):
        host = f"{host}:{s.port}"

    path = re.sub(r"/+", "/", s.path or "/").rstrip("/") or "/"

    kept = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if not _TRACKING.match(k)]
    kept.sort()

    # Scheme and fragment are dropped: http/https and #anchors are the same doc.
    return urlunsplit(("", host, path, urlencode(kept), ""))
