"""Build the `q` string we send upstream.

Two jobs:

1. Emulate Google's siteSearch/fileType/exactTerms/excludeTerms/orTerms by
   rewriting the query. This works because SearXNG does NOT interpret `site:`,
   `filetype:`, quotes or `-exclusions` -- it hands them to the upstream engine
   verbatim (searx/search/processors/abstract.py:268).

2. Guard against SearXNG's OWN query parser eating the user's words. It strips
   any whitespace-delimited token it can parse as an engine bang, a language, or
   a timeout (searx/query.py:_parse_query). A user searching for `!important` or
   `<3` would otherwise have that word silently deleted from their query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# searx/webutils.py:32
VALID_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(-[a-z]{2})?$")

# A language *name* or country name, e.g. :english, :united-kingdom. Matching
# these exactly would need SearXNG's sxng_locales table; this errs toward
# guarding, which is the safe direction (an over-guarded token becomes a quoted
# phrase rather than being deleted outright).
_NAMEISH = re.compile(r"^[a-z]+(-[a-z]+)*$")

_WS = re.compile(r"\s+")


@dataclass
class EngineCatalog:
    """The exact set of tokens the live SearXNG instance will consume, pulled
    from its own /config so the guard mirrors that instance and not our guess."""

    engines: set[str] = field(default_factory=set)
    shortcuts: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: dict) -> "EngineCatalog":
        engines = {e["name"].lower() for e in cfg.get("engines", []) if e.get("name")}
        shortcuts = {
            e["shortcut"].lower() for e in cfg.get("engines", []) if e.get("shortcut")
        }
        categories = {c.lower() for c in cfg.get("categories", [])}
        return cls(engines=engines, shortcuts=shortcuts, categories=categories)

    def eats_bang(self, value: str) -> bool:
        # searx/query.py:185 -- '-' and '_' both normalize to a space
        v = value.replace("-", " ").replace("_", " ").lower()
        return v in self.shortcuts or v in self.engines or v in self.categories


def _would_be_eaten(token: str, cat: EngineCatalog) -> bool:
    if not token:
        return False
    head = token[0]

    if head == "<":  # TimeoutParser: only if the remainder is all digits
        return token[1:].isdigit()

    if head == ":":  # LanguageParser
        v = token[1:].lower().replace("_", "-")
        if not v:
            return False
        return bool(VALID_LANGUAGE_CODE.match(v)) or v == "auto" or bool(_NAMEISH.match(v))

    if head == "!":
        if token.startswith("!!"):
            # ExternalBangParser (DDG bang list) / FeelingLucky. We cannot see
            # that list from here, so guard the whole shape.
            return True
        return cat.eats_bang(token[1:])

    return False


def guard(q: str, cat: EngineCatalog) -> str:
    """Neutralize tokens SearXNG would otherwise consume, by quoting them.

    Quoting works because every parser dispatches on the token's FIRST character
    (searx/query.py:check), and a leading `"` matches none of them. The token
    then reaches the engines as a literal.

    Caveat, and it is a real one: a guarded token's semantics shift from "word"
    to "exact phrase". For a single token that is near-identical on every major
    engine, and it is the only lever available -- SearXNG has no escape syntax.
    """
    out = []
    for token in _WS.split(q.strip()):
        if not token:
            continue
        if token.startswith('"'):
            out.append(token)  # already a phrase; leave it alone
        elif _would_be_eaten(token, cat):
            out.append('"' + token.replace('"', "") + '"')
        else:
            out.append(token)
    return " ".join(out)


def _phrase(term: str) -> str:
    term = term.strip().replace('"', "")
    if not term:
        return ""
    return f'"{term}"' if _WS.search(term) else term


def build_query(
    q: str,
    cat: EngineCatalog,
    *,
    exact_terms: str | None = None,
    exclude_terms: str | None = None,
    or_terms: str | None = None,
    site_search: str | None = None,
    site_search_filter: str = "i",  # i = include, e = exclude
    file_type: str | None = None,
) -> str:
    """User query first (guarded), then the operators we synthesize.

    Only the user's own text needs guarding -- the operators we append never
    begin with `!`, `:` or `<`.
    """
    parts = [guard(q, cat)]

    if exact_terms:
        parts.append(f'"{exact_terms.strip().replace(chr(34), "")}"')

    if site_search:
        host = site_search.strip().lstrip("*.").replace('"', "")
        if host:
            parts.append(f"-site:{host}" if site_search_filter == "e" else f"site:{host}")

    if file_type:
        ft = file_type.strip().lstrip(".").replace('"', "")
        if ft:
            parts.append(f"filetype:{ft}")

    if exclude_terms:
        for term in _WS.split(exclude_terms.strip()):
            p = _phrase(term)
            if p:
                parts.append(f"-{p}")

    if or_terms:
        terms = [_phrase(t) for t in _WS.split(or_terms.strip())]
        terms = [t for t in terms if t]
        if len(terms) == 1:
            parts.append(terms[0])
        elif terms:
            parts.append("(" + " OR ".join(terms) + ")")

    return " ".join(p for p in parts if p).strip()
