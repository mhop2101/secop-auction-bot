"""Dataclasses shared by every stage (plan §13 "Key dataclasses"; wire objects are §8.3).

:class:`BidItem` and :class:`BidPayload` are the only types here whose serialization is
constrained: they reproduce the observed ``ValidateBid`` request byte-for-byte apart from
``Date``. Everything else is a plain container.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .errors import Failure


@dataclass(frozen=True)
class AuctionRef:
    auction_id: str
    lang: str = "es-CO"
    market: str = "GovCo"
    eauction_doc_id: Optional[str] = None
    mkey: Optional[str] = None
    redirect_path: Optional[str] = None
    source_extractor: str = "unknown"
    state_text: Optional[str] = None
    lang_defaulted: bool = False
    market_defaulted: bool = False


@dataclass(frozen=True)
class ItemMeta:
    item_id: str
    quantity: Decimal
    item_unit: str
    item_internal_reference: str
    lot_internal_id: str
    lot_name: str
    initial_value: Decimal
    min_bid_difference: Decimal = Decimal(0)
    is_price_list_item: bool = True
    quantity_str: str = ""
    display_item_order: int = 0


@dataclass(frozen=True)
class AuctionDetail:
    version_code: str
    type_code: str
    currency: str
    round_type: str
    decimal_num: int
    total_num: int
    minimum_bid_type: Optional[int]
    start_price: Decimal
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    state_code: Optional[str]
    exec_state_code: Optional[str]
    allow_bids_below_best: bool
    test_auction: bool
    items: List[ItemMeta]
    title: Optional[str] = None
    time_zone_code: Optional[str] = None
    my_last_bid: Optional[Decimal] = None
    last_bid: Optional[Decimal] = None
    raw_simple: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class BidItem:
    """Wire object. `to_dict()` MUST emit keys in the observed wire order."""

    item_id: str
    quantity: str
    is_price_list_item: bool
    is_changed: bool
    item_internal_reference: str
    lot_internal_id: str
    lot_name: str
    item_unit: str
    total_value: str
    bid_value: str
    item_quantity_unit_price: str
    bidded: bool

    def to_dict(self) -> "OrderedDict[str, Any]":
        out = OrderedDict()  # type: OrderedDict[str, Any]
        # wire order, do not sort; totalValue precedes bidValue
        out["itemId"] = self.item_id
        out["quantity"] = self.quantity
        out["isPriceListItem"] = self.is_price_list_item
        out["isChanged"] = self.is_changed
        out["itemInternalReference"] = self.item_internal_reference
        out["lotInternalId"] = self.lot_internal_id
        out["lotName"] = self.lot_name
        out["itemUnit"] = self.item_unit
        out["totalValue"] = self.total_value
        out["bidValue"] = self.bid_value
        out["itemQuantityUnitPrice"] = self.item_quantity_unit_price
        out["bidded"] = self.bidded
        return out


@dataclass(frozen=True)
class BidPayload:
    auction_id: str
    currency: str
    date: str
    ranking: int
    items: List[BidItem]
    total_value: str
    language_code: str

    def to_dict(self) -> "OrderedDict[str, Any]":
        out = OrderedDict()  # type: OrderedDict[str, Any]
        # wire order, do not sort
        out["auctionId"] = self.auction_id
        out["currency"] = self.currency
        out["Date"] = self.date
        out["ranking"] = self.ranking
        out["items"] = [item.to_dict() for item in self.items]
        out["totalValue"] = self.total_value
        out["languageCode"] = self.language_code
        return out

    def to_bytes(self) -> bytes:
        """Serialize once; every thread sends these identical bytes (§8.5)."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


@dataclass
class BidResult:
    thread_id: str
    role: str
    wave_index: int
    scheduled_offset_ms: float
    actual_offset_ms: Optional[float]
    endpoint: str
    status: Optional[int]
    is_valid: Optional[bool]
    bid_id: Optional[int]
    error_type: Optional[str]
    error_message: Optional[str]
    error_message_locale: Optional[str]
    min_bid: Optional[str]
    ranking: Optional[int]
    t_send_mono_ns: int
    t_recv_mono_ns: Optional[int]
    rtt_ms: Optional[float]
    failure: Failure = Failure.UNKNOWN
    corr_id: str = ""
    raw: Optional[Dict[str, Any]] = None
    exception: Optional[str] = None


@dataclass(frozen=True)
class Wave:
    index: int
    offset_ms: float
    direct: int
    validate: int
    validate_only: int

    @property
    def submissions(self) -> int:
        return self.direct + self.validate


@dataclass
class ClockSample:
    date_header: Optional[str]
    t_send_mono_ns: int
    t_recv_mono_ns: int
    t_send_wall: datetime
    t_recv_wall: datetime
    endpoint: str = ""


@dataclass(frozen=True)
class ClockEstimate:
    """Marzullo result. offset_ms is the MIDPOINT; width_ms is the honest error bar."""

    source: str
    lo_ms: Optional[float]
    hi_ms: Optional[float]
    offset_ms: Optional[float]
    width_ms: Optional[float]
    samples: int
    empty_intersection: bool = False


__all__ = [
    "AuctionRef",
    "ItemMeta",
    "AuctionDetail",
    "BidItem",
    "BidPayload",
    "BidResult",
    "Wave",
    "ClockSample",
    "ClockEstimate",
]
