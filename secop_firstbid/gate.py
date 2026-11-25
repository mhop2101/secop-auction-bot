"""State gate: one ``GetAuctionSimple`` ahead of ``T``, verdict cached for the whole
schedule (plan §9.4).

**The gate is issued once, at ``T − gate_lead_ms``, and never per wave.** The gate *is* a
``GetAuctionSimple``, and that request costs a full round trip: min 90.3 / median 135.3 /
max 1138.3 ms over the 128 captured samples — between 1.5x and 12x the entire span of a
recommended 90 ms schedule (``pre_ms=60, interval_ms=30``). Gating immediately before each
``CreateBid`` would give the direct-submit threads two round trips, and one round trip
against two is exactly what distinguishes a validate-then-submit thread from a direct one,
so the role model would collapse into a single kind of thread. Worse, in ``single`` mode a
gate can only read ``Started`` if it is issued *after* ``T``, which pushes the bid to
``T +`` one gate RTT — at best 90.3 ms, the fastest response of any kind in the capture.
The verdict this module returns is therefore a value, computed once, that the caller reuses
for every wave.

The gate reads ``auctionStateCode``. It records ``auctionExecutionStateCode`` and decides
nothing from it: that field was ``Running`` on 128/128 captured responses, *including* all
125 taken after the auction had already flipped to ``Finished``, so a condition of
``!= "Paused"`` stays permissive past the end of the auction and contributes almost nothing.

The gate can only ever *stop* an armed run on positive evidence that the run is futile:
a terminal ``auctionStateCode``, or a 401 saying the bearer token is dead. ``ReadyToStart``
proceeds — that is the expected pre-open reading, not a failure. Everything else proceeds
too, loudly: a network error, a WAF block or an unreadable body is not evidence that the
auction is closed, and the server is the authority on every bid anyway. A gate that aborts
on a transient blip costs the whole run, which is strictly worse than a bid the server
rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from . import endpoints
from .errors import Failure, SecopError, classify
from .schedule import sleep_until_mono

# Type-only: the client and the logger are handed in, and importing session at runtime
# would pull `requests` into a module that never constructs a connection of its own.
if TYPE_CHECKING:
    from .config import Config
    from .logging_setup import RunLogger
    from .models import AuctionRef
    from .schedule import Armed
    from .session import HttpClient

GATE_STAGE = "gate"
GATE_THREAD_ID = "gate"
GATE_MODE_ONCE_BEFORE = "once_before"
GATE_MODE_OFF = "off"

STATE_FIELD = "auctionStateCode"
EXEC_STATE_FIELD = "auctionExecutionStateCode"

KNOWN_STATES = (
    "ReadyToStart",
    "Started",
    "Finished",
    "Canceled",
    "Paused",
    "Deprecated",
)

# The taxonomy has no STATE_CANCELED or STATE_DEPRECATED: all three readings mean the same
# operational fact — this auction will never accept another bid — and STATE_FINISHED is the
# tag for it. The raw code is preserved verbatim in the verdict and in the log.
_TERMINAL_STATES = {
    "finished": Failure.STATE_FINISHED,
    "canceled": Failure.STATE_FINISHED,
    "cancelled": Failure.STATE_FINISHED,
    "deprecated": Failure.STATE_FINISHED,
}

_OPEN_STATES = ("started",)
_PRE_OPEN_STATES = ("readytostart",)
_PAUSED_STATES = ("paused",)

_NS_PER_MS = 1000000


@dataclass(frozen=True)
class GateVerdict:
    """The single state reading, cached for every wave in the schedule.

    Frozen because it is shared: one gate feeds the whole schedule, and a verdict that could
    be edited between waves would be a per-wave gate wearing a disguise.

    ``failure`` is set exactly when ``proceed`` is False. A proceed-with-misgivings reading
    (``Paused``, an unreadable response) carries its evidence in ``state`` and ``reason``,
    and is logged at WARNING; it does not carry a failure code, because nothing failed yet.

    ``issued_offset_ms`` is where the request actually left, relative to ``T`` — negative by
    ``gate_lead_ms`` when the gate ran as intended.
    """

    mode: str
    issued_offset_ms: Optional[float]
    state: Optional[str]
    exec_state: Optional[str]
    rtt_ms: Optional[float]
    proceed: bool
    reason: str
    failure: Optional[Failure]


def verdict_detail(verdict: GateVerdict) -> Dict[str, Any]:
    """JSON-safe view of a verdict, for the event log and the run summary."""
    return {
        "mode": verdict.mode,
        "issued_offset_ms": verdict.issued_offset_ms,
        "state": verdict.state,
        "exec_state": verdict.exec_state,
        "rtt_ms": verdict.rtt_ms,
        "proceed": verdict.proceed,
        "reason": verdict.reason,
        "failure": None if verdict.failure is None else verdict.failure.value,
    }


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _state_key(state: Optional[str]) -> str:
    if not state:
        return ""
    return state.strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def run_gate(
    cfg: "Config",
    armed: "Armed",
    client: "HttpClient",
    ref: "AuctionRef",
    logger: "RunLogger",
) -> GateVerdict:
    """Read the auction state once, ahead of ``T``, and return the verdict for the run."""
    mode = cfg.gate_mode
    if mode == GATE_MODE_OFF:
        verdict = GateVerdict(
            mode=mode,
            issued_offset_ms=None,
            state=None,
            exec_state=None,
            rtt_ms=None,
            proceed=True,
            reason="gate disabled",
            failure=None,
        )
        logger.event(
            stage=GATE_STAGE,
            event="gate_skipped",
            thread_id=GATE_THREAD_ID,
            detail=dict(
                verdict_detail(verdict),
                note=(
                    "gate_mode=off issues no request; the server rejects an early or late "
                    "bid on its own, which is what pre-T waves already rely on"
                ),
            ),
        )
        return verdict

    if mode != GATE_MODE_ONCE_BEFORE:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "gate_mode %r is not one of %r, %r"
            % (mode, GATE_MODE_ONCE_BEFORE, GATE_MODE_OFF),
            {"gate_mode": mode},
        )

    release_mono_ns = armed.t_mono_ns - int(round(cfg.gate_lead_ms * _NS_PER_MS))
    logger.event(
        stage=GATE_STAGE,
        event="gate_scheduled",
        level="WARNING" if cfg.gate_lead_ms <= 0 else "INFO",
        thread_id=GATE_THREAD_ID,
        detail={
            "gate_lead_ms": cfg.gate_lead_ms,
            "release_mono_ns": release_mono_ns,
            "t_mono_ns": armed.t_mono_ns,
            "waves_covered": len(armed.waves),
            "note": (
                "gate_lead_ms must exceed the GetAuctionSimple RTT (p50 135.3 ms, p95 "
                "~665 ms) or the response lands after T and delays the whole schedule; at "
                "gate_lead_ms <= 0 the gate is issued at or after T, which is the inline "
                "gate this design exists to avoid"
            ),
        },
    )

    sleep_until_mono(release_mono_ns)
    outcome = client.get(
        endpoints.get_auction_simple(ref.auction_id),
        stage=GATE_STAGE,
        thread_id=GATE_THREAD_ID,
        role="gate",
    )

    issued_offset_ms = armed.offset_from_t_ms(outcome.t_send_mono_ns)
    landed_offset_ms = (
        None
        if outcome.t_recv_mono_ns is None
        else armed.offset_from_t_ms(outcome.t_recv_mono_ns)
    )

    body = outcome.body if isinstance(outcome.body, dict) else None
    state = _text(body.get(STATE_FIELD)) if body is not None else None
    exec_state = _text(body.get(EXEC_STATE_FIELD)) if body is not None else None
    transport = classify(
        outcome.status, outcome.body, outcome.exception, raw_text=outcome.text
    )

    verdict, level = _decide(
        mode=mode,
        state=state,
        exec_state=exec_state,
        issued_offset_ms=issued_offset_ms,
        rtt_ms=outcome.rtt_ms,
        status=outcome.status,
        transport=transport,
    )

    detail = dict(
        verdict_detail(verdict),
        status=outcome.status,
        transport_failure=transport.value,
        landed_offset_ms=landed_offset_ms,
        waves_covered=len(armed.waves),
        corr_id=outcome.corr_id,
    )
    if landed_offset_ms is not None and landed_offset_ms > 0.0:
        detail["late_gate"] = (
            "the gate response landed %.1f ms after T; gate_lead_ms=%d was smaller than "
            "this round trip, so the whole schedule was released late"
            % (landed_offset_ms, cfg.gate_lead_ms)
        )
        if level == "INFO":
            level = "WARNING"
    if exec_state is not None:
        detail["exec_state_note"] = (
            "%s=%r is recorded, never gated on: it read Running on 128/128 captured "
            "responses including all 125 after the auction had flipped to Finished"
            % (EXEC_STATE_FIELD, exec_state)
        )

    logger.event(
        stage=GATE_STAGE,
        event="gate_verdict",
        level=level,
        thread_id=GATE_THREAD_ID,
        corr_id=outcome.corr_id or None,
        detail=detail,
    )
    return verdict


def _decide(
    *,
    mode: str,
    state: Optional[str],
    exec_state: Optional[str],
    issued_offset_ms: float,
    rtt_ms: Optional[float],
    status: Optional[int],
    transport: Failure
) -> Tuple[GateVerdict, str]:
    def verdict(proceed: bool, reason: str, failure: Optional[Failure]) -> GateVerdict:
        return GateVerdict(
            mode=mode,
            issued_offset_ms=issued_offset_ms,
            state=state,
            exec_state=exec_state,
            rtt_ms=rtt_ms,
            proceed=proceed,
            reason=reason,
            failure=failure,
        )

    key = _state_key(state)

    if key in _TERMINAL_STATES:
        return (
            verdict(
                False,
                "%s=%r read %+.1f ms from T: the auction accepts no further bid, so the "
                "schedule is not released" % (STATE_FIELD, state, issued_offset_ms),
                _TERMINAL_STATES[key],
            ),
            "ERROR",
        )

    if key in _OPEN_STATES:
        return (
            verdict(
                True,
                "%s=%r read %+.1f ms from T: the auction is open"
                % (STATE_FIELD, state, issued_offset_ms),
                None,
            ),
            "INFO",
        )

    if key in _PRE_OPEN_STATES:
        return (
            verdict(
                True,
                "%s=%r read %+.1f ms from T: the expected pre-open reading, not a failure"
                % (STATE_FIELD, state, issued_offset_ms),
                None,
            ),
            "INFO",
        )

    if key in _PAUSED_STATES:
        return (
            verdict(
                True,
                "%s=%r read %+.1f ms from T: bidding is suspended and the schedule will "
                "most likely be rejected, but a pause is reversible and the server is the "
                "authority, so the schedule is released"
                % (STATE_FIELD, state, issued_offset_ms),
                None,
            ),
            "WARNING",
        )

    if state is not None:
        return (
            verdict(
                True,
                "%s=%r is not one of %s (STATE_UNKNOWN); an unrecognised state is not "
                "evidence the auction is closed, so the schedule is released"
                % (STATE_FIELD, state, ", ".join(KNOWN_STATES)),
                None,
            ),
            "WARNING",
        )

    if transport is Failure.AUTH_EXPIRED:
        return (
            verdict(
                False,
                "the gate GetAuctionSimple returned 401: the bearer token is dead and "
                "every request in the schedule would fail the same way",
                Failure.AUTH_EXPIRED,
            ),
            "ERROR",
        )

    return (
        verdict(
            True,
            "the gate GetAuctionSimple returned status=%s (%s) with no %s; a gate that "
            "cannot read the state is not evidence the auction is closed, so the schedule "
            "is released and the server decides each bid"
            % (status, transport.value, STATE_FIELD),
            None,
        ),
        "WARNING",
    )


__all__ = [
    "EXEC_STATE_FIELD",
    "GATE_MODE_OFF",
    "GATE_MODE_ONCE_BEFORE",
    "GATE_STAGE",
    "GATE_THREAD_ID",
    "GateVerdict",
    "KNOWN_STATES",
    "STATE_FIELD",
    "run_gate",
    "verdict_detail",
]
