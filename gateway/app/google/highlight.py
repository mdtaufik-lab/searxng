"""Google bolds query terms in htmlTitle / htmlSnippet. We reproduce that."""

from __future__ import annotations

import html
import re

# Split on entities so we can bold around them but never inside them.
_ENTITY = re.compile(r"(&[a-zA-Z#0-9]+;)")
_QUOTED = re.compile(r'"([^"]+)"')

_MAX_TERMS = 20  # bound the alternation; a pathological query shouldn't melt the regex


def extract_terms(q: str) -> list[str]:
    """The words Google would bold: the user's search terms, minus the syntax."""
    terms: list[str] = [m.group(1).strip() for m in _QUOTED.finditer(q)]

    for tok in _QUOTED.sub(" ", q).split():
        if tok.startswith("-"):  # excluded terms are never bolded
            continue
        if ":" in tok:  # site:, filetype:, intitle:, ...
            continue
        tok = tok.strip("()")
        if tok.upper() == "OR" or len(tok) < 2:
            continue
        terms.append(tok)

    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        k = t.casefold()
        if t and k not in seen:
            seen.add(k)
            out.append(t)

    # Longest first, so a phrase wins over its own constituent words.
    out.sort(key=len, reverse=True)
    return out[:_MAX_TERMS]


def _pattern(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in terms)
    # Lookarounds rather than \b: terms may start or end with non-word chars.
    return re.compile(rf"(?<!\w)({alts})(?!\w)", re.IGNORECASE)


def bold(text: str, terms: list[str]) -> str:
    """HTML-escape `text`, then wrap occurrences of `terms` in <b>.

    Order matters twice:

    1. Escape BEFORE bolding, or html.escape would eat our own <b> tags.
    2. But escaping mints entities (&amp;, &#39;), and a term like "amp" would
       then match *inside* &amp; and produce corrupt HTML. So bold only the
       non-entity segments.
    """
    escaped = html.escape(text or "", quote=False)
    pat = _pattern(terms)
    if pat is None:
        return escaped

    parts = _ENTITY.split(escaped)
    for i in range(0, len(parts), 2):  # odd indices are the entities themselves
        parts[i] = pat.sub(r"<b>\1</b>", parts[i])
    return "".join(parts)
