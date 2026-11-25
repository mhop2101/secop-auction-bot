"""Stage 2 tests (plan §6).

Two halves, with very different standing.

``build_detail``'s configuration half runs against ``getauctionsimple.json``, which is a real
captured response, so those assertions are evidence.

``parse_items`` runs against two bodies. ``getauction.json`` is a **real** captured response
(2026-07-29, the practice auction; its ``bidders`` array was emptied before committing), and
it settled ``docs/open-questions.md`` Q7 — correcting the plan on two fields: the item GUID is
``id``, not ``itemName``, and the lot id is the item's ``biddingListId``, because the entry's
``lotInternalId`` reads ``ALLLOTS``. ``getauction_synthetic.json`` is invented, now shaped on
that real body, and exists only to reach what one single-item auction cannot: a quantity other
than 1, a chapter entry, a non-price-list item, and the lot-id fallback.

Everything is offline: the HTTP tests mount a routing adapter on the client's own Session, so
the real ``requests`` pipeline runs and only the socket is missing.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from secop_firstbid import detail as detail_mod
from secop_firstbid import endpoints, numbers
from secop_firstbid.config import Config
from secop_firstbid.detail import (
    baseline_note,
    build_detail,
    fetch_auction_simple,
    fetch_detail,
    parse_items,
)
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionRef
from secop_firstbid.session import HttpClient, load_json

from conftest import (
    AUCTION_ID,
    FIXTURES,
    INITIAL_VALUE,
    ITEM_ID,
    LOT_INTERNAL_ID,
    QUANTITY_STR,
)

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    + ("VGhpc0lzTm90QVJlYWxUb2tlblBheWxvYWRf" * 20)
    + ".c2lnbmF0dXJlLWJ5dGVzLWdvLWhlcmVfLTAxMjM0NTY3ODk"
)

SECOND_ITEM_ID = "1a5c9f30-84be-4f2a-9d61-2c0f7b1e4d38"
QUANTITATIVE_ITEM_ID = "c47f2b18-6d05-4e93-b7aa-90e1c3fd5b62"
LOT_NAME = "GRUPO 1 - VIGILANCIA"

SYNTHETIC_PATH = FIXTURES / "getauction_synthetic.json"


@pytest.fixture(scope="session")
def get_auction_synthetic() -> Dict[str, Any]:
    """The invented ``GetAuction`` body, parsed the way the app parses a live one.

    ``session.load_json`` is used rather than ``json.load`` so the numeric fields arrive as
    ``Decimal``, exactly as they would at runtime.
    """
    return load_json(SYNTHETIC_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="1",
        fire_time="2026-07-28T21:00:00Z",
        run_root=str(tmp_path / "runs"),
    )


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id=AUCTION_ID)


@pytest.fixture
def logger(cfg: Config) -> Iterator[RunLogger]:
    run_logger = RunLogger(cfg)
    yield run_logger
    run_logger.close()


class RouteAdapter(HTTPAdapter):
    """Answers by exact URL; anything unrouted is a 404, never a socket."""

    def __init__(self, routes: Dict[str, Tuple[int, bytes]]) -> None:
        super().__init__(pool_connections=1, pool_maxsize=1, max_retries=0)
        self.routes = routes
        self.calls: List[str] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        self.calls.append(request.url)
        status, content = self.routes.get(request.url, (404, b'{"message":"no route"}'))
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.headers = CaseInsensitiveDict(
            {"Content-Type": "application/json; charset=utf-8"}
        )
        response.url = request.url
        response.request = request
        response.raw = None
        return response


def _routes(
    ref: AuctionRef,
    simple: Any,
    auction: Any,
    totals: Any,
    simple_status: int = 200,
    auction_status: int = 200,
    totals_status: int = 200,
) -> Dict[str, Tuple[int, bytes]]:
    def blob(body: Any) -> bytes:
        return body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")

    return {
        endpoints.url(endpoints.get_auction_simple(ref.auction_id)): (
            simple_status,
            blob(simple),
        ),
        endpoints.url(endpoints.get_auction(ref.auction_id, ref.lang)): (
            auction_status,
            blob(auction),
        ),
        endpoints.url(endpoints.get_bids_total_info(ref.auction_id)): (
            totals_status,
            blob(totals),
        ),
    }


def _client(
    cfg: Config, ref: AuctionRef, logger: RunLogger, routes: Dict[str, Tuple[int, bytes]]
) -> Tuple[HttpClient, RouteAdapter]:
    client = HttpClient(cfg, ref, RequestsCookieJar(), logger)
    adapter = RouteAdapter(routes)
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client, adapter


def _events(logger: RunLogger) -> List[Dict[str, Any]]:
    logger.flush()
    path = logger.run_dir / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _by_event(logger: RunLogger, name: str) -> List[Dict[str, Any]]:
    return [record for record in _events(logger) if record["event"] == name]


def test_the_getauction_fixture_is_declared_synthetic() -> None:
    raw = json.loads(SYNTHETIC_PATH.read_text(encoding="utf-8"))

    # The marker is the whole point of the file: it is the only thing standing between an
    # invented shape and a reader who assumes every fixture came out of the HAR.
    assert raw["_synthetic"] is True
    assert "Q7" in raw["_note"]
    assert "NOT CAPTURED" in raw["_note"]


REAL_PATH = FIXTURES / "getauction.json"


@pytest.fixture(scope="session")
def get_auction_real() -> Dict[str, Any]:
    """The captured ``GetAuction`` body, parsed the way the app parses a live one."""
    return load_json(REAL_PATH.read_text(encoding="utf-8"))


def test_the_real_body_carries_the_item_guid_in_id_and_never_in_itemname(
    get_auction_real,
) -> None:
    """The one field the plan got wrong, pinned against the server's own answer.

    §6.2 said ``itemId`` came from ``itemName``. That name describes the SPA's flattened
    model, not the response: the wire has no ``itemName`` at all.
    """
    raw = get_auction_real["biddingList"][0]["biddingPriceListItems"][0]

    assert raw["id"] == ITEM_ID
    assert "itemName" not in raw
    assert '"itemName"' not in REAL_PATH.read_text(encoding="utf-8")


def test_the_entry_lot_internal_id_is_a_marker_not_the_lot_id(get_auction_real) -> None:
    """``lotInternalId`` on the entry reads ``ALLLOTS``; the wire wants the item's lot."""
    entry = get_auction_real["biddingList"][0]

    assert entry["lotInternalId"] == "ALLLOTS"
    assert entry["biddingPriceListItems"][0]["biddingListId"] == LOT_INTERNAL_ID
    assert parse_items(get_auction_real)[0].lot_internal_id == LOT_INTERNAL_ID


def test_the_real_body_reproduces_every_captured_wire_item_field(get_auction_real) -> None:
    """End to end against evidence: live GetAuction -> ItemMeta -> the bytes the HAR shows.

    Each assertion is a field of the captured ``ValidateBid`` item, so this is the mapping
    checked against the server on both sides rather than against the plan's prose.
    """
    with (FIXTURES / "validatebid_requests.json").open(encoding="utf-8") as handle:
        wire = json.load(handle)[0]["items"][0]

    item = parse_items(get_auction_real)[0]

    assert item.item_id == wire["itemId"]
    assert item.quantity_str == wire["quantity"]
    assert item.item_unit == wire["itemUnit"]
    assert item.item_internal_reference == wire["itemInternalReference"]
    assert item.lot_internal_id == wire["lotInternalId"]
    assert item.lot_name == wire["lotName"]
    assert item.initial_value == INITIAL_VALUE
    assert item.is_price_list_item is True


def test_parse_items_maps_the_field_names(get_auction_synthetic) -> None:
    # The names below are now confirmed by getauction.json; this exercises them on the
    # invented body, which reaches the multi-item and chapter paths the real one cannot.
    items = parse_items(get_auction_synthetic)

    first = items[0]
    assert first.item_id == ITEM_ID  # from itemName, which holds the GUID
    assert first.quantity_str == QUANTITY_STR  # the string is kept verbatim for the wire
    assert first.quantity == Decimal(QUANTITY_STR)
    assert numbers.to_wire(first.quantity) == "1.000000"
    assert first.item_unit == "UN"
    assert first.item_internal_reference == "1"
    assert first.lot_internal_id == LOT_INTERNAL_ID
    assert first.lot_name == LOT_NAME  # lotName empty -> groupReference
    assert first.initial_value == INITIAL_VALUE  # from itemInitialUnitPriceStr
    assert first.min_bid_difference == Decimal(0)
    assert first.is_price_list_item is True
    assert first.display_item_order == 1


def test_parse_items_handles_a_quantity_other_than_one(get_auction_synthetic) -> None:
    items = {item.item_id: item for item in parse_items(get_auction_synthetic)}

    second = items[SECOND_ITEM_ID]
    assert second.quantity_str == "12.000000"
    assert second.quantity == Decimal("12.000000")
    assert second.item_unit == "MES"
    assert second.item_internal_reference == "2"
    assert second.initial_value == Decimal("2500000")
    assert second.min_bid_difference == Decimal("1000")
    # Empty lotInternalId on the item falls back to the entry's biddingListId.
    assert second.lot_internal_id == LOT_INTERNAL_ID


def test_quantitative_items_take_their_reference_from_the_description(
    get_auction_synthetic,
) -> None:
    items = {item.item_id: item for item in parse_items(get_auction_synthetic)}

    quantitative = items[QUANTITATIVE_ITEM_ID]
    assert quantitative.is_price_list_item is False
    assert quantitative.item_internal_reference == "PLAN DE CONTINGENCIA (CRITERIO TECNICO)"
    assert quantitative.initial_value == Decimal(0)


def test_items_are_flattened_and_sorted_by_display_item_order(get_auction_synthetic) -> None:
    items = parse_items(get_auction_synthetic)

    # Document order in the fixture is [B(3), A(1)] then the quantitative C(2), so an
    # unsorted flatten would give B, A, C.
    assert [item.item_id for item in items] == [
        ITEM_ID,
        QUANTITATIVE_ITEM_ID,
        SECOND_ITEM_ID,
    ]
    assert [item.display_item_order for item in items] == [1, 2, 3]


def test_chapter_entries_contribute_no_items(get_auction_synthetic) -> None:
    entries = get_auction_synthetic["biddingList"]
    chapter = entries[0]

    assert chapter["containtItems"] is False  # the server's spelling
    assert parse_items({"biddingList": [chapter]}) == []


def test_a_float_parsed_body_produces_the_same_items(get_auction_synthetic) -> None:
    # session.load_json yields Decimals; a body read with plain json.load yields floats. The
    # two must agree, or the fixture and the runtime would be testing different code.
    with SYNTHETIC_PATH.open(encoding="utf-8") as handle:
        floats = json.load(handle)

    assert parse_items(floats) == parse_items(get_auction_synthetic)


def test_an_item_without_an_id_is_a_parse_failure(get_auction_synthetic) -> None:
    body = copy.deepcopy(get_auction_synthetic)
    del body["biddingList"][1]["biddingPriceListItems"][1]["id"]

    with pytest.raises(SecopError) as excinfo:
        parse_items(body)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert "id" in excinfo.value.message


def test_an_amount_string_that_contradicts_its_number_is_refused(
    get_auction_synthetic,
) -> None:
    body = copy.deepcopy(get_auction_synthetic)
    item = body["biddingList"][1]["biddingPriceListItems"][1]
    item["itemQuantity"] = Decimal("1000000")  # as if "1.000000" had been read as Colombian

    with pytest.raises(SecopError) as excinfo:
        parse_items(body)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert "itemQuantityStr" in excinfo.value.message


def test_a_colombian_formatted_amount_is_refused_rather_than_guessed(
    get_auction_synthetic,
) -> None:
    body = copy.deepcopy(get_auction_synthetic)
    item = body["biddingList"][1]["biddingPriceListItems"][1]
    item["itemInitialUnitPriceStr"] = "390.698.716,00"
    del item["itemInitialUnitPrice"]

    with pytest.raises(SecopError) as excinfo:
        parse_items(body)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert "itemInitialUnitPriceStr" in excinfo.value.message


def test_a_price_list_item_without_an_initial_price_is_refused(
    get_auction_synthetic,
) -> None:
    body = copy.deepcopy(get_auction_synthetic)
    item = body["biddingList"][1]["biddingPriceListItems"][1]
    del item["itemInitialUnitPriceStr"]
    del item["itemInitialUnitPrice"]

    with pytest.raises(SecopError) as excinfo:
        parse_items(body)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert "itemQuantityUnitPrice" in excinfo.value.message


def test_build_detail_reads_the_captured_auction(auction_simple, bids_total_info) -> None:
    detail = build_detail(auction_simple, None, bids_total_info)

    assert detail.version_code == "AuctionbyProposal"
    assert detail.start_price == Decimal("390698716.0")
    assert detail.test_auction is True
    assert detail.state_code == "Started"
    assert detail.exec_state_code == "Running"

    assert detail.type_code == "Reversed"
    assert detail.currency == "COP"
    assert detail.round_type == "Up"
    assert detail.decimal_num == 2
    assert detail.total_num == 20
    assert detail.minimum_bid_type == 1
    assert detail.allow_bids_below_best is False  # allowBidsBellowBestBid, server's spelling
    assert detail.time_zone_code is None
    assert detail.start_date is not None
    assert detail.start_date.isoformat() == "2026-07-28T21:00:00+00:00"
    assert detail.end_date is not None
    assert detail.end_date.isoformat() == "2026-07-28T21:30:00+00:00"
    assert detail.raw_simple is not None and len(detail.raw_simple) == 59
    assert detail.items == []


def test_build_detail_carries_the_items_through(
    auction_simple, get_auction_synthetic, bids_total_info
) -> None:
    detail = build_detail(auction_simple, get_auction_synthetic, bids_total_info)

    assert [item.item_id for item in detail.items] == [
        ITEM_ID,
        QUANTITATIVE_ITEM_ID,
        SECOND_ITEM_ID,
    ]


def test_a_zero_my_last_bid_is_reported_not_worked_around(
    auction_simple, bids_total_info, logger
) -> None:
    detail = build_detail(auction_simple, None, bids_total_info, logger=logger)

    assert detail.my_last_bid == Decimal(0)
    assert detail.last_bid == Decimal(0)

    note = baseline_note(detail)
    assert note is not None
    assert "myLastBid is 0" in note
    assert "BidErrMin" in note
    # Two live auctions accepted a first bid from exactly this state, so the note must not
    # read as a prediction that this run's bid will bounce.
    assert "does not predict a rejection" in note
    assert "8 test bids" not in note

    records = _by_event(logger, "bid_baseline")
    assert len(records) == 1
    assert records[0]["level"] == "WARNING"
    assert records[0]["detail"]["my_last_bid"] == "0.0"
    assert records[0]["detail"]["last_bid"] == "0.0"
    assert records[0]["detail"]["note"] == note

    summary = _by_event(logger, "auction_detail")
    assert len(summary) == 1
    assert summary[0]["detail"]["version_code"] == "AuctionbyProposal"
    assert summary[0]["detail"]["state_code"] == "Started"


def test_a_standing_bid_needs_no_note(auction_simple, bids_total_info, logger) -> None:
    totals = dict(bids_total_info, myLastBid=Decimal("7813974.32"))

    detail = build_detail(auction_simple, None, totals, logger=logger)

    assert baseline_note(detail) is None
    assert _by_event(logger, "bid_baseline")[0]["level"] == "INFO"


def test_missing_totals_leave_the_baseline_unknown(auction_simple) -> None:
    detail = build_detail(auction_simple, None, None)

    assert detail.my_last_bid is None
    assert detail.last_bid is None
    note = baseline_note(detail)
    assert note is not None and "unknown" in note


@pytest.mark.parametrize("key", ["auctionVersionCode", "currencyCode"])
def test_build_detail_refuses_a_body_missing_a_wire_critical_field(
    auction_simple, key
) -> None:
    body = dict(auction_simple)
    del body[key]

    with pytest.raises(SecopError) as excinfo:
        build_detail(body, None, None)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert key in excinfo.value.message


def test_fetch_detail_reads_the_three_documented_routes(
    cfg, ref, logger, auction_simple, get_auction_synthetic, bids_total_info
) -> None:
    routes = _routes(ref, auction_simple, get_auction_synthetic, bids_total_info)
    client, adapter = _client(cfg, ref, logger, routes)

    detail = fetch_detail(client, ref, logger)

    assert adapter.calls == [
        endpoints.url(endpoints.get_auction_simple(AUCTION_ID)),
        endpoints.url(endpoints.get_auction(AUCTION_ID, "es-CO")),
        endpoints.url(endpoints.get_bids_total_info(AUCTION_ID)),
    ]
    assert detail.version_code == "AuctionbyProposal"
    assert detail.my_last_bid == Decimal(0)
    assert [item.item_id for item in detail.items] == [
        ITEM_ID,
        QUANTITATIVE_ITEM_ID,
        SECOND_ITEM_ID,
    ]

    # Q7 is answered, so this is no longer a warning — but the event still carries the
    # provenance note and the body's key set, which is what makes an unfamiliar auction
    # shape visible in the log rather than only in a failure.
    recorded = _by_event(logger, "item_model_shape")
    assert len(recorded) == 1
    assert recorded[0]["level"] == "INFO"
    assert recorded[0]["detail"]["question"] == "Q7"
    assert "biddingList" in recorded[0]["detail"]["top_level_keys"]

    client.close()


def test_fetch_detail_survives_an_unavailable_baseline(
    cfg, ref, logger, auction_simple, get_auction_synthetic
) -> None:
    routes = _routes(
        ref, auction_simple, get_auction_synthetic, b"", totals_status=500
    )
    client, _adapter = _client(cfg, ref, logger, routes)

    detail = fetch_detail(client, ref, logger)

    assert detail.my_last_bid is None
    assert len(detail.items) == 3
    warnings = _by_event(logger, "bid_baseline_unavailable")
    assert len(warnings) == 1
    assert warnings[0]["level"] == "WARNING"

    client.close()


def test_a_401_on_the_first_call_is_an_auth_failure(
    cfg, ref, logger, auction_simple, get_auction_synthetic, bids_total_info
) -> None:
    routes = _routes(
        ref, auction_simple, get_auction_synthetic, bids_total_info, simple_status=401
    )
    client, _adapter = _client(cfg, ref, logger, routes)

    with pytest.raises(SecopError) as excinfo:
        fetch_auction_simple(client, ref, logger)

    assert excinfo.value.code is Failure.AUTH_EXPIRED
    assert excinfo.value.detail["status"] == 401
    assert TOKEN not in (logger.run_dir / "events.jsonl").read_text(encoding="utf-8")

    client.close()


def test_a_200_that_is_not_json_is_a_parse_failure(
    cfg, ref, logger, get_auction_synthetic, bids_total_info
) -> None:
    routes = _routes(
        ref, b"<html>Request blocked</html>", get_auction_synthetic, bids_total_info
    )
    client, _adapter = _client(cfg, ref, logger, routes)

    with pytest.raises(SecopError) as excinfo:
        fetch_detail(client, ref, logger)

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert excinfo.value.detail["status"] == 200

    client.close()


def test_the_module_never_invents_a_missing_item_list() -> None:
    with pytest.raises(SecopError) as excinfo:
        parse_items({"id": AUCTION_ID})

    assert excinfo.value.code is Failure.PARSE_FAILED
    assert detail_mod.BIDDING_LIST_KEYS[0] in excinfo.value.message
