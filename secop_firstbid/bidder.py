"""Stage 6 — the fire path: thread roles, per-wave release, first-success arbitration
(plan §11).

Three properties here are the ones that decide whether a run's *report* is true, and they
are the reason this module owns them instead of leaving them to the call sites.

**One success predicate.** :func:`is_success` — ``isValid is True`` *and* an empty
``errorMessage`` — is the body half of "did this land". It is defined in
:mod:`secop_firstbid.errors` and re-exported here, because ``errors.classify`` answers
``OK`` on the same predicate: the plan stated the rule three times and two of them dropped
the ``errorMessage`` half, which would make a ``CreateBid`` answering ``isValid:true`` with
a warning a win in one place and not a win in another. ``stop_on_success`` cancels the rest
of the schedule on that answer, so the two readings do not merely disagree in a log line:
they fire different numbers of bids.

**A win is ``Failure.OK`` — the whole response, never the body on its own.** ``classify``
answers ``OK`` only within the statuses that can carry a verdict at all: 2xx, or a
bid-shaped 400/422. A 500, 502, 503, 401, 403 or 429 carrying a success-shaped body is
therefore ``UNKNOWN``, ``AUTH_EXPIRED``, ``WAF_BLOCK`` or ``RATE_LIMIT``, and re-reading its
body here would latch it as the winner, cancel every unreleased wave and print
``BID_PLACED`` over an attempt row that says the request failed. Nothing in the capture is a
successful ``CreateBid`` (``docs/open-questions.md`` Q5), so what a real win looks like is
unverified; what is not in doubt is that a status the server uses to say "not answered" is
not one.

**Send ordering is the only thing that separates a duplicate from a rejection.** A post-win
bounce and a genuine ``BidErrMin`` are byte-identical on the wire — same status, same
``errorMessage``, same ``minBid``. Every attempt released after the win is therefore
classified with ``RunState(sent_after_first_success=True)``; without it the taxonomy calls
every post-win wave a minimum-bid failure.

**Only a ``CreateBid`` can win.** A ``ValidateBid`` that passes is a prediction, not a bid,
and never sets the first-success latch.

**Nothing shares a thread's connection with its bid at the release instant.** Each thread
owns one :class:`~secop_firstbid.session.HttpClient`, and that client owns one pooled
connection, so any other request the same thread makes is *serial* with its bid: the bid
cannot leave until the earlier response comes back, or until it times out.

``SetStartBiddingAudit`` taught that twice. Issued inline immediately before the
submission, it put a full round trip between the release instant and the bid leaving the
socket. Moved ``start_audit_lead_ms`` ahead of each thread's release, it was fine for as
long as the server answered — and on 2026-11-20, with the server refusing or stalling
connections in the seconds before two openings, an audit that hung for its whole connect or
read timeout took its thread's bid with it: 5 to 16 bids per run left more than half a
second late, some by ten seconds. A lead is not isolation.

So the fire threads send **nothing but bids**. The run's one start audit is sent by
:class:`StartAudit` on a client no bid uses, and a fire thread's only other request,
a V or P thread's ``ValidateBid``, is the request its release instant positions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping as _AbcMapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from . import endpoints
from .config import Config, start_audit_planned
from .errors import Failure, RunState, SecopError, classify, is_success
from .logging_setup import RunLogger
from .models import AuctionDetail, AuctionRef, BidResult, Wave
from .schedule import Armed, schedule_span_ms, sleep_until_mono
from .session import HttpClient, HttpOutcome

if TYPE_CHECKING:  # pragma: no cover
    from .gate import GateVerdict

STAGE = "fire"

# §11.1 roles.
ROLE_DIRECT = "D"
ROLE_VALIDATE_SUBMIT = "V"
ROLE_VALIDATE_ONLY = "P"
ROLE_START_AUDIT = "audit"
START_AUDIT_THREAD_ID = "start-audit"

ENDPOINT_CREATE = "CreateBid"
ENDPOINT_VALIDATE = "ValidateBid"

# Re-exported from endpoints, which owns what the capture measured about this API. The
# minimum is the floor on how quickly any verdict can come back, so it is also the floor on
# the schedule span at which cancelling a later wave is physically possible.
OBSERVED_MIN_BID_RTT_MS = endpoints.OBSERVED_MIN_BID_RTT_MS
OBSERVED_P50_BID_RTT_MS = endpoints.OBSERVED_P50_BID_RTT_MS

_BARRIER_TIMEOUT_S = 0.25
_JOIN_MARGIN_S = 5.0
_MS_PER_S = 1000.0
_NS_PER_S = 1e9


class FirstSuccess:
    """The winner latch: the first result marked wins and no later one can displace it.

    Thread-safe. :meth:`mark` is a compare-and-set under a lock; :meth:`seen` is an
    unlocked read of a plain bool, because it is called on the send path of every attempt.
    A read that is stale by microseconds costs one attempt the ``DUPLICATE`` tag it would
    otherwise have got; taking the lock there would serialise the burst it exists to time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._winner = None  # type: Optional[BidResult]
        self._seen = False

    def mark(self, r: BidResult) -> bool:
        with self._lock:
            if self._winner is not None:
                return False
            self._winner = r
            self._seen = True
            return True

    def seen(self) -> bool:
        return self._seen

    @property
    def winner(self) -> Optional[BidResult]:
        with self._lock:
            return self._winner


@dataclass
class FireOutcome:
    """What the schedule did.

    ``waves_fired + waves_dropped == len(waves)``. A wave is *fired* once it reaches its
    release instant uncancelled — whether its threads then send anything is a matter of
    ``dry_run`` and the validate gate, and is reported separately by ``submissions_sent``.
    ``waves_dropped`` counts only waves ``stop_on_success`` (or a blocking gate) cancelled
    before release.
    """

    results: List[BidResult]
    winner: Optional[BidResult]
    waves_fired: int
    waves_dropped: int
    submissions_sent: int


@dataclass(frozen=True)
class _Attempt:
    wave: Wave
    role: str
    thread_id: str
    client: HttpClient
    own_release_mono_ns: int
    wave_release_mono_ns: int
    barrier: threading.Barrier


def _as_bool(body: Optional[Mapping[str, Any]], key: str) -> Optional[bool]:
    if body is None:
        return None
    value = body.get(key)
    return value if isinstance(value, bool) else None


def _as_int(body: Optional[Mapping[str, Any]], key: str) -> Optional[int]:
    if body is None:
        return None
    value = body.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, Decimal):
        try:
            return int(value)
        except (ValueError, ArithmeticError):
            return None
    return None


def _as_str(body: Optional[Mapping[str, Any]], key: str) -> Optional[str]:
    if body is None:
        return None
    value = body.get(key)
    return value if isinstance(value, str) else None


def _exception_text(exc: Optional[BaseException]) -> Optional[str]:
    if exc is None:
        return None
    return "%s: %s" % (type(exc).__name__, exc)


def _thread_id(wave_index: int, role: str, slot: int) -> str:
    return "w%02d-%s%02d" % (wave_index, role, slot)


def _submitters(waves: List[Wave]) -> int:
    return sum(wave.direct + wave.validate for wave in waves)


class _Fire:
    """The mutable state one :func:`run_schedule` call shares across its threads."""

    def __init__(
        self,
        cfg: Config,
        armed: Armed,
        ref: AuctionRef,
        payload_bytes: bytes,
        logger: RunLogger,
    ) -> None:
        self.cfg = cfg
        self.armed = armed
        self.ref = ref
        self.payload_bytes = payload_bytes
        self.logger = logger

        self.first_success = FirstSuccess()
        self.cancel = threading.Event()

        self._results = []  # type: List[BidResult]
        self._results_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._fired_waves = set()  # type: Set[int]
        # thread_id -> (endpoint, send instant) for a request that has left and not yet
        # returned; it is what an abandoned thread's placeholder row is built from.
        self._in_flight = {}  # type: Dict[str, Tuple[str, int]]
        self.submissions_sent = 0
        self.validate_sent = 0
        # Issued by StartAudit, outside the schedule; read off it once the threads have
        # joined so the footprint events can state the whole of what the run cost.
        self.start_audits_sent = 0

    @property
    def results(self) -> List[BidResult]:
        with self._results_lock:
            return list(self._results)

    def fired_wave_indices(self) -> Set[int]:
        with self._count_lock:
            return set(self._fired_waves)

    def work(self, attempt: _Attempt) -> None:
        try:
            if not self._release(attempt):
                return
            if attempt.role == ROLE_DIRECT:
                self._direct(attempt)
            else:
                self._validate_first(attempt)
        except Exception as exc:  # a thread that dies silently is a bid nobody can explain
            self.logger.event(
                stage=STAGE,
                event="thread_error",
                level="ERROR",
                thread_id=attempt.thread_id,
                role=attempt.role,
                detail={
                    "wave_index": attempt.wave.index,
                    "exception": _exception_text(exc),
                },
            )

    def _release(self, attempt: _Attempt) -> bool:
        sleep_until_mono(attempt.own_release_mono_ns, cancel=self.cancel)
        if self.cancel.is_set():
            self._decide(
                attempt,
                "wave_cancelled",
                "wave %d was cancelled before its release instant: a bid already succeeded "
                "and stop_on_success is on, so no bid was sent and this thread wrote no row. "
                "The run's start audit is sent once, ahead of every wave, and stands."
                % attempt.wave.index,
            )
            return False

        with self._count_lock:
            self._fired_waves.add(attempt.wave.index)

        self.logger.timeline(
            kind="wave_release",
            stage=STAGE,
            thread_id=attempt.thread_id,
            role=attempt.role,
            wave_index=attempt.wave.index,
            scheduled_offset_ms=self.armed.offset_from_t_ms(attempt.own_release_mono_ns),
            actual_offset_ms=self.armed.offset_from_t_ms(time.monotonic_ns()),
        )

        try:
            attempt.barrier.wait(timeout=_BARRIER_TIMEOUT_S)
        except threading.BrokenBarrierError:
            self.logger.event(
                stage=STAGE,
                event="release_barrier_broken",
                level="WARNING",
                thread_id=attempt.thread_id,
                role=attempt.role,
                detail={
                    "wave_index": attempt.wave.index,
                    "note": "a peer sharing this release instant did not arrive; firing "
                    "alone rather than holding a released wave",
                },
            )
        return True

    def _direct(self, attempt: _Attempt) -> None:
        if self.cfg.dry_run:
            self._decide(
                attempt,
                "dry_run_skip_create_bid",
                "dry_run: no CreateBid is sent by any role. A D thread's only request is "
                "the submission, so this thread issues nothing and writes no audit row.",
            )
            return
        self._submit(attempt)

    def _validate_first(self, attempt: _Attempt) -> None:
        result = self._bid(
            attempt,
            endpoints.validate_bid(),
            ENDPOINT_VALIDATE,
            attempt.own_release_mono_ns,
        )

        if attempt.role == ROLE_VALIDATE_ONLY:
            return

        if self.cfg.dry_run:
            self._decide(
                attempt,
                "dry_run_skip_create_bid",
                "dry_run: no CreateBid is sent. The ValidateBid above was still sent and "
                "still wrote a buyer-visible Logs_Search_NewBid row under this IP and "
                "company code; dry_run is not footprint-free.",
            )
            return

        if self.cfg.v_gate_on_valid and not is_success(result.raw or {}):
            self._decide(
                attempt,
                "validate_gate_blocked",
                "ValidateBid did not pass the success predicate (isValid true and an empty "
                "errorMessage), so this V thread self-aborts instead of submitting",
                extra={
                    "is_valid": result.is_valid,
                    "error_type": result.error_type,
                    "error_message_locale": result.error_message_locale,
                    "min_bid": result.min_bid,
                    "failure": result.failure.value,
                },
            )
            return

        self._submit(attempt)

    def _submit(self, attempt: _Attempt) -> None:
        result = self._bid(
            attempt,
            endpoints.create_bid(),
            ENDPOINT_CREATE,
            attempt.wave_release_mono_ns,
        )
        # The win is the classifier's verdict, not the body alone: a 500, 503, 401, 403 or
        # 429 carrying a success-shaped body is tagged UNKNOWN / AUTH_EXPIRED / WAF_BLOCK /
        # RATE_LIMIT, and latching it would cancel the remaining waves and report a bid this
        # run cannot show was placed.
        if result.failure is not Failure.OK:
            return
        if self.first_success.mark(result):
            self._announce(result)

    def _announce(self, result: BidResult) -> None:
        self.logger.event(
            stage=STAGE,
            event="first_success",
            thread_id=result.thread_id,
            role=result.role,
            corr_id=result.corr_id,
            detail={
                "wave_index": result.wave_index,
                "scheduled_offset_ms": result.scheduled_offset_ms,
                "actual_offset_ms": result.actual_offset_ms,
                "bid_id": result.bid_id,
                "ranking": result.ranking,
                "rtt_ms": result.rtt_ms,
            },
        )
        self.logger.timeline(
            kind="first_success",
            stage=STAGE,
            thread_id=result.thread_id,
            role=result.role,
            wave_index=result.wave_index,
            corr_id=result.corr_id,
            bid_id=result.bid_id,
        )
        if not self.cfg.stop_on_success:
            return
        self.cancel.set()
        self.logger.event(
            stage=STAGE,
            event="stop_on_success_triggered",
            thread_id=result.thread_id,
            role=result.role,
            detail={
                "winning_wave_index": result.wave_index,
                "note": "every wave that has not yet reached its release instant is "
                "cancelled; a wave already released runs to completion",
            },
        )

    def _bid(
        self,
        attempt: _Attempt,
        path: str,
        endpoint: str,
        scheduled_release_mono_ns: int,
    ) -> BidResult:
        # Read before the request leaves: an attempt that started after the win is a
        # duplicate even if the verdict arrives while the latch is still being set.
        sent_after_first_success = self.first_success.seen()

        with self._count_lock:
            # Counted at send time. A request that times out may still have reached the
            # server and written its row, so it counts as sent.
            if endpoint == ENDPOINT_CREATE:
                self.submissions_sent += 1
            else:
                self.validate_sent += 1
        with self._results_lock:
            self._in_flight[attempt.thread_id] = (endpoint, time.monotonic_ns())

        outcome = attempt.client.post_json(
            path,
            self.payload_bytes,
            stage=STAGE,
            thread_id=attempt.thread_id,
            role=attempt.role,
        )

        run_state = RunState(
            first_success_seen=self.first_success.seen(),
            sent_after_first_success=sent_after_first_success,
        )
        result = self._result(
            attempt,
            outcome,
            endpoint=endpoint,
            scheduled_release_mono_ns=scheduled_release_mono_ns,
            run_state=run_state,
        )
        with self._results_lock:
            # Clearing the in-flight mark and recording the row is one step, so an
            # abandonment sweep cannot see a request as both answered and outstanding.
            self._in_flight.pop(attempt.thread_id, None)
            self._results.append(result)

        self.logger.event(
            stage=STAGE,
            event="bid_result",
            level="INFO" if result.failure in (Failure.OK, Failure.DUPLICATE) else "WARNING",
            thread_id=attempt.thread_id,
            role=attempt.role,
            corr_id=result.corr_id,
            detail={
                "wave_index": attempt.wave.index,
                "endpoint": endpoint,
                "status": result.status,
                "failure": result.failure.value,
                "is_valid": result.is_valid,
                "bid_id": result.bid_id,
                # Preserved verbatim: an errorType this app has no mapping for must survive
                # into the log unchanged.
                "error_type": result.error_type,
                "error_message_locale": result.error_message_locale,
                "min_bid": result.min_bid,
                "ranking": result.ranking,
                "scheduled_offset_ms": result.scheduled_offset_ms,
                "actual_offset_ms": result.actual_offset_ms,
                "rtt_ms": result.rtt_ms,
                "sent_after_first_success": sent_after_first_success,
                "exception": result.exception,
            },
        )
        return result

    def _result(
        self,
        attempt: _Attempt,
        outcome: HttpOutcome,
        *,
        endpoint: str,
        scheduled_release_mono_ns: int,
        run_state: RunState
    ) -> BidResult:
        body = outcome.body if isinstance(outcome.body, _AbcMapping) else None
        return BidResult(
            thread_id=attempt.thread_id,
            role=attempt.role,
            wave_index=attempt.wave.index,
            scheduled_offset_ms=self.armed.offset_from_t_ms(scheduled_release_mono_ns),
            actual_offset_ms=self.armed.offset_from_t_ms(outcome.t_send_mono_ns),
            endpoint=endpoint,
            status=outcome.status,
            is_valid=_as_bool(body, "isValid"),
            bid_id=_as_int(body, "bidId"),
            error_type=_as_str(body, "errorType"),
            error_message=_as_str(body, "errorMessage"),
            error_message_locale=_as_str(body, "errorMessageLocale"),
            min_bid=_as_str(body, "minBid"),
            ranking=_as_int(body, "currentRankingPosition"),
            t_send_mono_ns=outcome.t_send_mono_ns,
            t_recv_mono_ns=outcome.t_recv_mono_ns,
            rtt_ms=outcome.rtt_ms,
            failure=classify(
                outcome.status,
                outcome.body,
                outcome.exception,
                run_state,
                outcome.text,
            ),
            corr_id=outcome.corr_id,
            raw=dict(body) if body is not None else None,
            exception=_exception_text(outcome.exception),
        )

    def abandon(self, attempt: _Attempt) -> BidResult:
        """Record the row a thread abandoned past the join deadline never wrote.

        A request already in flight was counted at send time and may have been received —
        and its bid counted — by the server, so the attempt cannot be reported as "not
        sent". Without this row the summary shows a submission with no attempt, no verdict
        and no error: a bid that may have landed with nothing in the record naming it.

        ``NETWORK`` is the tag because what is known is that no verdict came back.
        """
        with self._results_lock:
            in_flight = self._in_flight.pop(attempt.thread_id, None)
            if in_flight is None:
                endpoint = ""
                t_send_mono_ns = attempt.own_release_mono_ns
                actual_offset_ms = None  # type: Optional[float]
                note = (
                    "thread abandoned past the join deadline with no bid request in "
                    "flight; whatever it was doing produced no verdict in this run"
                )
            else:
                endpoint, t_send_mono_ns = in_flight
                actual_offset_ms = self.armed.offset_from_t_ms(t_send_mono_ns)
                note = (
                    "thread abandoned past the join deadline with a %s in flight; the "
                    "request may have been received and the bid counted by the server, "
                    "and no verdict will reach this run" % endpoint
                )
            scheduled_release_mono_ns = (
                attempt.wave_release_mono_ns
                if endpoint == ENDPOINT_CREATE
                else attempt.own_release_mono_ns
            )
            result = BidResult(
                thread_id=attempt.thread_id,
                role=attempt.role,
                wave_index=attempt.wave.index,
                scheduled_offset_ms=self.armed.offset_from_t_ms(
                    scheduled_release_mono_ns
                ),
                actual_offset_ms=actual_offset_ms,
                endpoint=endpoint,
                status=None,
                is_valid=None,
                bid_id=None,
                error_type=None,
                error_message=None,
                error_message_locale=None,
                min_bid=None,
                ranking=None,
                t_send_mono_ns=t_send_mono_ns,
                t_recv_mono_ns=None,
                rtt_ms=None,
                failure=Failure.NETWORK,
                exception=note,
            )
            self._results.append(result)
        return result

    def _decide(
        self,
        attempt: _Attempt,
        decision: str,
        reason: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        detail = {
            "wave_index": attempt.wave.index,
            "decision": decision,
            "reason": reason,
            "scheduled_offset_ms": self.armed.offset_from_t_ms(
                attempt.own_release_mono_ns
            ),
        }  # type: Dict[str, Any]
        if extra:
            detail.update(extra)
        self.logger.event(
            stage=STAGE,
            event="thread_decision",
            thread_id=attempt.thread_id,
            role=attempt.role,
            detail=detail,
        )


def require_live_confirmation(cfg: Config, waves: List[Wave]) -> None:
    """Refuse to arm a run that would submit without ``confirm_live``.

    Checked before a single thread starts, so the refusal happens while there is still
    time to fix the config rather than at the release instant. A caller that sends
    anything ahead of the schedule — the start audit, the keep-alives — must call this
    first: a run that is going to refuse has no business writing a row on its way there.
    """
    if cfg.dry_run:
        return
    submitters = _submitters(waves)
    if submitters == 0:
        return
    if cfg.confirm_live:
        return
    raise SecopError(
        Failure.CONFIG_INVALID,
        "confirm_live is False but this schedule plans %d real CreateBid submission(s) "
        "across %d wave(s) and dry_run is off. Set confirm_live (CLI --yes-i-mean-it) to "
        "send a live bid, or set dry_run to rehearse." % (submitters, len(waves)),
        {
            "submissions_planned": submitters,
            "waves": len(waves),
            "dry_run": cfg.dry_run,
            "confirm_live": cfg.confirm_live,
        },
    )


def _log_stop_on_success_reach(cfg: Config, waves: List[Wave], logger: RunLogger) -> None:
    """Record, before firing, how many waves ``stop_on_success`` can actually drop.

    A wave is only droppable if an earlier wave's verdict can return before that wave's
    release. The fastest bid-endpoint response anywhere in the capture is 168.5 ms and the
    median is 614.4 ms, while the recommended burst (``pre_ms=60``, ``interval_ms=30``)
    spans 90 ms end to end — wave 0's verdict cannot return before wave 3 has fired, so at
    the recommended densities this switch drops nothing and saves no audit row.
    """
    span_ms = schedule_span_ms(waves)
    first_offset_ms = min(wave.offset_ms for wave in waves)
    droppable = [
        wave.index
        for wave in waves
        if wave.offset_ms - first_offset_ms > OBSERVED_MIN_BID_RTT_MS
    ]
    logger.event(
        stage=STAGE,
        event="stop_on_success_reach",
        detail={
            "stop_on_success": cfg.stop_on_success,
            "schedule_span_ms": span_ms,
            "observed_min_bid_rtt_ms": OBSERVED_MIN_BID_RTT_MS,
            "observed_p50_bid_rtt_ms": OBSERVED_P50_BID_RTT_MS,
            "waves_droppable_at_min_rtt": droppable,
            "note": "a wave can only be dropped if an earlier wave's verdict returns "
            "before it releases. The schedule spans %.1f ms; the fastest bid response in "
            "the capture is %.1f ms and the median is %.1f ms. At a span below the "
            "minimum, stop_on_success drops nothing and saves no audit row."
            % (span_ms, OBSERVED_MIN_BID_RTT_MS, OBSERVED_P50_BID_RTT_MS),
        },
    )


def _plan(
    cfg: Config,
    armed: Armed,
    waves: List[Wave],
    clients: List[HttpClient],
    logger: RunLogger,
) -> List[_Attempt]:
    """Assign every planned thread its client, its release instant and its barrier.

    Clients are handed out round-robin over the *whole* schedule, not per wave: at the
    recommended densities the waves overlap (30 ms apart against a 614 ms median response),
    so a client reused by a later wave would be a ``requests.Session`` in two threads at
    once. When there are fewer clients than threads that is exactly what happens, and the
    caller is told so rather than being silently given a shared Session.
    """
    composition = (
        (ROLE_VALIDATE_SUBMIT, "validate"),
        (ROLE_VALIDATE_ONLY, "validate_only"),
        (ROLE_DIRECT, "direct"),
    )

    slots = []  # type: List[Tuple[Wave, str, str, HttpClient, int, int]]
    group_sizes = {}  # type: Dict[Tuple[int, int], int]
    index = 0
    for wave in waves:
        wave_release = armed.wave_release_mono_ns(wave, cfg.lead_ms)
        # V and P start early enough that their CreateBid lands near the wave's instant.
        early_release = wave_release - int(round(cfg.validate_lead_ms * 1e6))
        for role, field_name in composition:
            for slot in range(getattr(wave, field_name)):
                own_release = wave_release if role == ROLE_DIRECT else early_release
                slots.append(
                    (
                        wave,
                        role,
                        _thread_id(wave.index, role, slot),
                        clients[index % len(clients)],
                        own_release,
                        wave_release,
                    )
                )
                key = (wave.index, own_release)
                group_sizes[key] = group_sizes.get(key, 0) + 1
                index += 1

    barriers = dict(
        (key, threading.Barrier(size)) for key, size in group_sizes.items()
    )  # type: Dict[Tuple[int, int], threading.Barrier]

    attempts = [
        _Attempt(
            wave=wave,
            role=role,
            thread_id=thread_id,
            client=client,
            own_release_mono_ns=own_release,
            wave_release_mono_ns=wave_release,
            barrier=barriers[(wave.index, own_release)],
        )
        for wave, role, thread_id, client, own_release, wave_release in slots
    ]

    if attempts and len(clients) < len(attempts):
        logger.event(
            stage=STAGE,
            event="client_reuse",
            level="WARNING",
            detail={
                "clients": len(clients),
                "threads": len(attempts),
                "note": "fewer HttpClients than threads: at least one requests.Session "
                "will be used by two threads, which is not thread-safe. Build one client "
                "per planned thread (%d) before arming." % len(attempts),
            },
        )
    return attempts


def first_release_mono_ns(cfg: Config, armed: Armed) -> Optional[int]:
    """The earliest instant any fire thread wakes, or ``None`` for a schedule with no thread.

    Everything that has to be over before the bids — the start audit, the keep-alives —
    is placed against this instant rather than against ``T``, because a schedule may open
    seconds before ``T`` and a V or P thread opens ``validate_lead_ms`` earlier still.
    """
    releases = []  # type: List[int]
    for wave in armed.waves:
        wave_release = armed.wave_release_mono_ns(wave, cfg.lead_ms)
        if wave.direct:
            releases.append(wave_release)
        if wave.validate or wave.validate_only:
            releases.append(wave_release - int(round(cfg.validate_lead_ms * 1e6)))
    return min(releases) if releases else None


class StartAudit:
    """The run's one ``SetStartBiddingAudit``, sent from a thread and a client of its own.

    ``client`` must be a client no fire thread uses. That is the whole point: the audit
    writes its row from a connection of its own, so however long the server takes to answer
    it — or to refuse it — no bid waits behind it. It still shares the run's cookie jar,
    and with it the gateway affinity that lands it on the backend the bids will reach.

    It is sent ``start_audit_lead_ms`` before the first release; a run armed later than
    that sends it at once. A failure is not raised and the audit is not sent again:
    ``post_empty`` records the exchange either way, a second attempt would be a second row
    if the first was in fact received, and whether the server needs the row before a bid is
    unverified (``docs/open-questions.md``).

    ``on_sent`` runs on the audit's thread with the finished exchange, on the same client.
    It exists for the read-back that places SECOP's clock: the send and receive instants
    of this exchange bracket the moment the server stamped the row. Whatever it returns is
    kept in :attr:`readback`; an exception in it is logged and costs only the read-back.
    """

    def __init__(
        self,
        cfg: Config,
        armed: Armed,
        ref: AuctionRef,
        client: HttpClient,
        logger: RunLogger,
        on_sent: Optional[Callable[[HttpOutcome], Any]] = None,
    ) -> None:
        self.cfg = cfg
        self.armed = armed
        self.ref = ref
        self.client = client
        self.logger = logger
        self.on_sent = on_sent
        self.cancel = threading.Event()
        self.outcome = None  # type: Optional[HttpOutcome]
        self.readback = None  # type: Any
        self._issued = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]

    @property
    def planned(self) -> bool:
        return (
            start_audit_planned(self.cfg, list(self.armed.waves))
            and first_release_mono_ns(self.cfg, self.armed) is not None
        )

    @property
    def sent(self) -> int:
        """1 once the request has been issued, whether or not an answer came back."""
        return 1 if self._issued.is_set() else 0

    def start(self) -> None:
        if not self.planned or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=START_AUDIT_THREAD_ID, daemon=True
        )
        self._thread.start()

    def join(self, timeout_s: Optional[float] = None) -> bool:
        """Wait for the audit and its read-back; ``True`` when both are over."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._send()
        except Exception as exc:  # an audit thread that dies silently is a missing row
            self.logger.event(
                stage=STAGE,
                event="start_audit_error",
                level="ERROR",
                thread_id=START_AUDIT_THREAD_ID,
                role=ROLE_START_AUDIT,
                detail={"exception": _exception_text(exc)},
            )

    def _send(self) -> None:
        first_release = first_release_mono_ns(self.cfg, self.armed)
        if first_release is None:
            return
        sleep_until_mono(
            first_release - int(round(self.cfg.start_audit_lead_ms * 1e6)),
            cancel=self.cancel,
        )
        if self.cancel.is_set():
            return
        self._issued.set()
        outcome = self.client.post_empty(
            endpoints.set_start_bidding_audit(self.ref.auction_id),
            stage=STAGE,
            thread_id=START_AUDIT_THREAD_ID,
            role=ROLE_START_AUDIT,
        )
        self.outcome = outcome
        self.logger.event(
            stage=STAGE,
            event="start_audit",
            level="INFO" if outcome.status == 200 else "WARNING",
            thread_id=START_AUDIT_THREAD_ID,
            role=ROLE_START_AUDIT,
            corr_id=outcome.corr_id,
            detail={
                "status": outcome.status,
                "rtt_ms": outcome.rtt_ms,
                "offset_from_t_ms": self.armed.offset_from_t_ms(outcome.t_send_mono_ns),
                "exception": _exception_text(outcome.exception),
                "note": "one start audit for the whole run, on a connection no bid "
                "uses; it is not sent again if it failed",
            },
        )
        if self.on_sent is not None:
            self.readback = self.on_sent(outcome)


def run_schedule(
    cfg: Config,
    armed: Armed,
    ref: AuctionRef,
    detail: AuctionDetail,
    payload_bytes: bytes,
    clients: List[HttpClient],
    logger: RunLogger,
    gate: "GateVerdict",
    start_audit: Optional[StartAudit] = None,
) -> FireOutcome:
    """Fire the armed schedule and return what happened.

    Every thread is created and started up front; each one sleeps on the monotonic clock to
    its own release instant, meets the peers sharing that instant at a barrier, and fires.
    Waves are therefore free to overlap, which they must: at the recommended 30 ms spacing
    the previous wave is still in flight.

    One thread per planned attempt, and ``clients`` is expected to hold one
    :class:`~secop_firstbid.session.HttpClient` per thread, each pre-warmed by
    :func:`~secop_firstbid.session.prewarm`. Nothing is shared on the send path, so a thread
    waiting on its own response never delays another thread's release: the schedule's
    spacing is a property of the release instants alone. The one request a thread makes
    besides its bid — a V thread's ``ValidateBid`` — is serial with that bid over its single
    connection, which is why ``validate_lead_ms`` exists. The start audit is not sent from
    here at all: :class:`StartAudit` issues it once, earlier, on another client, and is
    passed in only so the footprint events below can state the whole of what the run cost.

    Cancellation is per wave and only before release. ``stop_on_success`` stops waves that
    have not yet woken; it never interrupts a wave that is already running, so a V thread
    whose ValidateBid has returned still submits.
    """
    waves = list(armed.waves)

    require_live_confirmation(cfg, waves)

    if not waves:
        logger.event(
            stage=STAGE,
            event="fire_skipped",
            level="WARNING",
            detail={"reason": "the schedule contains no waves"},
        )
        return FireOutcome([], None, 0, 0, 0)

    if not clients:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "run_schedule needs at least one HttpClient; build one per planned thread",
            {"waves": len(waves)},
        )

    planned_submissions = _submitters(waves)
    planned_validates = sum(wave.validate + wave.validate_only for wave in waves)

    logger.event(
        stage=STAGE,
        event="fire_start",
        detail={
            "t_wall": armed.t_wall.isoformat(),
            "waves": [
                {
                    "index": wave.index,
                    "offset_ms": wave.offset_ms,
                    "direct": wave.direct,
                    "validate": wave.validate,
                    "validate_only": wave.validate_only,
                }
                for wave in waves
            ],
            "schedule_span_ms": schedule_span_ms(waves),
            "submissions_planned": planned_submissions,
            "validate_calls_planned": planned_validates,
            "lead_ms": cfg.lead_ms,
            "validate_lead_ms": cfg.validate_lead_ms,
            "start_audit_lead_ms": cfg.start_audit_lead_ms,
            "dry_run": cfg.dry_run,
            "confirm_live": cfg.confirm_live,
            "clients": len(clients),
            "auction_version_code": detail.version_code,
            "auction_state_code": detail.state_code,
            "auction_exec_state_code": detail.exec_state_code,
            "payload_bytes": len(payload_bytes),
        },
    )

    _log_stop_on_success_reach(cfg, waves, logger)

    if not getattr(gate, "proceed", True):
        failure = getattr(gate, "failure", None)
        logger.event(
            stage=STAGE,
            event="fire_blocked_by_gate",
            level="ERROR",
            detail={
                "reason": getattr(gate, "reason", ""),
                "state": getattr(gate, "state", None),
                "exec_state": getattr(gate, "exec_state", None),
                "failure": failure.value if isinstance(failure, Failure) else failure,
                "waves_dropped": len(waves),
            },
        )
        return FireOutcome([], None, 0, len(waves), 0)

    fire = _Fire(cfg, armed, ref, payload_bytes, logger)
    attempts = _plan(cfg, armed, waves, clients, logger)

    threads = [
        threading.Thread(
            target=fire.work, args=(attempt,), name=attempt.thread_id, daemon=True
        )
        for attempt in attempts
    ]
    for thread in threads:
        thread.start()

    _join(fire, attempts, threads)

    if start_audit is not None:
        fire.start_audits_sent = start_audit.sent

    fired = fire.fired_wave_indices()
    results = fire.results
    winner = fire.first_success.winner

    if cfg.do_submit_audit and winner is not None and not cfg.dry_run:
        clients[0].post_empty(
            endpoints.set_submit_bidding_audit(ref.auction_id),
            stage=STAGE,
            thread_id="main",
            role=winner.role,
        )

    if cfg.dry_run:
        logger.event(
            stage=STAGE,
            event="dry_run_footprint",
            level="WARNING",
            detail={
                "create_bid_sent": fire.submissions_sent,
                "validate_bid_sent": fire.validate_sent,
                "start_audit_sent": fire.start_audits_sent,
                "note": "dry_run sent no CreateBid, but it is not footprint-free: each of "
                "the %d ValidateBid calls above wrote a buyer-visible Logs_Search_NewBid "
                "row into this auction's event log, labelled as a failed bid submission "
                "and carrying this host's public IP and the token's company code, and the "
                "%d SetStartBiddingAudit call(s) sent ahead of the schedule wrote a "
                "Logs_Search_StartBidding row beside them."
                % (fire.validate_sent, fire.start_audits_sent),
            },
        )

    outcome = FireOutcome(
        results=results,
        winner=winner,
        waves_fired=len(fired),
        waves_dropped=len(waves) - len(fired),
        submissions_sent=fire.submissions_sent,
    )

    logger.event(
        stage=STAGE,
        event="fire_complete",
        detail={
            "waves_fired": outcome.waves_fired,
            "waves_dropped": outcome.waves_dropped,
            "dropped_wave_indices": [w.index for w in waves if w.index not in fired],
            "submissions_planned": planned_submissions,
            "submissions_sent": outcome.submissions_sent,
            "validate_sent": fire.validate_sent,
            "start_audits_sent": fire.start_audits_sent,
            "results": len(results),
            "winner": None
            if winner is None
            else {
                "thread_id": winner.thread_id,
                "role": winner.role,
                "wave_index": winner.wave_index,
                "bid_id": winner.bid_id,
                "actual_offset_ms": winner.actual_offset_ms,
            },
            "failure_counts": _failure_counts(results),
        },
    )
    return outcome


def _failure_counts(results: List[BidResult]) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    for result in results:
        key = result.failure.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _join(
    fire: _Fire,
    attempts: List[_Attempt],
    threads: List[threading.Thread],
) -> None:
    """Wait out the countdown plus one full per-thread request budget, then give up.

    A V thread issues two requests (ValidateBid, CreateBid), each capped by the configured
    connect+read timeout; the budget is three of those, one to spare. Past the
    deadline the threads are left running as daemons: the run reports what it has rather
    than blocking on a socket that may never close.

    **An abandoned thread's request may still have been received**, and its bid counted, by
    the server — the process merely stops waiting for the answer. Each straggler is
    therefore given a placeholder result before this returns, because the caller closes the
    ``Session``s and the ``RunLogger`` immediately afterwards and anything the thread
    queues after that is dropped. Without the placeholder the run reports a submission with
    no attempt row, no verdict and no error.
    """
    # A schedule may hold waves whose composition is all zero, which plans no thread and
    # leaves no release instant to wait for.
    if not attempts:
        return

    last_release_ns = max(attempt.own_release_mono_ns for attempt in attempts)
    countdown_s = max(0.0, (last_release_ns - time.monotonic_ns()) / _NS_PER_S)
    budget_s = 3.0 * (fire.cfg.connect_timeout_ms + fire.cfg.read_timeout_ms) / _MS_PER_S
    deadline_ns = time.monotonic_ns() + int(
        (countdown_s + budget_s + _JOIN_MARGIN_S) * _NS_PER_S
    )

    for thread in threads:
        remaining_s = (deadline_ns - time.monotonic_ns()) / _NS_PER_S
        thread.join(timeout=max(remaining_s, 0.0))

    stragglers = [
        (attempt, thread)
        for attempt, thread in zip(attempts, threads)
        if thread.is_alive()
    ]
    if not stragglers:
        return

    recorded = [fire.abandon(attempt) for attempt, _thread in stragglers]
    fire.logger.event(
        stage=STAGE,
        event="threads_abandoned",
        level="ERROR",
        detail={
            "threads": [thread.name for _attempt, thread in stragglers],
            "recorded": [
                {
                    "thread_id": result.thread_id,
                    "endpoint": result.endpoint,
                    "actual_offset_ms": result.actual_offset_ms,
                    "failure": result.failure.value,
                    "exception": result.exception,
                }
                for result in recorded
            ],
            "note": "still running past the join deadline; each carries a placeholder "
            "result tagged NETWORK because no verdict came back, and its request may "
            "still have been received by the server",
        },
    )


__all__ = [
    "ENDPOINT_CREATE",
    "ENDPOINT_VALIDATE",
    "OBSERVED_MIN_BID_RTT_MS",
    "OBSERVED_P50_BID_RTT_MS",
    "ROLE_DIRECT",
    "ROLE_START_AUDIT",
    "ROLE_VALIDATE_ONLY",
    "ROLE_VALIDATE_SUBMIT",
    "STAGE",
    "START_AUDIT_THREAD_ID",
    "FireOutcome",
    "FirstSuccess",
    "StartAudit",
    "first_release_mono_ns",
    "is_success",
    "require_live_confirmation",
    "run_schedule",
]
