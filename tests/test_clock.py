"""Offline tests for :mod:`secop_firstbid.clock` (plan §9.3, §9.5).

Driven by ``clock_samples.json``: the 128 real ``GetAuctionSimple`` exchanges from the
capture, each carrying its ``Date`` header and the HAR ``timings`` breakdown. The send and
receive instants are reconstructed from those timings, which is what makes the central
regression here — naive median versus Marzullo midpoint — a measurement rather than a
hypothetical.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from secop_firstbid import __main__ as cli
from secop_firstbid import clock
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.models import AuctionRef, ClockEstimate, ClockSample
from secop_firstbid.session import HttpOutcome

FRACTIONAL_SECOND = re.compile(r"\d{2}:\d{2}:\d{2}(\.\d+)")


def _timing(timings: Dict[str, Any], key: str) -> float:
    value = timings.get(key, -1)
    return float(value) if value and value > 0 else 0.0


def _sample_from_har(entry: Dict[str, Any]) -> ClockSample:
    started = datetime.strptime(
        entry["startedDateTime"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=timezone.utc)
    timings = entry["timings"]
    # HAR 1.2: ssl is contained within connect, so adding both would double-count it.
    pre_ms = (
        _timing(timings, "blocked")
        + _timing(timings, "dns")
        + _timing(timings, "connect")
    )
    send_done_ms = pre_ms + _timing(timings, "send")
    recv_done_ms = send_done_ms + _timing(timings, "wait") + _timing(timings, "receive")
    return ClockSample(
        date_header=entry["date_header"],
        t_send_mono_ns=int(send_done_ms * 1e6),
        t_recv_mono_ns=int(recv_done_ms * 1e6),
        t_send_wall=started + timedelta(milliseconds=send_done_ms),
        t_recv_wall=started + timedelta(milliseconds=recv_done_ms),
        endpoint="GetAuctionSimple",
    )


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@pytest.fixture(scope="module")
def har_samples(clock_samples: List[Dict[str, Any]]) -> List[ClockSample]:
    return [_sample_from_har(entry) for entry in clock_samples]


def test_reconstruction_matches_har_elapsed(clock_samples: List[Dict[str, Any]]) -> None:
    for entry in clock_samples:
        sample = _sample_from_har(entry)
        elapsed_ms = (
            sample.t_recv_wall
            - datetime.strptime(
                entry["startedDateTime"], "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=timezone.utc)
        ).total_seconds() * 1000.0
        assert abs(elapsed_ms - entry["time_ms"]) < 1e-6


def test_every_date_header_is_whole_second(clock_samples: List[Dict[str, Any]]) -> None:
    assert len(clock_samples) == 128
    fractional = [
        e["date_header"]
        for e in clock_samples
        if FRACTIONAL_SECOND.search(e["date_header"] or "")
    ]
    assert fractional == []
    for entry in clock_samples:
        parsed = clock.parse_http_date(entry["date_header"])
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)
        assert parsed.microsecond == 0


def test_sample_interval_is_one_second_plus_the_round_trip(
    har_samples: List[ClockSample],
) -> None:
    for sample in har_samples:
        lo, hi = clock.sample_interval_ms(sample)
        rtt_ms = (sample.t_recv_wall - sample.t_send_wall).total_seconds() * 1000.0
        assert hi > lo
        assert math.isclose(hi - lo, 1000.0 + rtt_ms, rel_tol=0, abs_tol=1e-6)


def test_marzullo_yields_a_finite_interval_with_a_reported_width(
    har_samples: List[ClockSample],
) -> None:
    estimate = clock.marzullo(har_samples)
    assert isinstance(estimate, ClockEstimate)
    assert estimate.source == "date_header"
    assert estimate.samples == 128
    assert estimate.empty_intersection is False
    assert estimate.lo_ms is not None and estimate.hi_ms is not None
    assert estimate.offset_ms is not None and estimate.width_ms is not None
    assert math.isfinite(estimate.lo_ms) and math.isfinite(estimate.hi_ms)
    assert estimate.lo_ms <= estimate.offset_ms <= estimate.hi_ms
    assert math.isclose(estimate.width_ms, estimate.hi_ms - estimate.lo_ms)
    assert math.isclose(
        estimate.offset_ms, (estimate.lo_ms + estimate.hi_ms) / 2.0
    )
    # A whole-second header cannot do better than ~1 s minus the spread of send instants.
    assert 0.0 < estimate.width_ms < 1000.0
    # Every sample's bound really does contain the reported midpoint.
    for sample in har_samples:
        lo, hi = clock.sample_interval_ms(sample)
        assert lo <= estimate.offset_ms <= hi


def test_naive_median_differs_from_marzullo_midpoint_by_over_300ms(
    har_samples: List[ClockSample],
) -> None:
    naive = []
    for sample in har_samples:
        server = clock.parse_http_date(sample.date_header)
        assert server is not None
        naive.append((server - sample.t_recv_wall).total_seconds() * 1000.0)

    naive_median = _median(naive)
    estimate = clock.marzullo(har_samples)
    assert estimate.offset_ms is not None

    assert abs(naive_median - estimate.offset_ms) > 300.0
    # The bias has a sign: truncation makes Date - t_recv read low.
    assert naive_median < estimate.offset_ms
    # And the naive answer is not merely off-centre, it is outside the feasible set.
    assert estimate.lo_ms is not None
    assert naive_median < estimate.lo_ms


def test_marzullo_reports_an_empty_intersection_instead_of_a_point_estimate() -> None:
    base = datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc)

    def make(date_header: str, send_offset_s: float) -> ClockSample:
        send = base + timedelta(seconds=send_offset_s)
        recv = send + timedelta(milliseconds=100)
        return ClockSample(
            date_header=date_header,
            t_send_mono_ns=0,
            t_recv_mono_ns=100_000_000,
            t_send_wall=send,
            t_recv_wall=recv,
        )

    contradictory = [
        make("Tue, 28 Jul 2026 21:30:00 GMT", 0.0),
        make("Tue, 28 Jul 2026 21:40:00 GMT", 5.0),
    ]
    estimate = clock.marzullo(contradictory)
    assert estimate.empty_intersection is True
    assert estimate.offset_ms is None
    assert estimate.width_ms is None
    assert estimate.samples == 2
    assert clock.clock_guard(estimate, 90.0) is not None


def test_marzullo_with_no_date_headers_is_empty_not_zero() -> None:
    sample = ClockSample(
        date_header=None,
        t_send_mono_ns=0,
        t_recv_mono_ns=1,
        t_send_wall=datetime(2026, 7, 28, tzinfo=timezone.utc),
        t_recv_wall=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    estimate = clock.marzullo([sample])
    assert estimate.samples == 0
    assert estimate.offset_ms is None
    assert estimate.width_ms is None
    assert estimate.empty_intersection is False
    assert clock.clock_guard(estimate, 90.0) is not None


def test_one_unparsable_date_header_costs_a_sample_not_the_estimate(
    har_samples: List[ClockSample],
) -> None:
    """A broken proxy header is discarded, exactly as ``parse_http_date`` promises.

    ``marzullo`` runs in preflight ahead of ``summary.json``, so raising here takes the
    whole diagnostic and the summary with it, not just the one exchange.
    """
    clean = clock.marzullo(har_samples)
    poisoned = list(har_samples)
    poisoned.insert(
        len(poisoned) // 2,
        ClockSample(
            date_header="Tuesday, 28-Jul-26 21:00:00 GMT+00:00 (proxy)",
            t_send_mono_ns=0,
            t_recv_mono_ns=100_000_000,
            t_send_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
            t_recv_wall=datetime(2026, 7, 28, 21, 30, 0, 100_000, tzinfo=timezone.utc),
            endpoint="GetAuctionSimple",
        ),
    )

    estimate = clock.marzullo(poisoned)

    assert estimate.samples == clean.samples == 128
    assert estimate.lo_ms == clean.lo_ms
    assert estimate.hi_ms == clean.hi_ms
    assert estimate.offset_ms == clean.offset_ms
    assert estimate.width_ms == clean.width_ms
    # The loss is on the record rather than silent.
    assert clean.source == clock.SOURCE_DATE_HEADER
    assert estimate.source == clock.SOURCE_DATE_HEADER + ";discarded=1"


def test_a_date_header_out_of_range_is_discarded_by_marzullo_too() -> None:
    usable = ClockSample(
        date_header="Tue, 28 Jul 2026 21:30:00 GMT",
        t_send_mono_ns=0,
        t_recv_mono_ns=100_000_000,
        t_send_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
        t_recv_wall=datetime(2026, 7, 28, 21, 30, 0, 100_000, tzinfo=timezone.utc),
    )
    overflowing = ClockSample(
        date_header="Fri, 31 Dec 9999 23:59:59 -0500",
        t_send_mono_ns=0,
        t_recv_mono_ns=100_000_000,
        t_send_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
        t_recv_wall=datetime(2026, 7, 28, 21, 30, 0, 100_000, tzinfo=timezone.utc),
    )

    estimate = clock.marzullo([usable, overflowing])

    assert estimate.samples == 1
    assert estimate.offset_ms is not None
    assert estimate.source == clock.SOURCE_DATE_HEADER + ";discarded=1"


def test_every_header_unparsable_is_reported_as_a_loss_not_as_no_samples() -> None:
    def bad(header: Optional[str]) -> ClockSample:
        return ClockSample(
            date_header=header,
            t_send_mono_ns=0,
            t_recv_mono_ns=1,
            t_send_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
            t_recv_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
        )

    estimate = clock.marzullo([bad("not a date"), bad(""), bad(None)])

    assert estimate.samples == 0
    assert estimate.offset_ms is None
    assert estimate.empty_intersection is False
    assert estimate.source == clock.SOURCE_DATE_HEADER + ";discarded=3"
    # clock_guard is the only place an operator sees this, so it has to carry the count.
    message = clock.clock_guard(estimate, 90.0)
    assert message is not None
    assert "discarded=3" in message


def test_preflight_clock_diagnostics_survives_a_broken_date_header() -> None:
    """The preflight call site is unguarded, so a raise there costs ``summary.json``."""
    headers = [
        "Tue, 28 Jul 2026 21:30:00 GMT",
        "Tue, 28 Jul 2026 21:30:01 GMT",
        "<html>503 from the edge</html>",
        "Tue, 28 Jul 2026 21:30:03 GMT",
    ]
    sent: List[str] = []

    class _Client:
        def get(self, path: str, **kwargs: Any) -> HttpOutcome:
            sent.append(path)
            return HttpOutcome(
                status=200,
                body=None,
                text="",
                headers={},
                t_send_mono_ns=1,
                t_recv_mono_ns=2,
                rtt_ms=1.0,
                date_header=headers[len(sent) - 1],
            )

    class _Logger:
        def __init__(self) -> None:
            self.events: List[Dict[str, Any]] = []

        def event(self, **fields: Any) -> None:
            self.events.append(fields)

    class _Cfg:
        clock_probe = "date_header"
        rtt_samples = len(headers)

    logger = _Logger()
    ref = AuctionRef(auction_id="d6a49a23-3556-4b3d-9bb4-3e209279b9f8")
    estimate, elapsed_ms = cli._clock_diagnostics(_Cfg(), ref, _Client(), logger)

    assert len(sent) == len(headers)
    assert elapsed_ms == [1.0, 1.0, 1.0, 1.0]
    assert estimate is not None
    assert estimate.samples == 3
    assert estimate.source == clock.SOURCE_DATE_HEADER + ";discarded=1"
    logged = [e for e in logger.events if e.get("event") == "clock_offset"]
    assert logged and logged[0]["detail"]["source"] == estimate.source


def test_module_exposes_no_bare_median_as_the_offset() -> None:
    own_callables = {}
    for name in dir(clock):
        if name.startswith("_"):
            continue
        obj = getattr(clock, name)
        if callable(obj) and getattr(obj, "__module__", None) == clock.__name__:
            own_callables[name] = obj

    banned = [
        name
        for name in own_callables
        if any(token in name.lower() for token in ("median", "average", "mean", "naive"))
    ]
    assert banned == []

    # No public entry point hands back an offset at all: the only offset the module
    # produces is a Marzullo midpoint carried on ClockEstimate alongside its width.
    assert [n for n in own_callables if "offset" in n.lower()] == []
    assert clock.marzullo.__annotations__["return"] == "ClockEstimate"

    doc = (clock.marzullo.__doc__ or "").lower()
    assert "median" in doc and "bias" in doc


def test_clock_guard_compares_width_against_the_schedule_span(
    har_samples: List[ClockSample],
) -> None:
    estimate = clock.marzullo(har_samples)
    assert estimate.width_ms is not None

    message = clock.clock_guard(estimate, 90.0)
    assert message is not None
    assert "90.0" in message

    assert clock.clock_guard(estimate, estimate.width_ms + 1.0) is None
    assert clock.clock_guard(estimate, estimate.width_ms) is None


def test_parse_http_date_handles_the_observed_shape_and_rejects_junk() -> None:
    parsed = clock.parse_http_date("Tue, 28 Jul 2026 21:30:02 GMT")
    assert parsed == datetime(2026, 7, 28, 21, 30, 2, tzinfo=timezone.utc)
    assert clock.parse_http_date("") is None
    assert clock.parse_http_date("not a date") is None
    assert clock.parse_http_date("Tue, 32 Zzz 2026 21:30:02 GMT") is None


def test_a_date_header_out_of_datetime_range_is_discarded_not_raised() -> None:
    # Well-formed and unrepresentable: applying -0500 to the last hour datetime can hold
    # lands in year 10000, which is an OverflowError, not a ValueError.
    header = "Fri, 31 Dec 9999 23:59:59 -0500"
    assert clock.parse_http_date(header) is None

    sample = ClockSample(
        date_header=header,
        t_send_mono_ns=0,
        t_recv_mono_ns=1,
        t_send_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
        t_recv_wall=datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc),
        endpoint="GetAuctionSimple",
    )
    with pytest.raises(SecopError) as excinfo:
        clock.sample_interval_ms(sample)
    assert excinfo.value.code is Failure.CLOCK_SYNC_FAILED


def test_sample_interval_rejects_unusable_samples() -> None:
    aware = datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc)
    naive_dt = datetime(2026, 7, 28, 21, 30, 0)

    with pytest.raises(SecopError):
        clock.sample_interval_ms(
            ClockSample(
                date_header=None,
                t_send_mono_ns=0,
                t_recv_mono_ns=1,
                t_send_wall=aware,
                t_recv_wall=aware,
            )
        )
    with pytest.raises(SecopError):
        clock.sample_interval_ms(
            ClockSample(
                date_header="Tue, 28 Jul 2026 21:30:00 GMT",
                t_send_mono_ns=0,
                t_recv_mono_ns=1,
                t_send_wall=naive_dt,
                t_recv_wall=naive_dt,
            )
        )


def test_rtt_stats_over_the_captured_wait_times(
    clock_samples: List[Dict[str, Any]]
) -> None:
    waits = [e["timings"]["wait"] for e in clock_samples]
    stats = clock.rtt_stats(waits)

    assert stats["n"] == 128.0
    assert stats["min"] == min(waits)
    assert stats["max"] == max(waits)
    ordered_keys = ["min", "p10", "p25", "p50", "p75", "p95", "max"]
    values = [stats[k] for k in ordered_keys]
    assert values == sorted(values)
    assert set(stats) == set(ordered_keys) | {"n"}

    single = clock.rtt_stats([42.0])
    assert single["min"] == single["p50"] == single["max"] == 42.0
    with pytest.raises(SecopError):
        clock.rtt_stats([])


def test_recommended_lead_is_a_low_quantile_and_warns_about_validatebid(
    clock_samples: List[Dict[str, Any]]
) -> None:
    waits = [e["timings"]["wait"] for e in clock_samples]
    stats = clock.rtt_stats(waits)

    lead = clock.recommended_lead_ms(waits)
    assert stats["min"] <= lead <= stats["p25"]
    assert lead == stats["p10"]
    assert clock.recommended_lead_ms(waits, quantile=0.0) == stats["min"]
    assert clock.recommended_lead_ms(waits, quantile=1.0) == stats["max"]

    with pytest.raises(SecopError):
        clock.recommended_lead_ms(waits, quantile=1.5)
    with pytest.raises(SecopError):
        clock.recommended_lead_ms([])

    doc = clock.recommended_lead_ms.__doc__ or ""
    assert "ValidateBid" in doc
    assert "advisory" in doc.lower()


def test_probe_signalr_is_unbuildable_and_says_so() -> None:
    with pytest.raises(NotImplementedError) as excinfo:
        clock.probe_signalr()
    message = str(excinfo.value)
    assert "handshake" in message
    assert "connectionToken" in message


# ------------------------------------------------------- the event-log stamp estimate


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def test_a_stamp_bounds_the_offset_by_the_round_trip_and_nothing_wider() -> None:
    """A row stamped mid-request pins the server clock between send and receive.

    Unlike the whole-second ``Date`` header there is no truncation term: the interval is
    exactly as wide as the exchange.
    """
    sent = _utc(2026, 11, 20, 20, 59, 0, 100000)
    received = sent + timedelta(milliseconds=40)
    stamp = sent + timedelta(milliseconds=20) - timedelta(milliseconds=945)

    lo, hi = clock.stamp_interval_ms(stamp, sent, received)

    assert lo == pytest.approx(-965.0)
    assert hi == pytest.approx(-925.0)
    assert hi - lo == pytest.approx(40.0)


def test_a_stamp_interval_refuses_naive_instants() -> None:
    aware = _utc(2026, 11, 20, 21, 0, 0)
    with pytest.raises(SecopError) as excinfo:
        clock.stamp_interval_ms(datetime(2026, 11, 20, 21, 0, 0), aware, aware)
    assert excinfo.value.code is Failure.CLOCK_SYNC_FAILED


def test_no_candidate_row_is_no_offset_not_a_guess() -> None:
    aware = _utc(2026, 11, 20, 21, 0, 0)

    estimate = clock.stamp_estimate([], aware, aware)

    assert estimate.offset_ms is None
    assert estimate.width_ms is None
    assert estimate.samples == 0
    assert estimate.source == clock.SOURCE_EVENT_LOG


def test_candidates_are_covered_by_a_hull_never_intersected() -> None:
    """Only one candidate is this request's row, so they are not samples of one thing.

    Intersecting two disjoint intervals would come back empty, or — when they overlap —
    narrower than either, claiming a precision that rests on a row some other request
    wrote. The hull is the widest reading and the only one certain to hold the truth.
    """
    sent = _utc(2026, 11, 20, 20, 59, 0)
    received = sent + timedelta(milliseconds=40)
    stamps = [
        sent - timedelta(milliseconds=925),
        sent - timedelta(milliseconds=725),
    ]

    estimate = clock.stamp_estimate(stamps, sent, received)

    assert estimate.samples == 2
    assert estimate.source == "event_log;candidates=2"
    assert estimate.lo_ms == pytest.approx(-965.0)
    assert estimate.hi_ms == pytest.approx(-725.0)
    assert estimate.width_ms == pytest.approx(240.0)
    assert estimate.offset_ms == pytest.approx(-845.0)
    for stamp in stamps:
        lo, hi = clock.stamp_interval_ms(stamp, sent, received)
        assert estimate.lo_ms <= lo and hi <= estimate.hi_ms
