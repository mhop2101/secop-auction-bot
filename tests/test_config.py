"""Tests for :mod:`secop_firstbid.config` (plan §4).

Offline by construction: nothing here reads the auction page, the HAR or the network, and
the fire-time window is exercised with an injected ``now`` so the suite does not drift into
``FIRE_TIME_PAST`` as the wall clock moves.

:func:`audit_footprint` is the one thing here that is not checked against itself. It is the
safety number the CLI banner and ``summary.json`` publish, so it is measured against the
requests a real :func:`~secop_firstbid.bidder.run_schedule` issues — real threads, real
release arithmetic, clients that count and discard instead of opening a socket.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import pytest

from secop_firstbid import schedule
from secop_firstbid.bidder import StartAudit, run_schedule
from secop_firstbid.config import (
    AUDIT_FOOTPRINT_WARN_THRESHOLD,
    Config,
    audit_footprint,
    keepalive_quiet_ms,
    load,
    redacted_dict,
    start_audit_planned,
    validate,
)
from secop_firstbid.endpoints import DEFAULT_USER_AGENT
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionDetail, AuctionRef, Wave
from secop_firstbid.session import HttpOutcome

NOW = datetime(2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc)
FIRE_TIME = "2026-07-28T21:10:00Z"

# Long enough that first6…last6 is a real redaction; shaped like the captured token.
JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + ("QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 8) + ".sIgNaTuRe"


def make_config(**kwargs: Any) -> Config:
    base = dict(
        jwt=JWT,
        auction_page_html_path="beforebid.html",
        bid_value="7813974.32",
        fire_time=FIRE_TIME,
    )  # type: Dict[str, Any]
    base.update(kwargs)
    return Config(**base)


def raises_config(**kwargs: Any) -> SecopError:
    with pytest.raises(SecopError) as excinfo:
        validate(make_config(**kwargs), now=NOW)
    return excinfo.value


# --------------------------------------------------------------------------- defaults


def test_defaults_match_section_4() -> None:
    cfg = make_config()
    assert cfg.auction_id is None
    assert cfg.bid_is_total is False
    assert cfg.n_direct == 1
    assert cfg.n_validate_submit == 0
    assert cfg.n_validate_only == 0
    assert cfg.v_gate_on_valid is True
    assert cfg.max_arm_ahead_s == 3600
    assert cfg.fire_time_mismatch_warn_s == 2
    assert cfg.apply_clock_offset is False
    assert cfg.clock_probe == "date_header"
    assert cfg.lead_ms == 0
    assert cfg.validate_lead_ms == 0
    assert cfg.gate_mode == "once_before"
    assert cfg.gate_lead_ms == 1500
    assert cfg.mode == "single"
    assert cfg.bid_count == 1
    assert cfg.pre_ms == 0
    assert cfg.interval_ms == 0
    assert cfg.wave_direct == 1
    assert cfg.wave_validate == 0
    assert cfg.wave_validate_only == 0
    assert cfg.schedule_offsets_ms == []
    assert cfg.stop_on_success is True
    assert cfg.do_start_audit is True
    assert cfg.dry_run is False
    assert cfg.connect_timeout_ms == 3000
    assert cfg.read_timeout_ms == 5000
    assert cfg.auth_safety_margin_s == 300
    assert cfg.user_agent == DEFAULT_USER_AGENT
    assert cfg.rtt_samples == 10
    assert cfg.run_root == "runs"
    assert cfg.confirm_live is False


def test_never_observed_endpoint_is_off_by_default() -> None:
    assert make_config().do_submit_audit is False


def test_confirm_live_is_off_by_default() -> None:
    assert make_config().confirm_live is False


def test_schedule_offsets_are_not_shared_between_instances() -> None:
    first = make_config()
    second = make_config()
    first.schedule_offsets_ms.append(-50)
    assert second.schedule_offsets_ms == []


def test_default_config_validates_clean() -> None:
    assert validate(make_config(), now=NOW) == []


# ----------------------------------------------------------------------- fire_time


def test_fire_time_missing_is_an_error() -> None:
    assert raises_config(fire_time="").code is Failure.CONFIG_INVALID


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-28T21:10:00",
        "2026-07-28 21:10:00",
        "2026-07-28",
        "21:10:00Z",
        "not a timestamp",
    ],
)
def test_naive_or_malformed_fire_time_is_an_error(value: str) -> None:
    error = raises_config(fire_time=value)
    assert error.code is Failure.CONFIG_INVALID
    assert "fire_time" in error.message


# Every offset spelling this app accepts, and the instant each one denotes. `validate` and
# `schedule.arm` must agree on all of them: a form one takes and the other refuses is a
# config that passes the pre-flight and then aborts at arm time, with the run already
# holding open connections.
ACCEPTED_FIRE_TIMES = [
    ("2026-07-28T21:10:00Z", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28t21:10:00z", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T21:10:00+00:00", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T21:10:00+0000", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T21:10:00+00", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T16:10:00-05:00", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T16:10:00-0500", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T16:10:00-05", datetime(2026, 7, 28, 21, 10, tzinfo=timezone.utc)),
    ("2026-07-28T21:10:00.500Z", datetime(2026, 7, 28, 21, 10, 0, 500000, tzinfo=timezone.utc)),
    (
        "2026-07-28T16:10:00.500-0500",
        datetime(2026, 7, 28, 21, 10, 0, 500000, tzinfo=timezone.utc),
    ),
]


@pytest.mark.parametrize("value,instant", ACCEPTED_FIRE_TIMES)
def test_timezone_explicit_fire_times_are_accepted(value: str, instant: datetime) -> None:
    assert validate(make_config(fire_time=value), now=NOW) == []


@pytest.mark.parametrize("value,instant", ACCEPTED_FIRE_TIMES)
def test_a_fire_time_that_validates_also_arms(value: str, instant: datetime) -> None:
    """One parser, so the accepted set cannot differ between the two entry points."""
    cfg = make_config(fire_time=value)
    assert validate(cfg, now=NOW) == []

    armed = schedule.arm(
        cfg, schedule.build_waves(cfg), now_wall=NOW, now_mono_ns=1000000000
    )
    assert armed.t_wall == instant


def test_fire_time_in_the_past() -> None:
    error = raises_config(fire_time="2026-07-28T20:59:59Z")
    assert error.code is Failure.FIRE_TIME_PAST


def test_fire_time_beyond_max_arm_ahead() -> None:
    error = raises_config(fire_time="2026-07-29T21:10:00Z")
    assert error.code is Failure.FIRE_TIME_TOO_FAR


def test_max_arm_ahead_can_be_widened() -> None:
    cfg = make_config(fire_time="2026-07-29T21:10:00Z", max_arm_ahead_s=90000)
    assert validate(cfg, now=NOW) == []


# --------------------------------------------------------------------------- errors


def test_bid_is_total_is_rejected() -> None:
    error = raises_config(bid_is_total=True)
    assert error.code is Failure.CONFIG_INVALID
    assert "bid_is_total" in error.message


# ------------------------------------------------------------------------- bid_value


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "7.813.974,32,00",
        "1.23.456",
        "1,",
        "-",
        "1e6",
    ],
)
def test_an_unparseable_bid_value_is_fatal(value: str) -> None:
    """``validate`` is the only place that declares a config fatal, so it has to parse it.

    Unparsed, each of these survives to the payload builder and arrives as a traceback with
    the run already armed and holding connections.
    """
    error = raises_config(bid_value=value)
    assert error.code is Failure.CONFIG_INVALID
    assert value in error.message


def test_the_ambiguous_bid_value_is_refused_here_not_downstream() -> None:
    error = raises_config(bid_value="1.234")
    assert error.code is Failure.CONFIG_INVALID
    assert "ambiguous" in error.message
    # Both readings are named, because the operator is the only one who can decide.
    assert "1234" in error.message


@pytest.mark.parametrize("value", ["-500", "-0.01", "-7.813.974,32", "-0", "-0.00"])
def test_a_negative_bid_value_is_fatal(value: str) -> None:
    """These parse, so nothing downstream stops them: they go on the wire verbatim."""
    error = raises_config(bid_value=value)
    assert error.code is Failure.CONFIG_INVALID
    assert "must not be below zero" in error.message


def test_a_negative_bid_value_names_the_string_it_would_send() -> None:
    error = raises_config(bid_value="-500")
    assert 'bidValue "-500"' in error.message


@pytest.mark.parametrize(
    "value, wire", [("0", "0"), ("0.0", "0.0"), ("0.00", "0.00"), ("0,00", "0.00")]
)
def test_a_zero_bid_value_is_legal_and_warned_about(value: str, wire: str) -> None:
    """The server accepted a zero in two live auctions; what is pinned here is local handling."""
    warnings = validate(make_config(bid_value=value), now=NOW)
    assert len(warnings) == 1
    assert "is ZERO" in warnings[0]
    assert 'bidValue "%s"' % wire in warnings[0]


def test_a_zero_bid_is_still_not_armed_by_the_config_alone() -> None:
    assert make_config(bid_value="0").confirm_live is False


def test_a_non_string_bid_value_is_fatal() -> None:
    error = raises_config(bid_value=7813974.32)
    assert error.code is Failure.CONFIG_INVALID
    assert "quoted string" in error.message


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "7813974.32",
        "7.813.974,32",
        "7.813.974,32 COP",
        "390.698.716",
        "1.500000",
        "0.01",
    ],
)
def test_every_accepted_bid_value_form_still_validates(value: str) -> None:
    """The parser's accepted set is not narrowed by being called from here."""
    assert validate(make_config(bid_value=value), now=NOW) == []


# --------------------------------------------------------------------- composition


def test_no_threads_at_all_is_an_error() -> None:
    error = raises_config(n_direct=0, n_validate_submit=0, n_validate_only=0)
    assert error.code is Failure.CONFIG_INVALID


def test_dry_run_with_no_submitters_is_legal() -> None:
    warnings = validate(
        make_config(n_direct=0, n_validate_submit=0, n_validate_only=1, dry_run=True),
        now=NOW,
    )
    assert any("dry_run" in w for w in warnings)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(n_direct=0, n_validate_submit=0, n_validate_only=0),
        dict(
            mode="burst",
            bid_count=3,
            interval_ms=30,
            wave_direct=0,
            wave_validate=0,
            wave_validate_only=0,
        ),
    ],
    ids=["single", "burst"],
)
def test_an_all_zero_composition_is_a_typo_even_under_dry_run(
    kwargs: Dict[str, Any]
) -> None:
    """An empty dry run is the case the guard exists to catch, not an exemption from it.

    The schedule still has waves and no thread to release in them, which is a shape no
    later stage has anything to do with.
    """
    error = raises_config(dry_run=True, **kwargs)
    assert error.code is Failure.CONFIG_INVALID
    assert "no threads configured" in error.message


def test_probe_only_run_is_legal() -> None:
    cfg = make_config(n_direct=0, n_validate_submit=0, n_validate_only=1)
    assert validate(cfg, now=NOW) == []


def test_burst_needs_at_least_one_wave() -> None:
    error = raises_config(mode="burst", bid_count=0, interval_ms=30)
    assert error.code is Failure.CONFIG_INVALID
    assert "bid_count" in error.message


def test_burst_without_interval_collapses_onto_T() -> None:
    error = raises_config(mode="burst", bid_count=4, pre_ms=60, interval_ms=0)
    assert error.code is Failure.CONFIG_INVALID
    assert "interval_ms" in error.message
    assert "collapse" in error.message


def test_single_wave_burst_does_not_need_an_interval() -> None:
    assert validate(make_config(mode="burst", bid_count=1, interval_ms=0), now=NOW) == []


def test_custom_mode_needs_offsets() -> None:
    error = raises_config(mode="custom", schedule_offsets_ms=[])
    assert error.code is Failure.CONFIG_INVALID
    assert "schedule_offsets_ms" in error.message


def test_custom_mode_with_offsets_is_legal() -> None:
    cfg = make_config(mode="custom", schedule_offsets_ms=[-60, -30, 0])
    assert validate(cfg, now=NOW) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"clock_probe": "ntp"},
        {"gate_mode": "per_wave"},
        {"mode": "spray"},
    ],
)
def test_enum_keys_reject_unknown_values(kwargs: Dict[str, Any]) -> None:
    assert raises_config(**kwargs).code is Failure.CONFIG_INVALID


@pytest.mark.parametrize("name", ["jwt", "auction_page_html_path", "bid_value", "run_root"])
def test_required_strings_must_be_non_empty(name: str) -> None:
    error = raises_config(**{name: "   "})
    assert error.code is Failure.CONFIG_INVALID
    assert name in error.message


@pytest.mark.parametrize(
    "kwargs", [{"connect_timeout_ms": 0}, {"read_timeout_ms": 0}, {"max_arm_ahead_s": 0}]
)
def test_timeouts_must_be_positive(kwargs: Dict[str, Any]) -> None:
    assert raises_config(**kwargs).code is Failure.CONFIG_INVALID


def test_negative_thread_counts_are_rejected() -> None:
    assert raises_config(n_direct=-1).code is Failure.CONFIG_INVALID


def test_all_errors_are_reported_together() -> None:
    error = raises_config(mode="spray", clock_probe="ntp", bid_is_total=True)
    assert len(error.detail["errors"]) == 3
    for fragment in ("mode", "clock_probe", "bid_is_total"):
        assert fragment in error.message


# ------------------------------------------------------------------------- warnings


def test_dry_run_warning_names_the_row_it_writes() -> None:
    warnings = validate(make_config(dry_run=True), now=NOW)
    text = " ".join(warnings)
    assert "NOT footprint-free" in text
    assert "Logs_Search_NewBid" in text
    assert "IP address" in text
    assert "company code" in text


def test_apply_clock_offset_warns() -> None:
    warnings = validate(make_config(apply_clock_offset=True), now=NOW)
    assert any("apply_clock_offset" in w for w in warnings)


def test_do_submit_audit_warns_that_the_route_was_never_observed() -> None:
    warnings = validate(make_config(do_submit_audit=True), now=NOW)
    assert any("never observed" in w for w in warnings)


def test_confirm_live_warns() -> None:
    warnings = validate(make_config(confirm_live=True), now=NOW)
    assert any("real CreateBid" in w for w in warnings)


def test_confirm_live_under_dry_run_says_it_is_inert() -> None:
    warnings = validate(make_config(confirm_live=True, dry_run=True), now=NOW)
    assert any("no CreateBid will be sent" in w for w in warnings)


def test_a_start_audit_inside_the_opening_rush_warns_that_it_may_never_be_written() -> None:
    """The audit cannot delay a bid any more, but a late one may simply fail.

    It has a connection of its own, so the old hazard — holding the socket the bid needs —
    is gone. What is left is that requests made in the seconds before an opening failed a
    third to half of the time on 2026-11-20.
    """
    warnings = validate(make_config(do_start_audit=True, start_audit_lead_ms=50), now=NOW)
    assert any("start_audit_lead_ms" in warning for warning in warnings)
    assert any("own connection" in warning for warning in warnings)
    assert not any("connect+TLS" in warning for warning in warnings)


def test_the_default_start_audit_lead_does_not_warn() -> None:
    assert not any("start_audit_lead_ms" in w for w in validate(make_config(), now=NOW))


def test_no_start_audit_lead_warning_when_the_audit_is_off() -> None:
    cfg = make_config(do_start_audit=False, start_audit_lead_ms=0)
    assert not any("start_audit_lead_ms" in warning for warning in validate(cfg, now=NOW))


def test_low_rtt_samples_warns() -> None:
    warnings = validate(make_config(rtt_samples=3), now=NOW)
    assert any("rtt_samples" in w for w in warnings)


def test_large_audit_footprint_warns() -> None:
    cfg = make_config(mode="burst", bid_count=10, interval_ms=30, wave_direct=1)
    warnings = validate(cfg, now=NOW)
    # 10 waves x 1 CreateBid + the run's one start audit = 11 > 10
    assert any("audit footprint is 11" in w for w in warnings)


def test_footprint_below_threshold_does_not_warn() -> None:
    cfg = make_config(mode="burst", bid_count=9, interval_ms=30, wave_direct=1)
    assert validate(cfg, now=NOW) == []
    assert audit_footprint(cfg, waves_for(cfg))["audited_requests"] == (
        AUDIT_FOOTPRINT_WARN_THRESHOLD
    )


# ------------------------------------------------------------------ audit_footprint


def waves_for(cfg: Config) -> List[Wave]:
    """Mirror of ``schedule.build_waves`` composition; offsets do not enter the metric."""
    return [
        Wave(i, float(i), cfg.wave_direct, cfg.wave_validate, cfg.wave_validate_only)
        for i in range(cfg.bid_count)
    ]


def waves_for_single(cfg: Config) -> List[Wave]:
    """``single`` mode reads the ``n_*`` keys, not the ``wave_*`` ones."""
    return [Wave(0, 0.0, cfg.n_direct, cfg.n_validate_submit, cfg.n_validate_only)]


def test_burst_of_four_direct_waves_is_five_audited_requests() -> None:
    """Four CreateBid and the run's one start audit — not one audit per thread."""
    cfg = make_config(
        mode="burst", bid_count=4, pre_ms=60, interval_ms=30, wave_direct=1,
        do_start_audit=True,
    )
    footprint = audit_footprint(cfg, waves_for(cfg))
    assert footprint == {"submissions": 4, "audited_requests": 5, "waves": 4}


def test_start_audit_off_drops_one_row() -> None:
    cfg = make_config(
        mode="burst", bid_count=4, interval_ms=30, wave_direct=1, do_start_audit=False
    )
    assert audit_footprint(cfg, waves_for(cfg))["audited_requests"] == 4


def test_validate_only_probes_are_audited_but_are_not_submissions() -> None:
    cfg = make_config(
        mode="burst",
        bid_count=2,
        interval_ms=30,
        wave_direct=1,
        wave_validate=1,
        wave_validate_only=1,
        do_start_audit=True,
    )
    footprint = audit_footprint(cfg, waves_for(cfg))
    assert footprint["submissions"] == 4
    # Per wave: D costs 1, V costs 2 (ValidateBid *and* CreateBid), P costs 1; then one
    # start audit for the run.
    assert footprint["audited_requests"] == 9
    assert footprint["waves"] == 2


def test_a_validate_thread_costs_two_requests_not_one() -> None:
    cfg = make_config(n_direct=0, n_validate_submit=1, do_start_audit=False)
    assert audit_footprint(cfg, waves_for_single(cfg))["audited_requests"] == 2
    audited = make_config(n_direct=0, n_validate_submit=1, do_start_audit=True)
    assert audit_footprint(audited, waves_for_single(audited))["audited_requests"] == 3


def test_the_submit_audit_is_counted() -> None:
    cfg = make_config(do_submit_audit=True, do_start_audit=False)
    assert audit_footprint(cfg, waves_for_single(cfg))["audited_requests"] == 2


def test_dry_run_drops_the_create_bid_terms_and_keeps_the_rest() -> None:
    """A D-only dry run costs nothing; a V or P dry run still writes its rows."""
    direct = make_config(mode="burst", bid_count=4, interval_ms=30, wave_direct=1, dry_run=True)
    assert audit_footprint(direct, waves_for(direct)) == {
        "submissions": 0,
        "audited_requests": 0,
        "waves": 4,
    }

    probing = make_config(
        mode="burst",
        bid_count=4,
        interval_ms=30,
        wave_direct=1,
        wave_validate=1,
        wave_validate_only=1,
        dry_run=True,
    )
    footprint = audit_footprint(probing, waves_for(probing))
    assert footprint["submissions"] == 0
    # 4 waves x (V + P) ValidateBid, and the run's one start audit.
    assert footprint["audited_requests"] == 9


def test_empty_schedule_has_no_footprint() -> None:
    assert audit_footprint(make_config(), [])["audited_requests"] == 0


# ------------------------------------ audit_footprint vs the requests a run really sends

AUCTION_ID = "d6a49a23-3556-4b3d-9bb4-3e209279b9f8"

# Far enough ahead that every thread is created, started and asleep before the first
# release; short enough that seventeen runs stay quick.
FIRE_LEAD_MS = 80

ACCEPTED_BID = {
    "bidId": 4417231,
    "currentRankingPosition": 1,
    "isValid": True,
    "errorMessage": "",
    "errorMessageLocale": None,
    "minBid": None,
    "errorType": None,
}

FIRE_DETAIL = AuctionDetail(
    version_code="AuctionbyProposal",
    type_code="Reverse",
    currency="COP",
    round_type="Single",
    decimal_num=2,
    total_num=1,
    minimum_bid_type=1,
    start_price=Decimal("390698716.0"),
    start_date=None,
    end_date=None,
    state_code="Started",
    exec_state_code="Running",
    allow_bids_below_best=False,
    test_auction=True,
    items=[],
)


class ProceedGate:
    """The ``gate_mode='off'`` verdict shape; :mod:`secop_firstbid.gate` owns the real one."""

    proceed = True
    reason = "gate disabled"
    state = None
    exec_state = None
    failure = None


class CountingClient:
    """Duck-typed ``HttpClient`` that records each request's path instead of sending it.

    Every bid endpoint answers with the accepted body, so no V thread self-aborts at its
    validate gate and the run realises the full planned footprint — which is the number
    :func:`audit_footprint` projects.
    """

    def __init__(self, calls: List[str], lock: "threading.Lock") -> None:
        self.calls = calls
        self.lock = lock

    def post_empty(self, path: str, **kwargs: Any) -> HttpOutcome:
        return self._record(path, None)

    def post_json(self, path: str, body_bytes: bytes, **kwargs: Any) -> HttpOutcome:
        return self._record(path, ACCEPTED_BID)

    def _record(self, path: str, body: Any) -> HttpOutcome:
        with self.lock:
            self.calls.append(path)
        stamp = time.monotonic_ns()
        return HttpOutcome(
            status=200,
            body=body,
            text="",
            headers={},
            t_send_mono_ns=stamp,
            t_recv_mono_ns=stamp,
            rtt_ms=0.0,
            corr_id="test",
        )

    def close(self) -> None:
        pass


def requests_sent(tmp_path: Any, **overrides: Any) -> Tuple[Config, List[Wave], List[str]]:
    """Fire the whole schedule for real and return every request path it issued."""
    values = dict(
        run_root=str(tmp_path / "runs"),
        connect_timeout_ms=200,
        read_timeout_ms=200,
        confirm_live=True,
        stop_on_success=False,
    )  # type: Dict[str, Any]
    values.update(overrides)

    now_wall = datetime.now(timezone.utc)
    values["fire_time"] = (
        (now_wall + timedelta(milliseconds=FIRE_LEAD_MS))
        .isoformat()
        .replace("+00:00", "Z")
    )
    cfg = make_config(**values)

    waves = schedule.build_waves(cfg)
    armed = schedule.arm(cfg, waves, now_wall=now_wall)

    calls = []  # type: List[str]
    lock = threading.Lock()
    threads = sum(w.direct + w.validate + w.validate_only for w in waves)
    clients = [CountingClient(calls, lock) for _ in range(max(threads, 1))]
    ref = AuctionRef(auction_id=AUCTION_ID)

    run_logger = RunLogger(cfg)
    try:
        # The start audit is issued outside the schedule, on a client of its own, exactly
        # as a real run does it; it is part of what the footprint projects.
        audit = StartAudit(cfg, armed, ref, CountingClient(calls, lock), run_logger)
        audit.start()
        run_schedule(
            cfg,
            armed,
            ref,
            FIRE_DETAIL,
            b"{}",
            clients,
            run_logger,
            ProceedGate(),
            start_audit=audit,
        )
        assert audit.join(timeout_s=5.0)
    finally:
        run_logger.close()
    return cfg, waves, calls


# Every role combination, both audit switches, both modes, live and dry.
ROLE_MATRIX = [
    ("D", dict(n_direct=1)),
    ("V", dict(n_direct=0, n_validate_submit=1)),
    ("P", dict(n_direct=0, n_validate_only=1)),
    ("DDVP", dict(n_direct=2, n_validate_submit=1, n_validate_only=1)),
    ("DVP-no-start-audit", dict(n_direct=1, n_validate_submit=1, n_validate_only=1,
                                do_start_audit=False)),
    ("V-submit-audit", dict(n_direct=0, n_validate_submit=1, do_submit_audit=True)),
    ("D-submit-audit-only", dict(do_submit_audit=True, do_start_audit=False)),
    ("burst-D", dict(mode="burst", bid_count=2, interval_ms=5, wave_direct=1)),
    ("burst-V", dict(mode="burst", bid_count=2, interval_ms=5, wave_direct=0,
                     wave_validate=1)),
    ("burst-DVP", dict(mode="burst", bid_count=3, interval_ms=5, wave_direct=1,
                       wave_validate=1, wave_validate_only=1)),
    ("custom-DV", dict(mode="custom", schedule_offsets_ms=[0, 5], wave_direct=1,
                       wave_validate=1)),
    ("dry-D", dict(n_direct=1, dry_run=True)),
    ("dry-burst-D", dict(mode="burst", bid_count=4, interval_ms=5, wave_direct=1,
                         dry_run=True)),
    ("dry-V", dict(n_direct=0, n_validate_submit=1, dry_run=True)),
    ("dry-P-no-start-audit", dict(n_direct=0, n_validate_only=1, dry_run=True,
                                  do_start_audit=False)),
    ("dry-DVP", dict(n_direct=1, n_validate_submit=1, n_validate_only=1, dry_run=True)),
    ("dry-burst-DV-submit-audit", dict(mode="burst", bid_count=2, interval_ms=5,
                                       wave_direct=1, wave_validate=1, dry_run=True,
                                       do_submit_audit=True)),
]


@pytest.mark.parametrize(
    "overrides", [case[1] for case in ROLE_MATRIX], ids=[case[0] for case in ROLE_MATRIX]
)
def test_projected_footprint_equals_the_requests_a_run_issues(
    tmp_path: Any, overrides: Dict[str, Any]
) -> None:
    """Counted per request, so a validate-then-submit thread is three, not one."""
    cfg, waves, calls = requests_sent(tmp_path, **overrides)
    footprint = audit_footprint(cfg, waves)

    assert footprint["audited_requests"] == len(calls)
    assert footprint["submissions"] == sum(1 for path in calls if "CreateBid" in path)
    assert footprint["waves"] == len(waves)


def test_a_dry_run_of_direct_threads_issues_nothing_at_all(tmp_path: Any) -> None:
    cfg, waves, calls = requests_sent(
        tmp_path, mode="burst", bid_count=4, interval_ms=5, wave_direct=1, dry_run=True
    )
    assert calls == []
    assert audit_footprint(cfg, waves)["audited_requests"] == 0


def test_a_dry_run_of_probes_still_issues_its_rows(tmp_path: Any) -> None:
    """``dry_run`` is not zero: it drops CreateBid and nothing else."""
    cfg, waves, calls = requests_sent(
        tmp_path, n_direct=0, n_validate_only=2, dry_run=True
    )
    assert sum(1 for path in calls if "ValidateBid" in path) == 2
    assert sum(1 for path in calls if "SetStartBiddingAudit" in path) == 1
    assert [path for path in calls if "CreateBid" in path] == []
    assert audit_footprint(cfg, waves)["audited_requests"] == 3


# ------------------------------------------------------------------------ redaction


def test_redacted_dict_shows_only_the_ends_of_the_token() -> None:
    redacted = redacted_dict(make_config())
    assert redacted["jwt"] == JWT[:6] + "…" + JWT[-6:]
    assert JWT not in json.dumps(redacted, ensure_ascii=False)
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ" not in json.dumps(redacted, ensure_ascii=False)


def test_redacted_dict_carries_every_config_key() -> None:
    cfg = make_config()
    names = {f.name for f in dataclasses.fields(cfg)}
    assert set(redacted_dict(cfg)) == names


def test_redacted_dict_copies_mutable_values() -> None:
    cfg = make_config(schedule_offsets_ms=[-30, 0])
    redacted_dict(cfg)["schedule_offsets_ms"].append(999)
    assert cfg.schedule_offsets_ms == [-30, 0]


def test_short_token_is_dropped_rather_than_half_shown() -> None:
    assert redacted_dict(make_config(jwt="short.token"))["jwt"] == "<redacted>"


def test_repr_does_not_leak_the_token() -> None:
    text = repr(make_config())
    assert JWT not in text
    assert "…" in text


# ----------------------------------------------------------------------------- load


def write_json(tmp_path: Any, data: Dict[str, Any], name: str = "cfg.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def minimal_doc() -> Dict[str, Any]:
    return {
        "jwt": JWT,
        "auction_page_html_path": "beforebid.html",
        "bid_value": "7813974.32",
        "fire_time": FIRE_TIME,
    }


def test_load_json(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc.update({"mode": "burst", "bid_count": 3, "interval_ms": 30,
                "schedule_offsets_ms": [-60, -30, 0], "dry_run": True})
    cfg = load(write_json(tmp_path, doc))
    assert cfg.mode == "burst"
    assert cfg.bid_count == 3
    assert cfg.schedule_offsets_ms == [-60, -30, 0]
    assert cfg.dry_run is True
    assert cfg.user_agent == DEFAULT_USER_AGENT


def test_load_rejects_missing_required_keys(tmp_path: Any) -> None:
    doc = minimal_doc()
    del doc["bid_value"]
    with pytest.raises(SecopError) as excinfo:
        load(write_json(tmp_path, doc))
    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert excinfo.value.detail["missing_keys"] == ["bid_value"]


def test_load_rejects_unknown_keys_with_a_suggestion(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc["bid_valu"] = "1"
    with pytest.raises(SecopError) as excinfo:
        load(write_json(tmp_path, doc))
    assert excinfo.value.detail["unknown_keys"] == ["bid_valu"]
    assert "bid_value" in excinfo.value.message


def test_load_rejects_bid_is_total(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc["bid_is_total"] = True
    with pytest.raises(SecopError) as excinfo:
        load(write_json(tmp_path, doc))
    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert "bid_is_total" in excinfo.value.message


def test_load_rejects_a_numeric_bid_value(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc["bid_value"] = 7813974.32
    with pytest.raises(SecopError) as excinfo:
        load(write_json(tmp_path, doc))
    assert "quoted string" in excinfo.value.message


@pytest.mark.parametrize(
    "key,value", [("bid_count", "4"), ("dry_run", "yes"), ("schedule_offsets_ms", 30)]
)
def test_load_rejects_wrong_types(tmp_path: Any, key: str, value: Any) -> None:
    doc = minimal_doc()
    doc[key] = value
    with pytest.raises(SecopError) as excinfo:
        load(write_json(tmp_path, doc))
    assert key in excinfo.value.message


def test_load_rejects_a_bool_where_an_int_belongs(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc["n_direct"] = True
    with pytest.raises(SecopError):
        load(write_json(tmp_path, doc))


def test_load_rejects_invalid_json(tmp_path: Any) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SecopError) as excinfo:
        load(str(path))
    assert "not valid JSON" in excinfo.value.message


def test_load_rejects_a_missing_file(tmp_path: Any) -> None:
    with pytest.raises(SecopError) as excinfo:
        load(str(tmp_path / "absent.json"))
    assert excinfo.value.code is Failure.CONFIG_INVALID


def test_load_rejects_unsupported_extensions(tmp_path: Any) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("jwt: x", encoding="utf-8")
    with pytest.raises(SecopError) as excinfo:
        load(str(path))
    assert "unsupported config file extension" in excinfo.value.message


def test_load_toml_or_names_the_missing_support(tmp_path: Any) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text(
        'jwt = "%s"\nauction_page_html_path = "beforebid.html"\n'
        'bid_value = "7813974.32"\nfire_time = "%s"\nbid_count = 2\n' % (JWT, FIRE_TIME),
        encoding="utf-8",
    )
    try:
        import tomllib  # noqa: F401
        available = True
    except ImportError:
        try:
            import tomli  # noqa: F401
            available = True
        except ImportError:
            available = False

    if available:
        assert load(str(path)).bid_count == 2
    else:
        with pytest.raises(SecopError) as excinfo:
            load(str(path))
        assert excinfo.value.code is Failure.CONFIG_INVALID
        assert "tomli" in excinfo.value.message


def test_overrides_win_over_the_file(tmp_path: Any) -> None:
    cfg = load(write_json(tmp_path, minimal_doc()), overrides={"confirm_live": True})
    assert cfg.confirm_live is True


def test_overrides_alone_build_a_config() -> None:
    cfg = load(overrides=minimal_doc())
    assert cfg.bid_value == "7813974.32"


def test_none_overrides_do_not_erase_file_values(tmp_path: Any) -> None:
    doc = minimal_doc()
    doc["bid_count"] = 3
    cfg = load(write_json(tmp_path, doc), overrides={"bid_count": None, "auction_id": None})
    assert cfg.bid_count == 3
    assert cfg.auction_id is None


def test_overrides_are_type_checked(tmp_path: Any) -> None:
    with pytest.raises(SecopError):
        load(write_json(tmp_path, minimal_doc()), overrides={"bid_count": "4"})


def test_loaded_config_validates(tmp_path: Any) -> None:
    cfg = load(write_json(tmp_path, minimal_doc()))
    far_future = datetime.fromisoformat(FIRE_TIME.replace("Z", "+00:00")) - timedelta(minutes=5)
    assert validate(cfg, now=far_future) == []


# ------------------------------------------------------------------------ keep-alive


def test_the_default_keepalive_leaves_room_before_the_first_release() -> None:
    cfg = make_config()
    assert cfg.keepalive_offsets_ms == [-45000, -30000, -15000]
    assert -max(cfg.keepalive_offsets_ms) >= keepalive_quiet_ms(cfg)
    assert validate(cfg, now=NOW) == []


def test_keepalive_defaults_are_not_shared_between_configs() -> None:
    first, second = make_config(), make_config()
    first.keepalive_offsets_ms.append(-5)
    assert second.keepalive_offsets_ms == [-45000, -30000, -15000]


@pytest.mark.parametrize("offsets", [[0], [-30000, 0], [-30000, 250]])
def test_a_keepalive_at_or_after_the_release_is_refused(offsets: List[int]) -> None:
    error = raises_config(keepalive_offsets_ms=offsets)
    assert "keepalive_offsets_ms" in error.message
    assert "must all be negative" in error.message


def test_a_keepalive_that_could_still_hold_a_bids_connection_is_refused() -> None:
    """Nothing may share a connection with its bid at the release instant.

    A keep-alive can hold its connection for its connect timeout, then its read timeout,
    then the margin to settle, then the gate's lead — 3000 + 3000 + 2000 + 1500 ms at the
    defaults. One issued closer to the first release than that could still be in flight
    when the bid needs the connection, which is how a start audit once held bids back.
    """
    error = raises_config(keepalive_offsets_ms=[-30000, -9000])
    assert "keepalive_offsets_ms" in error.message
    assert "-9500 or earlier" in error.message

    assert validate(make_config(keepalive_offsets_ms=[-30000, -9500]), now=NOW) == []


def test_the_gate_lead_counts_only_when_the_gate_runs() -> None:
    cfg = make_config(keepalive_offsets_ms=[-8000], gate_mode="off")
    assert keepalive_quiet_ms(cfg) == 8000
    assert validate(cfg, now=NOW) == []
    assert keepalive_quiet_ms(make_config(gate_mode="once_before")) == 9500


def test_a_shorter_keepalive_timeout_buys_a_later_keepalive() -> None:
    cfg = make_config(keepalive_offsets_ms=[-4000], keepalive_timeout_ms=1000, gate_mode="off")
    assert validate(cfg, now=NOW) == []


def test_no_keepalive_is_legal() -> None:
    assert validate(make_config(keepalive_offsets_ms=[]), now=NOW) == []


def test_a_zero_keepalive_timeout_is_refused() -> None:
    assert "keepalive_timeout_ms" in raises_config(keepalive_timeout_ms=0).message


# ----------------------------------------------------------------------- SECOP's clock


def test_apply_clock_offset_is_off_and_must_be_asked_for() -> None:
    assert make_config().apply_clock_offset is False


def test_apply_clock_offset_warns_what_it_rests_on_and_when_it_declines() -> None:
    warnings = validate(make_config(apply_clock_offset=True), now=NOW)
    text = " ".join(warnings)
    assert "apply_clock_offset is True" in text
    assert "two auctions, not a rule" in text
    assert "clock_max_width_ms=500" in text
    assert "clock_window_ms=5000" in text


def test_apply_clock_offset_without_a_start_audit_has_nothing_to_measure() -> None:
    warnings = validate(
        make_config(apply_clock_offset=True, do_start_audit=False), now=NOW
    )
    assert any("nothing to measure from" in warning for warning in warnings)


def test_apply_clock_offset_with_a_short_audit_lead_warns_it_will_not_finish() -> None:
    warnings = validate(
        make_config(apply_clock_offset=True, start_audit_lead_ms=10000), now=NOW
    )
    assert any("will usually not finish" in warning for warning in warnings)


def test_the_zero_bid_warning_says_what_the_server_did_with_one() -> None:
    warnings = validate(make_config(bid_value="0"), now=NOW)
    zero = [warning for warning in warnings if "ZERO" in warning]
    assert len(zero) == 1
    assert "accepted bid in each of two" in zero[0]
    assert "supplying at no charge" in zero[0]
    assert "unknown" not in zero[0]


# ------------------------------------------------------------- start_audit_planned


def test_a_live_run_with_any_thread_plans_its_one_start_audit() -> None:
    cfg = make_config(mode="burst", bid_count=3, interval_ms=30, wave_direct=1)
    assert start_audit_planned(cfg, waves_for(cfg)) is True
    assert start_audit_planned(make_config(do_start_audit=False), waves_for_single(cfg)) is False
    assert start_audit_planned(cfg, []) is False


def test_a_dry_run_plans_the_start_audit_only_when_a_thread_still_sends() -> None:
    direct = make_config(n_direct=2, dry_run=True)
    assert start_audit_planned(direct, waves_for_single(direct)) is False
    probing = make_config(n_direct=1, n_validate_only=1, dry_run=True)
    assert start_audit_planned(probing, waves_for_single(probing)) is True


# ------------------------------------------------------------------ clock fallback


def test_the_clock_fallback_is_off_by_default() -> None:
    assert make_config().clock_fallback_shift_ms is None


def test_apply_clock_offset_without_a_fallback_warns_it_may_fire_early() -> None:
    warnings = validate(make_config(apply_clock_offset=True), now=NOW)
    assert any("no clock_fallback_shift_ms" in w for w in warnings)


def test_a_fallback_beyond_the_window_is_refused() -> None:
    error = raises_config(apply_clock_offset=True, clock_fallback_shift_ms=6000)
    assert "clock_fallback_shift_ms=6000" in error.message


def test_a_fallback_without_apply_clock_offset_is_ignored_and_says_so() -> None:
    warnings = validate(make_config(clock_fallback_shift_ms=945), now=NOW)
    assert any("fallback is ignored" in w for w in warnings)


def test_a_fallback_must_be_an_integer_in_the_file(tmp_path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps(
            dict(
                jwt=JWT,
                auction_page_html_path="beforebid.html",
                bid_value="1",
                fire_time=FIRE_TIME,
                clock_fallback_shift_ms="945",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(SecopError):
        load(str(path))
    path.write_text(path.read_text().replace('"945"', "945"), encoding="utf-8")
    assert load(str(path)).clock_fallback_shift_ms == 945
