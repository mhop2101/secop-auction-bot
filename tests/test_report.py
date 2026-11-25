"""Run summary tests (plan §12.3; the schema is published in ``docs/logging.md``).

Offline by construction: every outcome is assembled from hand-built :class:`BidResult`s and
a :class:`FireOutcome`, so no thread fires and no socket opens. The one test that writes to
disk points ``run_root`` at ``tmp_path``.

The interesting assertions are the ones about what a *successful* run is allowed to contain.
A ``BID_PLACED`` summary here carries ``DUPLICATE`` and ``STATE_NOT_STARTED`` in its
taxonomy on purpose: those are the designed products of a spread schedule, and a report that
downgraded the run because of them would be describing the tool working as a malfunction.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import pytest

from conftest import AUCTION_ID, INITIAL_VALUE, ITEM_ID, LOT_INTERNAL_ID, QUANTITY_STR
from secop_firstbid import payload as payload_mod
from secop_firstbid import report
from secop_firstbid.bidder import ENDPOINT_CREATE, ENDPOINT_VALIDATE, FireOutcome
from secop_firstbid.config import Config, audit_footprint, redacted_dict
from secop_firstbid.detail import build_detail
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.gate import GateVerdict
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionRef, BidResult, ClockEstimate, ItemMeta
from secop_firstbid.report import build_summary, human_summary, write_summary
from secop_firstbid.schedule import arm, build_waves, shift_t

# Long enough that config._redact_jwt uses first6…last6 rather than dropping it wholesale.
TOKEN = (
    "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
    "eyJjb21wYW55Q29kZSI6IlNVUEVSU0VDUkVUIiwiZXhwIjoxOTAwMDAwMDAwfQ."
    "c2lnbmF0dXJlLWJ5dGVzLXRoYXQtbXVzdC1uZXZlci1iZS1sb2dnZWQ"
)

FIRE_TIME = "2026-07-28T21:00:00Z"
ARMED_AT = datetime(2026, 7, 28, 20, 59, 0, tzinfo=timezone.utc)
ARMED_MONO_NS = 1000000000000
STARTED_AT = datetime(2026, 7, 28, 20, 58, 30, tzinfo=timezone.utc)
ENDED_AT = datetime(2026, 7, 28, 21, 0, 1, tzinfo=timezone.utc)

# docs/logging.md, "summary.json". Every key that document shows, and no substitutions.
DOCUMENTED_KEYS = (
    "run_id",
    "auction_id",
    "auction_version",
    "fire_time",
    "start_date",
    "fire_time_vs_start_date_ms",
    "clock",
    "gate",
    "mode",
    "schedule_planned",
    "waves_planned",
    "waves_fired",
    "waves_dropped",
    "submissions_planned",
    "submissions_sent",
    "audited_requests",
    "outcome",
    "winner",
    "bid_value",
    "items_count",
    "total_value",
    "my_last_bid",
    "last_bid",
    "secop_record",
    "attempts",
    "failure_taxonomy",
    "unverified_routes",
    "config_redacted",
    "started_at",
    "ended_at",
)

DOCUMENTED_WINNER_KEYS = (
    "thread",
    "role",
    "wave",
    "scheduled_offset_ms",
    "actual_offset_ms",
    "bid_id",
    "server_date",
    "rtt_ms",
)


def make_cfg(**overrides: Any) -> Config:
    kwargs = dict(
        jwt=TOKEN,
        auction_page_html_path="beforebid.html",
        bid_value="1",
        fire_time=FIRE_TIME,
        mode="burst",
        bid_count=4,
        pre_ms=60,
        interval_ms=30,
        wave_direct=1,
        confirm_live=True,
    )  # type: Dict[str, Any]
    kwargs.update(overrides)
    return Config(**kwargs)


def make_armed(cfg: Config):
    return arm(cfg, build_waves(cfg), now_wall=ARMED_AT, now_mono_ns=ARMED_MONO_NS)


def make_result(
    *,
    thread_id: str = "w00-D00",
    role: str = "D",
    wave_index: int = 0,
    endpoint: str = ENDPOINT_CREATE,
    scheduled_offset_ms: float = 0.0,
    actual_offset_ms: Optional[float] = 0.0,
    status: Optional[int] = 200,
    is_valid: Optional[bool] = False,
    bid_id: Optional[int] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    min_bid: Optional[str] = None,
    rtt_ms: Optional[float] = 180.0,
    failure: Failure = Failure.UNKNOWN,
    corr_id: str = "001-abcdef",
    raw: Optional[Dict[str, Any]] = None,
    exception: Optional[str] = None
) -> BidResult:
    return BidResult(
        thread_id=thread_id,
        role=role,
        wave_index=wave_index,
        scheduled_offset_ms=scheduled_offset_ms,
        actual_offset_ms=actual_offset_ms,
        endpoint=endpoint,
        status=status,
        is_valid=is_valid,
        bid_id=bid_id,
        error_type=error_type,
        error_message=error_message,
        error_message_locale=None,
        min_bid=min_bid,
        ranking=None,
        t_send_mono_ns=ARMED_MONO_NS,
        t_recv_mono_ns=ARMED_MONO_NS + 180000000,
        rtt_ms=rtt_ms,
        failure=failure,
        corr_id=corr_id,
        raw=raw,
        exception=exception,
    )


def winning_result() -> BidResult:
    return make_result(
        thread_id="w02-D00",
        wave_index=2,
        is_valid=True,
        bid_id=12345,
        failure=Failure.OK,
        raw={"isValid": True, "bidId": 12345, "date": "2026-07-28T21:00:00.006Z"},
    )


def rejected_result(**overrides: Any) -> BidResult:
    kwargs = dict(
        thread_id="w00-D00",
        wave_index=0,
        scheduled_offset_ms=-60.0,
        actual_offset_ms=-59.2,
        is_valid=False,
        error_type="BidErrMin",
        error_message="El valor debe ser inferior a su último lance",
        min_bid="7.813.974,32 COP",
        failure=Failure.BID_ERR_MIN,
    )  # type: Dict[str, Any]
    kwargs.update(overrides)
    return make_result(**kwargs)


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id=AUCTION_ID, source_extractor="onclick")


@pytest.fixture
def detail(auction_simple: Dict[str, Any], bids_total_info: Dict[str, Any]):
    """The real ``GetAuctionSimple`` plus one item, so a payload can be built from it."""
    base = build_detail(auction_simple, None, bids_total_info)
    item = ItemMeta(
        item_id=ITEM_ID,
        quantity=Decimal(QUANTITY_STR),
        item_unit="UN",
        item_internal_reference="1",
        lot_internal_id=LOT_INTERNAL_ID,
        lot_name="",
        initial_value=INITIAL_VALUE,
        quantity_str=QUANTITY_STR,
    )
    return dataclasses.replace(base, items=[item])


@pytest.fixture
def bid_payload(ref: AuctionRef, detail):
    built, _report = payload_mod.build_payload(ref, detail, Decimal("1"))
    return built


@pytest.fixture
def gate_verdict() -> GateVerdict:
    return GateVerdict(
        mode="once_before",
        issued_offset_ms=-1500.0,
        state="ReadyToStart",
        exec_state="Running",
        rtt_ms=121.0,
        proceed=True,
        reason="the expected pre-open reading",
        failure=None,
    )


def summarize(cfg: Config, ref: AuctionRef, **overrides: Any) -> Dict[str, Any]:
    kwargs = dict(
        run_id="1a2b3c4d",
        ref=ref,
        armed=make_armed(cfg),
        started_at=STARTED_AT,
        ended_at=ENDED_AT,
    )  # type: Dict[str, Any]
    kwargs.update(overrides)
    return build_summary(cfg, **kwargs)


def test_summary_carries_exactly_the_published_schema(
    ref: AuctionRef, detail, bid_payload, gate_verdict: GateVerdict
) -> None:
    cfg = make_cfg()
    summary = summarize(
        cfg,
        ref,
        detail=detail,
        payload=bid_payload,
        gate=gate_verdict,
        fire=FireOutcome([winning_result()], winning_result(), 4, 0, 4),
    )

    # "warnings" is the only key beyond docs/logging.md: human_summary() is handed the
    # summary and nothing else, so the warnings have to travel in it.
    assert set(summary) == set(DOCUMENTED_KEYS) | {"warnings"}
    assert set(summary["clock"]) == {
        "applied",
        "shift_ms",
        "date_header",
        "event_log",
        "signalr",
        "disagreement_ms",
        "host_ntp",
    }
    assert set(summary["clock"]["event_log"]) == {
        "offset_ms",
        "residual_width_ms",
        "candidates",
    }
    assert summary["clock"]["shift_ms"] == 0.0
    assert summary["secop_record"] is None
    assert set(summary["clock"]["date_header"]) == {
        "offset_ms",
        "residual_width_ms",
        "samples",
    }
    assert set(summary["clock"]["signalr"]) == {"offset_ms", "samples"}
    assert set(DOCUMENTED_WINNER_KEYS) == set(summary["winner"])
    assert set(DOCUMENTED_KEYS).issubset(set(summary))
    assert summary["auction_id"] == AUCTION_ID
    assert summary["auction_version"] == "AuctionbyProposal"
    assert summary["mode"] == "burst"
    assert summary["bid_value"] == "1"
    assert summary["items_count"] == 1
    assert summary["total_value"] == "1.000000"
    assert summary["my_last_bid"] == "0.0"
    assert summary["started_at"] == "2026-07-28T20:58:30+00:00"
    assert summary["ended_at"] == "2026-07-28T21:00:01+00:00"


def test_fire_time_is_compared_against_start_date_without_moving(
    ref: AuctionRef, detail
) -> None:
    cfg = make_cfg()
    summary = summarize(cfg, ref, detail=detail)

    assert summary["fire_time"] == "2026-07-28T21:00:00+00:00"
    assert summary["start_date"] == "2026-07-28T21:00:00+00:00"
    assert summary["fire_time_vs_start_date_ms"] == 0.0


def test_bid_placed_survives_duplicate_and_not_started_waves(
    ref: AuctionRef, gate_verdict: GateVerdict
) -> None:
    cfg = make_cfg()
    winner = winning_result()
    results = [
        rejected_result(failure=Failure.STATE_NOT_STARTED, error_type="AuctionNotStarted"),
        winner,
        make_result(
            thread_id="w03-D00",
            wave_index=3,
            scheduled_offset_ms=30.0,
            actual_offset_ms=31.4,
            failure=Failure.DUPLICATE,
            error_type="BidErrMin",
        ),
    ]
    summary = summarize(
        cfg,
        ref,
        gate=gate_verdict,
        fire=FireOutcome(results, winner, 4, 0, 3),
    )

    assert summary["outcome"] == "BID_PLACED"
    assert summary["failure_taxonomy"] == ["DUPLICATE", "STATE_NOT_STARTED"]
    assert summary["winner"]["thread"] == "w02-D00"
    assert summary["winner"]["bid_id"] == 12345
    assert summary["winner"]["actual_offset_ms"] == 0.0
    assert summary["winner"]["server_date"] == "2026-07-28T21:00:00.006Z"
    assert len(summary["attempts"]) == 3


def test_all_rejected_when_every_submission_bounced(ref: AuctionRef) -> None:
    cfg = make_cfg()
    results = [rejected_result(thread_id="w%02d-D00" % i, wave_index=i) for i in range(4)]
    summary = summarize(cfg, ref, fire=FireOutcome(results, None, 4, 0, 4))

    assert summary["outcome"] == "ALL_REJECTED"
    assert summary["winner"] is None
    assert summary["failure_taxonomy"] == ["BID_ERR_MIN"]
    assert summary["attempts"][0]["minBid"] == "7.813.974,32 COP"
    assert summary["attempts"][0]["errorType"] == "BidErrMin"


def test_submissions_that_were_never_answered_are_not_called_rejected(
    ref: AuctionRef,
) -> None:
    """A timeout is not a rejection: the request may have been processed after the wait.

    Every live run on 2026-11-20 ended this way, and each submission that had left the
    machine was processed by the server seconds later. ``ALL_REJECTED`` for such a run
    reports an answer that never arrived.
    """
    cfg = make_cfg()
    results = [
        make_result(
            thread_id="w%02d-D00" % i,
            wave_index=i,
            status=None,
            is_valid=None,
            rtt_ms=10009.0,
            failure=Failure.NETWORK,
            exception="ReadTimeout: read timed out",
        )
        for i in range(4)
    ]
    summary = summarize(cfg, ref, fire=FireOutcome(results, None, 4, 0, 4))

    assert summary["outcome"] == "NO_VERDICT"
    assert summary["failure_taxonomy"] == ["NETWORK"]

    text = human_summary(summary)

    assert "NO_VERDICT" in text
    assert "none was answered" in text
    assert "no bid was placed" not in text


def test_one_answered_submission_makes_the_run_all_rejected(ref: AuctionRef) -> None:
    cfg = make_cfg()
    results = [
        rejected_result(),
        make_result(
            thread_id="w01-D00",
            wave_index=1,
            status=None,
            is_valid=None,
            failure=Failure.NETWORK,
            exception="ReadTimeout: read timed out",
        ),
    ]
    summary = summarize(cfg, ref, fire=FireOutcome(results, None, 4, 0, 2))

    assert summary["outcome"] == "ALL_REJECTED"


def test_an_answered_validate_does_not_stand_in_for_a_submission_verdict(
    ref: AuctionRef,
) -> None:
    """Only a ``CreateBid`` answer says what happened to a bid."""
    cfg = make_cfg()
    results = [
        make_result(endpoint="ValidateBid", status=200, failure=Failure.BID_ERR_MIN),
        make_result(status=None, is_valid=None, failure=Failure.NETWORK),
    ]
    summary = summarize(cfg, ref, fire=FireOutcome(results, None, 4, 0, 1))

    assert summary["outcome"] == "NO_VERDICT"


def test_a_valid_body_with_a_warning_is_named_in_the_taxonomy_and_the_digest(
    ref: AuctionRef,
) -> None:
    """The run that placed no bid on an ``isValid:true`` answer has to say why.

    ``ALL_REJECTED`` beside an empty taxonomy is a record that contradicts itself: the
    summary and the digest both have to carry the reason the winner latch stayed empty.
    """
    cfg = make_cfg()
    warned = make_result(
        is_valid=True,
        error_message="Su lance supera el presupuesto oficial",
        failure=Failure.VALIDATION_FAILED,
    )
    summary = summarize(cfg, ref, fire=FireOutcome([warned], None, 4, 0, 1))

    assert summary["outcome"] == "ALL_REJECTED"
    assert summary["failure_taxonomy"] == ["VALIDATION_FAILED"]
    assert summary["attempts"][0]["isValid"] is True
    assert summary["attempts"][0]["errorMessage"] == "Su lance supera el presupuesto oficial"

    text = human_summary(summary)

    assert "no bid was placed" in text
    assert "warned" in text
    assert "isValid:true" in text
    assert "VALIDATION_FAILED" in text
    assert "Su lance supera el presupuesto oficial" in text


def test_the_digest_stays_quiet_when_no_attempt_carried_a_warning(ref: AuctionRef) -> None:
    cfg = make_cfg()
    summary = summarize(
        cfg, ref, fire=FireOutcome([rejected_result()], None, 4, 0, 4)
    )

    assert "warned" not in human_summary(summary)


def test_not_fired_when_nothing_was_submitted(ref: AuctionRef) -> None:
    """A dry run sends ValidateBid and still placed no bid: NOT_FIRED, not ALL_REJECTED."""
    cfg = make_cfg(dry_run=True, wave_direct=0, wave_validate=1, confirm_live=False)
    validated = make_result(
        thread_id="w00-V00",
        role="V",
        endpoint=ENDPOINT_VALIDATE,
        failure=Failure.BID_ERR_MIN,
        error_type="BidErrMin",
    )
    summary = summarize(cfg, ref, fire=FireOutcome([validated], None, 4, 0, 0))

    assert summary["outcome"] == "NOT_FIRED"
    assert summary["submissions_sent"] == 0
    assert summary["attempts"][0]["endpoint"] == "ValidateBid"


def test_not_fired_when_the_gate_stopped_the_schedule(ref: AuctionRef) -> None:
    cfg = make_cfg()
    blocked = GateVerdict(
        mode="once_before",
        issued_offset_ms=-1500.0,
        state="Finished",
        exec_state="Running",
        rtt_ms=135.3,
        proceed=False,
        reason="the auction accepts no further bid",
        failure=Failure.STATE_FINISHED,
    )
    summary = summarize(
        cfg, ref, gate=blocked, fire=FireOutcome([], None, 0, 4, 0)
    )

    assert summary["outcome"] == "NOT_FIRED"
    assert summary["waves_fired"] == 0
    assert summary["waves_dropped"] == 4
    assert summary["gate"]["state"] == "Finished"
    assert summary["gate"]["proceed"] is False
    assert summary["failure_taxonomy"] == ["STATE_FINISHED"]


def test_error_outcome_keeps_the_failure_code(ref: AuctionRef) -> None:
    cfg = make_cfg()
    error = SecopError(Failure.AUTH_EXPIRED, "the bearer token is dead")
    summary = summarize(cfg, ref, error=error)

    assert summary["outcome"] == "ERROR"
    assert summary["failure_taxonomy"] == ["AUTH_EXPIRED"]
    assert summary["error"]["failure"] == "AUTH_EXPIRED"
    assert summary["error"]["type"] == "SecopError"


def test_no_fire_outcome_at_all_is_not_fired(ref: AuctionRef) -> None:
    summary = summarize(make_cfg(), ref)

    assert summary["outcome"] == "NOT_FIRED"
    assert summary["attempts"] == []
    assert summary["submissions_sent"] == 0


def test_planned_and_sent_submissions_are_distinct_fields(ref: AuctionRef) -> None:
    """The gap between them is the only honest measure of what stop_on_success saved."""
    cfg = make_cfg()
    waves = build_waves(cfg)
    winner = winning_result()
    summary = summarize(cfg, ref, fire=FireOutcome([winner], winner, 3, 1, 3))

    assert summary["submissions_planned"] == 4
    assert summary["submissions_sent"] == 3
    assert summary["submissions_planned"] != summary["submissions_sent"]
    assert summary["submissions_planned"] == audit_footprint(cfg, waves)["submissions"]
    assert summary["waves_dropped"] == 1


def test_audited_requests_counts_probes_and_the_runs_one_start_audit(ref: AuctionRef) -> None:
    cfg = make_cfg(wave_direct=1, wave_validate_only=1, do_start_audit=True)
    waves = build_waves(cfg)
    summary = summarize(cfg, ref, waves=waves)

    # 4 waves x (1 direct + 1 probe), and one start-bidding audit for the run.
    assert summary["audited_requests"] == 9
    assert summary["audited_requests"] == audit_footprint(cfg, waves)["audited_requests"]
    assert summary["submissions_planned"] == 4


def test_schedule_planned_lists_every_wave_in_order(ref: AuctionRef) -> None:
    cfg = make_cfg()
    summary = summarize(cfg, ref)

    assert [wave["offset_ms"] for wave in summary["schedule_planned"]] == [
        -60.0,
        -30.0,
        0.0,
        30.0,
    ]
    assert summary["schedule_planned"][0] == {
        "wave": 0,
        "offset_ms": -60.0,
        "direct": 1,
        "validate": 0,
        "validate_only": 0,
    }
    assert summary["waves_planned"] == 4


def test_unverified_routes_lists_create_bid(ref: AuctionRef) -> None:
    summary = summarize(make_cfg(), ref)

    assert "bid/CreateBid" in summary["unverified_routes"]
    assert "auction/GetAuction" in summary["unverified_routes"]
    assert "bid/ValidateBid" not in summary["unverified_routes"]
    assert "auction/GetAuctionSimple" not in summary["unverified_routes"]


def test_unverified_routes_omits_create_bid_on_a_dry_run(ref: AuctionRef) -> None:
    cfg = make_cfg(dry_run=True, wave_validate=1, wave_direct=0, confirm_live=False)
    summary = summarize(cfg, ref)

    assert "bid/CreateBid" not in summary["unverified_routes"]
    assert "audit/SetSubmitBiddingAudit" not in summary["unverified_routes"]


def test_unverified_routes_names_the_submit_audit_when_enabled(ref: AuctionRef) -> None:
    summary = summarize(make_cfg(do_submit_audit=True), ref)

    assert "audit/SetSubmitBiddingAudit" in summary["unverified_routes"]


def test_config_redacted_never_carries_the_raw_token(
    ref: AuctionRef, detail, bid_payload, gate_verdict: GateVerdict
) -> None:
    cfg = make_cfg()
    summary = summarize(
        cfg,
        ref,
        detail=detail,
        payload=bid_payload,
        gate=gate_verdict,
        fire=FireOutcome([winning_result()], winning_result(), 4, 0, 4),
        warnings=["dry_run is NOT footprint-free"],
    )

    redacted = summary["config_redacted"]
    assert redacted == redacted_dict(cfg)
    assert redacted["jwt"] != TOKEN
    assert TOKEN not in json.dumps(redacted)
    assert TOKEN not in json.dumps(summary, default=str)
    # The middle of the token is the part that authenticates; only the ends may survive.
    assert TOKEN[20:80] not in json.dumps(summary, default=str)
    assert redacted["bid_value"] == "1"


def test_clock_block_reports_the_residual_width_and_is_not_applied(
    ref: AuctionRef,
) -> None:
    cfg = make_cfg()
    estimate = ClockEstimate(
        source="date_header",
        lo_ms=-23.0,
        hi_ms=441.0,
        offset_ms=209.0,
        width_ms=464.0,
        samples=12,
    )
    summary = summarize(cfg, ref, clock_estimate=estimate)

    assert summary["clock"]["applied"] is False
    assert summary["clock"]["date_header"] == {
        "offset_ms": 209.0,
        "residual_width_ms": 464.0,
        "samples": 12,
    }
    assert summary["clock"]["signalr"] == {"offset_ms": None, "samples": 0}
    assert summary["clock"]["disagreement_ms"] is None


def test_clock_block_reports_an_empty_intersection_without_a_point_estimate(
    ref: AuctionRef,
) -> None:
    estimate = ClockEstimate(
        source="date_header",
        lo_ms=800.0,
        hi_ms=-100.0,
        offset_ms=None,
        width_ms=None,
        samples=128,
        empty_intersection=True,
    )
    summary = summarize(make_cfg(), ref, clock_estimate=estimate)

    assert summary["clock"]["date_header"]["offset_ms"] is None
    assert summary["clock"]["date_header"]["residual_width_ms"] is None
    assert summary["clock"]["date_header"]["samples"] == 128


def test_clock_applied_follows_the_caller_not_the_config(ref: AuctionRef) -> None:
    cfg = make_cfg(apply_clock_offset=True)

    assert summarize(cfg, ref)["clock"]["applied"] is True
    assert summarize(cfg, ref, clock_applied=False)["clock"]["applied"] is False


def test_write_summary_writes_the_file_and_logs_the_outcome(
    tmp_path, ref: AuctionRef
) -> None:
    cfg = make_cfg(run_root=str(tmp_path))
    winner = winning_result()
    summary = summarize(cfg, ref, fire=FireOutcome([winner], winner, 4, 0, 4))

    logger = RunLogger(cfg, run_id="deadbeef")
    try:
        path = write_summary(logger, summary)
    finally:
        logger.close()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "summary.json"
    assert written["outcome"] == "BID_PLACED"
    assert written["submissions_planned"] == 4
    assert TOKEN not in path.read_text(encoding="utf-8")

    events = [
        json.loads(line)
        for line in (logger.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summaries = [record for record in events if record["event"] == "summary"]
    assert len(summaries) == 1
    assert summaries[0]["detail"]["outcome"] == "BID_PLACED"
    assert summaries[0]["stage"] == "report"


def test_human_summary_reads_at_a_glance(
    ref: AuctionRef, detail, bid_payload, gate_verdict: GateVerdict
) -> None:
    cfg = make_cfg()
    winner = winning_result()
    results = [
        rejected_result(failure=Failure.STATE_NOT_STARTED),
        winner,
        make_result(thread_id="w03-D00", wave_index=3, failure=Failure.DUPLICATE),
    ]
    summary = summarize(
        cfg,
        ref,
        detail=detail,
        payload=bid_payload,
        gate=gate_verdict,
        fire=FireOutcome(results, winner, 4, 0, 3),
        clock_estimate=ClockEstimate(
            source="date_header",
            lo_ms=-23.0,
            hi_ms=441.0,
            offset_ms=209.0,
            width_ms=464.0,
            samples=12,
        ),
        warnings=["apply_clock_offset is True", "rtt_samples=3 is below 10"],
    )

    text = human_summary(summary)

    assert "BID_PLACED" in text
    assert "w02-D00" in text
    assert "+0.0 ms" in text
    assert "bidId 12345" in text
    assert "3 of 4 submissions sent" in text
    assert "5 audited requests" in text
    assert "bid/CreateBid" in text
    assert "DUPLICATE" in text
    # The residual width travels with the midpoint; an offset printed alone reads as a
    # measurement rather than as the middle of a 464 ms-wide feasible interval.
    assert "464.0 ms" in text
    assert "not applied to T" in text
    assert "apply_clock_offset is True" in text
    assert "rtt_samples=3 is below 10" in text
    assert TOKEN not in text


def test_a_build_report_warning_reaches_both_the_summary_and_the_digest(
    ref: AuctionRef, detail
) -> None:
    """The CLI hands ``build_payload``'s warnings straight to :func:`build_summary`.

    They are the only record of what was uncertain about the payload once stdout is gone,
    so neither the record nor the digest may summarise them away.
    """
    cfg = make_cfg(bid_value="7500000")
    _built, build_report = payload_mod.build_payload(ref, detail, Decimal("7500000"))

    assert build_report.warnings

    summary = summarize(cfg, ref, detail=detail, warnings=build_report.warnings)
    text = human_summary(summary)

    assert summary["warnings"] == list(build_report.warnings)
    for warning in build_report.warnings:
        assert warning in text


SECOP_RECORD = {
    "read_at": "2026-11-20T21:00:50+00:00",
    "reference": "2026-11-20T21:00:00+00:00",
    "reference_source": "startDate",
    "company_code": "738484443",
    "my_last_bid": "0.0",
    "last_bid": "0.0",
    "bids": [
        {
            "bid_id": 1,
            "company": "900000002",
            "own": False,
            "date": "2026-11-20T21:00:00.0443673Z",
            "after_start_ms": 44.367,
            "total_value": "0.000000000000",
            "is_valid": True,
        }
    ],
    "own_bid_rows": {
        "count": 14,
        "first": "2026-11-20T21:00:11.580000+00:00",
        "first_after_start_ms": 11580.0,
        "last": "2026-11-20T21:00:35.280000+00:00",
        "last_after_start_ms": 35280.0,
        "first_description": "Error cuando presentado el lance.",
    },
    "own_start_rows": 1,
    "auction_start_row_after_start_ms": 159.0,
    "event_log_rows": 61,
    "event_log_truncated": False,
    "unread": [],
}


def test_the_digest_says_what_secop_recorded_when_nothing_was_answered(
    ref: AuctionRef,
) -> None:
    """The run that heard nothing still reports who won and when its bids were handled.

    These are the figures of the second auction of 2026-11-20: another company's zero was
    accepted 44 ms after the opening and this company's bids were processed 11 to 35
    seconds after it, none of which the run itself could see.
    """
    cfg = make_cfg()
    results = [
        make_result(status=None, is_valid=None, failure=Failure.NETWORK) for _ in range(2)
    ]
    summary = summarize(
        cfg,
        ref,
        fire=FireOutcome(results, None, 4, 0, 2),
        secop_record=SECOP_RECORD,
        secop_clock=ClockEstimate(
            source="event_log",
            lo_ms=-960.0,
            hi_ms=-930.0,
            offset_ms=-945.0,
            width_ms=30.0,
            samples=1,
        ),
    )

    assert summary["outcome"] == "NO_VERDICT"
    assert summary["secop_record"]["bids"][0]["company"] == "900000002"
    assert summary["clock"]["event_log"] == {
        "offset_ms": -945.0,
        "residual_width_ms": 30.0,
        "candidates": 1,
    }

    text = human_summary(summary)

    assert "SECOP says" in text
    assert "by company 900000002 (not yours)" in text
    assert "+44.4 ms from startDate" in text
    assert "14 of your company's bids were processed" in text
    assert "+11.58 s" in text and "+35.28 s" in text
    assert "SECOP clock 945.0 ms behind this host" in text
    assert "T not moved" in text


def test_the_digest_marks_an_accepted_bid_of_the_company_as_its_own(
    ref: AuctionRef,
) -> None:
    record = dict(SECOP_RECORD)
    record["bids"] = [dict(SECOP_RECORD["bids"][0], company="738484443", own=True)]
    summary = summarize(make_cfg(), ref, secop_record=record)

    assert "(YOURS)" in human_summary(summary)


def test_a_record_that_could_not_be_read_says_which_part(ref: AuctionRef) -> None:
    record = dict(SECOP_RECORD, bids=[], unread=["GetSupplierBids"])
    summary = summarize(make_cfg(), ref, secop_record=record)

    text = human_summary(summary)

    assert "no accepted bid in the bid list" in text
    assert "could not read: GetSupplierBids" in text


def test_a_moved_t_is_reported_with_the_distance(ref: AuctionRef) -> None:
    cfg = make_cfg(apply_clock_offset=True)
    armed = shift_t(make_armed(cfg), 945.0)
    summary = summarize(
        cfg,
        ref,
        armed=armed,
        clock_applied=True,
        secop_clock=ClockEstimate(
            source="event_log",
            lo_ms=-960.0,
            hi_ms=-930.0,
            offset_ms=-945.0,
            width_ms=30.0,
            samples=1,
        ),
    )

    assert summary["clock"]["applied"] is True
    assert summary["clock"]["shift_ms"] == 945.0
    assert "T moved +945.0 ms onto it" in human_summary(summary)


def test_no_secop_lines_when_nothing_was_read() -> None:
    text = human_summary({"outcome": "NOT_FIRED", "run_id": "1a2b3c4d"})

    assert "SECOP says" not in text
    assert "SECOP clock" not in text


def test_human_summary_without_a_winner_says_so(ref: AuctionRef) -> None:
    cfg = make_cfg()
    results = [
        rejected_result(actual_offset_ms=-59.2),
        rejected_result(thread_id="w03-D00", wave_index=3, actual_offset_ms=31.4),
    ]
    summary = summarize(cfg, ref, fire=FireOutcome(results, None, 4, 0, 2))

    text = human_summary(summary)

    assert "ALL_REJECTED" in text
    assert "no bid was placed" in text
    assert "-59.2 ms" in text
    assert "+31.4 ms" in text
    assert "warnings    none" in text


def test_human_summary_tolerates_a_bare_summary() -> None:
    """A run that died before anything was measured still prints something readable."""
    text = human_summary({"outcome": "ERROR", "run_id": "1a2b3c4d"})

    assert "ERROR" in text
    assert "nothing was sent" in text


def test_outcome_names_are_the_documented_five() -> None:
    assert set(report.OUTCOMES) == {
        "BID_PLACED",
        "ALL_REJECTED",
        "NO_VERDICT",
        "NOT_FIRED",
        "ERROR",
    }


def test_attempts_keep_the_servers_own_field_names(ref: AuctionRef) -> None:
    summary = summarize(
        make_cfg(), ref, fire=FireOutcome([rejected_result()], None, 4, 0, 4)
    )
    attempt = summary["attempts"][0]

    for key in (
        "thread",
        "role",
        "wave",
        "scheduled_offset_ms",
        "actual_offset_ms",
        "status",
        "isValid",
        "errorType",
        "errorMessage",
        "minBid",
        "rtt_ms",
    ):
        assert key in attempt
    assert attempt["failure"] == "BID_ERR_MIN"
    assert attempt["corr_id"] == "001-abcdef"


def test_summary_is_json_serializable_without_a_default_hook(
    ref: AuctionRef, detail, bid_payload, gate_verdict: GateVerdict
) -> None:
    cfg = make_cfg()
    winner = winning_result()
    summary = summarize(
        cfg,
        ref,
        detail=detail,
        payload=bid_payload,
        gate=gate_verdict,
        fire=FireOutcome([winner], winner, 4, 0, 4),
        warnings=["one warning"],
    )

    # No ``default=`` hook: a Decimal or a datetime left in the record would raise here.
    restored = json.loads(json.dumps(summary))

    assert restored["total_value"] == "1.000000"
    assert restored["fire_time"] == "2026-07-28T21:00:00+00:00"
    assert restored["winner"]["bid_id"] == 12345


def test_the_digest_says_when_none_of_this_hosts_bids_was_processed(ref: AuctionRef) -> None:
    record = dict(
        SECOP_RECORD,
        host_address="203.0.113.7",
        host_bid_rows={"count": 0},
        other_address_bid_rows=47,
    )
    text = human_summary(summarize(make_cfg(), ref, secop_record=record))

    assert "NONE of this host's bids was processed" in text
    assert "47 bid(s) of your company came from other addresses" in text
    assert "of your company's bids were processed" not in text


def test_the_digest_counts_this_hosts_processed_bids(ref: AuctionRef) -> None:
    record = dict(
        SECOP_RECORD,
        host_address="203.0.113.7",
        host_bid_rows={"count": 2, "first_after_start_ms": 3900.0, "last_after_start_ms": 4100.0},
        other_address_bid_rows=0,
    )
    text = human_summary(summarize(make_cfg(), ref, secop_record=record))

    assert "2 bid(s) from this host were processed, the first +3.90 s" in text
