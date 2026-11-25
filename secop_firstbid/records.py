"""SECOP's own record of an auction, read back: the event log and the bid list.

Two things in a run can only be learned from what the server wrote down, and both were
learned the hard way on 2026-11-20.

**Which clock the opening happens on.** The application timestamps every row of its event
log, to a tenth of a microsecond, with a clock that is not the one behind the HTTP ``Date``
header. The run's own start audit writes such a row, and the instants at which that
request left and was answered bracket the moment the row was stamped — which bounds the
difference between SECOP's clock and this host's to the width of one round trip.
:func:`clock_from_start_audit` reads the row back and does that arithmetic. It costs no
extra row: the audit is sent anyway.

**What happened to the bids.** A submission that times out has not been rejected; it may
be processed long after this process stopped waiting. Every submission of four live runs
ended that way, and the server's log shows each one that had left the machine processed
between 4 and 35 seconds after the opening. :func:`read_result` fetches the bid list and
the company's own rows so the run's summary can say what the server did, rather than what
the socket did.

Everything here is a ``GET``. Nothing in this module writes a row, and a failure in it
never raises: a record that could not be read is reported as unread.

The row shapes are taken from the event log and bid list of the two auctions of
2026-11-20 and nothing else. A field this code has not seen is passed over, not guessed.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import clock, endpoints, numbers
from .config import Config
from .logging_setup import RunLogger
from .models import AuctionRef, ClockEstimate
from .schedule import Armed
from .session import HttpClient, HttpOutcome

STAGE = "records"
THREAD_ID = "records"
ROLE = "records"

SUBTYPE_START_BIDDING = "Logs_Search_StartBidding"
SUBTYPE_NEW_BID = "Logs_Search_NewBid"
SUBTYPE_AUCTION_START = "Logs_Search_AuctionStart"

EVENT_LOG_PAGE = 100

# The whole log is read page by page up to this many pages. The third live auction held
# 110 rows, so one page of 100 lost the oldest ten.
EVENT_LOG_MAX_PAGES = 10

# The row is written while the audit request is handled, but nothing says the log read
# sees it at once. One read shortly after the answer, one more a little later, then stop.
_READBACK_WAITS_S = (0.3, 2.0)

_DESCRIPTION_LIMIT = 240

_INSTANT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:\d{2})$"
)


def parse_instant(text: Any) -> Optional[datetime]:
    """A SECOP timestamp as a tz-aware UTC datetime, or ``None``.

    The log writes seven fractional digits (``2026-11-20T21:00:00.0443673Z``), one more
    than ``datetime`` holds and more than Python 3.9's ``fromisoformat`` accepts, so the
    fraction is cut to microseconds by hand. A value with no zone is refused rather than
    assumed to be UTC.
    """
    if not isinstance(text, str):
        return None
    match = _INSTANT.match(text.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micros = int((fraction or "0")[:6].ljust(6, "0"))
    if zone in ("Z", "z"):
        offset = timedelta(0)
    else:
        sign = -1 if zone[0] == "-" else 1
        offset = sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    try:
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micros
        )
    except ValueError:
        return None
    return (local - offset).replace(tzinfo=timezone.utc)


def read_event_log(
    client: HttpClient,
    ref: AuctionRef,
    *,
    length: int = EVENT_LOG_PAGE,
    thread_id: str = THREAD_ID
) -> Tuple[List[Dict[str, Any]], HttpOutcome]:
    """The newest ``length`` rows of the auction's event log, newest first.

    One page, one request. A supplier's token is shown its own company's rows and the
    system's; that was the whole of what the log returned on 2026-11-20.
    """
    outcome = client.get(
        endpoints.get_event_logs(ref.auction_id, ref.lang, 0, length),
        stage=STAGE,
        thread_id=thread_id,
        role=ROLE,
    )
    rows = []  # type: List[Dict[str, Any]]
    if outcome.status == 200 and isinstance(outcome.body, Mapping):
        data = outcome.body.get("data")
        if isinstance(data, list):
            rows = [dict(row) for row in data if isinstance(row, Mapping)]
    return (rows, outcome)


def read_event_log_all(
    client: HttpClient,
    ref: AuctionRef,
    *,
    max_pages: int = EVENT_LOG_MAX_PAGES,
    thread_id: str = THREAD_ID
) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    """Every row of the event log, page by page: ``(rows, records_total, complete)``.

    ``complete`` is false when a page failed or ``max_pages`` ran out before
    ``recordsTotal``; the rows read so far are still returned. ``records_total`` is
    ``None`` when not even the first page was answered.
    """
    rows = []  # type: List[Dict[str, Any]]
    total = None  # type: Optional[int]
    for page in range(max(1, max_pages)):
        outcome = client.get(
            endpoints.get_event_logs(
                ref.auction_id, ref.lang, page * EVENT_LOG_PAGE, EVENT_LOG_PAGE
            ),
            stage=STAGE,
            thread_id=thread_id,
            role=ROLE,
        )
        if outcome.status != 200 or not isinstance(outcome.body, Mapping):
            return (rows, total, False)
        reported = outcome.body.get("recordsTotal")
        if isinstance(reported, int) and not isinstance(reported, bool):
            total = reported
        data = outcome.body.get("data")
        page_rows = (
            [dict(row) for row in data if isinstance(row, Mapping)]
            if isinstance(data, list)
            else []
        )
        rows.extend(page_rows)
        if not page_rows or total is None or len(rows) >= total:
            return (rows, total, True)
    return (rows, total, total is not None and len(rows) >= total)


def rows_of(
    rows: List[Dict[str, Any]],
    subtype: str,
    company_code: Optional[str] = None,
    address: Optional[str] = None,
    exclude_address: Optional[str] = None,
) -> List[Tuple[datetime, Dict[str, Any]]]:
    """Rows of one subtype — one company's when ``company_code`` is given — oldest first.

    ``userCode`` on a row is the *company* code from the token, not a user: every account
    of one company writes rows that cannot be told apart by it. ``ipAddress`` can: it is
    the public address the request came from, so ``address`` keeps only this host's rows
    and ``exclude_address`` only everyone else's — a person bidding by hand under the same
    company, for one.
    """
    out = []  # type: List[Tuple[datetime, Dict[str, Any]]]
    for row in rows:
        if row.get("eventHistorySubTypeCode") != subtype:
            continue
        if company_code is not None and str(row.get("userCode") or "") != company_code:
            continue
        row_address = str(row.get("ipAddress") or "")
        if address is not None and row_address != address:
            continue
        if exclude_address is not None and row_address == exclude_address:
            continue
        stamp = parse_instant(row.get("registerDate"))
        if stamp is None:
            continue
        out.append((stamp, row))
    out.sort(key=lambda pair: pair[0])
    return out


def _estimate_detail(estimate: ClockEstimate) -> Dict[str, Any]:
    return {
        "source": estimate.source,
        "candidates": estimate.samples,
        "lo_ms": estimate.lo_ms,
        "hi_ms": estimate.hi_ms,
        "offset_ms": estimate.offset_ms,
        "width_ms": estimate.width_ms,
    }


def clock_from_start_audit(
    cfg: Config,
    armed: Armed,
    ref: AuctionRef,
    client: HttpClient,
    outcome: HttpOutcome,
    company_code: Optional[str],
    logger: RunLogger,
    sleep: Any = time.sleep,
    seen: Optional[Dict[str, Any]] = None,
) -> ClockEstimate:
    """Place SECOP's log clock against this host's, from the start audit just sent.

    ``offset = SECOP clock - host clock``: negative when SECOP's clock is behind. The
    interval is the audit's own round trip, so it is narrow when the server is answering
    promptly and useless when it is not, and it says which.

    Candidates are this company's start-bidding rows stamped within ``clock_window_ms``
    of the exchange. More than one — another account of the same company starting its own
    audit in the same seconds — widens the interval to cover them all; see
    :func:`~secop_firstbid.clock.stamp_estimate`. Stagger ``start_audit_lead_ms`` between
    such runs by more than twice the window and each sees only its own row.

    No estimate, and the reason logged, when the audit was not answered with a 200 (a
    timed-out request does not bracket its stamp), when the token names no company, or
    when no row turns up in two reads.

    ``seen``, when given, receives ``address``: the public address SECOP recorded on the
    row, but only when exactly one candidate was found — the row is then certainly this
    run's, and its address is how :func:`read_result` later tells this host's bids from a
    person's bidding by hand under the same company.
    """
    empty = clock.stamp_estimate([], armed.armed_at_wall, armed.armed_at_wall)

    def unusable(reason: str) -> ClockEstimate:
        logger.event(
            stage=STAGE,
            event="secop_clock",
            level="WARNING",
            thread_id=THREAD_ID,
            detail=dict(_estimate_detail(empty), usable=False, reason=reason),
        )
        return empty

    if outcome.status != 200 or outcome.t_recv_mono_ns is None:
        return unusable(
            "the start audit was not answered with a 200, so its send and receive "
            "instants do not bracket the moment its row was stamped"
        )
    if not company_code:
        return unusable(
            "the token names no company code, so this run's row cannot be told from "
            "anyone else's"
        )

    t_send_wall = armed.wall_at(outcome.t_send_mono_ns)
    t_recv_wall = armed.wall_at(outcome.t_recv_mono_ns)
    window = timedelta(milliseconds=cfg.clock_window_ms)

    matched = []  # type: List[Tuple[datetime, Dict[str, Any]]]
    reads = 0
    for wait_s in _READBACK_WAITS_S:
        sleep(wait_s)
        rows, _exchange = read_event_log(client, ref, thread_id="clock-readback")
        reads += 1
        matched = [
            (stamp, row)
            for stamp, row in rows_of(rows, SUBTYPE_START_BIDDING, company_code)
            if t_send_wall - window <= stamp <= t_recv_wall + window
        ]
        if matched:
            break
    candidates = [stamp for stamp, _row in matched]
    if seen is not None and len(matched) == 1:
        address = matched[0][1].get("ipAddress")
        if isinstance(address, str) and address:
            seen["address"] = address

    if not candidates:
        return unusable(
            "no start-bidding row of company %s within %d ms of the audit in %d read(s) "
            "of the event log" % (company_code, cfg.clock_window_ms, reads)
        )

    estimate = clock.stamp_estimate(candidates, t_send_wall, t_recv_wall)
    logger.event(
        stage=STAGE,
        event="secop_clock",
        thread_id=THREAD_ID,
        detail=dict(
            _estimate_detail(estimate),
            usable=True,
            audit_rtt_ms=outcome.rtt_ms,
            audit_sent_at=t_send_wall.isoformat(),
            stamps=[stamp.isoformat() for stamp in candidates],
            reads=reads,
            note="offset is SECOP's log clock minus this host's clock; negative means "
            "SECOP's clock is behind, so an instant on it is reached later here",
        ),
    )
    return estimate


def _ms_after(stamp: Optional[datetime], reference: Optional[datetime]) -> Optional[float]:
    if stamp is None or reference is None:
        return None
    if reference.tzinfo is None or reference.utcoffset() is None:
        return None
    return (stamp - reference).total_seconds() * 1000.0


def _amount(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return numbers.to_wire(value)
    if isinstance(value, int):
        return str(value)
    return None


def _bid_row(
    bid: Mapping[str, Any], company_code: Optional[str], reference: Optional[datetime]
) -> Dict[str, Any]:
    company = bid.get("company")
    company = None if company is None else str(company)
    stamp = parse_instant(bid.get("date"))
    total = bid.get("totalValueStr")
    return {
        "bid_id": bid.get("bidId"),
        "company": company,
        "own": bool(company_code) and company == company_code,
        "date": bid.get("date") if isinstance(bid.get("date"), str) else None,
        "after_start_ms": _ms_after(stamp, reference),
        "total_value": total if isinstance(total, str) else _amount(bid.get("totalValue")),
        "is_valid": bid.get("isValid") if isinstance(bid.get("isValid"), bool) else None,
    }


def _own_rows_block(
    rows: List[Dict[str, Any]],
    subtype: str,
    company_code: Optional[str],
    reference: Optional[datetime],
) -> Dict[str, Any]:
    return _rows_block(rows_of(rows, subtype, company_code) if company_code else [], reference)


def _rows_block(
    own: List[Tuple[datetime, Dict[str, Any]]], reference: Optional[datetime]
) -> Dict[str, Any]:
    block = {
        "count": len(own),
        "first": None,
        "first_after_start_ms": None,
        "last": None,
        "last_after_start_ms": None,
        "first_description": None,
    }  # type: Dict[str, Any]
    if own:
        first, first_row = own[0]
        last, _last_row = own[-1]
        description = first_row.get("description")
        block.update(
            first=first.isoformat(),
            first_after_start_ms=_ms_after(first, reference),
            last=last.isoformat(),
            last_after_start_ms=_ms_after(last, reference),
            first_description=description[:_DESCRIPTION_LIMIT]
            if isinstance(description, str)
            else None,
        )
    return block


def read_result(
    ref: AuctionRef,
    client: HttpClient,
    company_code: Optional[str],
    reference: Optional[datetime],
    logger: RunLogger,
    reference_source: str = "startDate",
    host_address: Optional[str] = None,
) -> Dict[str, Any]:
    """What SECOP recorded about the auction and about this company, after the bids.

    Three reads: the totals, the bid list, and the event log, page by page. ``reference`` is
    the instant the ``*_after_start_ms`` figures are measured from — the auction's
    ``startDate`` when it has a zone, otherwise ``T`` — and both it and every stamp are on
    SECOP's clock, so those figures need no clock correction.

    ``own_bid_rows`` are the company's ``Logs_Search_NewBid`` rows: one per bid the server
    processed, rejected ones included, each carrying the time it was processed. A bid that
    never reached the server leaves none. ``event_log_truncated`` says when not every row
    could be read.

    ``host_address`` is the public address SECOP recorded on this run's own start audit
    (see :func:`clock_from_start_audit`). With it, ``host_bid_rows`` counts only the bids
    that came from this host and ``other_address_bid_rows`` the company's bids from
    anywhere else. On 2026-11-23 that split was the whole story: 47 of the company's bids
    were processed, every one from a person's browser, and none of the 7 this host sent.
    Without it the two cannot be told apart and ``host_bid_rows`` is ``None``.

    Never raises. Each read that failed is named in ``unread`` and its block left empty.
    """
    record = OrderedDict()  # type: OrderedDict[str, Any]
    record["read_at"] = datetime.now(timezone.utc).isoformat()
    record["reference"] = None if reference is None else reference.isoformat()
    record["reference_source"] = reference_source
    record["company_code"] = company_code
    record["my_last_bid"] = None
    record["last_bid"] = None
    record["bids"] = []
    record["own_bid_rows"] = _own_rows_block([], SUBTYPE_NEW_BID, None, reference)
    record["host_address"] = host_address
    record["host_bid_rows"] = None
    record["other_address_bid_rows"] = None
    record["own_start_rows"] = 0
    record["auction_start_row_after_start_ms"] = None
    record["event_log_rows"] = 0
    record["event_log_truncated"] = False
    record["unread"] = []
    unread = record["unread"]  # type: List[str]

    totals = client.get(
        endpoints.get_bids_total_info(ref.auction_id),
        stage=STAGE,
        thread_id=THREAD_ID,
        role=ROLE,
    )
    if totals.status == 200 and isinstance(totals.body, Mapping):
        record["my_last_bid"] = _amount(totals.body.get("myLastBid"))
        record["last_bid"] = _amount(totals.body.get("lastBid"))
    else:
        unread.append("GetBidsTotalInfo")

    if company_code:
        bids = client.get(
            endpoints.get_supplier_bids(ref.auction_id, company_code),
            stage=STAGE,
            thread_id=THREAD_ID,
            role=ROLE,
        )
        if bids.status == 200 and isinstance(bids.body, list):
            record["bids"] = [
                _bid_row(bid, company_code, reference)
                for bid in bids.body
                if isinstance(bid, Mapping)
            ]
        else:
            unread.append("GetSupplierBids")
    else:
        unread.append("GetSupplierBids (the token names no company code)")

    rows, total, complete = read_event_log_all(client, ref)
    if total is not None:
        record["event_log_rows"] = len(rows)
        record["event_log_truncated"] = not complete
        record["own_bid_rows"] = _own_rows_block(
            rows, SUBTYPE_NEW_BID, company_code, reference
        )
        if host_address and company_code:
            record["host_bid_rows"] = _rows_block(
                rows_of(rows, SUBTYPE_NEW_BID, company_code, address=host_address),
                reference,
            )
            record["other_address_bid_rows"] = len(
                rows_of(rows, SUBTYPE_NEW_BID, company_code, exclude_address=host_address)
            )
        record["own_start_rows"] = (
            len(rows_of(rows, SUBTYPE_START_BIDDING, company_code)) if company_code else 0
        )
        opened = rows_of(rows, SUBTYPE_AUCTION_START)
        if opened:
            record["auction_start_row_after_start_ms"] = _ms_after(opened[0][0], reference)
    else:
        unread.append("GetEventLogs")

    logger.event(
        stage=STAGE,
        event="secop_record",
        level="WARNING" if unread else "INFO",
        thread_id=THREAD_ID,
        detail=dict(record),
    )
    return record


__all__ = [
    "EVENT_LOG_MAX_PAGES",
    "EVENT_LOG_PAGE",
    "STAGE",
    "SUBTYPE_AUCTION_START",
    "SUBTYPE_NEW_BID",
    "SUBTYPE_START_BIDDING",
    "clock_from_start_audit",
    "parse_instant",
    "read_event_log",
    "read_event_log_all",
    "read_result",
    "rows_of",
]
