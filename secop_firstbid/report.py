"""Run summary: the ``summary.json`` record and the human digest (plan §12.3).

This module decides nothing about the run — it reports what the other stages decided. In
particular it never re-evaluates whether a bid landed: ``bidder.FirstSuccess`` already
arbitrated that with the single success predicate, and the latch it set is the authority
here. A second opinion computed from the same bodies is how two parts of one run come to
disagree about whether a bid was placed.

Three fields exist because the naive version of each is misleading.

``outcome`` is ``BID_PLACED`` whenever any wave succeeded, however many other waves came
back ``STATE_NOT_STARTED`` or ``DUPLICATE``. A spread schedule is *designed* to have waves
that bounce: a pre-``T`` wave is expected to be too early and every wave after the winner is
expected to be a duplicate. Counting those as failures would report the intended behaviour
of the tool as a malfunction. Only ``ALL_REJECTED`` and ``ERROR`` mean nothing landed.

``NO_VERDICT`` is not ``ALL_REJECTED``. A submission that timed out was not rejected: it
may never have connected, or it may have been received and processed after this process
stopped listening. On 2026-11-20 every submission of four live runs ended in a timeout and
the server processed each one that had left the machine seconds later; a summary saying
"rejected" for those runs described an answer nobody received. What the server did with
them is in its own record — see :mod:`secop_firstbid.records`.

``submissions_planned`` and ``submissions_sent`` are separate fields because their
difference is the only honest measure of what ``stop_on_success`` saved, and at the
recommended schedule densities that difference is zero — a wave can only be cancelled if an
earlier verdict returns before it releases, and the fastest bid response in the capture is
slower than the whole recommended schedule.

``audited_requests`` — not the submission count — is the footprint the auction owner can
read. It counts validate-only threads, which write the same buyer-visible row a rejected
submission does, and doubles for ``do_start_audit``.

The schema is published in ``docs/logging.md``; every key documented there is produced here
with the documented meaning. The one key beyond it is ``warnings``, because
:func:`human_summary` is handed the summary and nothing else.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping as _AbcMapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Set

from . import endpoints, numbers
from .config import Config, audit_footprint, redacted_dict
from .errors import Failure, SecopError
from .gate import verdict_detail
from .models import AuctionDetail, AuctionRef, BidPayload, BidResult, ClockEstimate, Wave

if TYPE_CHECKING:  # pragma: no cover
    from .bidder import FireOutcome
    from .gate import GateVerdict
    from .logging_setup import RunLogger
    from .schedule import Armed

STAGE = "report"
SUMMARY_FILENAME = "summary.json"

OUTCOME_BID_PLACED = "BID_PLACED"
OUTCOME_ALL_REJECTED = "ALL_REJECTED"
OUTCOME_NO_VERDICT = "NO_VERDICT"
OUTCOME_NOT_FIRED = "NOT_FIRED"
OUTCOME_ERROR = "ERROR"

OUTCOMES = (
    OUTCOME_BID_PLACED,
    OUTCOME_ALL_REJECTED,
    OUTCOME_NO_VERDICT,
    OUTCOME_NOT_FIRED,
    OUTCOME_ERROR,
)

# bidder.ENDPOINT_CREATE, spelled here because importing bidder at module load would pull
# the whole fire path into a module that only reports on it.
_SUBMISSION_ENDPOINT = "CreateBid"

# Body fields that have carried a server-side timestamp for a bid. BidResult holds no
# response headers, so the server's `Date` header is not reachable from here; it stays in
# events.jsonl and capture.har.
_SERVER_DATE_KEYS = (
    "date",
    "serverdate",
    "biddate",
    "registerdate",
    "bidregisterdate",
    "creationdate",
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _money(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else numbers.to_wire(value)


def _outcome(fire: Optional["FireOutcome"], error: Optional[BaseException]) -> str:
    """One of :data:`OUTCOMES`, from the winner latch and the send count.

    ``BID_PLACED`` asks only whether a bid landed, never how many others bounced. A wave
    tagged ``DUPLICATE`` after the win and a pre-``T`` wave tagged ``STATE_NOT_STARTED`` are
    both expected products of a spread schedule.

    A run that sent no submission at all is ``NOT_FIRED``, not ``ALL_REJECTED``: a dry run,
    a gate that stopped the schedule and a probe-only run all had nothing to reject.

    ``NO_VERDICT`` is the run whose submissions left and none was answered — every one
    ended in a timeout or a connection error. Nothing was rejected, because nothing was
    said; one answered submission is enough to make the run ``ALL_REJECTED`` instead.
    """
    if error is not None:
        return OUTCOME_ERROR
    if fire is None:
        return OUTCOME_NOT_FIRED
    if fire.winner is not None:
        return OUTCOME_BID_PLACED
    if fire.submissions_sent > 0:
        answered = [
            result
            for result in fire.results
            if result.endpoint == _SUBMISSION_ENDPOINT and result.status is not None
        ]
        return OUTCOME_ALL_REJECTED if answered else OUTCOME_NO_VERDICT
    return OUTCOME_NOT_FIRED


def _clock_block(
    applied: bool,
    date_header: Optional[ClockEstimate],
    signalr: Optional[ClockEstimate],
    host_ntp: Optional[str],
    event_log: Optional[ClockEstimate] = None,
    shift_ms: float = 0.0,
) -> Dict[str, Any]:
    """The clock diagnostics, always carrying the residual width beside the midpoint.

    ``offset_ms`` alone would read as a measurement; it is a midpoint of a feasible interval
    whose width is what the whole-second ``Date`` header actually leaves unknown. An empty
    intersection reports ``offset_ms: null`` with the sample count intact rather than
    substituting a point estimate.

    ``event_log`` is the other clock: the one SECOP stamps its log and its bids with,
    placed from the run's own start audit. ``shift_ms`` is how far the countdown was moved
    onto it, and is zero unless ``applied`` is true.
    """
    header = {"offset_ms": None, "residual_width_ms": None, "samples": 0}
    if date_header is not None:
        header = {
            "offset_ms": date_header.offset_ms,
            "residual_width_ms": date_header.width_ms,
            "samples": date_header.samples,
        }
    hub = {"offset_ms": None, "samples": 0}
    if signalr is not None:
        hub = {"offset_ms": signalr.offset_ms, "samples": signalr.samples}

    disagreement = None  # type: Optional[float]
    if header["offset_ms"] is not None and hub["offset_ms"] is not None:
        disagreement = float(hub["offset_ms"]) - float(header["offset_ms"])

    stamped = {"offset_ms": None, "residual_width_ms": None, "candidates": 0}
    if event_log is not None:
        stamped = {
            "offset_ms": event_log.offset_ms,
            "residual_width_ms": event_log.width_ms,
            "candidates": event_log.samples,
        }

    return {
        "applied": bool(applied),
        "shift_ms": float(shift_ms),
        "date_header": header,
        "event_log": stamped,
        "signalr": hub,
        "disagreement_ms": disagreement,
        "host_ntp": host_ntp,
    }


def _gate_block(cfg: Config, gate: Optional["GateVerdict"]) -> Dict[str, Any]:
    if gate is not None:
        return verdict_detail(gate)
    return {
        "mode": cfg.gate_mode,
        "issued_offset_ms": None,
        "state": None,
        "exec_state": None,
        "rtt_ms": None,
        "proceed": None,
        "reason": "the gate did not run",
        "failure": None,
    }


def _schedule_planned(waves: Sequence[Wave]) -> List[Dict[str, Any]]:
    return [
        {
            "wave": wave.index,
            "offset_ms": wave.offset_ms,
            "direct": wave.direct,
            "validate": wave.validate,
            "validate_only": wave.validate_only,
        }
        for wave in waves
    ]


def _server_date(result: BidResult) -> Optional[str]:
    raw = result.raw or {}
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        if str(key).replace("_", "").lower() in _SERVER_DATE_KEYS:
            return value
    return None


def _winner_block(winner: Optional[BidResult]) -> Optional[Dict[str, Any]]:
    if winner is None:
        return None
    return {
        "thread": winner.thread_id,
        "role": winner.role,
        "wave": winner.wave_index,
        "scheduled_offset_ms": winner.scheduled_offset_ms,
        "actual_offset_ms": winner.actual_offset_ms,
        "bid_id": winner.bid_id,
        "server_date": _server_date(winner),
        "rtt_ms": winner.rtt_ms,
    }


def _attempt(result: BidResult) -> Dict[str, Any]:
    # The server's own field names (isValid, errorType, errorMessage, minBid) are kept as it
    # spells them, so one grep matches the summary, the event log and the captured body.
    return {
        "thread": result.thread_id,
        "role": result.role,
        "wave": result.wave_index,
        "endpoint": result.endpoint,
        "scheduled_offset_ms": result.scheduled_offset_ms,
        "actual_offset_ms": result.actual_offset_ms,
        "status": result.status,
        "isValid": result.is_valid,
        "errorType": result.error_type,
        "errorMessage": result.error_message,
        "minBid": result.min_bid,
        "rtt_ms": result.rtt_ms,
        "failure": result.failure.value,
        "corr_id": result.corr_id,
        "exception": result.exception,
    }


def _failure_taxonomy(
    results: Sequence[BidResult],
    gate: Optional["GateVerdict"],
    error: Optional[BaseException],
) -> List[str]:
    """Every distinct tag the run produced, including on a run that placed a bid.

    A ``BID_PLACED`` run routinely carries ``DUPLICATE`` and ``STATE_NOT_STARTED`` here.
    The list is the taxonomy of what happened, not a verdict on the run — ``outcome`` is
    the verdict.
    """
    tags = set()  # type: Set[str]
    for result in results:
        if result.failure is not Failure.OK:
            tags.add(result.failure.value)
    if gate is not None and gate.failure is not None:
        tags.add(gate.failure.value)
    if isinstance(error, SecopError):
        tags.add(error.code.value)
    elif error is not None:
        tags.add(Failure.UNKNOWN.value)
    return sorted(tags)


def _route(path: str) -> str:
    """Route identity: ``auction/GetAuction/{id}/{lang}`` -> ``auction/GetAuction``."""
    return "/".join(path.split("?", 1)[0].split("/")[:2])


def planned_routes(
    cfg: Config, ref: Optional[AuctionRef], waves: Sequence[Wave]
) -> "OrderedDict[str, str]":
    """The routes this configuration would call, keyed by their ``endpoints`` builder name."""
    auction_id = ref.auction_id if ref is not None else ""
    lang = ref.lang if ref is not None else "es-CO"

    validate_calls = sum(wave.validate + wave.validate_only for wave in waves)
    create_calls = 0 if cfg.dry_run else sum(wave.direct + wave.validate for wave in waves)

    used = OrderedDict()  # type: OrderedDict[str, str]
    used["get_auction_simple"] = endpoints.get_auction_simple(auction_id)
    used["get_auction"] = endpoints.get_auction(auction_id, lang)
    used["get_bids_total_info"] = endpoints.get_bids_total_info(auction_id)
    if validate_calls:
        used["validate_bid"] = endpoints.validate_bid()
    if create_calls:
        used["create_bid"] = endpoints.create_bid()
    if cfg.do_start_audit and (validate_calls or create_calls):
        used["set_start_bidding_audit"] = endpoints.set_start_bidding_audit(auction_id)
    if cfg.do_submit_audit:
        used["set_submit_bidding_audit"] = endpoints.set_submit_bidding_audit(auction_id)
    return used


def unverified_routes(
    cfg: Config, ref: Optional[AuctionRef], waves: Sequence[Wave]
) -> List[str]:
    """The routes this run used that were never seen on the wire (:data:`endpoints.OBSERVED_ON_WIRE`).

    ``bid/CreateBid`` is on this list for every live run: its path and casing come from the
    SPA's source alone, and a casing miss is a 404 that only shows up at the fire instant.
    Listing it in the summary is what makes that 404 diagnosable from the summary alone,
    without re-reading the capture.
    """
    return [
        _route(path)
        for name, path in planned_routes(cfg, ref, waves).items()
        if not endpoints.OBSERVED_ON_WIRE.get(name, False)
    ]


def _fire_time_vs_start_date_ms(
    t_wall: Optional[datetime], start_date: Optional[datetime]
) -> Optional[float]:
    """``startDate - T`` in ms, or ``None`` when the two cannot be compared.

    Same sign as ``schedule.check_fire_time_vs_start_date``: positive means the server's
    ``startDate`` is later than the operator's ``T``. A naive ``startDate`` is not compared
    at all — ``timeZoneCode`` is null in the capture, so there is no zone to assume.
    """
    if t_wall is None or start_date is None:
        return None
    if start_date.tzinfo is None or start_date.utcoffset() is None:
        return None
    return (start_date.astimezone(timezone.utc) - t_wall).total_seconds() * 1000.0


def build_summary(
    cfg: Config,
    *,
    run_id: Optional[str] = None,
    ref: Optional[AuctionRef] = None,
    detail: Optional[AuctionDetail] = None,
    payload: Optional[BidPayload] = None,
    armed: Optional["Armed"] = None,
    waves: Optional[Sequence[Wave]] = None,
    gate: Optional["GateVerdict"] = None,
    fire: Optional["FireOutcome"] = None,
    clock_estimate: Optional[ClockEstimate] = None,
    signalr_estimate: Optional[ClockEstimate] = None,
    secop_clock: Optional[ClockEstimate] = None,
    secop_record: Optional[Mapping[str, Any]] = None,
    clock_applied: Optional[bool] = None,
    host_ntp: Optional[str] = None,
    warnings: Optional[Sequence[str]] = None,
    error: Optional[BaseException] = None,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Assemble the §12.3 summary record.

    Every argument past ``cfg`` is optional so a run that died in preflight still produces a
    summary — the stages it never reached report ``null`` rather than blocking the record.

    ``clock_applied`` defaults to ``cfg.apply_clock_offset``. It is a parameter because only
    the caller knows whether the measured offset was actually used to place ``T``: the
    config key is a request, and a run declines it whenever the measurement is missing, too
    wide or too late.

    ``secop_record`` is what :func:`secop_firstbid.records.read_result` returned, or
    ``None`` when the record was not read. It is reported beside ``outcome`` and never
    folded into it: ``outcome`` is what this process saw, the record is what the server
    wrote, and a ``NO_VERDICT`` run is exactly the case where only the second one knows.
    """
    wave_list = list(waves) if waves is not None else (
        list(armed.waves) if armed is not None else []
    )
    results = list(fire.results) if fire is not None else []
    winner = fire.winner if fire is not None else None
    footprint = audit_footprint(cfg, wave_list)
    t_wall = armed.t_wall if armed is not None else None
    start_date = detail.start_date if detail is not None else None

    if payload is not None:
        items_count = len(payload.items)
        total_value = payload.total_value
    elif detail is not None:
        items_count = len(detail.items)
        total_value = None
    else:
        items_count = 0
        total_value = None

    summary = OrderedDict()  # type: OrderedDict[str, Any]
    summary["run_id"] = run_id
    summary["auction_id"] = ref.auction_id if ref is not None else cfg.auction_id
    summary["auction_version"] = detail.version_code if detail is not None else None
    summary["fire_time"] = _iso(t_wall) if t_wall is not None else cfg.fire_time
    summary["start_date"] = _iso(start_date)
    summary["fire_time_vs_start_date_ms"] = _fire_time_vs_start_date_ms(
        t_wall, start_date
    )
    summary["clock"] = _clock_block(
        cfg.apply_clock_offset if clock_applied is None else clock_applied,
        clock_estimate,
        signalr_estimate,
        host_ntp,
        secop_clock,
        armed.clock_shift_ms if armed is not None else 0.0,
    )
    summary["gate"] = _gate_block(cfg, gate)
    summary["mode"] = cfg.mode
    summary["schedule_planned"] = _schedule_planned(wave_list)
    summary["waves_planned"] = len(wave_list)
    summary["waves_fired"] = fire.waves_fired if fire is not None else 0
    summary["waves_dropped"] = fire.waves_dropped if fire is not None else 0
    summary["submissions_planned"] = footprint["submissions"]
    summary["submissions_sent"] = fire.submissions_sent if fire is not None else 0
    summary["audited_requests"] = footprint["audited_requests"]
    summary["outcome"] = _outcome(fire, error)
    summary["winner"] = _winner_block(winner)
    summary["bid_value"] = cfg.bid_value
    summary["items_count"] = items_count
    summary["total_value"] = total_value
    summary["my_last_bid"] = _money(detail.my_last_bid) if detail is not None else None
    summary["last_bid"] = _money(detail.last_bid) if detail is not None else None
    summary["secop_record"] = None if secop_record is None else dict(secop_record)
    summary["attempts"] = [_attempt(result) for result in results]
    summary["failure_taxonomy"] = _failure_taxonomy(results, gate, error)
    summary["unverified_routes"] = unverified_routes(cfg, ref, wave_list)
    summary["config_redacted"] = redacted_dict(cfg)
    summary["warnings"] = list(warnings) if warnings else []
    summary["started_at"] = _iso(started_at)
    summary["ended_at"] = _iso(ended_at if ended_at is not None else datetime.now(timezone.utc))

    if error is not None:
        summary["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "failure": error.code.value if isinstance(error, SecopError) else None,
        }
    return summary


def _summary_level(outcome: str) -> str:
    if outcome == OUTCOME_ERROR:
        return "ERROR"
    if outcome == OUTCOME_BID_PLACED:
        return "INFO"
    return "WARNING"


def write_summary(logger: "RunLogger", summary: Dict[str, Any]) -> Path:
    """Write ``summary.json`` into the run directory and return its path.

    The write goes through the logger's single writer thread like every other sink, and the
    call blocks on a flush before returning, so the returned path names a file that exists.
    """
    path = logger.write_json(SUMMARY_FILENAME, summary)
    outcome = str(summary.get("outcome"))
    logger.event(
        stage=STAGE,
        event="summary",
        level=_summary_level(outcome),
        detail={
            "outcome": outcome,
            "path": str(path),
            "waves_planned": summary.get("waves_planned"),
            "waves_fired": summary.get("waves_fired"),
            "submissions_planned": summary.get("submissions_planned"),
            "submissions_sent": summary.get("submissions_sent"),
            "audited_requests": summary.get("audited_requests"),
            "failure_taxonomy": summary.get("failure_taxonomy"),
            "unverified_routes": summary.get("unverified_routes"),
        },
    )
    logger.flush()
    return path


def _ms(value: Any) -> str:
    if value is None:
        return "?"
    return "%+.1f ms" % float(value)


def _plain_ms(value: Any) -> str:
    if value is None:
        return "?"
    return "%.1f ms" % float(value)


def _winner_line(summary: Mapping[str, Any]) -> str:
    winner = summary.get("winner")
    if not isinstance(winner, _AbcMapping):
        attempts = summary.get("attempts") or []
        offsets = [
            attempt.get("actual_offset_ms")
            for attempt in attempts
            if isinstance(attempt, _AbcMapping)
            and attempt.get("actual_offset_ms") is not None
        ]
        if not offsets:
            return "no bid was placed and nothing was sent"
        if summary.get("outcome") == OUTCOME_NO_VERDICT:
            return (
                "unknown — %d attempt(s) sent between %s and %s from T and none was "
                "answered; a request that timed out may still have been processed, so "
                "the server's own record decides"
                % (len(attempts), _ms(min(offsets)), _ms(max(offsets)))
            )
        return "no bid was placed; %d attempt(s) sent between %s and %s from T" % (
            len(attempts),
            _ms(min(offsets)),
            _ms(max(offsets)),
        )
    return "%s (%s) wave %s fired %s from T — bidId %s, rtt %s" % (
        winner.get("thread"),
        winner.get("role"),
        winner.get("wave"),
        _ms(winner.get("actual_offset_ms")),
        winner.get("bid_id"),
        _plain_ms(winner.get("rtt_ms")),
    )


def _warned_attempts(summary: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Attempts the classifier tagged ``VALIDATION_FAILED`` on an ``isValid:true`` body.

    Read off the tag the classifier already assigned, not off the body: this names which
    entry in ``failure_taxonomy`` an operator is looking at, it does not re-decide whether
    the bid landed.
    """
    attempts = summary.get("attempts") or []
    return [
        attempt
        for attempt in attempts
        if isinstance(attempt, _AbcMapping)
        and attempt.get("failure") == Failure.VALIDATION_FAILED.value
        and attempt.get("isValid") is True
    ]


def _warned_line(warned: Sequence[Mapping[str, Any]]) -> str:
    message = ""
    for attempt in warned:
        message = str(attempt.get("errorMessage") or "").strip()
        if message:
            break
    return (
        "%d attempt(s) came back isValid:true with a non-empty errorMessage and were "
        "tagged %s, not counted as a win%s"
        % (
            len(warned),
            Failure.VALIDATION_FAILED.value,
            ": %s" % message if message else "",
        )
    )


def _clock_line(summary: Mapping[str, Any]) -> str:
    clock = summary.get("clock")
    if not isinstance(clock, _AbcMapping):
        return "not measured"
    header = clock.get("date_header")
    header = header if isinstance(header, _AbcMapping) else {}
    applied = "applied to T" if clock.get("applied") else "not applied to T"
    if header.get("offset_ms") is None:
        return "no usable Date samples (%d taken), %s" % (
            int(header.get("samples") or 0),
            applied,
        )
    # The width is the whole feasible interval, not a symmetric error bar, so it is named
    # rather than written as "±": half of it would understate what is unknown.
    return "Date offset %s, midpoint of a %s-wide interval over %d samples, %s" % (
        _ms(header.get("offset_ms")),
        _plain_ms(header.get("residual_width_ms")),
        int(header.get("samples") or 0),
        applied,
    )


def _secop_clock_line(summary: Mapping[str, Any]) -> Optional[str]:
    """SECOP's log clock against this host's, or ``None`` when it was not looked for."""
    clock = summary.get("clock")
    if not isinstance(clock, _AbcMapping):
        return None
    stamped = clock.get("event_log")
    if not isinstance(stamped, _AbcMapping):
        return None
    shift = float(clock.get("shift_ms") or 0.0)
    moved = (
        "T moved %s onto it" % _ms(shift) if clock.get("applied") else "T not moved"
    )
    offset = stamped.get("offset_ms")
    if offset is None:
        return "not placed — the start audit's row was not read back; %s" % moved
    behind = "behind" if float(offset) < 0 else "ahead of"
    return "%s %s this host, a %s-wide interval over %d candidate row(s); %s" % (
        _plain_ms(abs(float(offset))),
        behind,
        _plain_ms(stamped.get("residual_width_ms")),
        int(stamped.get("candidates") or 0),
        moved,
    )


def _seconds(value: Any) -> str:
    if value is None:
        return "?"
    return "%+.2f s" % (float(value) / 1000.0)


def _secop_record_lines(summary: Mapping[str, Any]) -> List[str]:
    """What the server wrote down: who holds the accepted bid, and when ours were handled."""
    record = summary.get("secop_record")
    if not isinstance(record, _AbcMapping):
        return []

    lines = []  # type: List[str]
    bids = [bid for bid in (record.get("bids") or []) if isinstance(bid, _AbcMapping)]
    valid = [bid for bid in bids if bid.get("is_valid") is not False]
    if valid:
        for bid in valid:
            lines.append(
                "bid %s: %s by company %s (%s) at %s, %s from startDate"
                % (
                    bid.get("bid_id"),
                    bid.get("total_value"),
                    bid.get("company"),
                    "YOURS" if bid.get("own") else "not yours",
                    bid.get("date"),
                    _ms(bid.get("after_start_ms")),
                )
            )
    else:
        lines.append("no accepted bid in the bid list")

    own = record.get("own_bid_rows")
    own = own if isinstance(own, _AbcMapping) else {}
    count = int(own.get("count") or 0)
    if count:
        lines.append(
            "%d of your company's bids were processed, the first %s and the last %s "
            "from startDate%s"
            % (
                count,
                _seconds(own.get("first_after_start_ms")),
                _seconds(own.get("last_after_start_ms")),
                " (the log had more rows than were read)"
                if record.get("event_log_truncated")
                else "",
            )
        )
    else:
        lines.append("the event log holds no processed bid of your company")

    unread = record.get("unread") or []
    if unread:
        lines.append("could not read: %s" % ", ".join(str(name) for name in unread))
    return lines


def _gate_line(summary: Mapping[str, Any]) -> str:
    gate = summary.get("gate")
    if not isinstance(gate, _AbcMapping):
        return "?"
    return "%s: state=%s issued %s, proceed=%s" % (
        gate.get("mode"),
        gate.get("state"),
        _ms(gate.get("issued_offset_ms")),
        gate.get("proceed"),
    )


def human_summary(summary: Mapping[str, Any]) -> str:
    """What a person needs in the ten seconds after a run.

    Outcome, who won and how far from ``T`` it actually fired, what the run cost in rows the
    auction owner can read, and every warning that fired — including the ones raised before
    the schedule was armed, which are otherwise buried in ``events.jsonl``.

    The ``warned`` line appears only when the run holds the one response shape the capture
    cannot resolve — ``isValid:true`` with a non-empty ``errorMessage`` — because that is
    the case where "no bid was placed" is otherwise unexplained: the server said valid and
    the run still counted no winner.
    """
    warnings = summary.get("warnings") or []
    unverified = summary.get("unverified_routes") or []
    taxonomy = summary.get("failure_taxonomy") or []
    warned = _warned_attempts(summary)

    lines = [
        "%s  run %s  auction %s (%s)"
        % (
            summary.get("outcome"),
            summary.get("run_id"),
            summary.get("auction_id"),
            summary.get("auction_version"),
        ),
        "  T           %s  (mode %s, %s wave(s), %s fired, %s dropped)"
        % (
            summary.get("fire_time"),
            summary.get("mode"),
            summary.get("waves_planned"),
            summary.get("waves_fired"),
            summary.get("waves_dropped"),
        ),
        "  winner      %s" % _winner_line(summary),
    ]

    if warned:
        lines.append("  warned      %s" % _warned_line(warned))

    lines += [
        "  bid         value %s across %s item(s), totalValue %s"
        % (
            summary.get("bid_value"),
            summary.get("items_count"),
            summary.get("total_value"),
        ),
        "  footprint   %s of %s submissions sent, %s audited requests "
        "(validate-only threads and the run's one start audit included)"
        % (
            summary.get("submissions_sent"),
            summary.get("submissions_planned"),
            summary.get("audited_requests"),
        ),
        "  gate        %s" % _gate_line(summary),
        "  clock       %s" % _clock_line(summary),
    ]

    secop_clock = _secop_clock_line(summary)
    if secop_clock:
        lines.append("  SECOP clock %s" % secop_clock)
    for index, line in enumerate(_secop_record_lines(summary)):
        lines.append("  %s %s" % ("SECOP says " if index == 0 else "           ", line))

    lines += [
        "  tags        %s" % (", ".join(taxonomy) if taxonomy else "none"),
        "  unverified  %s" % (", ".join(unverified) if unverified else "none"),
    ]

    if warnings:
        lines.append("  warnings    %d" % len(warnings))
        lines.extend("    - %s" % warning for warning in warnings)
    else:
        lines.append("  warnings    none")

    return "\n".join(lines)


__all__ = [
    "OUTCOMES",
    "OUTCOME_ALL_REJECTED",
    "OUTCOME_BID_PLACED",
    "OUTCOME_ERROR",
    "OUTCOME_NOT_FIRED",
    "OUTCOME_NO_VERDICT",
    "STAGE",
    "SUMMARY_FILENAME",
    "build_summary",
    "human_summary",
    "planned_routes",
    "unverified_routes",
    "write_summary",
]
