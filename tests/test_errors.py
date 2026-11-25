"""Classifier tests (plan §12.6; the taxonomy is published in ``docs/logging.md``).

Pure mapping tests — ``classify`` takes a status and a body and never touches a socket.

The assertions that matter are the two places the classifier can quietly disagree with the
rest of the run. ``OK`` must mean exactly what :func:`secop_firstbid.errors.is_success`
means, or an attempt is tagged a success inside a run that counted no winner and the summary
cannot say why the bid was not placed. And a 2xx carrying no verdict must not read as a
success, because on the bid endpoints that response is silence, not acceptance.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
import requests

from secop_firstbid import bidder, errors
from secop_firstbid.errors import Failure, RunState, classify, is_success

# The real captured rejection (all 8 ValidateBid responses have this shape).
BID_ERR_MIN = {
    "bidId": 0,
    "currentRankingPosition": 2,
    "isValid": False,
    "errorMessage": (
        "El valor total tiene que ser inferior al valor de su último lance para la "
        "subasta actual."
    ),
    "errorMessageLocale": "BidWarn_Total_MinBid_Diff_Desc",
    "minBid": "7.813.974,32 COP",
    "errorType": "BidErrMin",
}

ACCEPTED = {
    "bidId": 4417231,
    "currentRankingPosition": 1,
    "isValid": True,
    "errorMessage": "",
    "errorMessageLocale": None,
    "minBid": None,
    "errorType": None,
}

# Never observed: the capture holds no CreateBid at all and no isValid:true response.
VALID_WITH_WARNING = dict(ACCEPTED, errorMessage="Su lance supera el presupuesto oficial")


def test_is_success_is_one_function_shared_with_the_fire_path() -> None:
    """Two definitions of "did this land" fire different numbers of bids."""
    assert bidder.is_success is errors.is_success


@pytest.mark.parametrize(
    "body",
    [
        ACCEPTED,
        VALID_WITH_WARNING,
        BID_ERR_MIN,
        {},
        {"isValid": True},
        {"isValid": True, "errorMessage": "   "},
        {"isValid": True, "errorMessage": None},
        {"isValid": False},
        {"isValid": "true"},
    ],
)
def test_ok_is_returned_exactly_when_the_body_is_a_success(body: Dict[str, Any]) -> None:
    assert (classify(200, body) is Failure.OK) is is_success(body)


def test_a_valid_body_carrying_a_warning_is_tagged_not_swallowed() -> None:
    """The one response the capture cannot resolve still has to name itself in the taxonomy."""
    failure = classify(200, VALID_WITH_WARNING)

    assert failure is not Failure.OK
    assert failure is Failure.VALIDATION_FAILED
    assert is_success(VALID_WITH_WARNING) is False


@pytest.mark.parametrize(
    "status, body, raw_text",
    [
        (200, None, None),
        (200, None, ""),
        (200, "", None),
        (200, "   ", None),
        (204, None, None),
        (200, "<html>interstitial</html>", None),
    ],
)
def test_a_2xx_carrying_no_verdict_is_unknown(
    status: int, body: Optional[Any], raw_text: Optional[str]
) -> None:
    """On the bid endpoints an empty or non-JSON 2xx is silence, not an accepted bid."""
    assert classify(status, body, raw_text=raw_text) is Failure.UNKNOWN


def test_transport_statuses_keep_their_own_tags() -> None:
    assert classify(401, None) is Failure.AUTH_EXPIRED
    assert classify(403, "<html>blocked</html>") is Failure.WAF_BLOCK
    assert classify(429, "") is Failure.RATE_LIMIT
    assert classify(503, "unavailable") is Failure.UNKNOWN
    assert classify(None, None) is Failure.NETWORK
    assert (
        classify(None, None, requests.exceptions.ReadTimeout("read timed out"))
        is Failure.NETWORK
    )


def test_send_ordering_still_outranks_the_error_type() -> None:
    """A post-win duplicate and a genuine BidErrMin are byte-identical on the wire."""
    assert classify(200, BID_ERR_MIN) is Failure.BID_ERR_MIN
    assert (
        classify(200, BID_ERR_MIN, run_state=RunState(sent_after_first_success=True))
        is Failure.DUPLICATE
    )


def test_a_schema_rejection_is_a_malformed_payload() -> None:
    assert (
        classify(400, {"errors": {"items": ["required"]}}) is Failure.MALFORMED_PAYLOAD
    )
    assert classify(400, None, raw_text="Invalid JSON") is Failure.MALFORMED_PAYLOAD


def test_a_state_error_type_maps_to_its_state_tag() -> None:
    assert (
        classify(200, {"isValid": False, "errorType": "AuctionNotStarted"})
        is Failure.STATE_NOT_STARTED
    )
    assert (
        classify(200, {"isValid": False, "errorType": "AuctionFinished"})
        is Failure.STATE_FINISHED
    )
