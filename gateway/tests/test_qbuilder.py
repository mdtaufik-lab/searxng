"""The guard. SearXNG silently DELETES tokens it can parse as a bang, a language
or a timeout, so an unguarded `<3 emoji` searches for just "emoji"."""

from __future__ import annotations

import pytest

from app.searxng.qbuilder import build_query, guard


@pytest.mark.parametrize(
    "query, expected",
    [
        # Eaten by SearXNG -> must be quoted.
        ("<3 emoji meaning", '"<3" emoji meaning'),
        (":en language tag", '":en" language tag'),
        ("covid :fr news", 'covid ":fr" news'),
        ("!ddg is down", '"!ddg" is down'),            # ddg IS a shortcut
        ("search !images today", 'search "!images" today'),  # a category
        ("!!g something", '"!!g" something'),          # external bang
        # NOT eaten -> must be left completely alone. Over-guarding turns a word
        # into a phrase, which changes the search.
        ("why is !important used in css", "why is !important used in css"),
        ("the <script> tag", "the <script> tag"),
        ("smiley :) meaning", "smiley :) meaning"),
        ("!google is down", "!google is down"),  # no engine named exactly "google"
        ("plain query", "plain query"),
    ],
)
def test_guard(query, expected, catalog):
    assert guard(query, catalog) == expected


def test_guard_leaves_existing_phrases_alone(catalog):
    assert guard('"exact phrase" here', catalog) == '"exact phrase" here'


def test_google_operators_are_emulated_by_rewriting_the_query(catalog):
    q = build_query(
        "climate report",
        catalog,
        site_search="nasa.gov",
        file_type="pdf",
        exclude_terms="draft",
        or_terms="2025 2026",
        exact_terms="sea level",
    )
    assert q == 'climate report "sea level" site:nasa.gov filetype:pdf -draft (2025 OR 2026)'


def test_site_search_exclusion(catalog):
    q = build_query("news", catalog, site_search="example.com", site_search_filter="e")
    assert "-site:example.com" in q


def test_operators_do_not_get_guarded(catalog):
    """Our own injected operators must survive: none of them start with ! : or <."""
    q = build_query("test", catalog, site_search="a.com", file_type="pdf")
    assert "site:a.com" in q and '"site:a.com"' not in q
