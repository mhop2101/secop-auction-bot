"""Server-clock and RTT diagnostics (plan §9.3, §9.5).

Everything here is arithmetic on samples; nothing in this module moves the fire instant.
``T`` comes from the operator's ``fire_time`` on an NTP-synced host clock. The ``Date``
header estimate is consumed only by the log and :func:`clock_guard`. The event-log estimate
(:func:`stamp_estimate`) is the one a run may act on, and only when the operator set
``apply_clock_offset`` — that decision and its guards live in ``__main__``, not here.

SECOP has at least two clocks. The ``Date`` header comes from the web front end and tracks
real time. The application stamps its event log and its bids with another, which on
2026-11-20 read 0.9-1.0 s behind NTP across two auctions an hour apart. The accepted bid in
each was stamped 32 and 44 ms after ``startDate`` on that second clock, so it is the one
the opening is observed on. Whether its offset holds from day to day is unverified.

The measurement this module exists to get right is the HTTP ``Date`` header. It is
whole-second (RFC 7231), so ``Date - local_now`` is a *biased* estimator with expected error
around -0.5 s rather than a noisy centred one, and averaging or taking a median of such
samples converges to the bias instead of removing it. The honest treatment is per-sample
interval bounds intersected across samples (Marzullo), with the residual width reported as
the error bar.
"""

from __future__ import annotations

import email.utils
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, NoReturn, Optional, Tuple

from .errors import Failure, SecopError
from .models import ClockEstimate, ClockSample

DATE_HEADER_GRANULARITY_MS = 1000.0

SOURCE_DATE_HEADER = "date_header"
SOURCE_EVENT_LOG = "event_log"

_QUANTILES = (
    ("p10", 0.10),
    ("p25", 0.25),
    ("p50", 0.50),
    ("p75", 0.75),
    ("p95", 0.95),
)


def parse_http_date(s: str) -> Optional[datetime]:
    """Parse an RFC 7231 IMF-fixdate into a tz-aware UTC datetime, or ``None``.

    Uses :func:`email.utils.parsedate_tz` rather than :func:`datetime.strptime`: ``%a``
    and ``%b`` are locale-dependent, and the header's month and weekday names are not.
    The result never carries a sub-second component — the format has no field for one.

    Anything the header cannot be turned into a real UTC instant — bad fields, or a date
    the zone offset pushes outside ``datetime``'s range — returns ``None`` so the sample is
    discarded. A hostile or broken proxy header must cost one sample, not the run.
    """
    if not s:
        return None
    parsed = email.utils.parsedate_tz(s)
    if parsed is None:
        return None
    tz_seconds = parsed[9]
    if tz_seconds is None:
        tz_seconds = 0
    try:
        naive = datetime(*parsed[:6])
        # The offset can carry a syntactically valid header out of datetime's range
        # (OverflowError), so it has to be applied under the same guard as the fields.
        utc = naive - timedelta(seconds=tz_seconds)
    except (ValueError, OverflowError):
        return None
    return utc.replace(tzinfo=timezone.utc)


def sample_interval_ms(sample: ClockSample) -> Tuple[float, float]:
    """Bound ``offset = server_clock - local_clock`` for one exchange, in milliseconds.

    The server stamped ``Date`` at some instant between the request leaving and the
    response arriving, and the stamp is truncated to the second, so the true server
    reading at that instant lies in ``[Date, Date + 1s)``. Both facts together give::

        Date - t_recv  <=  offset  <  Date + 1s - t_send

    Every sample is therefore at least 1000 ms wide plus the round trip. That width is the
    real information content of a whole-second header; a point estimate discards it.

    Raises on a header it cannot parse, so a caller holding a *batch* must pre-filter with
    :func:`parse_http_date` — over a batch one broken header is a lost sample, not a lost
    run. :func:`marzullo` does that.
    """
    server = parse_http_date(sample.date_header) if sample.date_header else None
    if server is None:
        raise SecopError(
            Failure.CLOCK_SYNC_FAILED,
            "clock sample carries no parsable Date header",
            {"date_header": sample.date_header, "endpoint": sample.endpoint},
        )
    if sample.t_send_wall.tzinfo is None or sample.t_recv_wall.tzinfo is None:
        raise SecopError(
            Failure.CLOCK_SYNC_FAILED,
            "clock sample wall times must be timezone-aware",
            {"endpoint": sample.endpoint},
        )
    lo = (server - sample.t_recv_wall).total_seconds() * 1000.0
    hi = (server - sample.t_send_wall).total_seconds() * 1000.0 + DATE_HEADER_GRANULARITY_MS
    return (lo, hi)


def _source(discarded: int) -> str:
    """``date_header``, or ``date_header;discarded=N`` when N samples were unusable.

    ``ClockEstimate`` carries no field for the discard count and ``samples`` counts only
    what was used, so a sample lost to a missing or unparsable ``Date`` would otherwise
    vanish between the probe and the log. ``source`` reaches both the run log and every
    :func:`clock_guard` message, which is where "0 usable samples" has to say whether the
    server sent nothing or sent something we could not read.
    """
    if discarded <= 0:
        return SOURCE_DATE_HEADER
    return "%s;discarded=%d" % (SOURCE_DATE_HEADER, discarded)


def marzullo(samples: List[ClockSample]) -> ClockEstimate:
    """Intersect every sample's offset interval; report the midpoint and the width.

    Never reduce these samples to a bare median. ``Date`` is whole-second, so
    ``Date - local_now`` is biased low by the truncation (expected error about -0.5 s) and
    a median of many draws converges to that bias rather than cancelling it — over the 128
    captured exchanges the median sits roughly 0.45 s away from what the same bytes
    actually support. Intersection cannot do that: each interval is a hard bound, so the
    result either contains the true offset or is empty, and ``width_ms`` states how much
    is genuinely unknown.

    An empty intersection means the samples contradict each other (a clock step mid-run,
    or a proxy rewriting ``Date``). ``offset_ms`` is then ``None`` and the caller must WARN
    rather than substitute a point estimate.

    A sample whose ``Date`` is absent or unparsable is dropped, never fatal: this runs in
    preflight ahead of the summary, so one hostile or broken proxy header costs one sample
    and not the whole diagnostic. The drop is counted into ``source`` — see :func:`_source`
    — because silently returning a narrower ``samples`` would hide it.
    """
    lo = None  # type: Optional[float]
    hi = None  # type: Optional[float]
    n = 0
    discarded = 0
    for sample in samples:
        header = sample.date_header
        if not header or parse_http_date(header) is None:
            discarded += 1
            continue
        sample_lo, sample_hi = sample_interval_ms(sample)
        n += 1
        if lo is None or sample_lo > lo:
            lo = sample_lo
        if hi is None or sample_hi < hi:
            hi = sample_hi
    source = _source(discarded)
    if lo is None or hi is None:
        return ClockEstimate(
            source=source,
            lo_ms=None,
            hi_ms=None,
            offset_ms=None,
            width_ms=None,
            samples=0,
        )
    if lo > hi:
        return ClockEstimate(
            source=source,
            lo_ms=lo,
            hi_ms=hi,
            offset_ms=None,
            width_ms=None,
            samples=n,
            empty_intersection=True,
        )
    return ClockEstimate(
        source=source,
        lo_ms=lo,
        hi_ms=hi,
        offset_ms=(lo + hi) / 2.0,
        width_ms=hi - lo,
        samples=n,
    )


def stamp_interval_ms(
    stamp: datetime, t_send_wall: datetime, t_recv_wall: datetime
) -> Tuple[float, float]:
    """Bound ``offset = server_clock - local_clock`` from one row the server timestamped.

    The server wrote the row while handling a request that left at ``t_send_wall`` and was
    answered by ``t_recv_wall``, so at the moment its clock read ``stamp`` the local clock
    read something in between::

        stamp - t_recv  <=  offset  <=  stamp - t_send

    Unlike a ``Date`` header the stamp carries sub-millisecond digits, so the interval is
    as wide as the round trip and no wider.

    Only valid for an exchange that was *answered*. A request that timed out may be
    processed after the local side stopped waiting, which puts the stamp outside the
    bracket and makes the bound false.
    """
    for instant in (stamp, t_send_wall, t_recv_wall):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise SecopError(
                Failure.CLOCK_SYNC_FAILED,
                "a stamp interval needs timezone-aware instants",
                {"instant": instant.isoformat()},
            )
    lo = (stamp - t_recv_wall).total_seconds() * 1000.0
    hi = (stamp - t_send_wall).total_seconds() * 1000.0
    return (lo, hi)


def stamp_estimate(
    stamps: List[datetime], t_send_wall: datetime, t_recv_wall: datetime
) -> ClockEstimate:
    """The offset interval supported by the candidate rows for one answered request.

    The event log names a company, not a session, so when several rows could be the one
    this request wrote — two runs under one company code starting their audits together —
    there is no telling which is ours. The result is then the **hull** of every candidate's
    interval, not their intersection: exactly one candidate is the true row, each other
    one is a row some other request wrote, and intersecting would treat all of them as
    measurements of the same exchange. The hull is wider and still contains the truth.

    ``samples`` is the number of candidates and ``source`` says so when it is more than
    one. No candidates gives an estimate with no offset, never a guess.
    """
    if not stamps:
        return ClockEstimate(
            source=SOURCE_EVENT_LOG,
            lo_ms=None,
            hi_ms=None,
            offset_ms=None,
            width_ms=None,
            samples=0,
        )
    intervals = [stamp_interval_ms(stamp, t_send_wall, t_recv_wall) for stamp in stamps]
    lo = min(interval[0] for interval in intervals)
    hi = max(interval[1] for interval in intervals)
    source = SOURCE_EVENT_LOG
    if len(stamps) > 1:
        source = "%s;candidates=%d" % (SOURCE_EVENT_LOG, len(stamps))
    return ClockEstimate(
        source=source,
        lo_ms=lo,
        hi_ms=hi,
        offset_ms=(lo + hi) / 2.0,
        width_ms=hi - lo,
        samples=len(stamps),
    )


def _quantile(ordered: List[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def rtt_stats(wait_ms: List[float]) -> Dict[str, float]:
    """Summarise a preflight latency sample set (§9.5 wants N >= 10).

    Feed this ``timings.wait``, not total elapsed: total also carries client-side
    ``blocked`` queueing (up to 235.6 ms in the capture) and ``receive``, neither of which
    is propagation. The observed spread for ``GetAuctionSimple`` runs 90.3 ms to 1138.3 ms,
    so a single draw is meaningless and the whole distribution belongs in the log.
    """
    if not wait_ms:
        raise SecopError(
            Failure.CLOCK_SYNC_FAILED, "rtt_stats requires at least one sample"
        )
    ordered = sorted(float(v) for v in wait_ms)
    out = {"n": float(len(ordered)), "min": ordered[0], "max": ordered[-1]}  # type: Dict[str, float]
    for name, q in _QUANTILES:
        out[name] = _quantile(ordered, q)
    return out


def recommended_lead_ms(wait_ms: List[float], quantile: float = 0.1) -> float:
    """Advisory only: a defensible starting point for ``lead_ms``, never auto-applied.

    ``lead_ms`` defaults to 0 and stays there unless the operator sets it deliberately.
    This returns a low quantile of ``timings.wait`` so that leading by it compensates for
    transit rather than for the server's own work.

    Do **not** derive a lead from ``ValidateBid`` round-trip time. That endpoint's elapsed
    time is almost entirely server-side validation — 1627.1 ms of ``wait`` out of 1633.1 ms
    total on the slowest captured sample — which the server spends *after* the request has
    already arrived. Leading by it would push the request in before the auction opens, and
    an early request is rejected, not queued.
    """
    if not wait_ms:
        raise SecopError(
            Failure.CLOCK_SYNC_FAILED, "recommended_lead_ms requires at least one sample"
        )
    if not 0.0 <= quantile <= 1.0:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "quantile must be within [0, 1]",
            {"quantile": quantile},
        )
    return _quantile(sorted(float(v) for v in wait_ms), quantile)


def clock_guard(estimate: ClockEstimate, span_ms: float) -> Optional[str]:
    """Return a ``CLOCK_SYNC_FAILED``-worthy message, or ``None`` when the estimate is usable.

    The estimate is usable only when what we do not know about the server clock is smaller
    than the schedule we are trying to place against it. A whole-second ``Date`` header
    will not clear a schedule spanning tens of milliseconds — that is the expected verdict,
    not a malfunction, and it is why ``T`` is operator-supplied. The caller decides whether
    to abort; this function never touches ``T``.
    """
    if estimate.empty_intersection:
        return (
            "clock samples do not intersect (source=%s, samples=%d, lo=%s ms, hi=%s ms): "
            "the server clock reading is self-contradictory, so no offset is defensible"
            % (estimate.source, estimate.samples, estimate.lo_ms, estimate.hi_ms)
        )
    if estimate.samples == 0 or estimate.width_ms is None:
        return (
            "no usable clock samples (source=%s): server offset is unmeasured"
            % estimate.source
        )
    if estimate.width_ms > span_ms:
        return (
            "clock offset is known only to within %.1f ms (source=%s, samples=%d, "
            "midpoint %.1f ms) while the schedule spans %.1f ms: the schedule is finer "
            "than the measurement and any offset applied to T would be guesswork"
            % (
                estimate.width_ms,
                estimate.source,
                estimate.samples,
                estimate.offset_ms if estimate.offset_ms is not None else float("nan"),
                span_ms,
            )
        )
    return None


def probe_signalr(*args: object, **kwargs: object) -> NoReturn:
    """Always raises. The SignalR hub clock is not reachable from anything we hold (§9.3)."""
    raise NotImplementedError(
        "SignalR clock probe is not implemented and is not buildable from this capture. "
        "The handshake was never captured: negotiate/start/connect/abort have zero hits "
        "across all 429 recorded URLs, and the 182-character connectionToken is already "
        "minted on the first captured request, so it cannot be derived from any observed "
        "value. It has to be reversed out of jquery.signalR 2.x first. The hub clock is "
        "also the outlier of the two server clocks (hub median -0.547 s versus a Date "
        "interval consistent with roughly zero), and neither is tied by any evidence to "
        "the clock the auction state machine uses. Use clock_probe='date_header'."
    )


__all__ = [
    "DATE_HEADER_GRANULARITY_MS",
    "SOURCE_DATE_HEADER",
    "SOURCE_EVENT_LOG",
    "parse_http_date",
    "sample_interval_ms",
    "marzullo",
    "stamp_interval_ms",
    "stamp_estimate",
    "rtt_stats",
    "recommended_lead_ms",
    "clock_guard",
    "probe_signalr",
]
