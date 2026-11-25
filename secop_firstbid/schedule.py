"""Fire-instant scheduling: ``fire_time`` -> ``T_wall`` -> ``T_mono``, wave offsets, release
(plan §9.2, §11.4).

Two distinct quantities are easy to confuse and are kept apart by name everywhere in this
module: ``offset_ms`` is a *schedule position* relative to ``T`` (negative = before ``T``),
while ``clock_offset`` (:mod:`secop_firstbid.clock`) is the measured server-vs-local
difference. The operator-supplied ``fire_time`` is the only source of ``T``. A measurement
moves the countdown in exactly one place, :func:`shift_t`, and only for an operator who
set ``apply_clock_offset``; nothing else here consumes a ``clock_offset``.

Everything in this module is local arithmetic — no network, no I/O.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional

from .errors import Failure, SecopError
from .models import Wave

if TYPE_CHECKING:
    from .config import Config

_NS_PER_MS = 1000000
_US = timedelta(microseconds=1)

VALID_MODES = ("single", "burst", "custom")

# Matched against the time component only: a date's own trailing '-28' satisfies this
# pattern, so searching the whole string would read "2026-07-28" as carrying an offset.
_TZ_SUFFIX = re.compile(r"(Z|z|[+-]\d{2}(?::?\d{2})?)$")


def parse_fire_time(s: str) -> datetime:
    """Parse ``fire_time`` into a tz-aware UTC instant, requiring an explicit offset.

    The app's only fire-time parser: :mod:`secop_firstbid.config` delegates here, so the
    set of strings ``config.validate`` accepts is by construction the set :func:`arm`
    accepts. A second parser that disagreed would let a config pass validation and then
    abort at arm time, after the run had already opened connections and spent audit rows.

    A naive timestamp is a configuration error, never a zone to guess: the auction's own
    ``timeZoneCode`` is ``null`` in the capture, so there is nothing to infer from and a
    wrong guess silently moves the bid by hours.

    ``datetime.fromisoformat`` on this interpreter rejects a ``Z`` suffix and requires the
    offset written ``±HH:MM``, so ``Z``/``z`` and the ISO-8601 basic forms (``+0000``,
    ``-05``) are normalised before it sees them. Fractional seconds must still have exactly
    3 or 6 digits — that one it enforces itself.
    """
    if not isinstance(s, str) or not s.strip():
        raise SecopError(
            Failure.CONFIG_INVALID,
            "fire_time is required: an ISO-8601 instant with an explicit UTC offset "
            "or 'Z' (e.g. '2026-07-28T21:00:00Z' or '2026-07-28T16:00:00-05:00')",
            {"fire_time": s},
        )
    text = s.strip()

    head, sep, tail = text.partition("T")
    if not sep:
        head, sep, tail = text.partition("t")
    if not sep:
        head, sep, tail = text.partition(" ")
    if not sep or not tail:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "fire_time %r has no time component; it must be a full ISO-8601 instant with "
            "an explicit UTC offset or 'Z'" % (s,),
            {"fire_time": s},
        )

    match = _TZ_SUFFIX.search(tail)
    if match is None:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "fire_time %r has no UTC offset. Add 'Z' or an explicit offset such as "
            "'-05:00'; the host time zone is not assumed and the server's is unknown."
            % (s,),
            {"fire_time": s},
        )

    suffix = match.group(1)
    body = tail[: match.start(1)]
    if suffix in ("Z", "z"):
        offset = "+00:00"
    elif len(suffix) == 3:
        offset = suffix + ":00"
    elif ":" not in suffix:
        offset = suffix[:3] + ":" + suffix[3:]
    else:
        offset = suffix

    try:
        parsed = datetime.fromisoformat(head + "T" + body + offset)
    except ValueError as exc:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "fire_time %r is not an ISO-8601 instant this interpreter can parse "
            "(fractional seconds must have 3 or 6 digits): %s" % (s, exc),
            {"fire_time": s},
        )
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "fire_time %r resolved to a naive datetime" % (s,),
            {"fire_time": s},
        )
    return parsed.astimezone(timezone.utc)


def build_waves(cfg: "Config") -> List[Wave]:
    """Build the ordered wave schedule from ``cfg.mode`` (§11.4).

    ``single`` composes the wave from ``n_direct``/``n_validate_submit``/``n_validate_only``;
    ``burst`` and ``custom`` compose every wave from ``wave_direct``/``wave_validate``/
    ``wave_validate_only``.
    """
    mode = cfg.mode
    if mode not in VALID_MODES:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "mode %r is not one of %s" % (mode, ", ".join(VALID_MODES)),
            {"mode": mode},
        )

    if mode == "single":
        return [
            Wave(
                index=0,
                offset_ms=0.0,
                direct=cfg.n_direct,
                validate=cfg.n_validate_submit,
                validate_only=cfg.n_validate_only,
            )
        ]

    if mode == "burst":
        if cfg.bid_count < 1:
            raise SecopError(
                Failure.CONFIG_INVALID,
                "burst mode needs bid_count >= 1, got %r" % (cfg.bid_count,),
                {"bid_count": cfg.bid_count},
            )
        offsets = [
            float(-cfg.pre_ms + i * cfg.interval_ms) for i in range(cfg.bid_count)
        ]
    else:
        if not cfg.schedule_offsets_ms:
            raise SecopError(
                Failure.CONFIG_INVALID,
                "custom mode needs a non-empty schedule_offsets_ms",
                {"schedule_offsets_ms": list(cfg.schedule_offsets_ms)},
            )
        # operator order, do not sort: the index is what the logs and the summary report
        offsets = [float(value) for value in cfg.schedule_offsets_ms]

    return [
        Wave(
            index=i,
            offset_ms=offset_ms,
            direct=cfg.wave_direct,
            validate=cfg.wave_validate,
            validate_only=cfg.wave_validate_only,
        )
        for i, offset_ms in enumerate(offsets)
    ]


@dataclass(frozen=True)
class Armed:
    """The countdown, expressed on the monotonic clock.

    ``t_mono_ns`` is the single wall->monotonic conversion of ``t_wall``; every release
    instant is derived from it, so an NTP step or a DST transition during the countdown
    cannot move a wave.

    ``clock_shift_ms`` is zero unless :func:`shift_t` moved the countdown onto another
    clock. ``t_wall`` is never rewritten: it stays the instant the operator named, and
    ``t_mono_ns`` is where this host has to act for that instant to be reached on the
    clock the shift was measured against.
    """

    t_wall: datetime
    t_mono_ns: int
    armed_at_wall: datetime
    armed_at_mono_ns: int
    waves: List[Wave]
    clock_shift_ms: float = 0.0

    def wave_release_mono_ns(self, wave: Wave, lead_ms: int) -> int:
        return self.t_mono_ns + int(round((wave.offset_ms - lead_ms) * _NS_PER_MS))

    def offset_from_t_ms(self, mono_ns: int) -> float:
        return (mono_ns - self.t_mono_ns) / float(_NS_PER_MS)

    def wall_at(self, mono_ns: int) -> datetime:
        """This host's wall-clock reading at a monotonic instant.

        Derived from the one wall/monotonic pair read at arming, so every instant of a run
        is placed on the same timeline the releases are — a second ``datetime.now()`` would
        be a second conversion, free to disagree with the first after an NTP step.
        """
        return self.armed_at_wall + timedelta(
            microseconds=(mono_ns - self.armed_at_mono_ns) // 1000
        )


def arm(
    cfg: "Config",
    waves: List[Wave],
    now_wall: Optional[datetime] = None,
    now_mono_ns: Optional[int] = None,
) -> Armed:
    """Convert ``fire_time`` to a monotonic deadline **once** and sanity-gate it (§9.2).

    ``now_wall``/``now_mono_ns`` are injectable so the conversion is testable offline and
    deterministic. A wave whose release instant has already passed is not an error here —
    it is released immediately by :func:`sleep_until_mono`.
    """
    t_wall = parse_fire_time(cfg.fire_time)

    if now_mono_ns is None:
        now_mono_ns = time.monotonic_ns()
    if now_wall is None:
        now_wall = datetime.now(timezone.utc)
    if now_wall.tzinfo is None or now_wall.tzinfo.utcoffset(now_wall) is None:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "now_wall must be timezone-aware; a naive instant cannot be compared with "
            "fire_time without guessing a zone",
            {"now_wall": now_wall.isoformat()},
        )
    now_wall = now_wall.astimezone(timezone.utc)

    delta = t_wall - now_wall
    delta_s = delta.total_seconds()
    if delta_s <= 0.0:
        raise SecopError(
            Failure.FIRE_TIME_PAST,
            "fire_time %s is %.3f s in the past (now %s)"
            % (t_wall.isoformat(), -delta_s, now_wall.isoformat()),
            {
                "fire_time": t_wall.isoformat(),
                "now": now_wall.isoformat(),
                "delta_s": delta_s,
            },
        )
    if delta_s > cfg.max_arm_ahead_s:
        raise SecopError(
            Failure.FIRE_TIME_TOO_FAR,
            "fire_time %s is %.1f s ahead, past max_arm_ahead_s=%d — check the date; a "
            "run armed that long outlives the token"
            % (t_wall.isoformat(), delta_s, cfg.max_arm_ahead_s),
            {
                "fire_time": t_wall.isoformat(),
                "now": now_wall.isoformat(),
                "delta_s": delta_s,
                "max_arm_ahead_s": cfg.max_arm_ahead_s,
            },
        )

    t_mono_ns = now_mono_ns + (delta // _US) * 1000
    return Armed(
        t_wall=t_wall,
        t_mono_ns=t_mono_ns,
        armed_at_wall=now_wall,
        armed_at_mono_ns=now_mono_ns,
        waves=list(waves),
    )


def shift_t(armed: Armed, shift_ms: float) -> Armed:
    """Move the whole countdown by ``shift_ms`` and record that it was moved.

    Positive is later. Every release instant derives from ``t_mono_ns``, so this moves all
    of them together and changes nothing else. It is the one place a measured clock offset
    becomes a control input, it is reached only when the operator set
    ``apply_clock_offset``, and it must be called while every thread is still asleep.
    """
    return dataclasses.replace(
        armed,
        t_mono_ns=armed.t_mono_ns + int(round(shift_ms * _NS_PER_MS)),
        clock_shift_ms=armed.clock_shift_ms + float(shift_ms),
    )


def sleep_until_mono(
    target_mono_ns: int,
    spin_ms: float = 2.0,
    cancel: Optional[threading.Event] = None,
) -> None:
    """Block until ``target_mono_ns`` on the monotonic clock, then return.

    Coarse ``time.sleep`` up to ``spin_ms`` short of the target — the OS timer granularity
    makes it unreliable closer than that — then a busy-wait for the final approach. Returns
    early, before the target, if ``cancel`` is set.
    """
    spin_ns = int(max(spin_ms, 0.0) * _NS_PER_MS)

    while True:
        remaining_ns = target_mono_ns - time.monotonic_ns()
        if remaining_ns <= spin_ns:
            break
        coarse_s = (remaining_ns - spin_ns) / 1e9
        if cancel is not None:
            if cancel.wait(coarse_s):
                return
        else:
            time.sleep(coarse_s)

    if cancel is None:
        while time.monotonic_ns() < target_mono_ns:
            pass
        return
    while time.monotonic_ns() < target_mono_ns:
        if cancel.is_set():
            return


def schedule_span_ms(waves: List[Wave]) -> float:
    """End-to-end span of the schedule, the yardstick every timing claim is measured against."""
    if not waves:
        return 0.0
    offsets = [wave.offset_ms for wave in waves]
    return float(max(offsets) - min(offsets))


def check_fire_time_vs_start_date(
    t_wall: datetime,
    start_date: Optional[datetime],
    warn_s: float,
) -> Optional[str]:
    """Compare ``T`` against the server's ``startDate`` and return a warning, or ``None``.

    This never changes ``t_wall`` and has no other effect. The operator is the authority on
    when to fire; a wrong or misparsed server ``startDate`` must not be able to move the bid
    silently, so the disagreement is reported and ``fire_time`` is honoured either way.
    """
    if start_date is None:
        return None
    if start_date.tzinfo is None or start_date.tzinfo.utcoffset(start_date) is None:
        return (
            "server startDate %s carries no UTC offset (timeZoneCode is null), so it was "
            "not compared against fire_time %s; firing at fire_time"
            % (start_date.isoformat(), t_wall.isoformat())
        )
    delta_s = (start_date.astimezone(timezone.utc) - t_wall).total_seconds()
    if abs(delta_s) <= warn_s:
        return None
    return (
        "fire_time %s and server startDate %s differ by %+.3f s (> %g s); firing at "
        "fire_time — startDate is never applied"
        % (
            t_wall.isoformat(),
            start_date.astimezone(timezone.utc).isoformat(),
            delta_s,
            warn_s,
        )
    )


__all__ = [
    "VALID_MODES",
    "Armed",
    "arm",
    "build_waves",
    "check_fire_time_vs_start_date",
    "parse_fire_time",
    "schedule_span_ms",
    "shift_t",
    "sleep_until_mono",
]
