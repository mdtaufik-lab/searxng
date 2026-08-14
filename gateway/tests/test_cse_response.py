from __future__ import annotations

import pytest

from app.google.highlight import bold, extract_terms
from app.google.params import ParamError, _date_restrict_to_time_range, map_params
from app.google.profiles import Profile

WEB = Profile(name="web", title="Web Search", categories=("general",), engines=())


# --- bolding ---------------------------------------------------------------

def test_bold_wraps_query_terms():
    assert bold("Python packaging guide", ["python"]) == "<b>Python</b> packaging guide"


def test_bold_escapes_before_bolding():
    assert bold("a <b>tag</b> here", ["tag"]) == "a &lt;b&gt;<b>tag</b>&lt;/b&gt; here"


def test_bold_does_not_corrupt_html_entities():
    """The bug everyone hits: escaping mints &amp;, and a term like "amp" then
    matches INSIDE it, producing corrupt HTML."""
    out = bold("Fish & Chips", ["amp"])
    assert out == "Fish &amp; Chips"  # the entity survives intact
    assert "<b>amp</b>" not in out


def test_bold_prefers_the_longest_match():
    out = bold("machine learning models", ["machine learning", "machine"])
    assert "<b>machine learning</b>" in out


def test_extract_terms_drops_operators_and_exclusions():
    terms = extract_terms('python "web framework" -django site:example.com OR flask')
    assert "web framework" in terms
    assert "python" in terms and "flask" in terms
    assert "django" not in terms  # excluded terms are never bolded
    assert not any(":" in t for t in terms)


# --- dateRestrict ----------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("d1", "day"), ("d3", "week"), ("d20", "month"), ("d200", "year"),
        ("w1", "week"), ("w3", "month"), ("m1", "month"), ("m6", "year"),
        ("y1", "year"),
        ("y5", None),  # wider than SearXNG can express -> no filter, not a lie
    ],
)
def test_date_restrict_buckets(value, expected):
    assert _date_restrict_to_time_range(value) == expected


# --- params ----------------------------------------------------------------

def test_num_and_start_never_reach_searxng(catalog):
    m = map_params({"q": "x", "num": "10", "start": "11"}, WEB, catalog)
    assert m.start == 11 and m.num == 10
    # SearXNG has no num/start, so they must live only in the window, never in
    # the params we send upstream.
    assert not hasattr(m.params, "num") and not hasattr(m.params, "start")


def test_start_beyond_googles_ceiling_is_rejected(catalog):
    with pytest.raises(ParamError):
        map_params({"q": "x", "start": "200"}, WEB, catalog)


def test_num_is_clamped_to_googles_range(catalog):
    assert map_params({"q": "x", "num": "50"}, WEB, catalog).num == 10


def test_safe_maps_to_safesearch(catalog):
    assert map_params({"q": "x", "safe": "active"}, WEB, catalog).params.safesearch == 2
    assert map_params({"q": "x", "safe": "off"}, WEB, catalog).params.safesearch == 0


def test_lr_and_gl_combine_into_a_locale(catalog):
    m = map_params({"q": "x", "lr": "lang_en", "gl": "gb"}, WEB, catalog)
    assert m.params.language == "en-GB"


def test_unsupported_params_are_reported_not_silently_dropped(catalog):
    m = map_params({"q": "x", "imgSize": "large", "hl": "de"}, WEB, catalog)
    assert "imgSize" in m.unsupported and "hl" in m.unsupported


def test_strict_mode_rejects_unsupported_params(catalog):
    with pytest.raises(ParamError):
        map_params({"q": "x", "imgSize": "large"}, WEB, catalog, strict=True)


def test_structured_sort_is_rejected_rather_than_faked(catalog):
    with pytest.raises(ParamError):
        map_params({"q": "x", "sort": "review-rating:d:s"}, WEB, catalog)


def test_cr_boolean_expression_is_flagged_unsupported(catalog):
    m = map_params({"q": "x", "cr": "countryUS|countryGB"}, WEB, catalog)
    assert "cr" in m.unsupported


def test_search_type_image_overrides_the_profile_category(catalog):
    m = map_params({"q": "x", "searchType": "image"}, WEB, catalog)
    assert m.params.categories == ("images",)


def test_missing_q_is_a_400(catalog):
    with pytest.raises(ParamError):
        map_params({}, WEB, catalog)
