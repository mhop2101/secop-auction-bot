"""Wire-fidelity tests for :mod:`secop_firstbid.endpoints` (plan §1, §10).

Every literal asserted here was extracted from ``www.secop.gov.co.har``. These are
regression tests against tidying: a URL joiner that collapses ``v1//``, a lower-cased
``Bid/``, an ``Origin`` sent on GETs, or a "cleaned up" User-Agent all produce a request
the server has never been observed to answer.
"""

from __future__ import annotations

import pytest
import urllib3.util.request

from conftest import AUCTION_ID
from secop_firstbid import endpoints
from secop_firstbid.models import AuctionRef

CAPTURED_REFERER = (
    "https://subastas.secop.gov.co/eauctions.site/"
    "?auctionId=d6a49a23-3556-4b3d-9bb4-3e209279b9f8"
    "&languageCode=es-CO&marketCode=GovCo"
)

CAPTURED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

CAPTURED_SEC_CH_UA = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'

ACCEPT_HEADER = "application/json, text/plain, */*"


@pytest.fixture()
def ref() -> AuctionRef:
    return AuctionRef(auction_id=AUCTION_ID, lang="es-CO", market="GovCo")


def test_api_base_keeps_the_double_slash():
    assert endpoints.API_BASE == "https://subastas.secop.gov.co/eauctions.api/v1//"
    assert endpoints.API_BASE.endswith("v1//")


def test_url_preserves_the_double_slash_for_every_route():
    assert (
        endpoints.url(endpoints.validate_bid())
        == "https://subastas.secop.gov.co/eauctions.api/v1//bid/ValidateBid"
    )
    assert (
        endpoints.url(endpoints.get_auction_simple(AUCTION_ID))
        == "https://subastas.secop.gov.co/eauctions.api/v1//auction/GetAuctionSimple/"
        + AUCTION_ID
    )
    assert (
        endpoints.url(endpoints.set_start_bidding_audit(AUCTION_ID))
        == "https://subastas.secop.gov.co/eauctions.api/v1//audit/"
        "SetStartBiddingAudit?auctionId=" + AUCTION_ID
    )
    for name in (
        endpoints.validate_bid(),
        endpoints.create_bid(),
        endpoints.get_min_bid(),
        endpoints.get_auction(AUCTION_ID, "es-CO"),
        endpoints.get_bids_total_info(AUCTION_ID),
        endpoints.get_system_health(),
    ):
        assert "/v1//" in endpoints.url(name)


def test_url_does_not_normalise_slashes():
    assert endpoints.url("/leading") == endpoints.API_BASE + "/leading"
    assert endpoints.url("/leading").endswith("v1///leading")


def test_bids_total_info_has_a_capital_b():
    assert endpoints.get_bids_total_info(AUCTION_ID) == "Bid/GetBidsTotalInfo/" + AUCTION_ID
    assert endpoints.get_bids_total_info(AUCTION_ID).startswith("Bid/")
    assert not endpoints.get_bids_total_info(AUCTION_ID).startswith("bid/")


def test_bid_routes_are_lower_case_bid():
    assert endpoints.validate_bid() == "bid/ValidateBid"
    assert endpoints.create_bid() == "bid/CreateBid"
    assert endpoints.get_min_bid() == "bid/GetMinBid"


def test_route_literals():
    assert endpoints.get_auction_simple(AUCTION_ID) == "auction/GetAuctionSimple/" + AUCTION_ID
    assert (
        endpoints.get_auction(AUCTION_ID, "es-CO")
        == "auction/GetAuction/" + AUCTION_ID + "/es-CO"
    )
    assert (
        endpoints.set_start_bidding_audit(AUCTION_ID)
        == "audit/SetStartBiddingAudit?auctionId=" + AUCTION_ID
    )
    assert (
        endpoints.set_submit_bidding_audit(AUCTION_ID)
        == "audit/SetSubmitBiddingAudit?auctionId=" + AUCTION_ID
    )
    assert endpoints.get_system_health() == "common/GetSystemHealth/"


def test_event_logs_carries_the_datatables_block():
    path = endpoints.get_event_logs(AUCTION_ID)
    assert path.startswith("audit/GetEventLogs?sEcho=1&iColumns=6&sColumns=%2C%2C%2C%2C%2C")
    assert "mDataProp_5=ipAddress" in path
    assert path.endswith("&auctionId=" + AUCTION_ID + "&languageCode=es-CO&isOwner=false&advancedFilter=%7B%7D")


def test_supplier_bids_names_the_auction_the_company_and_the_last_bid_seen():
    assert (
        endpoints.get_supplier_bids(AUCTION_ID, "900000001")
        == "bid/GetSupplierBids/" + AUCTION_ID + "/900000001/0"
    )


def test_observed_on_wire_marks_exactly_the_routes_seen_answering():
    """The five routes of the original capture, and the bid list read live on 2026-11-20.

    ``create_bid`` stays out: the server processed submissions sent through it that day,
    but no response to one has ever been received, so its answer is still unobserved.
    """
    seen = set(name for name, observed in endpoints.OBSERVED_ON_WIRE.items() if observed)
    assert seen == {
        "get_supplier_bids",
        "validate_bid",
        "set_start_bidding_audit",
        "get_auction_simple",
        "get_bids_total_info",
        "get_event_logs",
    }
    assert endpoints.OBSERVED_ON_WIRE["create_bid"] is False
    assert endpoints.OBSERVED_ON_WIRE["set_submit_bidding_audit"] is False
    for name in endpoints.OBSERVED_ON_WIRE:
        assert callable(getattr(endpoints, name))


def test_referer_matches_the_captured_literal(ref):
    assert endpoints.referer(ref) == CAPTURED_REFERER
    assert endpoints.base_headers(ref)["Referer"] == CAPTURED_REFERER


def test_origin_is_post_only(ref):
    assert "Origin" not in endpoints.base_headers(ref)
    assert "Origin" not in endpoints.get_headers(ref)
    assert "Origin" not in endpoints.get_headers(ref, conditional=False)
    assert endpoints.post_headers(ref)["Origin"] == "https://subastas.secop.gov.co"
    assert endpoints.audit_post_headers(ref)["Origin"] == "https://subastas.secop.gov.co"


def test_conditional_pair_is_get_only(ref):
    headers = endpoints.get_headers(ref)
    assert headers["Cache-Control"] == "max-age=0"
    assert headers["If-Modified-Since"] == "Mon, 26 Jul 1997 05:00:00 GMT"
    assert "Cache-Control" not in endpoints.get_headers(ref, conditional=False)
    assert "If-Modified-Since" not in endpoints.get_headers(ref, conditional=False)
    for headers in (endpoints.post_headers(ref), endpoints.audit_post_headers(ref)):
        assert "Cache-Control" not in headers
        assert "If-Modified-Since" not in headers


def test_content_type_charset_casing(ref):
    assert endpoints.post_headers(ref)["Content-Type"] == "application/json;charset=UTF-8"
    assert (
        endpoints.audit_post_headers(ref)["Content-Type"]
        == "application/json;charset=utf-8"
    )
    assert "Content-Type" not in endpoints.get_headers(ref)
    assert "Content-Type" not in endpoints.base_headers(ref)


def test_user_agent_literal(ref):
    assert endpoints.DEFAULT_USER_AGENT == CAPTURED_USER_AGENT
    assert endpoints.base_headers(ref)["User-Agent"] == CAPTURED_USER_AGENT
    for token in ("AppleWebKit/537.36", "Chrome/150.0.0.0", "Safari/537.36"):
        assert token in endpoints.DEFAULT_USER_AGENT
    assert endpoints.base_headers(ref, "custom/1.0")["User-Agent"] == "custom/1.0"


def test_client_hint_and_fetch_metadata_literals(ref):
    headers = endpoints.base_headers(ref)
    assert headers["sec-ch-ua"] == (
        '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
    )
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Site"] == "same-origin"
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Accept-Language"] == "es-US,es-419;q=0.9,es;q=0.8,en;q=0.7"


def test_library_emitted_and_signalr_only_headers_are_never_set(ref):
    for headers in (
        endpoints.base_headers(ref),
        endpoints.get_headers(ref),
        endpoints.post_headers(ref),
        endpoints.audit_post_headers(ref),
    ):
        for name in (
            "X-Requested-With",
            "Authorization",
            "Host",
            "Connection",
            "Content-Length",
        ):
            assert name not in headers


def test_accept_encoding_advertises_only_what_this_install_can_decode(ref):
    """Advertising br/zstd without a decoder makes every honoured response unreadable."""
    decodable = set(urllib3.util.request.ACCEPT_ENCODING.replace(" ", "").split(","))
    advertised = [
        part.strip() for part in endpoints.base_headers(ref)["Accept-Encoding"].split(",")
    ]

    assert advertised[:2] == ["gzip", "deflate"]
    assert set(advertised) <= decodable
    for headers in (
        endpoints.get_headers(ref),
        endpoints.post_headers(ref),
        endpoints.audit_post_headers(ref),
    ):
        assert headers["Accept-Encoding"] == endpoints.ACCEPT_ENCODING


def test_accept_encoding_is_the_one_header_that_diverges_from_the_capture(ref):
    assert endpoints.ACCEPT_ENCODING_CAPTURED == "gzip, deflate, br, zstd"

    divergence = endpoints.header_divergence()
    captured = {
        "Accept": ACCEPT_HEADER,
        "Accept-Encoding": endpoints.ACCEPT_ENCODING_CAPTURED,
        "Accept-Language": "es-US,es-419;q=0.9,es;q=0.8,en;q=0.7",
        "Referer": CAPTURED_REFERER,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": CAPTURED_USER_AGENT,
        "sec-ch-ua": CAPTURED_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    sent = endpoints.base_headers(ref)

    assert sorted(sent) == sorted(captured)
    differing = set(name for name in captured if sent[name] != captured[name])
    assert differing == set(divergence)
    for name, (sent_value, captured_value) in divergence.items():
        assert sent[name] == sent_value
        assert captured[name] == captured_value


def test_accept_encoding_divergence_is_absent_once_the_decoders_are_installed():
    """The report is derived, not a constant: nothing to declare when the values agree."""
    saved = endpoints.ACCEPT_ENCODING
    try:
        endpoints.ACCEPT_ENCODING = endpoints.ACCEPT_ENCODING_CAPTURED
        assert endpoints.header_divergence() == {}
    finally:
        endpoints.ACCEPT_ENCODING = saved
    assert "Accept-Encoding" in endpoints.header_divergence() or (
        endpoints.ACCEPT_ENCODING == endpoints.ACCEPT_ENCODING_CAPTURED
    )


def test_header_builders_return_independent_dicts(ref):
    first = endpoints.post_headers(ref)
    first["Origin"] = "https://evil.example"
    assert endpoints.post_headers(ref)["Origin"] == "https://subastas.secop.gov.co"
