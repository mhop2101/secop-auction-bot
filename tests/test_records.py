"""SECOP's own record, read back: the stamp parser, the clock placement, the result.

Two fixtures here are the server's own answers from the second live auction of 2026-11-20,
fetched after it closed: ``geteventlogs_20261120.json`` (the one page the log held) and
``getsupplierbids_20261120.json``. Every timestamp, description and count in them is as the
server sent it. Two kinds of value were replaced, and nothing else: the company codes
(``900000001`` is the company that ran this app, ``900000002`` the one whose bid was
accepted) and the two source addresses.

What they pin is what that auction showed: another company's zero accepted 44 ms after
``startDate``, this company's bids processed 11 to 36 seconds after it, and a start audit
per thread leaving so many rows in the same seconds that none could be told from another.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import pytest

from conftest import FIXTURES
from secop_firstbid import records
from secop_firstbid.config import Config
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionRef
from secop_firstbid.schedule import Armed
from secop_firstbid.session import HttpOutcome

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    + ("VGhpc0lzTm90QVJlYWxUb2tlblBheWxvYWRf" * 20)
    + ".c2lnbmF0dXJlLWJ5dGVzLWdvLWhlcmVfLTAxMjM0NTY3ODk"
)

OWN_COMPANY = "900000001"
OTHER_COMPANY = "900000002"
START_DATE = datetime(2026, 11, 20, 21, 0, 0, tzinfo=timezone.utc)

ANCHOR_WALL = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
ANCHOR_MONO_NS = 5000000000000


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="0",
        fire_time="2026-11-20T21:00:00Z",
        run_root=str(tmp_path / "runs"),
    )


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id="730d8416-15fa-46f3-8af0-1713fe9a97b7")


@pytest.fixture
def logger(cfg: Config) -> Iterator[RunLogger]:
    run_logger = RunLogger(cfg)
    yield run_logger
    run_logger.close()


@pytest.fixture
def event_log() -> Dict[str, Any]:
    return json.loads(
        (FIXTURES / "geteventlogs_20261120.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def supplier_bids() -> List[Dict[str, Any]]:
    return json.loads(
        (FIXTURES / "getsupplierbids_20261120.json").read_text(encoding="utf-8")
    )


def anchor() -> Armed:
    return Armed(
        t_wall=START_DATE,
        t_mono_ns=ANCHOR_MONO_NS + 60 * 1000000000,
        armed_at_wall=ANCHOR_WALL,
        armed_at_mono_ns=ANCHOR_MONO_NS,
        waves=[],
    )


def mono_at(wall: datetime) -> int:
    return ANCHOR_MONO_NS + int((wall - ANCHOR_WALL).total_seconds() * 1e9)


def answered(sent: datetime, rtt_ms: float, status: Optional[int] = 200) -> HttpOutcome:
    t_send = mono_at(sent)
    return HttpOutcome(
        status=status,
        body=None,
        text="",
        headers={},
        t_send_mono_ns=t_send,
        t_recv_mono_ns=t_send + int(rtt_ms * 1e6),
        rtt_ms=rtt_ms,
        corr_id="audit",
    )


class ScriptedClient:
    """Answers each route from a table and records the paths asked for."""

    def __init__(self, answers: Dict[str, Any]) -> None:
        self.answers = answers
        self.paths = []  # type: List[str]

    def get(self, path: str, **kwargs: Any) -> HttpOutcome:
        self.paths.append(path)
        stamp = time.monotonic_ns()
        for marker, answer in self.answers.items():
            if marker in path:
                if isinstance(answer, Exception):
                    return HttpOutcome(
                        status=None,
                        body=None,
                        text=None,
                        headers={},
                        t_send_mono_ns=stamp,
                        t_recv_mono_ns=stamp,
                        rtt_ms=0.0,
                        exception=answer,
                    )
                status, body = answer
                return HttpOutcome(
                    status=status,
                    body=body,
                    text="",
                    headers={},
                    t_send_mono_ns=stamp,
                    t_recv_mono_ns=stamp,
                    rtt_ms=0.0,
                )
        raise AssertionError("unexpected route: %s" % path)


def start_row(stamp: datetime, company: str = OWN_COMPANY) -> Dict[str, Any]:
    return {
        "eventHistoryTypeCode": "Logs_Search_Bid",
        "eventHistorySubTypeCode": records.SUBTYPE_START_BIDDING,
        "registerDate": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "3Z",
        "description": "Comenzar Lances",
        "ipAddress": "203.0.113.7",
        "userCode": company,
    }


def log_body(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"draw": 1, "recordsTotal": len(rows), "recordsFiltered": len(rows), "data": rows}


def no_sleep(_seconds: float) -> None:
    return None


def secop_clock_events(logger: RunLogger) -> List[Dict[str, Any]]:
    logger.flush()
    lines = (logger.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [
        record
        for record in (json.loads(line) for line in lines if line.strip())
        if record["event"] == "secop_clock"
    ]


# ------------------------------------------------------------------------ parse_instant


def test_a_seven_digit_fraction_is_cut_to_microseconds_not_refused() -> None:
    """The log writes one more digit than ``datetime`` holds or 3.9 will parse."""
    assert records.parse_instant("2026-11-20T21:00:00.0443673Z") == datetime(
        2026, 11, 20, 21, 0, 0, 44367, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-11-20T21:01:48.149204Z", datetime(2026, 11, 20, 21, 1, 48, 149204)),
        ("2026-11-20T21:00:00Z", datetime(2026, 11, 20, 21, 0, 0)),
        ("2026-11-20T21:00:00.5Z", datetime(2026, 11, 20, 21, 0, 0, 500000)),
        ("2026-11-20T16:00:00.25-05:00", datetime(2026, 11, 20, 21, 0, 0, 250000)),
    ],
)
def test_instants_are_read_to_utc(text: str, expected: datetime) -> None:
    assert records.parse_instant(text) == expected.replace(tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text",
    ["2026-11-20T21:00:00", "", "yesterday", "2026-13-40T21:00:00Z", None, 20261120],
)
def test_an_instant_with_no_zone_or_no_sense_is_refused(text: Any) -> None:
    """A naive stamp is not assumed to be UTC: that guess is a silent five-hour error."""
    assert records.parse_instant(text) is None


# ------------------------------------------------------------------------------ rows_of


def test_the_real_log_sorts_into_the_rows_the_audit_counted(event_log) -> None:
    rows = event_log["data"]

    bids = records.rows_of(rows, records.SUBTYPE_NEW_BID, OWN_COMPANY)
    starts = records.rows_of(rows, records.SUBTYPE_START_BIDDING, OWN_COMPANY)

    assert len(bids) == 25
    assert len(starts) == 18
    assert [stamp for stamp, _row in bids] == sorted(stamp for stamp, _row in bids)
    assert records.rows_of(rows, records.SUBTYPE_NEW_BID, OTHER_COMPANY) == []


def test_the_system_row_has_no_company_and_is_found_without_one(event_log) -> None:
    opened = records.rows_of(event_log["data"], records.SUBTYPE_AUCTION_START)

    assert len(opened) == 1
    assert opened[0][1]["userCode"] == ""
    assert records.rows_of(event_log["data"], records.SUBTYPE_AUCTION_START, OWN_COMPANY) == []


def test_a_row_with_an_unreadable_stamp_is_passed_over() -> None:
    rows = [start_row(START_DATE), dict(start_row(START_DATE), registerDate="soon")]

    assert len(records.rows_of(rows, records.SUBTYPE_START_BIDDING, OWN_COMPANY)) == 1


# --------------------------------------------------------------- clock_from_start_audit


def test_one_answered_audit_places_secops_clock_to_within_its_round_trip(
    cfg, ref, logger
) -> None:
    """The stamp sits between send and receive, so the interval is the round trip.

    SECOP's clock is 945 ms behind here: a row written mid-flight carries a time 945 ms
    earlier than this host's clock read at that moment.
    """
    sent = datetime(2026, 11, 20, 20, 59, 0, 100000, tzinfo=timezone.utc)
    outcome = answered(sent, rtt_ms=40.0)
    stamp = sent + timedelta(milliseconds=20) - timedelta(milliseconds=945)
    client = ScriptedClient({"GetEventLogs": (200, log_body([start_row(stamp)]))})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, outcome, OWN_COMPANY, logger, sleep=no_sleep
    )

    assert estimate.samples == 1
    assert estimate.source == "event_log"
    assert estimate.lo_ms == pytest.approx(-965.0, abs=0.01)
    assert estimate.hi_ms == pytest.approx(-925.0, abs=0.01)
    assert estimate.offset_ms == pytest.approx(-945.0, abs=0.01)
    assert estimate.width_ms == pytest.approx(40.0, abs=0.01)
    assert len(client.paths) == 1

    logged = secop_clock_events(logger)
    assert len(logged) == 1
    assert logged[0]["detail"]["usable"] is True
    assert logged[0]["detail"]["candidates"] == 1


def test_another_companys_row_is_never_a_candidate(cfg, ref, logger) -> None:
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    outcome = answered(sent, rtt_ms=40.0)
    theirs = start_row(sent - timedelta(milliseconds=300), company=OTHER_COMPANY)
    ours = start_row(sent - timedelta(milliseconds=925))
    client = ScriptedClient({"GetEventLogs": (200, log_body([theirs, ours]))})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, outcome, OWN_COMPANY, logger, sleep=no_sleep
    )

    assert estimate.samples == 1
    assert estimate.offset_ms == pytest.approx(-945.0, abs=0.01)


def test_two_rows_that_could_be_ours_widen_the_interval_to_cover_both(
    cfg, ref, logger
) -> None:
    """Two accounts of one company start their audits together: one row is ours, the
    other is not, and nothing says which. The interval has to contain both readings."""
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    outcome = answered(sent, rtt_ms=40.0)
    first = start_row(sent - timedelta(milliseconds=925))
    second = start_row(sent - timedelta(milliseconds=725))
    client = ScriptedClient({"GetEventLogs": (200, log_body([first, second]))})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, outcome, OWN_COMPANY, logger, sleep=no_sleep
    )

    assert estimate.samples == 2
    assert estimate.source == "event_log;candidates=2"
    assert estimate.lo_ms == pytest.approx(-965.0, abs=0.01)
    assert estimate.hi_ms == pytest.approx(-725.0, abs=0.01)
    assert estimate.width_ms == pytest.approx(240.0, abs=0.01)


def test_a_start_audit_per_thread_made_the_real_log_unreadable_as_a_clock(
    cfg, ref, logger, event_log
) -> None:
    """The second auction of 2026-11-20, as the server recorded it.

    Fourteen threads on one host and fourteen on another each sent their own start audit
    in the same few seconds. Against one real answered exchange — sent 20:59:54.300468,
    answered 165.9 ms later — fifteen of the company's rows fall inside the window, and
    the interval that covers them all is three seconds wide. The estimate has to come out
    that wide, because the truth is that this log could not place the clock for a single
    request; it is one audit per run that makes the row identifiable.
    """
    sent = datetime(2026, 11, 20, 20, 59, 54, 300468, tzinfo=timezone.utc)
    outcome = answered(sent, rtt_ms=165.929)
    client = ScriptedClient({"GetEventLogs": (200, event_log)})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, outcome, OWN_COMPANY, logger, sleep=no_sleep
    )

    assert estimate.samples == 15
    assert estimate.width_ms is not None and estimate.width_ms > 3000.0
    assert estimate.width_ms > cfg.clock_max_width_ms
    # The reading the audit arrived at by other means, 875 to 1000 ms behind, is inside it.
    assert estimate.lo_ms < -1000.0 and estimate.hi_ms > -875.0


@pytest.mark.parametrize("status", [None, 500, 401])
def test_an_audit_that_was_not_answered_places_nothing(
    cfg, ref, logger, status: Optional[int]
) -> None:
    """A request that timed out may be stamped after the wait ended; its bracket is false."""
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    client = ScriptedClient({"GetEventLogs": (200, log_body([start_row(sent)]))})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, answered(sent, 40.0, status=status), OWN_COMPANY,
        logger, sleep=no_sleep,
    )

    assert estimate.offset_ms is None
    assert estimate.samples == 0
    assert client.paths == []
    assert secop_clock_events(logger)[0]["detail"]["usable"] is False


def test_a_token_with_no_company_places_nothing(cfg, ref, logger) -> None:
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    client = ScriptedClient({"GetEventLogs": (200, log_body([start_row(sent)]))})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, answered(sent, 40.0), None, logger, sleep=no_sleep
    )

    assert estimate.offset_ms is None
    assert client.paths == []


def test_a_row_outside_the_window_is_not_ours_and_the_log_is_read_twice_then_left(
    cfg, ref, logger
) -> None:
    """Two reads, not a loop: a row that has not appeared by then is not waited for."""
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    stale = start_row(sent - timedelta(milliseconds=cfg.clock_window_ms + 1000))
    client = ScriptedClient({"GetEventLogs": (200, log_body([stale]))})
    waits = []  # type: List[float]

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, answered(sent, 40.0), OWN_COMPANY, logger,
        sleep=waits.append,
    )

    assert estimate.offset_ms is None
    assert len(client.paths) == 2
    assert waits == list(records._READBACK_WAITS_S)
    assert "no start-bidding row" in secop_clock_events(logger)[0]["detail"]["reason"]


def test_a_log_that_cannot_be_read_places_nothing_and_does_not_raise(
    cfg, ref, logger
) -> None:
    sent = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    client = ScriptedClient({"GetEventLogs": ConnectionError("refused")})

    estimate = records.clock_from_start_audit(
        cfg, anchor(), ref, client, answered(sent, 40.0), OWN_COMPANY, logger,
        sleep=no_sleep,
    )

    assert estimate.offset_ms is None


# -------------------------------------------------------------------------- read_result


def test_the_real_record_says_who_won_and_when_this_companys_bids_were_handled(
    ref, logger, event_log, supplier_bids
) -> None:
    """The second auction of 2026-11-20, from the server's own answers.

    Every submission of that run timed out, so the run itself could report nothing. The
    record shows the rest: another company's zero accepted 44 ms after the opening, and
    25 of this company's bids processed between 11.6 and 36.8 seconds after it.
    """
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetSupplierBids": (200, supplier_bids),
            "GetEventLogs": (200, event_log),
        }
    )

    record = records.read_result(ref, client, OWN_COMPANY, START_DATE, logger)

    assert record["unread"] == []
    assert record["reference_source"] == "startDate"

    assert len(record["bids"]) == 1
    accepted = record["bids"][0]
    assert accepted["company"] == OTHER_COMPANY
    assert accepted["own"] is False
    assert accepted["is_valid"] is True
    assert accepted["total_value"] == "0.000000000000"
    assert accepted["date"] == "2026-11-20T21:00:00.0443673Z"
    assert accepted["after_start_ms"] == pytest.approx(44.367, abs=0.001)

    own = record["own_bid_rows"]
    assert own["count"] == 25
    assert own["first_after_start_ms"] == pytest.approx(11580.0, abs=100.0)
    assert own["last_after_start_ms"] == pytest.approx(36768.0, abs=100.0)
    assert own["first_description"].startswith("Error cuando presentado el lance")

    assert record["own_start_rows"] == 18
    # The "auction started" row was written after the accepted bid, so it is not the gate.
    assert record["auction_start_row_after_start_ms"] == pytest.approx(159.0, abs=5.0)
    assert record["auction_start_row_after_start_ms"] > accepted["after_start_ms"]

    assert record["event_log_rows"] == 62
    assert record["event_log_truncated"] is False


def test_an_accepted_bid_of_this_company_is_marked_as_its_own(
    ref, logger, supplier_bids
) -> None:
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetSupplierBids": (200, supplier_bids),
            "GetEventLogs": (200, log_body([])),
        }
    )

    record = records.read_result(ref, client, OTHER_COMPANY, START_DATE, logger)

    assert record["bids"][0]["own"] is True


def test_each_read_that_fails_is_named_and_nothing_raises(ref, logger) -> None:
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": ConnectionError("refused"),
            "GetSupplierBids": (503, None),
            "GetEventLogs": (200, "<html>blocked</html>"),
        }
    )

    record = records.read_result(ref, client, OWN_COMPANY, START_DATE, logger)

    assert record["unread"] == ["GetBidsTotalInfo", "GetSupplierBids", "GetEventLogs"]
    assert record["bids"] == []
    assert record["own_bid_rows"]["count"] == 0
    assert record["my_last_bid"] is None


def test_without_a_company_code_the_bid_list_is_not_asked_for(ref, logger) -> None:
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetEventLogs": (200, log_body([])),
        }
    )

    record = records.read_result(ref, client, None, START_DATE, logger)

    assert not any("GetSupplierBids" in path for path in client.paths)
    assert any("GetSupplierBids" in name for name in record["unread"])


def test_a_log_with_more_rows_than_the_page_says_it_was_truncated(ref, logger) -> None:
    body = log_body([start_row(START_DATE)])
    body["recordsTotal"] = 250
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetSupplierBids": (200, []),
            "GetEventLogs": (200, body),
        }
    )

    record = records.read_result(ref, client, OWN_COMPANY, START_DATE, logger)

    assert record["event_log_truncated"] is True


def test_a_naive_reference_gives_no_offsets_rather_than_guessed_ones(
    ref, logger, supplier_bids
) -> None:
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetSupplierBids": (200, supplier_bids),
            "GetEventLogs": (200, log_body([])),
        }
    )

    record = records.read_result(
        ref, client, OWN_COMPANY, datetime(2026, 11, 20, 21, 0, 0), logger
    )

    assert record["bids"][0]["after_start_ms"] is None


def test_every_read_is_a_get_on_a_route_this_module_names(ref, logger) -> None:
    """Nothing here writes a row: three reads, no POST."""
    client = ScriptedClient(
        {
            "GetBidsTotalInfo": (200, {"myLastBid": 0, "lastBid": 0}),
            "GetSupplierBids": (200, []),
            "GetEventLogs": (200, log_body([])),
        }
    )

    records.read_result(ref, client, OWN_COMPANY, START_DATE, logger)

    assert len(client.paths) == 3
    assert not hasattr(client, "post_json") and not hasattr(client, "post_empty")
