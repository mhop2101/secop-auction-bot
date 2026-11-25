"""Shared test fixtures.

All fixtures here are offline. The JSON files under ``tests/fixtures/`` were extracted
verbatim from ``www.secop.gov.co.har`` (429 entries, one real *test* auction) with the
bearer token scrubbed.

Two families of saved page back the parser tests.

``tests/fixtures/opportunity_page.html`` and ``tests/fixtures/launch_page.html`` are
committed. They are scrubbed re-creations carrying a deliberately fake auction GUID and
doc id, and they reproduce every shape the parser depends on: the DOM-serialised
one-attribute-per-line whitespace, the JavaScript string-concatenation ``onclick`` with
``&amp;`` entities, both the ``RedirectToEAuction`` and ``RedirectToEAuctionIFrame``
anchors, the ``_tr0``/``_0`` grid ids, the state ``<td>`` with its nested ``<a>``, the
company ``<option selected>`` that precedes the ``languageSelector``, and the
``iframe#iframeEauction``. Most parser coverage runs against these, so a fresh checkout
gets it.

``beforebid.html`` and ``bid.html`` are the genuine saved artifacts and are the acceptance
gate: they are the only evidence the parser works on bytes a real operator saved. They are
session-scoped and identify the bidder, so they are ``.gitignore``d and absent from a fresh
checkout — :func:`require_real_page` skips the tests that need them rather than failing.
``§5.1`` of the plan requires them to stay byte-identical at the repository root, so tests
read them in place rather than keeping a second copy that could drift.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

BEFOREBID_HTML = REPO_ROOT / "beforebid.html"
BID_HTML = REPO_ROOT / "bid.html"

OPPORTUNITY_PAGE = FIXTURES / "opportunity_page.html"
LAUNCH_PAGE = FIXTURES / "launch_page.html"

# The captured auction (Appendix A). Every value here was verified against the HAR.
AUCTION_ID = "d6a49a23-3556-4b3d-9bb4-3e209279b9f8"
EAUCTION_DOC_ID = "CO1.EAREQ.937510"
ITEM_ID = "8552d67b-29ce-4b30-877b-e361fd876072"
LOT_INTERNAL_ID = "CO1.OTLN.74514427"
INITIAL_VALUE = Decimal("390698716")
QUANTITY_STR = "1.000000"

# Invented identifiers carried by the two committed HTML fixtures. They exist so a
# committed page can never be mistaken for, or replayed against, a real auction.
FIXTURE_AUCTION_ID = "deadbeef-0000-4000-8000-feedfacecafe"
FIXTURE_DOC_ID = "CO1.EAREQ.900001"
FIXTURE_PARENT_DOSSIER = "CO1.OPDOS.900002"
FIXTURE_MKEY = (
    "11111111_2222_3333_4444_555555555555.66666666_7777_8888_9999_aaaabbbbcccc"
)
FIXTURE_COMPANY_OPTION = "700000001"

MISSING_REAL_PAGE = (
    "%s is not in this checkout: it is a session-scoped saved auction page that "
    "identifies the bidder, so it is .gitignore'd. Save the auction page from the browser "
    "to the repository root as %s to run the acceptance tests against genuine bytes; "
    "tests/fixtures/%s covers the same parser behaviour on scrubbed bytes."
)

_SCRUBBED_EQUIVALENT = {
    "beforebid.html": "opportunity_page.html",
    "bid.html": "launch_page.html",
}


def require_real_page(path: Path) -> Path:
    """Return ``path``, or skip the calling test when the real artifact is not present."""
    if not path.is_file():
        pytest.skip(
            reason=MISSING_REAL_PAGE
            % (path.name, path.name, _SCRUBBED_EQUIVALENT.get(path.name, path.name))
        )
    return path


def _load(name: str) -> Any:
    with (FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def beforebid_html() -> str:
    return require_real_page(BEFOREBID_HTML).read_text(
        encoding="utf-8", errors="replace"
    )


@pytest.fixture(scope="session")
def bid_html() -> str:
    return require_real_page(BID_HTML).read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def opportunity_page_html() -> str:
    """The committed stand-in for ``beforebid.html`` (extractor A, both anchors)."""
    return OPPORTUNITY_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def launch_page_html() -> str:
    """The committed stand-in for ``bid.html`` (extractor B, ``iframe#iframeEauction``)."""
    return LAUNCH_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def auction_simple() -> Dict[str, Any]:
    """A real 59-key ``GetAuctionSimple`` response."""
    return _load("getauctionsimple.json")


@pytest.fixture(scope="session")
def validatebid_requests() -> List[Dict[str, Any]]:
    """All 8 real ``ValidateBid`` request bodies (bidValues 10x4, 1x2, 200x1, 7813974.32x1)."""
    return _load("validatebid_requests.json")


@pytest.fixture(scope="session")
def validatebid_responses() -> List[Dict[str, Any]]:
    """All 8 real responses; every one is ``isValid: false`` / ``errorType: "BidErrMin"``."""
    return _load("validatebid_responses.json")


@pytest.fixture(scope="session")
def bids_total_info() -> Dict[str, Any]:
    return _load("getbidstotalinfo.json")


@pytest.fixture(scope="session")
def clock_samples() -> List[Dict[str, Any]]:
    """128 ``GetAuctionSimple`` exchanges: Date header, HAR timings, start instant."""
    return _load("clock_samples.json")


@pytest.fixture(scope="session")
def event_logs() -> Dict[str, Any]:
    """``GetEventLogs`` proving ValidateBid writes buyer-visible rows with the bidder's IP."""
    return _load("geteventlogs.json")
