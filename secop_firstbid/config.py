"""Configuration: the plan §4 key set, its loader, its validator and the audit footprint.

Nothing in this module touches the network, the auction page or the clock beyond
``fire_time``. :func:`validate` is the only place that declares a config fatal; it returns
warnings and raises, so a caller that survives it may arm.

Three defaults are a posture rather than a preference, and changing them changes what the
auction owner sees: ``do_submit_audit`` is off because that route was never observed on the
wire, ``apply_clock_offset`` is off because the measured offset is a diagnostic until the
operator says otherwise, and ``confirm_live`` is off so no ``CreateBid`` can leave the
process without a deliberate act. ``bid_is_total`` is kept for schema fidelity and rejected
wherever it is set true.

The keys past the plan's §4 set — ``keepalive_offsets_ms``, ``keepalive_timeout_ms``,
``clock_window_ms``, ``clock_max_width_ms``, ``clock_fallback_shift_ms``,
``read_secop_record``, ``secop_record_delay_s``, ``secop_record_attempts`` and
``secop_record_retry_s`` — exist because of what the first live runs showed, and
``docs/configuration.md`` says what each one is for.

``connect_timeout_ms`` and ``read_timeout_ms`` default to 8 s and 60 s, not the plan's 3 s
and 5 s. In the live auctions SECOP took 4 to 35 s to process a bid, and on 2026-11-23
none of the seven bids that this app gave up on after 10 s was ever processed. Whether
closing the connection is what lost them is not established; waiting costs nothing,
because no retry follows a timeout and every thread has its own connection.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import numbers
from .endpoints import DEFAULT_USER_AGENT
from .errors import Failure, SecopError
from .models import Wave
from .schedule import parse_fire_time

CLOCK_PROBES = ("date_header", "signalr", "both", "none")
GATE_MODES = ("once_before", "off")
MODES = ("single", "burst", "custom")

AUDIT_FOOTPRINT_WARN_THRESHOLD = 10
RTT_SAMPLES_RECOMMENDED = 10

DEFAULT_KEEPALIVE_OFFSETS_MS = (-45000, -30000, -15000)

# What a keep-alive is allowed on top of its own connect and read timeouts before the
# thread that owns the connection may need it: thread start-up, the join, the log queue.
KEEPALIVE_SETTLE_MS = 2000

# Below this the stamp cannot reliably be read back before the last moment T may still be
# moved: one POST and up to two reads, each bounded by connect + read timeouts.
START_AUDIT_LEAD_FOR_CLOCK_MS = 20000

# The seconds before an opening in which SECOP refused or stalled a third to half of the
# requests sent to it on 2026-11-20 (the failures began about 6 s out). A start audit
# sent inside this window may never be written.
OPENING_RUSH_MS = 10000

# A live reading and the clock-probe fallback further apart than this are reported: one of
# them is wrong, or SECOP's clock moved between the two.
CLOCK_FALLBACK_DISAGREE_MS = 100


@dataclass(repr=False)
class Config:
    """Every §4 key under its §4 name, plus the keys the first live runs added.

    ``start_audit_lead_ms`` keeps its name and changed its meaning and default: it is how
    long before the *first* release the run's one start audit is sent, not a per-thread
    lead, because the audit no longer travels on any bid's connection.

    ``repr`` is hand-written: the generated one would print the bearer token in full into
    every traceback and every ``%r`` in a log line.
    """

    jwt: str
    auction_page_html_path: str
    bid_value: str
    fire_time: str
    auction_id: Optional[str] = None
    bid_is_total: bool = False
    n_direct: int = 1
    n_validate_submit: int = 0
    n_validate_only: int = 0
    v_gate_on_valid: bool = True
    max_arm_ahead_s: int = 3600
    fire_time_mismatch_warn_s: int = 2
    apply_clock_offset: bool = False
    clock_probe: str = "date_header"
    clock_window_ms: int = 5000
    clock_max_width_ms: int = 500
    clock_fallback_shift_ms: Optional[int] = None
    lead_ms: int = 0
    validate_lead_ms: int = 0
    start_audit_lead_ms: int = 60000
    keepalive_offsets_ms: List[int] = field(
        default_factory=lambda: list(DEFAULT_KEEPALIVE_OFFSETS_MS)
    )
    keepalive_timeout_ms: int = 3000
    gate_mode: str = "once_before"
    gate_lead_ms: int = 1500
    mode: str = "single"
    bid_count: int = 1
    pre_ms: int = 0
    interval_ms: int = 0
    wave_direct: int = 1
    wave_validate: int = 0
    wave_validate_only: int = 0
    schedule_offsets_ms: List[int] = field(default_factory=list)
    stop_on_success: bool = True
    do_start_audit: bool = True
    do_submit_audit: bool = False
    read_secop_record: bool = True
    secop_record_delay_s: int = 90
    secop_record_attempts: int = 4
    secop_record_retry_s: int = 60
    dry_run: bool = False
    connect_timeout_ms: int = 8000
    read_timeout_ms: int = 60000
    auth_safety_margin_s: int = 300
    user_agent: str = DEFAULT_USER_AGENT
    rtt_samples: int = 10
    run_root: str = "runs"
    confirm_live: bool = False

    def __repr__(self) -> str:
        inner = ", ".join(
            "%s=%r" % (name, value) for name, value in redacted_dict(self).items()
        )
        return "Config(%s)" % inner


_FIELD_KINDS = {f.name: f.type for f in dataclasses.fields(Config)}

_REQUIRED_KEYS = tuple(
    f.name
    for f in dataclasses.fields(Config)
    if f.default is dataclasses.MISSING
    and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
)

_NON_EMPTY_STRINGS = (
    "jwt",
    "auction_page_html_path",
    "bid_value",
    "user_agent",
    "run_root",
)

_NON_NEGATIVE_INTS = (
    "n_direct",
    "n_validate_submit",
    "n_validate_only",
    "wave_direct",
    "wave_validate",
    "wave_validate_only",
    "gate_lead_ms",
    "start_audit_lead_ms",
    "clock_window_ms",
    "clock_max_width_ms",
    "secop_record_delay_s",
    "secop_record_retry_s",
    "fire_time_mismatch_warn_s",
    "auth_safety_margin_s",
    "rtt_samples",
)

_POSITIVE_INTS = (
    "connect_timeout_ms",
    "read_timeout_ms",
    "keepalive_timeout_ms",
    "secop_record_attempts",
    "max_arm_ahead_s",
)

DRY_RUN_WARNING = (
    "dry_run is NOT footprint-free. It skips CreateBid, but every ValidateBid still writes "
    "a buyer-visible Logs_Search_NewBid row into the auction's event log, labelled as a "
    "failed bid submission and carrying your public IP address and your company code; "
    "do_start_audit adds one Logs_Search_StartBidding row for the run. Rehearse against a "
    "throwaway auction, not one whose owner you would rather not appear in front of."
)

BID_IS_TOTAL_ERROR = (
    "bid_is_total is True: its semantics are unresolved. It contradicts the rule that the "
    "operator's value is sent as-is, makes bidValue differ per item, and inherits the "
    "undefined rounding of the itemQuantityUnitPrice division. Every captured item has "
    "quantity 1, so this branch has never been exercised against the server. Leave it false."
)


def _config_error(message: str, detail: Optional[Dict[str, Any]] = None) -> SecopError:
    return SecopError(Failure.CONFIG_INVALID, message, detail)


def keepalive_quiet_ms(cfg: Config) -> int:
    """How long before the first release the last keep-alive must have been issued.

    A keep-alive is a request on the connection a bid is about to use, so it has to be
    over — answered or timed out — before anything else needs that connection: its own
    connect and read timeouts, a margin to settle, and the gate's lead when the gate runs,
    because the gate reads on the first thread's client.
    """
    quiet = 2 * cfg.keepalive_timeout_ms + KEEPALIVE_SETTLE_MS
    if cfg.gate_mode == "once_before":
        quiet += cfg.gate_lead_ms
    return quiet


def _planned_composition(cfg: Config) -> Tuple[int, int, int]:
    """(direct, validate, validate_only) per wave, from the keys the mode actually reads."""
    if cfg.mode in ("burst", "custom"):
        return (cfg.wave_direct, cfg.wave_validate, cfg.wave_validate_only)
    return (cfg.n_direct, cfg.n_validate_submit, cfg.n_validate_only)


def _planned_waves(cfg: Config) -> List[Wave]:
    """Wave composition as configured, for footprint accounting only.

    Offsets stay 0.0 because they do not enter the footprint. ``schedule.build_waves`` owns
    the real schedule; this twin exists because it must not raise on a config ``validate``
    has not finished checking, and ``build_waves`` does.
    """
    if cfg.mode == "burst":
        count = cfg.bid_count
    elif cfg.mode == "custom":
        count = len(cfg.schedule_offsets_ms)
    else:
        count = 1
    direct, validate_submit, validate_only = _planned_composition(cfg)
    return [
        Wave(i, 0.0, direct, validate_submit, validate_only) for i in range(max(count, 0))
    ]


def start_audit_planned(cfg: Config, waves: List[Wave]) -> bool:
    """Whether this run writes its one ``SetStartBiddingAudit`` row.

    One per run, not one per thread: the audit is sent once, ahead of the first release, on
    a connection no bid uses. A run whose threads send nothing writes none — under
    ``dry_run`` a D thread's only request is the submission it skips, so a D-only dry run
    stays at zero rows, while V and P threads still send their ``ValidateBid`` and the run
    still pairs them with the audit.

    :func:`audit_footprint` and the fire path both ask this one function, so the projection
    and the request cannot drift apart.
    """
    if not cfg.do_start_audit:
        return False
    if cfg.dry_run:
        return any(w.validate + w.validate_only for w in waves)
    return any(w.direct + w.validate + w.validate_only for w in waves)


def audit_footprint(cfg: Config, waves: List[Wave]) -> Dict[str, int]:
    """Projection of what this configuration would cost in rows the auction owner can read.

    Both numbers are *planned*, never observed. What a run actually sent is
    ``submissions_sent`` and the attempt rows in ``summary.json``; the gap between the two
    is what ``stop_on_success`` and the validate gate saved.

    Counted per **request**, not per thread — a validate-then-submit thread issues two of
    them, so counting threads understates the footprint::

        audited = Σ waves × [ direct×1              CreateBid
                            + validate×2            ValidateBid *and* CreateBid
                            + validate_only×1 ]     ValidateBid
                  + int(start_audit_planned)        one SetStartBiddingAudit for the run
                  + int(do_submit_audit)

    ``submissions`` counts CreateBid only. Validate-only probes still count as audited: a
    ValidateBid writes the same Logs_Search_NewBid row a rejected CreateBid does.

    Under ``dry_run`` the CreateBid terms drop out and nothing else does. A D thread's only
    request is the submission, so it then issues nothing at all, while V and P threads still
    send their ValidateBid. That residue is the arithmetic behind "dry_run is not
    footprint-free"; a dry run's ``submissions`` is zero.

    Both figures are upper bounds: a V thread that ``v_gate_on_valid`` self-aborts, or any
    wave a blocking gate or ``stop_on_success`` cancels, sends fewer requests than planned.

    The start audit used to be one row per thread, mirroring the browser's one click per
    attempt. It is one per run because a per-thread audit had to share its thread's only
    connection with the bid, and on 2026-11-20 that put 5 to 16 bids per run more than half
    a second late. Whether the server needs the row at all before a bid is unverified
    (``docs/open-questions.md``); bids whose own audit had failed were still processed.
    """
    start = int(start_audit_planned(cfg, waves))
    if cfg.dry_run:
        submissions = 0
        audited = sum(w.validate + w.validate_only for w in waves) + start
    else:
        submissions = sum(w.direct + w.validate for w in waves)
        audited = (
            sum(w.direct + w.validate * 2 + w.validate_only for w in waves)
            + start
            + int(cfg.do_submit_audit)
        )
    return {
        "submissions": submissions,
        "audited_requests": audited,
        "waves": len(waves),
    }


def validate(cfg: Config, now: Optional[datetime] = None) -> List[str]:
    """Return warnings; raise :class:`SecopError` on anything fatal.

    Structural errors are collected and reported together, because an operator fixing a
    config one exception at a time is an operator who arms late. The two fire-time window
    failures keep their own taxonomy codes (``FIRE_TIME_PAST`` / ``FIRE_TIME_TOO_FAR``);
    every other error is ``CONFIG_INVALID``.

    ``bid_value`` is parsed here with the same :func:`numbers.parse_user_decimal` the payload
    builder uses, so an amount that survives this function is one the wire serializer can
    render. Left unparsed it reaches the operator either as a traceback from a later stage
    (``"abc"``, the ambiguous ``"1.234"``) or, for ``"-500"``, not at all: that parses, and
    goes on the wire verbatim as ``bidValue "-500"``.

    A zero ``bid_value`` is legal and warned about. No zero bid has ever been sent to this
    server, so how it responds is unverified: see Q16 in ``docs/open-questions.md``.

    ``now`` is injectable so the fire-time window is testable offline; it must be tz-aware.
    """
    errors = []  # type: List[str]

    for name in _NON_EMPTY_STRINGS:
        if not str(getattr(cfg, name) or "").strip():
            errors.append("%s is required and must not be empty" % name)

    if not isinstance(cfg.bid_value, str):
        errors.append(
            "bid_value must be a quoted string, got %s: a JSON number is decoded as a "
            "float, which is the precision loss the money handling exists to avoid"
            % type(cfg.bid_value).__name__
        )
    elif cfg.bid_value.strip():
        try:
            amount = numbers.parse_user_decimal(cfg.bid_value)
        except SecopError as exc:
            errors.append(exc.message)
        else:
            # is_signed, because Decimal("-0") compares equal to zero and would reach the wire as "-0"
            if amount < 0 or amount.is_signed():
                errors.append(
                    'bid_value %r is negative: a bid must not be below zero. Nothing floors or '
                    'adjusts it, so it would go on the wire verbatim as bidValue "%s".'
                    % (cfg.bid_value, numbers.to_wire(amount))
                )

    if cfg.clock_probe not in CLOCK_PROBES:
        errors.append(
            "clock_probe %r is not one of %s" % (cfg.clock_probe, list(CLOCK_PROBES))
        )
    if cfg.gate_mode not in GATE_MODES:
        errors.append("gate_mode %r is not one of %s" % (cfg.gate_mode, list(GATE_MODES)))
    if cfg.mode not in MODES:
        errors.append("mode %r is not one of %s" % (cfg.mode, list(MODES)))

    if cfg.bid_is_total:
        errors.append(BID_IS_TOTAL_ERROR)

    for name in _NON_NEGATIVE_INTS:
        if getattr(cfg, name) < 0:
            errors.append("%s must be >= 0, got %r" % (name, getattr(cfg, name)))
    for name in _POSITIVE_INTS:
        if getattr(cfg, name) < 1:
            errors.append("%s must be >= 1, got %r" % (name, getattr(cfg, name)))

    direct, validate_submit, validate_only = _planned_composition(cfg)
    submitters = direct + validate_submit
    if submitters == 0 and validate_only == 0:
        errors.append(
            "no threads configured: submitters (%s) and probes are all zero, so the run "
            "would fire nothing at all. dry_run does not excuse it — the schedule still has "
            "waves and no thread to release in them. A probe-only or dry run is legal; an "
            "empty one is a typo."
            % ("n_direct + n_validate_submit" if cfg.mode == "single" else "wave_direct + wave_validate")
        )

    if cfg.mode == "burst":
        if cfg.bid_count < 1:
            errors.append("mode 'burst' needs bid_count >= 1, got %r" % (cfg.bid_count,))
        elif cfg.bid_count > 1 and cfg.interval_ms < 1:
            errors.append(
                "mode 'burst' with bid_count=%d needs interval_ms >= 1: at interval_ms=%d "
                "every wave offset is -pre_ms + i*0, so all %d waves collapse onto T. That is "
                "single mode with a larger direct count wearing a disguise, at the maximum "
                "audit footprint." % (cfg.bid_count, cfg.interval_ms, cfg.bid_count)
            )
    elif cfg.mode == "custom" and not cfg.schedule_offsets_ms:
        errors.append(
            "mode 'custom' needs a non-empty schedule_offsets_ms, e.g. [-100,-50,-20,0,20,50]"
        )

    if cfg.clock_fallback_shift_ms is not None and abs(
        cfg.clock_fallback_shift_ms
    ) > cfg.clock_window_ms:
        errors.append(
            "clock_fallback_shift_ms=%d is beyond clock_window_ms=%d: no shift that large "
            "is ever applied, and a clock-probe reading that far off is a wrong reading, "
            "not a clock" % (cfg.clock_fallback_shift_ms, cfg.clock_window_ms)
        )

    if any(offset >= 0 for offset in cfg.keepalive_offsets_ms):
        errors.append(
            "keepalive_offsets_ms %r must all be negative: they are measured from the first "
            "release, and a keep-alive at or after it would be on a bid's connection at the "
            "instant the bid needs it" % (list(cfg.keepalive_offsets_ms),)
        )
    elif cfg.keepalive_offsets_ms and cfg.keepalive_timeout_ms >= 1:
        closest_ms = -max(cfg.keepalive_offsets_ms)
        needed_ms = keepalive_quiet_ms(cfg)
        if closest_ms < needed_ms:
            errors.append(
                "keepalive_offsets_ms %r puts a keep-alive %d ms before the first release, "
                "but one can hold its connection for up to %d ms (connect and read timeouts "
                "of %d ms each, %d ms to settle%s). Nothing may share a connection with its "
                "bid at the release instant: move the last keep-alive to -%d or earlier, or "
                "lower keepalive_timeout_ms."
                % (
                    list(cfg.keepalive_offsets_ms),
                    closest_ms,
                    needed_ms,
                    cfg.keepalive_timeout_ms,
                    KEEPALIVE_SETTLE_MS,
                    ", then the gate's %d ms" % cfg.gate_lead_ms
                    if cfg.gate_mode == "once_before"
                    else "",
                    needed_ms,
                )
            )

    try:
        parse_fire_time(cfg.fire_time)
    except SecopError as exc:
        errors.append(exc.message)

    if errors:
        raise _config_error(
            "invalid config: " + "; ".join(errors), {"errors": list(errors)}
        )

    fire_time = parse_fire_time(cfg.fire_time)
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise _config_error("validate(now=...) must be timezone-aware")

    ahead_s = (fire_time - reference).total_seconds()
    if ahead_s <= 0:
        raise SecopError(
            Failure.FIRE_TIME_PAST,
            "fire_time %s is %.3f s in the past (now %s)"
            % (fire_time.isoformat(), -ahead_s, reference.astimezone(timezone.utc).isoformat()),
            {"fire_time": fire_time.isoformat(), "seconds_ahead": ahead_s},
        )
    if ahead_s > cfg.max_arm_ahead_s:
        raise SecopError(
            Failure.FIRE_TIME_TOO_FAR,
            "fire_time %s is %.0f s ahead, past max_arm_ahead_s=%d: either the date is wrong "
            "or the run would sit armed long enough to outlive the 24 h token"
            % (fire_time.isoformat(), ahead_s, cfg.max_arm_ahead_s),
            {"fire_time": fire_time.isoformat(), "seconds_ahead": ahead_s},
        )

    warnings = []  # type: List[str]

    bid_amount = numbers.parse_user_decimal(cfg.bid_value)
    if bid_amount == 0:
        warnings.append(
            'bid_value %r is ZERO: bidValue "%s" and a totalValue of zero will be sent. The '
            "server accepts it: on 2026-11-20 a zero was the one accepted bid in each of two "
            "live auctions. On a reverse auction the lowest value wins, so an accepted zero "
            "may commit your company to supplying at no charge."
            % (cfg.bid_value, numbers.to_wire(bid_amount))
        )

    footprint = audit_footprint(cfg, _planned_waves(cfg))
    if footprint["audited_requests"] > AUDIT_FOOTPRINT_WARN_THRESHOLD:
        warnings.append(
            "planned audit footprint is %d audited requests across %d waves (%d CreateBid "
            "submissions), above the threshold of %d: every one of them is a row the auction "
            "owner can read, and a burst that size is also what a rate limiter or WAF sees."
            % (
                footprint["audited_requests"],
                footprint["waves"],
                footprint["submissions"],
                AUDIT_FOOTPRINT_WARN_THRESHOLD,
            )
        )

    if cfg.apply_clock_offset:
        warnings.append(
            "apply_clock_offset is True: T will be moved onto the clock SECOP stamps its "
            "event log with, measured from this run's own start audit read back from that "
            "log. On 2026-11-20 that clock read 0.9-1.0 s behind NTP and the accepted bid in "
            "each of two auctions was stamped 32 and 44 ms after startDate on it. That is two "
            "auctions, not a rule, and whether the offset is stable from day to day is "
            "unknown. T stays where fire_time put it, and the run says so, whenever the "
            "stamp cannot be read back in time, the feasible interval is wider than "
            "clock_max_width_ms=%d, or the offset is beyond clock_window_ms=%d."
            % (cfg.clock_max_width_ms, cfg.clock_window_ms)
        )
        if cfg.clock_fallback_shift_ms is None:
            warnings.append(
                "apply_clock_offset is True with no clock_fallback_shift_ms: if this run "
                "cannot place SECOP's clock in time, T is not moved and the schedule fires "
                "on fire_time as given — about a second early on SECOP's clock if it runs "
                "as it did on 2026-11-20. Run clock-probe first and set the fallback."
            )
        else:
            warnings.append(
                "clock_fallback_shift_ms=%d: if this run cannot place SECOP's clock in "
                "time, T is moved by this instead. It is only as good as the clock-probe "
                "reading it came from and as recent." % cfg.clock_fallback_shift_ms
            )
        if not cfg.do_start_audit:
            warnings.append(
                "apply_clock_offset is True but do_start_audit is False: the start audit is "
                "the only request of this run that SECOP stamps before T, so there is "
                "nothing to measure from and T will %s."
                % (
                    "not move"
                    if cfg.clock_fallback_shift_ms is None
                    else "move by clock_fallback_shift_ms"
                )
            )
        elif cfg.start_audit_lead_ms < START_AUDIT_LEAD_FOR_CLOCK_MS:
            warnings.append(
                "apply_clock_offset is True with start_audit_lead_ms=%d: the stamp has to be "
                "sent, written and read back before T can still be moved, and under %d ms "
                "that will usually not finish. T then stays where fire_time put it."
                % (cfg.start_audit_lead_ms, START_AUDIT_LEAD_FOR_CLOCK_MS)
            )

    if cfg.clock_fallback_shift_ms is not None and not cfg.apply_clock_offset:
        warnings.append(
            "clock_fallback_shift_ms is set but apply_clock_offset is False: T is never "
            "moved, so the fallback is ignored."
        )

    if cfg.dry_run:
        warnings.append(DRY_RUN_WARNING)

    if cfg.do_submit_audit:
        warnings.append(
            "do_submit_audit is True: SetSubmitBiddingAudit was never observed on the wire, "
            "and the SPA's own naming for the audit pair is inverted, so this route is more "
            "likely the session-open audit than the post-submit one. It costs an unverified "
            "request and an extra audit row after a win."
        )

    if cfg.confirm_live and cfg.dry_run:
        warnings.append(
            "confirm_live is True but dry_run is also True: no CreateBid will be sent."
        )
    elif cfg.confirm_live:
        warnings.append(
            "confirm_live is True: this run will send %d real CreateBid submission(s). The "
            "value is sent exactly as configured — nothing floors it, adjusts it or retries it."
            % footprint["submissions"]
        )

    opening_rush_ms = OPENING_RUSH_MS
    if cfg.do_start_audit and cfg.start_audit_lead_ms < opening_rush_ms:
        warnings.append(
            "start_audit_lead_ms=%d is inside the last %d ms before the first release. The "
            "start audit travels on its own connection, so it cannot delay a bid, but in the "
            "seconds before an opening on 2026-11-20 between a third and a half of the "
            "requests this app made failed to connect or timed out. A start audit sent there "
            "may never be written. Raise it, or set do_start_audit=false."
            % (cfg.start_audit_lead_ms, opening_rush_ms)
        )

    if cfg.rtt_samples < RTT_SAMPLES_RECOMMENDED:
        warnings.append(
            "rtt_samples=%d is below the %d samples the RTT budget asks for; the quantiles it "
            "reports will not mean much." % (cfg.rtt_samples, RTT_SAMPLES_RECOMMENDED)
        )

    return warnings


def _redact_jwt(token: str) -> str:
    """``first6…last6``. Anything short enough that this would expose most of it is dropped."""
    if not isinstance(token, str) or len(token) < 24:
        return "<redacted>"
    return token[:6] + "…" + token[-6:]


def redacted_dict(cfg: Config) -> Dict[str, Any]:
    """Every config key, safe to write to disk: the token never appears whole."""
    out = {}  # type: Dict[str, Any]
    for f in dataclasses.fields(cfg):
        value = getattr(cfg, f.name)
        if f.name == "jwt":
            value = _redact_jwt(value)
        elif isinstance(value, list):
            value = list(value)
        out[f.name] = value
    return out


def _type_error(name: str, value: Any, expected: str) -> SecopError:
    return _config_error(
        "config key %r must be %s, got %s" % (name, expected, type(value).__name__)
    )


def _coerce(name: str, value: Any) -> Any:
    kind = _FIELD_KINDS[name]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise _type_error(name, value, "a boolean")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(name, value, "an integer")
        return value
    if kind == "str":
        if isinstance(value, str):
            return value
        if name == "bid_value":
            raise _config_error(
                "config key 'bid_value' must be a quoted string, got %s. A JSON number is "
                "decoded as a float, which is exactly the precision loss the money handling "
                'exists to avoid: write "bid_value": "7813974.32".' % type(value).__name__
            )
        raise _type_error(name, value, "a string")
    if kind == "Optional[str]":
        if value is None or isinstance(value, str):
            return value
        raise _type_error(name, value, "a string or null")
    if kind == "Optional[int]":
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise _type_error(name, value, "an integer or null")
    if kind == "List[int]":
        if isinstance(value, (list, tuple)) and all(
            isinstance(v, int) and not isinstance(v, bool) for v in value
        ):
            return [int(v) for v in value]
        raise _type_error(name, value, "a list of integers")
    raise RuntimeError("no coercion rule for config key %r (annotated %r)" % (name, kind))


def _checked(data: Any, source: str, skip_none: bool) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise _config_error(
            "%s must be a mapping of config keys, got %s" % (source, type(data).__name__)
        )

    unknown = [str(k) for k in data if k not in _FIELD_KINDS]
    if unknown:
        hints = []
        for key in unknown:
            close = difflib.get_close_matches(key, list(_FIELD_KINDS), n=1)
            hints.append("%r%s" % (key, (" (did you mean %r?)" % close[0]) if close else ""))
        raise _config_error(
            "%s has unknown config key(s): %s" % (source, ", ".join(hints)),
            {"unknown_keys": unknown},
        )

    out = {}  # type: Dict[str, Any]
    for key, value in data.items():
        if value is None and skip_none:
            continue
        out[key] = _coerce(key, value)
    return out


def _load_toml(text: str, path: str) -> Any:
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise _config_error(
                "cannot read TOML config %s: this interpreter is Python 3.9, which has no "
                "stdlib tomllib (added in 3.11), and the tomli backport is not installed. "
                "JSON is the supported config format — convert the file, or install tomli."
                % (path,)
            )
    try:
        return tomllib.loads(text)
    except Exception as exc:
        raise _config_error("config file %s is not valid TOML: %s" % (path, exc))


def _read_config_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise _config_error("cannot read config file %s: %s" % (path, exc))

    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _config_error("config file %s is not valid JSON: %s" % (path, exc))
    elif ext == ".toml":
        data = _load_toml(text, path)
    else:
        raise _config_error(
            "unsupported config file extension %r on %s: use .json, or .toml if tomllib "
            "(Python 3.11+) or tomli is available" % (ext, path)
        )
    return _checked(data, "config file %s" % path, skip_none=False)


def load(
    path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None
) -> Config:
    """Build a :class:`Config` from a JSON (or TOML) file plus overrides.

    Overrides win over the file. ``None`` values in ``overrides`` are dropped rather than
    applied, so a CLI can pass its whole argparse namespace without every unset flag
    erasing a file value. Unknown keys are rejected: a silently ignored ``bid_calue`` is a
    bid at the default that nobody typed.

    Structural only — :func:`validate` decides whether the result is usable. The one
    semantic check here is ``bid_is_total``, which is refused at the door.
    """
    values = {}  # type: Dict[str, Any]
    if path is not None:
        values.update(_read_config_file(path))
    if overrides:
        values.update(_checked(overrides, "overrides", skip_none=True))

    missing = [name for name in _REQUIRED_KEYS if name not in values]
    if missing:
        raise _config_error(
            "missing required config key(s): %s" % ", ".join(missing),
            {"missing_keys": missing},
        )

    if values.get("bid_is_total") is True:
        raise _config_error(BID_IS_TOTAL_ERROR)

    return Config(**values)


__all__ = [
    "AUDIT_FOOTPRINT_WARN_THRESHOLD",
    "BID_IS_TOTAL_ERROR",
    "CLOCK_FALLBACK_DISAGREE_MS",
    "CLOCK_PROBES",
    "Config",
    "DEFAULT_KEEPALIVE_OFFSETS_MS",
    "DRY_RUN_WARNING",
    "GATE_MODES",
    "KEEPALIVE_SETTLE_MS",
    "MODES",
    "RTT_SAMPLES_RECOMMENDED",
    "START_AUDIT_LEAD_FOR_CLOCK_MS",
    "audit_footprint",
    "keepalive_quiet_ms",
    "load",
    "redacted_dict",
    "start_audit_planned",
    "validate",
]
