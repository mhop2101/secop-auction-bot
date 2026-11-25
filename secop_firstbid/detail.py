"""Stage 2 — auction detail and the item model (plan §6).

Three GETs and one assembly. ``GetAuctionSimple`` gives the configuration, the state and the
timing; ``GetAuction`` gives the items the payload is built from; ``Bid/GetBidsTotalInfo``
gives the baseline the server measures a bid against. The result is one
:class:`~secop_firstbid.models.AuctionDetail`, which is the only thing later stages read.

**The item mapping in :func:`parse_items` has never been checked against a response.**
``GetAuction`` appears zero times in the capture — every field name below was read out of the
SPA's JavaScript (``auctionService.js``, ``priceListController.js``) and nothing else. See
``docs/open-questions.md`` Q7; the first live ``preflight`` answers it, and its body belongs in
``tests/fixtures/`` the moment there is one.

This module reports and never repairs. A ``myLastBid`` of ``0`` is logged with what it means
(§7) rather than worked around; an amount string it cannot read unambiguously raises rather
than being guessed at, because a Colombian-formatted number read as a wire decimal is wrong by
three orders of magnitude.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import endpoints, numbers
from .errors import Failure, SecopError, classify
from .logging_setup import RunLogger
from .models import AuctionDetail, AuctionRef, ItemMeta
from .session import HttpClient

STAGE = "detail"

PRICE_LIST_KEY = "biddingPriceListItems"
QUANTITATIVE_KEY = "biddingQuantitativeItems"

# "containtItems" is the server's spelling, not a typo here.
CONTAINS_ITEMS_KEY = "containtItems"

# §6.2 names it biddingList; the observed GetAuctionSimple carries an always-empty
# biddingsList. The two spellings come from different sources and only a live GetAuction can
# say which one that route uses, so both are accepted.
BIDDING_LIST_KEYS = ("biddingList", "biddingsList")

_FRACTION = re.compile(r"\.(\d+)")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        return numbers.to_wire(_coerce_decimal(value))
    return ""


def _coerce_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    # repr() of a float is the shortest string that round-trips it, so this reproduces the
    # literal the server sent whenever the body was parsed without parse_float=Decimal.
    return Decimal(repr(value))


def _wire_decimal(text: str, field: str, label: str) -> Decimal:
    try:
        return numbers.parse_wire_decimal(text)
    except SecopError:
        raise SecopError(
            Failure.PARSE_FAILED,
            "%s: %s is %r, which is not a wire decimal. The *Str fields are the SPA's own "
            "wire strings and use a dot decimal separator; a Colombian-formatted amount "
            "read as one of them is wrong by three orders of magnitude, so it is refused "
            "rather than guessed (§8.1)." % (label, field, text),
            {"item": label, "field": field, "value": text},
        )


def _decimal(value: Any, field: str, label: str) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return _wire_decimal(value.strip(), field, label)
    if isinstance(value, (int, float, Decimal)):
        return _coerce_decimal(value)
    raise SecopError(
        Failure.PARSE_FAILED,
        "%s: %s is %r, which is not an amount" % (label, field, value),
        {"item": label, "field": field},
    )


def _amount(
    raw: Mapping[str, Any], str_key: str, numeric_key: str, label: str
) -> Optional[Decimal]:
    """The ``*Str`` reading of an amount, cross-checked against its numeric sibling.

    The string is authoritative — it is the form the SPA puts on the wire — but when the
    response also carries the plain number, a disagreement between the two means one of the
    readings is wrong, and neither is worth guessing at silently.
    """
    from_string = _decimal(raw.get(str_key), str_key, label)
    from_number = _decimal(raw.get(numeric_key), numeric_key, label)
    if from_string is not None and from_number is not None and from_string != from_number:
        raise SecopError(
            Failure.PARSE_FAILED,
            "%s: %s is %r (%s) but %s is %s. The two disagree, so the wire reading of this "
            "amount is undecidable."
            % (
                label,
                str_key,
                raw.get(str_key),
                numbers.to_wire(from_string),
                numeric_key,
                numbers.to_wire(from_number),
            ),
            {
                "item": label,
                str_key: _text(raw.get(str_key)),
                numeric_key: numbers.to_wire(from_number),
            },
        )
    return from_string if from_string is not None else from_number


def _int(value: Any, default: int = 0) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(_coerce_decimal(value) if isinstance(value, (float, Decimal)) else str(value).strip())
    except (ValueError, ArithmeticError):
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse a .NET-rendered UTC timestamp, or give up quietly.

    Python 3.9's ``fromisoformat`` rejects both the ``Z`` suffix and the 7 fractional digits
    .NET emits, so each is normalised first. These timestamps are diagnostics — the fire time
    is operator-supplied (§9.1) — so an unreadable one degrades to ``None`` instead of
    stopping a run.
    """
    text = _text(value)
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # The API renders UTC and timeZoneCode is null, so a value without an offset is UTC
        # and never the host's local zone.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_text(sources: Iterable[Mapping[str, Any]], keys: Tuple[str, ...]) -> str:
    ordered = list(sources)
    for key in keys:
        for source in ordered:
            value = _text(source.get(key))
            if value:
                return value
    return ""


def _fetch_json(
    client: HttpClient,
    ref: AuctionRef,
    logger: RunLogger,
    path: str,
    what: str,
) -> Dict[str, Any]:
    outcome = client.get(path, stage=STAGE)
    if outcome.status == 200 and isinstance(outcome.body, dict):
        return outcome.body

    failure = classify(
        outcome.status, outcome.body, outcome.exception, raw_text=outcome.text
    )
    if failure in (Failure.OK, Failure.UNKNOWN):
        # A 2xx that is not a JSON object is not a transport failure; it is a body no
        # auction can be built from.
        failure = Failure.PARSE_FAILED
    detail = {
        "auction_id": ref.auction_id,
        "path": path,
        "status": outcome.status,
        "corr_id": outcome.corr_id,
        "failure": failure.value,
    }  # type: Dict[str, Any]
    if outcome.exception is not None:
        detail["exception"] = "%s: %s" % (
            type(outcome.exception).__name__,
            outcome.exception,
        )
    logger.event(
        stage=STAGE,
        event="error",
        level="ERROR",
        corr_id=outcome.corr_id,
        detail=dict(detail, what=what),
    )
    raise SecopError(
        failure,
        "%s returned status %s with no JSON object" % (what, outcome.status),
        detail,
    )


def fetch_auction_simple(
    client: HttpClient, ref: AuctionRef, logger: RunLogger
) -> Dict[str, Any]:
    """The 59-key configuration/state/timing view (observed, §6.1)."""
    return _fetch_json(
        client,
        ref,
        logger,
        endpoints.get_auction_simple(ref.auction_id),
        "GetAuctionSimple",
    )


def fetch_auction(
    client: HttpClient, ref: AuctionRef, logger: RunLogger
) -> Dict[str, Any]:
    """The item list. **This route has never been observed** (docs/open-questions.md Q7).

    The response is logged in full like every other exchange, which is what turns the first
    real run into the answer: the body lands in the run directory and belongs in
    ``tests/fixtures/`` from there.
    """
    body = _fetch_json(
        client,
        ref,
        logger,
        endpoints.get_auction(ref.auction_id, ref.lang),
        "GetAuction",
    )
    logger.event(
        stage=STAGE,
        event="item_model_shape",
        detail={
            "auction_id": ref.auction_id,
            "question": "Q7",
            "note": (
                "The item mapping was verified against a real GetAuction body on 2026-07-29 "
                "(tests/fixtures/getauction.json): the item GUID is `id`, and the lot id is "
                "the item's `biddingListId`. Only that one auction has been seen, so a body "
                "whose shape differs from the fixture is worth capturing from this run's "
                "http/ directory."
            ),
            "top_level_keys": sorted(str(key) for key in body),
        },
    )
    return body


def fetch_bids_total_info(
    client: HttpClient, ref: AuctionRef, logger: RunLogger
) -> Dict[str, Any]:
    """``{auctionId, myLastBid, lastBid, score}`` — the baseline behind §7."""
    return _fetch_json(
        client,
        ref,
        logger,
        endpoints.get_bids_total_info(ref.auction_id),
        "GetBidsTotalInfo",
    )


def _item_meta(
    raw: Mapping[str, Any], entry: Mapping[str, Any], is_price_list_item: bool
) -> ItemMeta:
    label = (
        _text(raw.get("id"))
        or _text(raw.get("itemName"))
        or _text(raw.get("itemInternalReference"))
        or "<unnamed item>"
    )

    # The item GUID is `id` on the response. The SPA's own builder reads it as `itemName`
    # because auctionService.js renames it while flattening biddingList into self.Items —
    # so the plan's client-code-derived name describes the SPA's model, not the wire.
    # `itemName` is kept as a fallback and is absent from every observed response.
    item_id = _text(raw.get("id")) or _text(raw.get("itemName"))
    if not item_id:
        raise SecopError(
            Failure.PARSE_FAILED,
            "an item in bidding list %r has neither id nor itemName; one of them carries the "
            "item GUID that goes on the wire as itemId, and there is nothing else to derive "
            "it from"
            % (_first_text((entry,), ("biddingListId", "lotInternalId", "groupReference")),),
            {"item": label, "field": "id"},
        )

    quantity = _amount(raw, "itemQuantityStr", "itemQuantity", label)
    if quantity is None:
        raise SecopError(
            Failure.PARSE_FAILED,
            "item %s has no itemQuantityStr and no itemQuantity; quantity is sent on the "
            "wire and multiplies into totalValue, so there is no safe default" % item_id,
            {"item": item_id, "field": "itemQuantityStr"},
        )

    initial_value = _amount(
        raw, "itemInitialUnitPriceStr", "itemInitialUnitPrice", label
    )
    if initial_value is None:
        if is_price_list_item:
            raise SecopError(
                Failure.PARSE_FAILED,
                "price-list item %s has no itemInitialUnitPriceStr; it is the numerator of "
                "itemQuantityUnitPrice and a zero default would ship a silently wrong "
                "payload" % item_id,
                {"item": item_id, "field": "itemInitialUnitPriceStr"},
            )
        initial_value = Decimal(0)

    min_bid_difference = _amount(raw, "minBidDifferenceStr", "minBidDifference", label)

    return ItemMeta(
        item_id=item_id,
        quantity=quantity,
        item_unit=_text(raw.get("itemUnit")),
        # Non-price-list items carry no reference of their own; the SPA sends the description.
        item_internal_reference=_text(
            raw.get("itemInternalReference") if is_price_list_item else raw.get("itemDescription")
        ),
        # The item's own `biddingListId` is what the wire carries as `lotInternalId`. The
        # bidding-list entry also has a `lotInternalId`, but on an AuctionbyProposal it reads
        # "ALLLOTS" — a marker, not the lot id — so the item must be consulted first and the
        # entry's copy is the last resort behind its `internalId`/`id`.
        lot_internal_id=(
            _first_text((raw,), ("biddingListId", "lotInternalId"))
            or _first_text((entry,), ("internalId", "id", "lotInternalId"))
        ),
        lot_name=_first_text((raw, entry), ("lotName", "groupReference")),
        initial_value=initial_value,
        min_bid_difference=Decimal(0) if min_bid_difference is None else min_bid_difference,
        is_price_list_item=is_price_list_item,
        quantity_str=_text(raw.get("itemQuantityStr")),
        display_item_order=_int(raw.get("displayItemOrder")),
    )


def _entries(get_auction_body: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    found = None  # type: Optional[Any]
    for key in BIDDING_LIST_KEYS:
        value = get_auction_body.get(key)
        if isinstance(value, list) and value:
            found = value
            break
        if found is None and isinstance(value, list):
            found = value
    if found is None:
        raise SecopError(
            Failure.PARSE_FAILED,
            "GetAuction body carries no %s; without it there are no items to bid on"
            % " or ".join(BIDDING_LIST_KEYS),
            {"top_level_keys": sorted(str(key) for key in get_auction_body)},
        )
    return [entry for entry in found if isinstance(entry, Mapping)]


def _item_array(entry: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = entry.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def parse_items(get_auction_body: Mapping[str, Any]) -> List[ItemMeta]:
    """Flatten ``GetAuction``'s bidding lists into the item model (§6.2).

    **Client-code-only and unverified.** ``GetAuction`` appears zero times in the capture —
    no response has ever been observed — so every field name here comes from reading the
    SPA's JavaScript and nothing corroborates it. ``docs/open-questions.md`` Q7 tracks it;
    the first live run settles it.

    The mapping, in the SPA's own terms:

    * ``itemId`` ← ``itemName``, which holds a GUID despite the name.
    * ``quantity`` ← ``itemQuantityStr``, **kept as a string** in
      :attr:`ItemMeta.quantity_str`: it carries the 6-dp form ``"1.000000"`` that goes on the
      wire verbatim. The :class:`~decimal.Decimal` beside it drives the formulas.
    * ``itemUnit`` ← ``itemUnit``; ``itemInternalReference`` ← ``itemInternalReference``,
      except on non-price-list items, where the SPA uses ``itemDescription``.
    * ``lotInternalId`` ← ``lotInternalId`` ‖ ``biddingListId``,
      ``lotName`` ← ``lotName`` ‖ ``groupReference``, each looked up on the item and then on
      its bidding-list entry.
    * ``initial_value`` ← ``itemInitialUnitPriceStr`` (per-unit), plus ``minBidDifference``
      for §7 reasoning.

    Walks ``biddingList[]``, taking ``biddingPriceListItems[]`` and
    ``biddingQuantitativeItems[]`` from every entry that carries items, then sorts the whole
    flattened list by ``displayItemOrder`` the way ``auctionService.js`` builds ``self.Items``.
    The sort is stable, so items sharing an order keep document order.
    """
    metas = []  # type: List[ItemMeta]
    for entry in _entries(get_auction_body):
        price_items = _item_array(entry, PRICE_LIST_KEY)
        quantitative_items = _item_array(entry, QUANTITATIVE_KEY)
        # containtItems marks the entries that hold items rather than chapter headings. It
        # never excludes an entry that carries items: dropping a line the auction requires
        # because a flag we have never seen on the wire says so would ship an incomplete
        # payload silently.
        if not (entry.get(CONTAINS_ITEMS_KEY) or price_items or quantitative_items):
            continue
        for raw in price_items:
            metas.append(_item_meta(raw, entry, True))
        for raw in quantitative_items:
            metas.append(_item_meta(raw, entry, False))

    metas.sort(key=lambda meta: meta.display_item_order)
    return metas


def baseline_note(detail: AuctionDetail) -> Optional[str]:
    """What ``myLastBid`` means for this run, or ``None`` when it means nothing notable."""
    if detail.my_last_bid is None:
        return (
            "GetBidsTotalInfo was not read, so the baseline the server decrements from is "
            "unknown for this run."
        )
    if detail.my_last_bid == 0:
        return (
            "myLastBid is 0: you have no standing bid in this auction. That is the state "
            "every bidder is in before a first bid and it does not predict a rejection — "
            "on 2026-11-20 the one accepted bid in each of two live auctions was placed "
            "from it. In those auctions the same value sent after that bid stood was "
            'answered errorType="BidErrMin" every time. What BidErrMin is measured against '
            "is still not established: the capture shows it on an auction with no bids at "
            "all. lastBid does not settle it either — it reads 0 both with no bid and with "
            "a standing bid of 0. The app sends your value once, as given, and reports "
            "whatever comes back (§7)."
        )
    return None


def _log_detail(logger: RunLogger, detail: AuctionDetail) -> None:
    logger.event(
        stage=STAGE,
        event="auction_detail",
        detail={
            "version_code": detail.version_code,
            "type_code": detail.type_code,
            "currency": detail.currency,
            "round_type": detail.round_type,
            "decimal_num": detail.decimal_num,
            "total_num": detail.total_num,
            "minimum_bid_type": detail.minimum_bid_type,
            "start_price": numbers.to_wire(detail.start_price),
            "start_date": None if detail.start_date is None else detail.start_date.isoformat(),
            "end_date": None if detail.end_date is None else detail.end_date.isoformat(),
            "state_code": detail.state_code,
            "exec_state_code": detail.exec_state_code,
            "allow_bids_below_best": detail.allow_bids_below_best,
            "test_auction": detail.test_auction,
            "time_zone_code": detail.time_zone_code,
            "items": len(detail.items),
            "price_list_items": sum(1 for item in detail.items if item.is_price_list_item),
        },
    )
    note = baseline_note(detail)
    logger.event(
        stage=STAGE,
        event="bid_baseline",
        level="INFO" if note is None else "WARNING",
        detail={
            "my_last_bid": None
            if detail.my_last_bid is None
            else numbers.to_wire(detail.my_last_bid),
            "last_bid": None
            if detail.last_bid is None
            else numbers.to_wire(detail.last_bid),
            "note": note,
        },
    )
    for item in detail.items:
        logger.event(
            stage=STAGE,
            event="item_model",
            detail={
                "item_id": item.item_id,
                "quantity_str": item.quantity_str,
                "quantity": numbers.to_wire(item.quantity),
                "item_unit": item.item_unit,
                "item_internal_reference": item.item_internal_reference,
                "lot_internal_id": item.lot_internal_id,
                "lot_name": item.lot_name,
                "initial_value": numbers.to_wire(item.initial_value),
                "min_bid_difference": numbers.to_wire(item.min_bid_difference),
                "is_price_list_item": item.is_price_list_item,
                "display_item_order": item.display_item_order,
            },
        )


def build_detail(
    simple: Mapping[str, Any],
    auction: Optional[Mapping[str, Any]],
    totals: Optional[Mapping[str, Any]],
    *,
    logger: Optional[RunLogger] = None
) -> AuctionDetail:
    """Assemble the three responses into one :class:`AuctionDetail`, and say what it means.

    ``auctionVersionCode`` and ``currencyCode`` are required: the first decides the payload
    shape and the second is sent as ``currency``, so neither has a safe default. Everything
    else degrades — an absent ``startPrice`` or an unreadable ``startDate`` is a thinner log
    line, not a stopped run.

    With a ``logger``, this also writes the run's record of ``myLastBid``/``lastBid`` and, when
    ``myLastBid`` is 0, the §7 note explaining what that costs (see :func:`baseline_note`).
    """
    version_code = _text(simple.get("auctionVersionCode"))
    if not version_code:
        raise SecopError(
            Failure.PARSE_FAILED,
            "GetAuctionSimple carries no auctionVersionCode; it decides the payload shape "
            "(AuctionbyProposal vs AuctionbyItem vs AuctionbyLot) and cannot be assumed",
            {"top_level_keys": sorted(str(key) for key in simple)},
        )
    currency = _text(simple.get("currencyCode"))
    if not currency:
        raise SecopError(
            Failure.PARSE_FAILED,
            "GetAuctionSimple carries no currencyCode; it is sent verbatim as the payload's "
            "currency field",
            {"top_level_keys": sorted(str(key) for key in simple)},
        )

    start_price = _decimal(simple.get("startPrice"), "startPrice", "auction")
    my_last_bid = None  # type: Optional[Decimal]
    last_bid = None  # type: Optional[Decimal]
    if totals is not None:
        my_last_bid = _decimal(totals.get("myLastBid"), "myLastBid", "totals")
        last_bid = _decimal(totals.get("lastBid"), "lastBid", "totals")

    detail = AuctionDetail(
        version_code=version_code,
        type_code=_text(simple.get("auctionTypeCode")),
        currency=currency,
        round_type=_text(simple.get("roundTypeCode")),
        decimal_num=_int(simple.get("decimalNum")),
        total_num=_int(simple.get("totalNum")),
        minimum_bid_type=_optional_int(simple.get("minimumBidType")),
        start_price=Decimal(0) if start_price is None else start_price,
        start_date=_parse_iso(simple.get("startDate")),
        end_date=_parse_iso(simple.get("endDate")),
        state_code=_text(simple.get("auctionStateCode")) or None,
        exec_state_code=_text(simple.get("auctionExecutionStateCode")) or None,
        # "Bellow" is the server's spelling.
        allow_bids_below_best=bool(simple.get("allowBidsBellowBestBid")),
        test_auction=bool(simple.get("testAuction")),
        items=[] if auction is None else parse_items(auction),
        title=_text(simple.get("title")) or None,
        time_zone_code=_text(simple.get("timeZoneCode")) or None,
        my_last_bid=my_last_bid,
        last_bid=last_bid,
        raw_simple=dict(simple),
    )
    if logger is not None:
        _log_detail(logger, detail)
    return detail


def fetch_detail(
    client: HttpClient, ref: AuctionRef, logger: RunLogger
) -> AuctionDetail:
    """Run all of stage 2 in §6.1 order and return the assembled detail.

    ``GetBidsTotalInfo`` is the one call that may fail without stopping the run: it feeds a
    log line and the §7 note, never the payload. Losing it costs an explanation, not a bid,
    so the failure is recorded and the run continues with an unknown baseline.
    """
    simple = fetch_auction_simple(client, ref, logger)
    auction = fetch_auction(client, ref, logger)

    totals = None  # type: Optional[Dict[str, Any]]
    try:
        totals = fetch_bids_total_info(client, ref, logger)
    except SecopError as exc:
        logger.event(
            stage=STAGE,
            event="bid_baseline_unavailable",
            level="WARNING",
            detail={
                "failure": exc.code.value,
                "message": exc.message,
                "note": (
                    "myLastBid/lastBid are unknown for this run; the §7 rejection reason "
                    "cannot be anticipated, only read off the response."
                ),
            },
        )

    return build_detail(simple, auction, totals, logger=logger)


__all__ = [
    "STAGE",
    "PRICE_LIST_KEY",
    "QUANTITATIVE_KEY",
    "CONTAINS_ITEMS_KEY",
    "BIDDING_LIST_KEYS",
    "fetch_auction_simple",
    "fetch_auction",
    "fetch_bids_total_info",
    "parse_items",
    "baseline_note",
    "build_detail",
    "fetch_detail",
]
