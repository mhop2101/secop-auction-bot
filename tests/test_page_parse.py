"""Acceptance tests for :mod:`secop_firstbid.page_parse` (plan §5) — the milestone-5 gate.

Most of this file runs against the two committed, scrubbed pages under ``tests/fixtures/``,
so a fresh checkout exercises the parser. The tests at the bottom run against the real
saved artifacts at the repository root and skip when those are absent: they are the
acceptance gate that the parser matches bytes an operator actually saved, which no
re-creation can stand in for.

Beyond the acceptance table these tests pin the three ways this parser is known to fail:
the naive ``externalId=([0-9a-f-]{36})`` regex (matches zero bytes on either page),
``parse_qs`` on the un-de-concatenated ``onclick`` (returns quote-junk keys), and
de-concatenating before unescaping (``&amp;`` survives as a separator and swallows the
following key).
"""

from __future__ import annotations

import fnmatch
import html
import re
import urllib.parse

import pytest

from conftest import (
    AUCTION_ID,
    BEFOREBID_HTML,
    BID_HTML,
    EAUCTION_DOC_ID,
    FIXTURE_AUCTION_ID,
    FIXTURE_COMPANY_OPTION,
    FIXTURE_DOC_ID,
    FIXTURE_MKEY,
    FIXTURE_PARENT_DOSSIER,
    LAUNCH_PAGE,
    OPPORTUNITY_PAGE,
    REPO_ROOT,
    require_real_page,
)
from secop_firstbid import page_parse
from secop_firstbid.errors import Failure, SecopError

NAIVE_EXTERNAL_ID = r"externalId=([0-9a-f-]{36})"

MKEY = "5f1742a6_49c6_4580_be1f_374ba783e23c.4e56070c_05a7_4aa6_aed2_df63be6dbccb"
REDIRECT_PATH = "/CO1BusinessLine/Tendering/WidgetAuction/RedirectToEAuction"
PARENT_DOSSIER = "CO1.OPDOS.31397606"
# The real page carries a nine-digit company code in a selected <option>; the exact code
# identifies the bidder, so it is matched by shape rather than spelled out here.
COMPANY_OPTION_PATTERN = re.compile(r'<option[^>]*selected="true"[^>]*value="\d{9}"')

GUID_A = "11111111-2222-3333-4444-555555555555"
GUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GUID_ABSENT = "99999999-8888-7777-6666-555555555555"


def _onclick_row(index: int, guid: str, doc_id: str, state: str) -> str:
    """One grid row in the page's own shape: a JS concatenation, HTML-escaped ``&``."""
    return (
        '<tr id="grdEAuctionGrid_tr{i}"><td id="grdtd_thEAuctionReferenceCol">'
        '<a id="lnkEAuctionViewLinkNew_{i}" onclick="javascript:getAction('
        "'/CO1BusinessLine/Tendering/WidgetAuction/RedirectToEAuction' + '?' + "
        "'docUniqueIdentifier=' + '{doc}' + '&amp;' + 'market=' + 'GovCo' + '&amp;' + "
        "'externalId=' + '{guid}' + '&amp;mkey=key_{i}',true);"
        '" href="javascript:void(0);">Subasta {i}</a></td>'
        '<td id="grdtd_thEAuctionStateCol">'
        '<a id="lnkState_{i}" href="javascript:void(0);">{state}</a></td></tr>'
    ).format(i=index, guid=guid, doc=doc_id, state=state)


def _synthetic_page(*rows: str) -> str:
    return (
        "<html><head><title>Oportunidades</title></head><body><table><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        '<select size="1" id="languageSelector" name="languageSelector">'
        '<option value="es-CO" selected="true">Espa&ntilde;ol</option>'
        '<option value="en">English</option></select></body></html>'
    )


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture()
def two_auction_page(tmp_path) -> str:
    return _write(
        tmp_path,
        "two.html",
        _synthetic_page(
            _onclick_row(0, GUID_A, "CO1.EAREQ.111111", "Terminado"),
            _onclick_row(1, GUID_B, "CO1.EAREQ.222222", "En curso"),
        ),
    )


def test_opportunity_page_acceptance_table():
    ref = page_parse.parse_auction_page(str(OPPORTUNITY_PAGE))

    assert ref.auction_id == FIXTURE_AUCTION_ID
    assert ref.market == "GovCo"
    assert ref.eauction_doc_id == FIXTURE_DOC_ID
    assert ref.lang == "es-CO"
    assert ref.state_text == "Terminado"
    assert ref.source_extractor == "onclick"


def test_opportunity_page_carries_the_session_scoped_extras():
    ref = page_parse.parse_auction_page(str(OPPORTUNITY_PAGE))

    assert ref.mkey == FIXTURE_MKEY
    assert ref.redirect_path == REDIRECT_PATH
    assert not ref.redirect_path.startswith("http")
    assert ref.lang_defaulted is False
    assert ref.market_defaulted is False


def test_launch_page_acceptance_table():
    ref = page_parse.parse_auction_page(str(LAUNCH_PAGE))

    assert ref.auction_id == FIXTURE_AUCTION_ID
    assert ref.lang == "es-CO"
    assert ref.market == "GovCo"
    assert ref.source_extractor == "iframe"
    assert ref.eauction_doc_id is None
    assert ref.state_text is None


def test_onclick_yields_two_anchors_for_one_auction(opportunity_page_html):
    candidates = page_parse.extract_candidates_onclick(opportunity_page_html)

    assert len(candidates) == 2
    assert set(c.external_id for c in candidates) == {FIXTURE_AUCTION_ID}
    with_doc = [c for c in candidates if c.doc_id]
    assert len(with_doc) == 1
    assert with_doc[0].doc_id == FIXTURE_DOC_ID
    assert with_doc[0].redirect_path == REDIRECT_PATH
    assert all(c.market == "GovCo" and c.row_index == 0 for c in candidates)


def test_each_page_matches_exactly_one_extractor(opportunity_page_html, launch_page_html):
    assert page_parse.extract_candidates_iframe(opportunity_page_html) == []
    assert page_parse.extract_candidates_onclick(launch_page_html) == []
    assert page_parse.extract_candidates_fallback(launch_page_html) == []


@pytest.mark.parametrize(
    "path", [OPPORTUNITY_PAGE, LAUNCH_PAGE], ids=["opportunity", "launch"]
)
@pytest.mark.parametrize("unescape", [False, True], ids=["raw", "unescaped"])
def test_naive_external_id_regex_matches_zero_bytes(path, unescape):
    text = path.read_text(encoding="utf-8")
    if unescape:
        text = html.unescape(text)

    assert re.findall(NAIVE_EXTERNAL_ID, text) == []


def test_concatenation_aware_fallback_finds_what_the_naive_regex_cannot(
    opportunity_page_html,
):
    candidates = page_parse.extract_candidates_fallback(opportunity_page_html)

    assert len(candidates) == 1
    assert candidates[0].external_id == FIXTURE_AUCTION_ID
    assert candidates[0].doc_id == FIXTURE_DOC_ID
    assert candidates[0].extractor == "fallback"


def test_doc_id_anchor_excludes_the_parent_dossier(opportunity_page_html):
    text = html.unescape(opportunity_page_html)

    assert page_parse.FB_DOC.findall(text) == [FIXTURE_DOC_ID]
    assert FIXTURE_PARENT_DOSSIER in text


def test_parse_qs_on_the_raw_onclick_returns_quote_junk(opportunity_page_html):
    anchor = page_parse.ANCHOR.search(html.unescape(opportunity_page_html)).group(0)
    query = anchor.split("?", 1)[1].split("',true)")[0]

    params = urllib.parse.parse_qs(query)

    assert params.get("externalId") is None
    assert any(key != "externalId" and key.endswith("externalId") for key in params)


def test_deconcatenation_makes_it_a_real_query_string(opportunity_page_html):
    anchor = page_parse.ANCHOR.search(html.unescape(opportunity_page_html)).group(0)
    joined = page_parse.DECONCAT.sub("", anchor)
    query = joined.split("?", 1)[1].split("',true)")[0]

    params = urllib.parse.parse_qs(query)

    assert params["externalId"] == [FIXTURE_AUCTION_ID]
    assert params["docUniqueIdentifier"] == [FIXTURE_DOC_ID]
    assert params["market"] == ["GovCo"]
    assert params["mkey"] == [FIXTURE_MKEY]


def test_unescaping_must_precede_deconcatenation(opportunity_page_html):
    anchor = page_parse.ANCHOR.search(opportunity_page_html).group(0)
    joined = page_parse.DECONCAT.sub("", anchor)
    query = joined.split("?", 1)[1].split("',true)")[0]

    params = urllib.parse.parse_qs(query)

    assert "externalId" not in params
    assert "amp;externalId" in params


def test_state_text_strips_the_nested_anchor(opportunity_page_html):
    assert page_parse.extract_state_text(opportunity_page_html) == "Terminado"


def test_state_text_absent_on_the_launch_page_is_unknown(launch_page_html):
    assert page_parse.extract_state_text(launch_page_html) is None


def test_extract_lang_on_both_pages(opportunity_page_html, launch_page_html):
    assert page_parse.extract_lang(opportunity_page_html) == "es-CO"
    assert page_parse.extract_lang(launch_page_html) == "es-CO"


def test_extract_lang_ignores_the_company_selector(opportunity_page_html):
    """Both pages carry an earlier selected numeric ``<option>`` (the company selector)."""
    assert FIXTURE_COMPANY_OPTION in opportunity_page_html
    assert page_parse.extract_lang(opportunity_page_html) != FIXTURE_COMPANY_OPTION


def test_two_auction_page_is_ambiguous(two_auction_page):
    with pytest.raises(SecopError) as excinfo:
        page_parse.parse_auction_page(two_auction_page)

    error = excinfo.value
    assert error.code is Failure.PARSE_AMBIGUOUS
    listed = error.detail["candidates"]
    assert set(c["external_id"] for c in listed) == {GUID_A, GUID_B}
    assert set(c["doc_id"] for c in listed) == {"CO1.EAREQ.111111", "CO1.EAREQ.222222"}
    assert set(c["row_index"] for c in listed) == {"0", "1"}
    assert set(c["state_text"] for c in listed) == {"Terminado", "En curso"}


def test_override_present_on_the_page_resolves_ambiguity(two_auction_page):
    ref = page_parse.parse_auction_page(two_auction_page, auction_id_override=GUID_B)

    assert ref.auction_id == GUID_B
    assert ref.eauction_doc_id == "CO1.EAREQ.222222"
    assert ref.state_text == "En curso"
    assert ref.source_extractor == "override"


def test_override_absent_from_the_page_still_aborts(two_auction_page):
    with pytest.raises(SecopError) as excinfo:
        page_parse.parse_auction_page(two_auction_page, auction_id_override=GUID_ABSENT)

    assert excinfo.value.code is Failure.PARSE_AMBIGUOUS
    assert excinfo.value.detail["auction_id_override"] == GUID_ABSENT


def test_override_absent_from_an_unambiguous_page_aborts():
    with pytest.raises(SecopError) as excinfo:
        page_parse.parse_auction_page(
            str(OPPORTUNITY_PAGE), auction_id_override=GUID_ABSENT
        )

    assert excinfo.value.code is Failure.PARSE_FAILED


def test_override_matching_the_only_candidate_keeps_the_extractor():
    ref = page_parse.parse_auction_page(
        str(OPPORTUNITY_PAGE), auction_id_override=FIXTURE_AUCTION_ID
    )

    assert ref.auction_id == FIXTURE_AUCTION_ID
    assert ref.source_extractor == "onclick"


def test_a_page_of_bare_guids_is_not_parsed(tmp_path):
    """No bare-GUID fallback: an unanchored GUID is never an auction id."""
    page = "<html><body>" + "".join(
        '<li id="ctx_{i}" data-guid="{g}">menu</li>'.format(i=i, g=GUID_A)
        for i in range(7)
    ) + '<li data-guid="{g}">target</li></body></html>'.format(g=GUID_B)

    with pytest.raises(SecopError) as excinfo:
        page_parse.parse_auction_page(_write(tmp_path, "guids.html", page))

    assert excinfo.value.code is Failure.PARSE_FAILED


def test_missing_file_is_a_parse_failure(tmp_path):
    with pytest.raises(SecopError) as excinfo:
        page_parse.parse_auction_page(str(tmp_path / "absent.html"))

    assert excinfo.value.code is Failure.PARSE_FAILED


def test_defaults_are_flagged_when_no_extractor_supplied_them(tmp_path):
    page = '<html><body><span>externalId={g}</span></body></html>'.format(g=GUID_A)

    ref = page_parse.parse_auction_page(_write(tmp_path, "bare.html", page))

    assert ref.auction_id == GUID_A
    assert ref.source_extractor == "fallback"
    assert ref.lang == "es-CO" and ref.lang_defaulted is True
    assert ref.market == "GovCo" and ref.market_defaulted is True
    assert ref.eauction_doc_id is None


def _gitignore_patterns():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def test_gitignore_keeps_the_real_pages_out_and_the_scrubbed_fixtures_in():
    """A checkout that cannot run its own suite is the defect this pins down."""
    patterns = _gitignore_patterns()

    assert "/beforebid.html" in patterns
    assert "/bid.html" in patterns
    # unanchored, these would also match tests/fixtures/beforebid.html
    assert "beforebid.html" not in patterns
    assert "bid.html" not in patterns

    for path in (OPPORTUNITY_PAGE, LAUNCH_PAGE):
        assert path.is_file()
        relative = path.relative_to(REPO_ROOT).as_posix()
        for pattern in patterns:
            assert not fnmatch.fnmatch(relative, pattern.lstrip("/"))
            assert not fnmatch.fnmatch(path.name, pattern)


def test_a_missing_real_page_skips_rather_than_fails(tmp_path):
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_real_page(tmp_path / "beforebid.html")

    assert "beforebid.html" in str(excinfo.value)
    assert "opportunity_page.html" in str(excinfo.value)


def test_beforebid_acceptance_table():
    require_real_page(BEFOREBID_HTML)
    ref = page_parse.parse_auction_page(str(BEFOREBID_HTML))

    assert ref.auction_id == AUCTION_ID
    assert ref.market == "GovCo"
    assert ref.eauction_doc_id == EAUCTION_DOC_ID
    assert ref.lang == "es-CO"
    assert ref.state_text == "Terminado"
    assert ref.source_extractor == "onclick"


def test_beforebid_carries_the_session_scoped_extras():
    require_real_page(BEFOREBID_HTML)
    ref = page_parse.parse_auction_page(str(BEFOREBID_HTML))

    assert ref.mkey == MKEY
    assert ref.redirect_path == REDIRECT_PATH
    assert ref.lang_defaulted is False
    assert ref.market_defaulted is False


def test_bid_acceptance_table():
    require_real_page(BID_HTML)
    ref = page_parse.parse_auction_page(str(BID_HTML))

    assert ref.auction_id == AUCTION_ID
    assert ref.lang == "es-CO"
    assert ref.market == "GovCo"
    assert ref.source_extractor == "iframe"
    assert ref.eauction_doc_id is None
    assert ref.state_text is None


def test_real_beforebid_yields_two_anchors_and_one_doc_id(beforebid_html):
    candidates = page_parse.extract_candidates_onclick(beforebid_html)

    assert len(candidates) == 2
    assert set(c.external_id for c in candidates) == {AUCTION_ID}
    assert [c.doc_id for c in candidates if c.doc_id] == [EAUCTION_DOC_ID]
    assert page_parse.FB_DOC.findall(html.unescape(beforebid_html)) == [EAUCTION_DOC_ID]
    assert PARENT_DOSSIER in beforebid_html


@pytest.mark.parametrize("path", [BEFOREBID_HTML, BID_HTML], ids=["beforebid", "bid"])
@pytest.mark.parametrize("unescape", [False, True], ids=["raw", "unescaped"])
def test_naive_external_id_regex_matches_zero_real_bytes(path, unescape):
    require_real_page(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if unescape:
        text = html.unescape(text)

    assert re.findall(NAIVE_EXTERNAL_ID, text) == []


def test_real_pages_match_exactly_one_extractor_each(beforebid_html, bid_html):
    assert page_parse.extract_candidates_iframe(beforebid_html) == []
    assert page_parse.extract_candidates_onclick(bid_html) == []
    assert page_parse.extract_candidates_fallback(bid_html) == []


def test_real_pages_resolve_the_locale_over_the_company_selector(
    beforebid_html, bid_html
):
    assert COMPANY_OPTION_PATTERN.search(beforebid_html)
    assert page_parse.extract_lang(beforebid_html) == "es-CO"
    assert page_parse.extract_lang(bid_html) == "es-CO"
