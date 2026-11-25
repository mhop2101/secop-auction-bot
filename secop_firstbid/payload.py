"""Bid payload construction (plan §8.3 shape, §8.4 formulas, §8.5 worked example).

Builds the one JSON object the run sends. It is assembled once, ahead of the fire instant,
and serialized once (:meth:`BidPayload.to_bytes`) so every thread puts identical bytes on
the wire.

``items[]`` is one entry per **price-list** item, and ``isPriceListItem`` is literal ``true``
on every one (§8.3). ``detail.parse_items`` flattens ``biddingQuantitativeItems[]`` too, at
``is_price_list_item=False``, so those stay in ``AuctionDetail`` for logging and are filtered
out here — a criterion item on the wire would carry the full ``bidValue`` and add its
``totalValue`` into the top-level sum.

Two things this module deliberately does not do. It never touches the operator's number:
``bidValue`` on the wire is the given ``bid_value`` rendered as-is — no floor, no minimum,
no decrement, no per-item adjustment (§7). And it never normalizes a computed amount:
per-item and top-level ``totalValue`` keep every digit the multiplication and the sum
produced, because scale addition is what reproduces the captured strings.

What it does do is refuse. A ``quantity``/``quantity_str`` disagreement, an invariant that
breaks by more than the documented 12-dp quantization, or an item list holding no price-list
item raises ``MALFORMED_PAYLOAD`` rather than putting a guess on the wire; an
``auctionVersionCode`` other than ``AuctionbyProposal`` raises
``UNSUPPORTED_AUCTION_VERSION`` (v1 scope); and any item at ``quantity != 1`` produces a loud
warning carrying the competing formulas' values, because the capture cannot decide between
them.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from . import numbers
from .errors import Failure, SecopError
from .models import AuctionDetail, AuctionRef, BidItem, BidPayload, ItemMeta

SUPPORTED_AUCTION_VERSION = "AuctionbyProposal"
RANKING = -1
IQUP_ROUNDING = decimal.ROUND_HALF_UP

_ONE = Decimal(1)
_IQUP_ULP = Decimal(1).scaleb(-numbers.IQUP_SCALE)


@dataclass(frozen=True)
class BuildReport:
    """Everything the operator needs to judge a payload before it is sent.

    ``invariant_ok`` is True on every report a caller receives: a real breach raises
    ``MALFORMED_PAYLOAD`` instead of returning, so the field is there to be logged as a
    positive statement that the check ran, not as a condition to branch on.

    ``excluded_items`` and ``excluded_total_value`` account for the non-price-list items
    ``build_payload`` left out: their ids, and the amount ``totalValue`` would have carried
    had they been bid on. Both are stated even when nothing was excluded, so a run's records
    show the filter ran.
    """

    inexact_items: List[str]
    residuals: Dict[str, str]
    invariant_ok: bool
    warnings: List[str]
    excluded_items: List[str]
    excluded_total_value: str


def js_iso_now(dt: Optional[datetime] = None) -> str:
    """Format ``dt`` the way JavaScript's ``Date.toISOString()`` does.

    The captured bodies carry ``"2026-07-28T21:19:31.751Z"``: exactly three fractional
    digits and a ``Z`` suffix. ``datetime.isoformat()`` produces six fractional digits and
    ``+00:00``, so it cannot be used here — the string is built digit by digit instead.

    Sub-millisecond precision is truncated, not rounded, matching a JS ``Date``, which holds
    integer milliseconds. A naive ``dt`` is read as UTC; an aware one is converted.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond // 1000,
    )


_wire = numbers.to_wire


def _quantity_wire(meta: ItemMeta) -> str:
    if not meta.quantity_str:
        return _wire(meta.quantity)
    declared = numbers.parse_wire_decimal(meta.quantity_str)
    if declared != meta.quantity:
        raise SecopError(
            Failure.MALFORMED_PAYLOAD,
            "item %s: quantity_str %r is %s but quantity is %s; the string goes on the "
            "wire and the Decimal drives the formulas, so they must be the same number"
            % (
                meta.item_id,
                meta.quantity_str,
                _wire(declared),
                _wire(meta.quantity),
            ),
            {
                "item_id": meta.item_id,
                "quantity_str": meta.quantity_str,
                "quantity": _wire(meta.quantity),
            },
        )
    return meta.quantity_str


def _verify_invariant(
    meta: ItemMeta, bid_value: Decimal, iqup: Decimal, is_exact: bool
) -> None:
    if meta.initial_value == 0 or bid_value == 0:
        return
    if numbers.check_invariant(iqup, bid_value, meta.initial_value, meta.quantity):
        return
    with decimal.localcontext(numbers.CTX):
        product = iqup * bid_value
        expected = meta.initial_value * meta.quantity
        drift = abs(product - expected)
        tolerance = _IQUP_ULP * abs(bid_value)
    if not is_exact and drift <= tolerance:
        return
    raise SecopError(
        Failure.MALFORMED_PAYLOAD,
        "item %s: itemQuantityUnitPrice x bidValue != initialValue x quantity "
        "(%s x %s = %s, expected %s, drift %s); that is a formatting or precision bug, "
        "not a server decision"
        % (
            meta.item_id,
            _wire(iqup),
            _wire(bid_value),
            _wire(product),
            _wire(expected),
            _wire(drift),
        ),
        {
            "item_id": meta.item_id,
            "item_quantity_unit_price": _wire(iqup),
            "bid_value": _wire(bid_value),
            "drift": _wire(drift),
        },
    )


def build_bid_item(meta: ItemMeta, bid_value: Decimal) -> Tuple[BidItem, bool, Decimal]:
    """Build one wire item, returning it with ``(is_exact, residual)`` from the division.

    ``isChanged`` and ``bidded`` are literal ``true``: every item carries the operator's
    value, so every item changed and every item is bid on.
    """
    quantity_wire = _quantity_wire(meta)
    total = numbers.total_value(meta.quantity, bid_value)
    iqup, is_exact, residual = numbers.item_quantity_unit_price(
        meta.initial_value, meta.quantity, bid_value, rounding=IQUP_ROUNDING
    )
    _verify_invariant(meta, bid_value, iqup, is_exact)
    item = BidItem(
        item_id=meta.item_id,
        quantity=quantity_wire,
        is_price_list_item=meta.is_price_list_item,
        is_changed=True,
        item_internal_reference=meta.item_internal_reference,
        lot_internal_id=meta.lot_internal_id,
        lot_name=meta.lot_name,
        item_unit=meta.item_unit,
        total_value=_wire(total),
        bid_value=_wire(bid_value),
        item_quantity_unit_price=_wire(iqup),
        bidded=True,
    )
    return (item, is_exact, residual)


def _quantity_warning(meta: ItemMeta, bid_value: Decimal, sent: str) -> str:
    without_quantity, _, _ = numbers.item_quantity_unit_price(
        meta.initial_value, _ONE, bid_value, rounding=IQUP_ROUNDING
    )
    with decimal.localcontext(numbers.CTX):
        scaled_bid = bid_value * meta.quantity
    divided_by_quantity, _, _ = numbers.item_quantity_unit_price(
        meta.initial_value, _ONE, scaled_bid, rounding=IQUP_ROUNDING
    )
    return (
        "UNVERIFIED QUANTITY FACTOR: item %s has quantity %s, not 1. Every captured bid "
        "carried exactly one item at quantity 1.000000, where the quantity factor of "
        "itemQuantityUnitPrice is non-discriminating, so nothing in the capture decides "
        "this. Sending initialValue*quantity/bidValue = %s. The competing readings of the "
        "same capture give initialValue/bidValue = %s and initialValue/(bidValue*quantity) "
        "= %s. Verify against the SPA before trusting this payload."
        % (
            meta.item_id,
            _wire(meta.quantity),
            sent,
            _wire(without_quantity),
            _wire(divided_by_quantity),
        )
    )


def _inexact_warning(meta: ItemMeta, bid_value: Decimal, residual: Decimal) -> str:
    return (
        "INEXACT DIVISION: item %s at bidValue %s — initialValue*quantity/bidValue does "
        "not terminate within %d decimal places. Quantized with %s, residual %s. The "
        "server's rounding mode for this division is undocumented, so this value may "
        "differ from the one the SPA would have sent; a bidValue that divides exactly "
        "avoids the question entirely."
        % (
            meta.item_id,
            _wire(bid_value),
            numbers.IQUP_SCALE,
            IQUP_ROUNDING,
            _wire(residual),
        )
    )


def _zero_warning(meta: ItemMeta, bid_value: Decimal) -> str:
    return (
        "ZERO SPECIAL CASE: item %s has initialValue %s and bidValue %s, so "
        "itemQuantityUnitPrice is sent as 0 and the invariant "
        "itemQuantityUnitPrice*bidValue == initialValue*quantity is not checked for it."
        % (
            meta.item_id,
            _wire(meta.initial_value),
            _wire(bid_value),
        )
    )


def _excluded_note(
    excluded: List[ItemMeta], excluded_total: Decimal, sent_total: Decimal
) -> str:
    with decimal.localcontext(numbers.CTX):
        combined = sent_total + excluded_total
    return (
        "NON-PRICE-LIST ITEMS EXCLUDED: %d item(s) from GetAuction carry "
        "isPriceListItem false — quantitative/criterion items, which the bid body does not "
        "carry. items[] holds the price-list items only. Bidding them too would have added "
        "%s, making totalValue %s instead of the %s being sent. Excluded: %s."
        % (
            len(excluded),
            _wire(excluded_total),
            _wire(combined),
            _wire(sent_total),
            ", ".join(
                "%s (%s)" % (meta.item_id, meta.item_internal_reference or meta.item_unit)
                for meta in excluded
            ),
        )
    )


def build_payload(
    ref: AuctionRef,
    detail: AuctionDetail,
    bid_value: Decimal,
    now: Optional[datetime] = None,
) -> Tuple[BidPayload, BuildReport]:
    """Build the whole ``CreateBid``/``ValidateBid`` body plus its build report.

    Only the price-list items of ``detail`` reach ``items[]``; the report names the ones left
    out and the ``totalValue`` they would have added. Every item that does reach it gets the
    same ``bid_value``, exactly as the operator supplied it. The top-level ``totalValue`` is
    the plain Decimal sum of the per-item totals, so the widest scale in the payload wins and
    no trailing zero is dropped.
    """
    if detail.version_code != SUPPORTED_AUCTION_VERSION:
        raise SecopError(
            Failure.UNSUPPORTED_AUCTION_VERSION,
            "auctionVersionCode is %r; v1 supports only %r. AuctionbyItem and AuctionbyLot "
            "bid per item or per lot, which needs several bodies and breaks the "
            "one-payload-serialized-once model this run is built on."
            % (detail.version_code, SUPPORTED_AUCTION_VERSION),
            {
                "auction_id": ref.auction_id,
                "version_code": detail.version_code,
                "supported": SUPPORTED_AUCTION_VERSION,
            },
        )
    # items[] is one entry per price-list item; a criterion item there would be bid on.
    price_list = [meta for meta in detail.items if meta.is_price_list_item]
    excluded = [meta for meta in detail.items if not meta.is_price_list_item]
    if not price_list:
        raise SecopError(
            Failure.MALFORMED_PAYLOAD,
            "auction %s has no price-list items to bid on (%d of %d parsed item(s) carry "
            "isPriceListItem false); an empty items[] would send a totalValue of 0 against "
            "a live auction" % (ref.auction_id, len(excluded), len(detail.items)),
            {
                "auction_id": ref.auction_id,
                "item_count": len(detail.items),
                "excluded_count": len(excluded),
                "excluded_items": [meta.item_id for meta in excluded],
            },
        )

    items = []  # type: List[BidItem]
    inexact_items = []  # type: List[str]
    residuals = {}  # type: Dict[str, str]
    warnings = []  # type: List[str]
    total = Decimal(0)

    for meta in price_list:
        # list order is wire order: detail.parse_items already ordered by displayItemOrder
        item, is_exact, residual = build_bid_item(meta, bid_value)
        items.append(item)
        residuals[meta.item_id] = _wire(residual)
        if not is_exact:
            inexact_items.append(meta.item_id)
            warnings.append(_inexact_warning(meta, bid_value, residual))
        if meta.quantity != _ONE:
            warnings.append(
                _quantity_warning(meta, bid_value, item.item_quantity_unit_price)
            )
        if meta.initial_value == 0 or bid_value == 0:
            warnings.append(_zero_warning(meta, bid_value))
        with decimal.localcontext(numbers.CTX):
            total = total + numbers.parse_wire_decimal(item.total_value)

    excluded_total = Decimal(0)
    for meta in excluded:
        with decimal.localcontext(numbers.CTX):
            excluded_total = excluded_total + numbers.total_value(meta.quantity, bid_value)
    if excluded:
        warnings.append(_excluded_note(excluded, excluded_total, total))

    payload = BidPayload(
        auction_id=ref.auction_id,
        currency=detail.currency,
        date=js_iso_now(now),
        ranking=RANKING,
        items=items,
        total_value=_wire(total),
        language_code=ref.lang,
    )
    report = BuildReport(
        inexact_items=inexact_items,
        residuals=residuals,
        invariant_ok=True,
        warnings=warnings,
        excluded_items=[meta.item_id for meta in excluded],
        excluded_total_value=_wire(excluded_total),
    )
    return (payload, report)


__all__ = [
    "SUPPORTED_AUCTION_VERSION",
    "RANKING",
    "IQUP_ROUNDING",
    "BuildReport",
    "js_iso_now",
    "build_bid_item",
    "build_payload",
]
