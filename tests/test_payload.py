"""Tests for :mod:`secop_firstbid.payload` (plan §8.3-§8.5).

The oracle is ``tests/fixtures/validatebid_requests.json`` — the 8 real ``ValidateBid``
bodies. Each one is rebuilt from the captured item metadata at its own ``bidValue`` and
compared to the wire dict, **excluding the ``Date`` key**: ``Date`` is ``new Date()`` at
send time, a fresh per-run placeholder the server ignores (it echoes
``date: "0001-01-01T00:00:00"``), so it is the one field a rebuild cannot and should not
reproduce. Its *shape* is asserted separately.

Key order is checked as well as content, in both directions: the fixture's own order is
pinned to the wire constants, and the built dicts are compared against those constants.
An equal-but-reordered dict would pass a content comparison and change the bytes.

``tests/fixtures/validatebid_raw_bodies.txt`` closes that last gap at the byte level. It
holds the same 8 bodies as the *unparsed* strings the browser put on the wire, so
``to_bytes()`` can be compared to them directly. ``Date`` is reproducible after all when
the captured instant is fed back through ``build_payload(..., now=...)``, which makes each
captured body an exact golden rather than a field-by-field one — key order, separators,
string-vs-number and every digit in a single assertion.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import (
    AUCTION_ID,
    FIXTURES,
    INITIAL_VALUE,
    ITEM_ID,
    LOT_INTERNAL_ID,
    QUANTITY_STR,
)
from secop_firstbid import detail as detail_mod
from secop_firstbid import payload as payload_mod
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.models import AuctionDetail, AuctionRef, ItemMeta
from secop_firstbid.payload import build_bid_item, build_payload, js_iso_now

WIRE_TOP_KEYS = [
    "auctionId",
    "currency",
    "Date",
    "ranking",
    "items",
    "totalValue",
    "languageCode",
]
WIRE_ITEM_KEYS = [
    "itemId",
    "quantity",
    "isPriceListItem",
    "isChanged",
    "itemInternalReference",
    "lotInternalId",
    "lotName",
    "itemUnit",
    "totalValue",
    "bidValue",
    "itemQuantityUnitPrice",
    "bidded",
]

JS_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
PLAIN_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")

REF = AuctionRef(auction_id=AUCTION_ID, lang="es-CO", market="GovCo")

RAW_BODIES = FIXTURES / "validatebid_raw_bodies.txt"


def raw_captured_bodies():
    """The 8 ``ValidateBid`` request bodies verbatim, one per line, newline-terminated.

    Kept as unparsed text because a JSON fixture is re-serialized on the way in and can
    therefore only witness content, never byte order — so it cannot be a byte oracle for
    itself. ``split("\\n")`` rather than ``splitlines()``: U+2028 is a line break to
    ``splitlines`` and a legal unescaped character inside a JSON string, which would cut a
    body in half.
    """
    lines = RAW_BODIES.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == ""
    return lines[:-1]


def captured_instant(body):
    """The ``Date`` of a captured body, back as the ``datetime`` that reproduces it."""
    return datetime.strptime(body["Date"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )


def captured_item(**overrides):
    """The single price-list item of the captured auction (Appendix A)."""
    fields = dict(
        item_id=ITEM_ID,
        quantity=Decimal(QUANTITY_STR),
        item_unit="UN",
        item_internal_reference="1",
        lot_internal_id=LOT_INTERNAL_ID,
        lot_name="",
        initial_value=INITIAL_VALUE,
        quantity_str=QUANTITY_STR,
    )
    fields.update(overrides)
    return ItemMeta(**fields)


def detail(items, version_code="AuctionbyProposal"):
    """An ``AuctionDetail`` carrying the captured auction's real configuration."""
    return AuctionDetail(
        version_code=version_code,
        type_code="Reversed",
        currency="COP",
        round_type="Up",
        decimal_num=2,
        total_num=20,
        minimum_bid_type=1,
        start_price=INITIAL_VALUE,
        start_date=None,
        end_date=None,
        state_code="Started",
        exec_state_code="Running",
        allow_bids_below_best=False,
        test_auction=True,
        items=list(items),
    )


def without_date(body):
    return dict((key, value) for key, value in body.items() if key != "Date")


# The invented GetAuction fixture's one biddingQuantitativeItems[] entry — a technical
# criterion, isPriceListItem false. GetAuction has never been captured (Q7), so the item
# *shape* is unverified; what is asserted below is that a false flag keeps an item off the
# wire, which holds whatever the real field names turn out to be.
CRITERION_ITEM_ID = "c47f2b18-6d05-4e93-b7aa-90e1c3fd5b62"


def synthetic_items():
    with (FIXTURES / "getauction_synthetic.json").open(encoding="utf-8") as fh:
        return detail_mod.parse_items(json.load(fh))


def test_fixture_key_order_matches_the_wire_constants(validatebid_requests):
    assert len(validatebid_requests) == 8
    for body in validatebid_requests:
        assert list(body.keys()) == WIRE_TOP_KEYS
        assert len(body["items"]) == 1
        assert list(body["items"][0].keys()) == WIRE_ITEM_KEYS


def test_the_real_auction_metadata_is_what_the_tests_assume(auction_simple):
    assert auction_simple["auctionVersionCode"] == payload_mod.SUPPORTED_AUCTION_VERSION
    assert auction_simple["currencyCode"] == "COP"
    assert Decimal(str(auction_simple["startPrice"])) == INITIAL_VALUE


def test_rebuilds_all_eight_captured_bodies(validatebid_requests):
    for body in validatebid_requests:
        wire_item = body["items"][0]
        bid_value = Decimal(wire_item["bidValue"])
        built, report = build_payload(REF, detail([captured_item()]), bid_value)

        # Date excluded: it is a per-run new Date() placeholder the server ignores.
        assert without_date(built.to_dict()) == without_date(body)
        assert report.warnings == []
        assert report.inexact_items == []
        assert report.invariant_ok is True


def test_rebuilt_key_order_equals_the_wire_order(validatebid_requests):
    for body in validatebid_requests:
        bid_value = Decimal(body["items"][0]["bidValue"])
        built, _ = build_payload(REF, detail([captured_item()]), bid_value)
        as_dict = built.to_dict()
        assert list(as_dict.keys()) == WIRE_TOP_KEYS
        assert list(as_dict.keys()) == list(body.keys())
        assert list(as_dict["items"][0].keys()) == WIRE_ITEM_KEYS
        assert list(as_dict["items"][0].keys()) == list(body["items"][0].keys())


def test_serialized_bytes_round_trip_to_the_captured_body(validatebid_requests):
    for body in validatebid_requests:
        bid_value = Decimal(body["items"][0]["bidValue"])
        built, _ = build_payload(REF, detail([captured_item()]), bid_value)
        raw = built.to_bytes()
        assert b", " not in raw and b'": ' not in raw
        assert without_date(json.loads(raw)) == without_date(body)


def test_the_raw_fixture_is_the_same_eight_captured_bodies(validatebid_requests):
    raws = raw_captured_bodies()
    assert len(raws) == 8
    assert [json.loads(raw) for raw in raws] == validatebid_requests
    for raw in raws:
        # json decoding inserts keys in document order and dicts keep insertion order,
        # so this reads the order out of the captured bytes.
        decoded = json.loads(raw)
        assert list(decoded.keys()) == WIRE_TOP_KEYS
        assert list(decoded["items"][0].keys()) == WIRE_ITEM_KEYS


def test_serialized_bytes_equal_every_captured_body_byte_for_byte():
    raws = raw_captured_bodies()
    assert len(raws) == 8
    for raw in raws:
        body = json.loads(raw)
        built, _ = build_payload(
            REF,
            detail([captured_item()]),
            Decimal(body["items"][0]["bidValue"]),
            now=captured_instant(body),
        )
        assert built.to_bytes() == raw.encode("utf-8")


def test_a_decoded_comparison_cannot_witness_key_order():
    # Why the golden above is compared as bytes: dict equality is order-insensitive, so a
    # reordering introduced anywhere in serialization survives every == against a decoded
    # body. Only the byte comparison sees it.
    ordered = '{"totalValue":"10.000000","languageCode":"es-CO"}'
    reordered = '{"languageCode":"es-CO","totalValue":"10.000000"}'
    assert json.loads(ordered) == json.loads(reordered)
    assert ordered.encode("utf-8") != reordered.encode("utf-8")


def test_the_literal_fields_are_literal(validatebid_requests):
    built, _ = build_payload(REF, detail([captured_item()]), Decimal("10"))
    as_dict = built.to_dict()
    assert as_dict["ranking"] == -1
    assert as_dict["currency"] == "COP"
    assert as_dict["languageCode"] == "es-CO"
    assert as_dict["auctionId"] == AUCTION_ID
    item = as_dict["items"][0]
    assert item["isPriceListItem"] is True
    assert item["isChanged"] is True
    assert item["bidded"] is True
    assert item["quantity"] == QUANTITY_STR


def test_the_operator_value_reaches_the_wire_unmodified(validatebid_responses):
    # Every captured response rejected these bids with minBid "7.813.974,32 COP"; the app
    # still sends the value it was given. No floor, no adjustment, no retry (§7).
    assert {body["minBid"] for body in validatebid_responses} == {"7.813.974,32 COP"}
    built, _ = build_payload(REF, detail([captured_item()]), Decimal("1"))
    assert built.items[0].bid_value == "1"
    assert built.to_dict()["items"][0]["bidValue"] == "1"


def test_trailing_zeros_in_the_operator_value_survive():
    built, _ = build_payload(REF, detail([captured_item()]), Decimal("10.00"))
    item = built.to_dict()["items"][0]
    assert item["bidValue"] == "10.00"
    assert item["totalValue"] == "10.00000000"
    assert built.to_dict()["totalValue"] == "10.00000000"


def test_top_level_total_is_a_max_scale_sum_of_the_item_totals():
    wide = captured_item(
        item_id="b-second-in-the-list",
        quantity=Decimal("2.5000"),
        quantity_str="2.5000",
    )
    built, _ = build_payload(REF, detail([captured_item(), wide]), Decimal("10"))
    as_dict = built.to_dict()
    assert [item["totalValue"] for item in as_dict["items"]] == [
        "10.000000",
        "25.0000",
    ]
    assert as_dict["totalValue"] == "35.000000"


def test_item_order_is_preserved_not_sorted():
    first = captured_item(item_id="zzz-first")
    second = captured_item(item_id="aaa-second")
    built, _ = build_payload(REF, detail([first, second]), Decimal("10"))
    assert [item["itemId"] for item in built.to_dict()["items"]] == [
        "zzz-first",
        "aaa-second",
    ]


def test_build_bid_item_reports_the_division(validatebid_requests):
    for body in validatebid_requests:
        wire_item = body["items"][0]
        item, is_exact, residual = build_bid_item(
            captured_item(), Decimal(wire_item["bidValue"])
        )
        assert item.to_dict() == wire_item
        assert is_exact is True
        assert residual == 0


def test_quantity_other_than_one_warns_loudly_with_the_competing_value():
    meta = captured_item(quantity=Decimal("2.000000"), quantity_str="2.000000")
    built, report = build_payload(REF, detail([meta]), Decimal("10"))

    item = built.to_dict()["items"][0]
    assert item["quantity"] == "2.000000"
    assert item["totalValue"] == "20.000000"
    assert item["itemQuantityUnitPrice"] == "78139743.200000000000"

    assert len(report.warnings) == 1
    warning = report.warnings[0]
    assert "UNVERIFIED QUANTITY FACTOR" in warning
    assert ITEM_ID in warning
    assert "78139743.200000000000" in warning
    # the reading that drops quantity entirely — what the capture cannot rule out
    assert "39069871.600000000000" in warning
    # and the reading that divides by it
    assert "19534935.800000000000" in warning


def test_quantity_one_never_warns():
    _, report = build_payload(REF, detail([captured_item()]), Decimal("10"))
    assert report.warnings == []
    _, report = build_payload(
        REF,
        detail([captured_item(quantity=Decimal("1"), quantity_str="1")]),
        Decimal("10"),
    )
    assert report.warnings == []


def test_unsupported_auction_version_aborts():
    for version in ("AuctionbyItem", "AuctionbyLot", "", "auctionbyproposal"):
        with pytest.raises(SecopError) as excinfo:
            build_payload(REF, detail([captured_item()], version_code=version), Decimal("1"))
        assert excinfo.value.code is Failure.UNSUPPORTED_AUCTION_VERSION
        assert excinfo.value.detail["version_code"] == version


def test_empty_item_list_is_a_malformed_payload():
    with pytest.raises(SecopError) as excinfo:
        build_payload(REF, detail([]), Decimal("1"))
    assert excinfo.value.code is Failure.MALFORMED_PAYLOAD


def test_non_price_list_items_are_never_bid_on():
    items = synthetic_items()
    assert [meta.is_price_list_item for meta in items] == [True, False, True]
    assert [meta.item_id for meta in items][1] == CRITERION_ITEM_ID

    built, report = build_payload(REF, detail(items), Decimal("10"))
    as_dict = built.to_dict()

    assert [item["itemId"] for item in as_dict["items"]] == [
        ITEM_ID,
        "1a5c9f30-84be-4f2a-9d61-2c0f7b1e4d38",
    ]
    assert all(item["isPriceListItem"] is True for item in as_dict["items"])
    # 1.000000 x 10 + 12.000000 x 10; the criterion item's own 10.000000 is not in the sum
    assert as_dict["totalValue"] == "130.000000"
    assert report.excluded_items == [CRITERION_ITEM_ID]
    assert report.excluded_total_value == "10.000000"
    assert CRITERION_ITEM_ID not in report.residuals


def test_the_exclusion_is_reported_with_the_totalValue_difference():
    _, report = build_payload(REF, detail(synthetic_items()), Decimal("10"))
    notes = [line for line in report.warnings if "NON-PRICE-LIST ITEMS EXCLUDED" in line]
    assert len(notes) == 1
    assert CRITERION_ITEM_ID in notes[0]
    assert "added 10.000000" in notes[0]
    assert "totalValue 140.000000 instead of the 130.000000" in notes[0]


def test_an_auction_of_only_non_price_list_items_is_a_malformed_payload():
    criterion = captured_item(item_id=CRITERION_ITEM_ID, is_price_list_item=False)
    with pytest.raises(SecopError) as excinfo:
        build_payload(REF, detail([criterion]), Decimal("10"))
    assert excinfo.value.code is Failure.MALFORMED_PAYLOAD
    assert excinfo.value.detail["excluded_items"] == [CRITERION_ITEM_ID]
    assert excinfo.value.detail["item_count"] == 1


def test_a_price_list_only_auction_excludes_nothing_and_says_so():
    _, report = build_payload(REF, detail([captured_item()]), Decimal("10"))
    assert report.excluded_items == []
    assert report.excluded_total_value == "0"
    assert report.warnings == []


def test_quantity_string_disagreeing_with_the_decimal_is_refused():
    meta = captured_item(quantity=Decimal("1.000000"), quantity_str="2.000000")
    with pytest.raises(SecopError) as excinfo:
        build_payload(REF, detail([meta]), Decimal("10"))
    assert excinfo.value.code is Failure.MALFORMED_PAYLOAD
    assert excinfo.value.detail["item_id"] == ITEM_ID


def test_a_broken_invariant_raises_malformed_payload(monkeypatch):
    monkeypatch.setattr(
        payload_mod.numbers,
        "item_quantity_unit_price",
        lambda *args, **kwargs: (Decimal("1.000000000000"), True, Decimal(0)),
    )
    with pytest.raises(SecopError) as excinfo:
        build_payload(REF, detail([captured_item()]), Decimal("10"))
    assert excinfo.value.code is Failure.MALFORMED_PAYLOAD
    assert excinfo.value.detail["item_id"] == ITEM_ID


def test_an_inexact_division_is_reported_but_not_fatal():
    built, report = build_payload(REF, detail([captured_item()]), Decimal("3"))
    item = built.to_dict()["items"][0]
    assert item["itemQuantityUnitPrice"] == "130232905.333333333333"
    assert report.inexact_items == [ITEM_ID]
    assert Decimal(report.residuals[ITEM_ID]) > 0
    assert report.invariant_ok is True
    assert len(report.warnings) == 1
    assert "INEXACT DIVISION" in report.warnings[0]
    assert "ROUND_HALF_UP" in report.warnings[0]


def test_residuals_cover_every_item():
    built, report = build_payload(
        REF,
        detail([captured_item(item_id="one"), captured_item(item_id="two")]),
        Decimal("10"),
    )
    assert sorted(report.residuals) == ["one", "two"]
    assert {Decimal(text) for text in report.residuals.values()} == {Decimal(0)}
    assert len(built.items) == 2


def test_zero_bid_value_takes_the_documented_special_case():
    built, report = build_payload(REF, detail([captured_item()]), Decimal("0"))
    item = built.to_dict()["items"][0]
    assert item["bidValue"] == "0"
    assert item["itemQuantityUnitPrice"] == "0.000000000000"
    assert len(report.warnings) == 1
    assert "ZERO SPECIAL CASE" in report.warnings[0]


def test_no_amount_on_the_wire_is_in_exponent_notation():
    # str(Decimal("0.000000000000")) is "0E-12"; nothing that shape may reach the server.
    for bid in (Decimal("0"), Decimal("1"), Decimal("7813974.32"), Decimal("3")):
        built, report = build_payload(REF, detail([captured_item()]), bid)
        as_dict = built.to_dict()
        amounts = [as_dict["totalValue"]] + [
            as_dict["items"][0][key]
            for key in ("quantity", "totalValue", "bidValue", "itemQuantityUnitPrice")
        ]
        amounts += list(report.residuals.values())
        for amount in amounts:
            assert PLAIN_DECIMAL.match(amount), amount


def test_js_iso_now_reproduces_the_captured_date_exactly():
    instant = datetime(2026, 7, 28, 21, 19, 31, 751000, tzinfo=timezone.utc)
    assert js_iso_now(instant) == "2026-07-28T21:19:31.751Z"


def test_js_iso_now_is_not_isoformat():
    instant = datetime(2026, 7, 28, 21, 19, 31, 751000, tzinfo=timezone.utc)
    # isoformat() gives six fractional digits and "+00:00"; toISOString() gives three and Z.
    assert instant.isoformat() == "2026-07-28T21:19:31.751000+00:00"
    assert js_iso_now(instant) != instant.isoformat()
    assert js_iso_now(instant).endswith("Z")
    assert len(js_iso_now(instant).split(".")[1]) == len("751Z")


def test_js_iso_now_truncates_sub_millisecond_precision():
    instant = datetime(2026, 7, 28, 21, 19, 31, 751999, tzinfo=timezone.utc)
    assert js_iso_now(instant) == "2026-07-28T21:19:31.751Z"
    instant = datetime(2026, 7, 28, 21, 19, 31, 0, tzinfo=timezone.utc)
    assert js_iso_now(instant) == "2026-07-28T21:19:31.000Z"


def test_js_iso_now_converts_other_offsets_to_utc():
    bogota = timezone(timedelta(hours=-5))
    instant = datetime(2026, 7, 28, 16, 19, 31, 751000, tzinfo=bogota)
    assert js_iso_now(instant) == "2026-07-28T21:19:31.751Z"


def test_js_iso_now_reads_a_naive_datetime_as_utc():
    naive = datetime(2026, 7, 28, 21, 19, 31, 751000)
    assert js_iso_now(naive) == "2026-07-28T21:19:31.751Z"


def test_js_iso_now_default_is_the_current_utc_instant():
    before = datetime.now(timezone.utc)
    produced = js_iso_now()
    after = datetime.now(timezone.utc)
    assert JS_ISO.match(produced)
    parsed = datetime.strptime(produced, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    assert before - timedelta(milliseconds=1) <= parsed <= after


def test_every_captured_date_has_the_js_shape(validatebid_requests):
    for body in validatebid_requests:
        assert JS_ISO.match(body["Date"])


def test_the_payload_date_is_the_injected_instant():
    instant = datetime(2026, 7, 28, 21, 19, 31, 751000, tzinfo=timezone.utc)
    built, _ = build_payload(REF, detail([captured_item()]), Decimal("1"), now=instant)
    assert built.to_dict()["Date"] == "2026-07-28T21:19:31.751Z"
    assert built.date == js_iso_now(instant)
