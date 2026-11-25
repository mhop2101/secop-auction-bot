"""Tests for :mod:`secop_firstbid.schedule` (plan §9.2, §11.4).

Offline and deterministic: every wall/monotonic reading the arming path uses is injected,
so nothing here depends on the host clock. The only real clock reading is in the
:func:`sleep_until_mono` tests, which use a few-millisecond target and a tolerant upper
bound so a scheduler hiccup cannot make them flaky.

:func:`~secop_firstbid.schedule.parse_fire_time` is the app's only fire-time parser, so the
offset matrix below is exercised through both of its entry points — this module's, and
``config.validate``. A second parser that disagreed would let a config clear the pre-flight
and then abort at arm time, once the run had already opened connections.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from secop_firstbid import schedule
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.models import Wave

NOW = datetime(2026, 7, 28, 20, 0, 0, tzinfo=timezone.utc)
NOW_MONO_NS = 1234567890123456
FIRE_TIME = "2026-07-28T21:00:00Z"


@dataclass
class StubConfig:
    """The schedule-relevant slice of §4's config, duck-typed.

    ``build_waves``/``arm`` read attributes only, so the real
    :class:`secop_firstbid.config.Config` is not needed to exercise them — and not
    importing it keeps these tests independent of that module's construction rules.
    ``test_field_names_match_the_real_config`` guards against name drift.
    """

    fire_time: str = FIRE_TIME
    mode: str = "single"
    max_arm_ahead_s: int = 3600
    n_direct: int = 1
    n_validate_submit: int = 0
    n_validate_only: int = 0
    bid_count: int = 1
    pre_ms: int = 0
    interval_ms: int = 0
    wave_direct: int = 1
    wave_validate: int = 0
    wave_validate_only: int = 0
    schedule_offsets_ms: List[int] = field(default_factory=list)


def burst_waves() -> List[Wave]:
    return schedule.build_waves(
        StubConfig(mode="burst", bid_count=4, pre_ms=60, interval_ms=30)
    )


def test_field_names_match_the_real_config():
    config = pytest.importorskip("secop_firstbid.config")
    real = set(f.name for f in dataclasses.fields(config.Config))
    stub = set(f.name for f in dataclasses.fields(StubConfig))
    assert stub - real == set()


# --- parse_fire_time ---------------------------------------------------------------


def test_z_suffix_parses_on_this_interpreter():
    assert schedule.parse_fire_time("2026-07-28T21:00:00Z") == datetime(
        2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc
    )


def test_z_and_explicit_offsets_are_the_same_instant():
    from_z = schedule.parse_fire_time("2026-07-28T21:00:00Z")
    from_utc = schedule.parse_fire_time("2026-07-28T21:00:00+00:00")
    from_bogota = schedule.parse_fire_time("2026-07-28T16:00:00-05:00")
    assert from_z == from_utc == from_bogota
    for parsed in (from_z, from_utc, from_bogota):
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


def test_javascript_toisostring_shape_parses():
    parsed = schedule.parse_fire_time("2026-07-28T21:19:31.751Z")
    assert parsed.microsecond == 751000
    assert parsed.utcoffset() == timedelta(0)


def test_lowercase_z_parses():
    assert schedule.parse_fire_time("2026-07-28t21:00:00z") == schedule.parse_fire_time(
        "2026-07-28T21:00:00Z"
    )


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-28T21:00:00",
        "2026-07-28 21:00:00",
        "2026-07-28T21:00:00.751",
        "2026-07-28",
    ],
)
def test_naive_timestamp_is_a_config_error_not_a_guessed_zone(value):
    with pytest.raises(SecopError) as exc:
        schedule.parse_fire_time(value)
    assert exc.value.code is Failure.CONFIG_INVALID


@pytest.mark.parametrize("value", ["", "   ", "tomorrow at nine", "2026-13-40T00:00:00Z"])
def test_unparseable_fire_time_is_a_config_error(value):
    with pytest.raises(SecopError) as exc:
        schedule.parse_fire_time(value)
    assert exc.value.code is Failure.CONFIG_INVALID


# --- one parser, two entry points --------------------------------------------------

# Every spelling of "21:00:00Z" this app accepts. The basic forms (+0000, -05) are the ones
# `datetime.fromisoformat` rejects on this interpreter, so they are the ones a second parser
# would silently disagree about.
ACCEPTED_OFFSET_FORMS = [
    "2026-07-28T21:00:00Z",
    "2026-07-28t21:00:00z",
    "2026-07-28T21:00:00+00:00",
    "2026-07-28T21:00:00+0000",
    "2026-07-28T21:00:00+00",
    "2026-07-28T16:00:00-05:00",
    "2026-07-28T16:00:00-0500",
    "2026-07-28T16:00:00-05",
    "2026-07-28 21:00:00+0000",
]

T_UTC = datetime(2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ACCEPTED_OFFSET_FORMS)
def test_iso_8601_basic_offsets_parse(value):
    assert schedule.parse_fire_time(value) == T_UTC


@pytest.mark.parametrize("value", ACCEPTED_OFFSET_FORMS)
def test_config_accepts_exactly_what_arm_accepts(value):
    config = pytest.importorskip("secop_firstbid.config")
    cfg = config.Config(
        jwt="header.payload-long-enough-to-redact-sensibly.signature",
        auction_page_html_path="beforebid.html",
        bid_value="1",
        fire_time=value,
    )
    assert config.validate(cfg, now=NOW) == []

    armed = schedule.arm(cfg, schedule.build_waves(cfg), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert armed.t_wall == T_UTC


def test_config_delegates_to_this_parser():
    config = pytest.importorskip("secop_firstbid.config")
    assert config.parse_fire_time is schedule.parse_fire_time


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-28T21:00:00+5",
        "2026-07-28T21:00:00+00:0",
        "2026-07-28T21:00:00-",
        "2026-07-28",
    ],
)
def test_a_truncated_offset_is_not_read_as_an_offset(value):
    """`2026-07-28` ends in `-28`; an offset search over the whole string would take it."""
    with pytest.raises(SecopError) as exc:
        schedule.parse_fire_time(value)
    assert exc.value.code is Failure.CONFIG_INVALID


# --- build_waves -------------------------------------------------------------------


def test_single_mode_is_one_wave_at_t():
    cfg = StubConfig(mode="single", n_direct=2, n_validate_submit=1, n_validate_only=3)
    waves = schedule.build_waves(cfg)
    assert len(waves) == 1
    assert waves[0] == Wave(
        index=0, offset_ms=0.0, direct=2, validate=1, validate_only=3
    )
    assert waves[0].submissions == 3


def test_burst_offsets_are_exactly_minus60_minus30_0_plus30():
    waves = burst_waves()
    assert [wave.offset_ms for wave in waves] == [-60.0, -30.0, 0.0, 30.0]
    assert [wave.index for wave in waves] == [0, 1, 2, 3]


def test_burst_waves_all_carry_the_wave_composition():
    cfg = StubConfig(
        mode="burst",
        bid_count=3,
        pre_ms=60,
        interval_ms=30,
        wave_direct=2,
        wave_validate=1,
        wave_validate_only=1,
    )
    for wave in schedule.build_waves(cfg):
        assert (wave.direct, wave.validate, wave.validate_only) == (2, 1, 1)
        assert wave.submissions == 3


def test_burst_of_one_fires_at_t():
    waves = schedule.build_waves(StubConfig(mode="burst", bid_count=1))
    assert [wave.offset_ms for wave in waves] == [0.0]


def test_burst_rejects_bid_count_below_one():
    with pytest.raises(SecopError) as exc:
        schedule.build_waves(StubConfig(mode="burst", bid_count=0))
    assert exc.value.code is Failure.CONFIG_INVALID


def test_custom_offsets_are_taken_verbatim_and_unsorted():
    cfg = StubConfig(mode="custom", schedule_offsets_ms=[200, -100, 0, -100])
    waves = schedule.build_waves(cfg)
    assert [wave.offset_ms for wave in waves] == [200.0, -100.0, 0.0, -100.0]
    assert [wave.index for wave in waves] == [0, 1, 2, 3]


def test_custom_rejects_an_empty_schedule():
    with pytest.raises(SecopError) as exc:
        schedule.build_waves(StubConfig(mode="custom", schedule_offsets_ms=[]))
    assert exc.value.code is Failure.CONFIG_INVALID


def test_unknown_mode_is_a_config_error():
    with pytest.raises(SecopError) as exc:
        schedule.build_waves(StubConfig(mode="brust"))
    assert exc.value.code is Failure.CONFIG_INVALID


def test_wave_position_is_named_offset_ms_only():
    wave = burst_waves()[0]
    assert wave.offset_ms == -60.0
    assert not hasattr(wave, "offset")


# --- arm ---------------------------------------------------------------------------


def test_arm_converts_wall_to_monotonic_once():
    cfg = StubConfig(fire_time="2026-07-28T20:00:30Z")
    armed = schedule.arm(
        cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS
    )
    assert armed.t_mono_ns == NOW_MONO_NS + 30 * 1000000000
    assert armed.t_wall == datetime(2026, 7, 28, 20, 0, 30, tzinfo=timezone.utc)
    assert armed.armed_at_mono_ns == NOW_MONO_NS
    assert armed.armed_at_wall == NOW


def test_arm_is_deterministic_and_reads_no_live_clock_when_injected():
    cfg = StubConfig(fire_time="2026-07-28T20:00:30Z")
    first = schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    time.sleep(0.005)
    second = schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert first.t_mono_ns == second.t_mono_ns


def test_arm_keeps_sub_millisecond_precision():
    cfg = StubConfig(fire_time="2026-07-28T20:00:00.751Z")
    armed = schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert armed.t_mono_ns == NOW_MONO_NS + 751 * 1000000


def test_arm_accepts_a_non_utc_now_wall():
    cfg = StubConfig(fire_time="2026-07-28T20:00:30Z")
    bogota = timezone(timedelta(hours=-5))
    armed = schedule.arm(
        cfg,
        burst_waves(),
        now_wall=NOW.astimezone(bogota),
        now_mono_ns=NOW_MONO_NS,
    )
    assert armed.t_mono_ns == NOW_MONO_NS + 30 * 1000000000


def test_arm_rejects_a_naive_now_wall():
    cfg = StubConfig(fire_time="2026-07-28T20:00:30Z")
    with pytest.raises(SecopError) as exc:
        schedule.arm(
            cfg,
            burst_waves(),
            now_wall=NOW.replace(tzinfo=None),
            now_mono_ns=NOW_MONO_NS,
        )
    assert exc.value.code is Failure.CONFIG_INVALID


@pytest.mark.parametrize("fire_time", ["2026-07-28T19:59:59Z", "2026-07-28T20:00:00Z"])
def test_arm_refuses_a_fire_time_in_the_past(fire_time):
    cfg = StubConfig(fire_time=fire_time)
    with pytest.raises(SecopError) as exc:
        schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert exc.value.code is Failure.FIRE_TIME_PAST


def test_arm_refuses_a_fire_time_past_max_arm_ahead():
    cfg = StubConfig(fire_time="2026-07-28T22:00:00Z", max_arm_ahead_s=3600)
    with pytest.raises(SecopError) as exc:
        schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert exc.value.code is Failure.FIRE_TIME_TOO_FAR


def test_arm_allows_exactly_max_arm_ahead():
    cfg = StubConfig(fire_time="2026-07-28T21:00:00Z", max_arm_ahead_s=3600)
    armed = schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert armed.t_mono_ns == NOW_MONO_NS + 3600 * 1000000000


def test_arm_copies_the_wave_list():
    waves = burst_waves()
    armed = schedule.arm(StubConfig(), waves, now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    waves.append(Wave(index=9, offset_ms=999.0, direct=1, validate=0, validate_only=0))
    assert len(armed.waves) == 4


# --- release arithmetic ------------------------------------------------------------


def test_wave_release_applies_offset_and_lead():
    armed = schedule.arm(StubConfig(), burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    t = armed.t_mono_ns
    releases = [armed.wave_release_mono_ns(wave, 0) for wave in armed.waves]
    assert releases == [
        t - 60 * 1000000,
        t - 30 * 1000000,
        t,
        t + 30 * 1000000,
    ]


def test_lead_ms_pulls_every_wave_earlier():
    armed = schedule.arm(StubConfig(), burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    wave = armed.waves[2]
    assert armed.wave_release_mono_ns(wave, 5) == armed.t_mono_ns - 5 * 1000000
    assert armed.wave_release_mono_ns(
        armed.waves[0], 5
    ) == armed.t_mono_ns - 65 * 1000000


def test_offset_from_t_ms_is_signed_milliseconds_from_t():
    armed = schedule.arm(StubConfig(), burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert armed.offset_from_t_ms(armed.t_mono_ns) == 0.0
    assert armed.offset_from_t_ms(armed.t_mono_ns + 6500000) == 6.5
    assert armed.offset_from_t_ms(armed.t_mono_ns - 60000000) == -60.0


def test_offset_from_t_ms_recovers_the_scheduled_offset():
    armed = schedule.arm(StubConfig(), burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    for wave in armed.waves:
        release = armed.wave_release_mono_ns(wave, 0)
        assert armed.offset_from_t_ms(release) == wave.offset_ms


# --- schedule_span_ms --------------------------------------------------------------


def test_recommended_burst_spans_90_ms():
    assert schedule.schedule_span_ms(burst_waves()) == 90.0


def test_single_wave_and_empty_schedule_span_zero():
    assert schedule.schedule_span_ms(schedule.build_waves(StubConfig())) == 0.0
    assert schedule.schedule_span_ms([]) == 0.0


def test_span_ignores_wave_order():
    cfg = StubConfig(mode="custom", schedule_offsets_ms=[200, -100, 0])
    assert schedule.schedule_span_ms(schedule.build_waves(cfg)) == 300.0


# --- sleep_until_mono --------------------------------------------------------------


def test_sleep_until_mono_lands_at_or_after_the_target():
    target = time.monotonic_ns() + 8 * 1000000
    schedule.sleep_until_mono(target)
    landed = time.monotonic_ns()
    assert landed >= target
    assert landed - target < 50 * 1000000


def test_sleep_until_mono_returns_immediately_for_a_past_target():
    started = time.monotonic_ns()
    schedule.sleep_until_mono(started - 1000000000)
    assert time.monotonic_ns() - started < 50 * 1000000


def test_sleep_until_mono_honours_an_already_set_cancel():
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic_ns()
    schedule.sleep_until_mono(started + 5 * 1000000000, cancel=cancel)
    assert time.monotonic_ns() - started < 500 * 1000000


def test_sleep_until_mono_honours_a_cancel_set_while_waiting():
    cancel = threading.Event()
    timer = threading.Timer(0.01, cancel.set)
    started = time.monotonic_ns()
    timer.start()
    try:
        schedule.sleep_until_mono(started + 5 * 1000000000, cancel=cancel)
    finally:
        timer.cancel()
    assert time.monotonic_ns() - started < 2 * 1000000000
    assert cancel.is_set()


def test_sleep_until_mono_with_a_cancel_that_never_fires_still_lands():
    cancel = threading.Event()
    target = time.monotonic_ns() + 8 * 1000000
    schedule.sleep_until_mono(target, cancel=cancel)
    landed = time.monotonic_ns()
    assert landed >= target
    assert landed - target < 50 * 1000000
    assert not cancel.is_set()


# --- check_fire_time_vs_start_date -------------------------------------------------


def test_matching_start_date_produces_no_warning():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    assert schedule.check_fire_time_vs_start_date(t_wall, t_wall, 2) is None
    assert (
        schedule.check_fire_time_vs_start_date(
            t_wall, t_wall + timedelta(seconds=1), 2
        )
        is None
    )


def test_disagreeing_start_date_warns_and_never_overrides():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    start_date = t_wall + timedelta(seconds=5)
    warning = schedule.check_fire_time_vs_start_date(t_wall, start_date, 2)
    assert isinstance(warning, str)
    assert t_wall.isoformat() in warning
    assert start_date.isoformat() in warning
    assert "+5.000" in warning

    cfg = StubConfig(fire_time=FIRE_TIME)
    armed = schedule.arm(cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS)
    assert armed.t_wall == t_wall
    assert (
        schedule.arm(
            cfg, burst_waves(), now_wall=NOW, now_mono_ns=NOW_MONO_NS
        ).t_mono_ns
        == armed.t_mono_ns
    )


def test_start_date_earlier_than_fire_time_warns_with_a_negative_delta():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    warning = schedule.check_fire_time_vs_start_date(
        t_wall, t_wall - timedelta(seconds=30), 2
    )
    assert warning is not None
    assert "-30.000" in warning


def test_missing_start_date_is_silent():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    assert schedule.check_fire_time_vs_start_date(t_wall, None, 2) is None


def test_naive_start_date_is_reported_not_compared():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    naive = t_wall.replace(tzinfo=None) + timedelta(seconds=5)
    warning = schedule.check_fire_time_vs_start_date(t_wall, naive, 2)
    assert warning is not None
    assert "not compared" in warning


def test_start_date_in_another_zone_is_compared_as_an_instant():
    t_wall = schedule.parse_fire_time(FIRE_TIME)
    bogota = timezone(timedelta(hours=-5))
    assert (
        schedule.check_fire_time_vs_start_date(t_wall, t_wall.astimezone(bogota), 2)
        is None
    )


# ----------------------------------------------------------------- shift_t and wall_at


def _armed_at(now_wall: datetime, t_wall: datetime, waves: List[Wave]) -> schedule.Armed:
    mono = 7000000000000
    return schedule.Armed(
        t_wall=t_wall,
        t_mono_ns=mono + int((t_wall - now_wall).total_seconds() * 1e9),
        armed_at_wall=now_wall,
        armed_at_mono_ns=mono,
        waves=waves,
    )


def test_shift_t_moves_every_release_together_and_leaves_the_named_instant_alone() -> None:
    """A shift moves when this host acts, not which instant the operator named."""
    now = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    t_wall = datetime(2026, 11, 20, 21, 0, 0, tzinfo=timezone.utc)
    waves = [Wave(0, -50.0, 1, 0, 0), Wave(1, 0.0, 1, 0, 0), Wave(2, 120.0, 1, 0, 0)]
    armed = _armed_at(now, t_wall, waves)

    moved = schedule.shift_t(armed, 945.0)

    assert moved.t_wall == armed.t_wall
    assert moved.clock_shift_ms == 945.0
    assert armed.clock_shift_ms == 0.0
    for wave in waves:
        before = armed.wave_release_mono_ns(wave, 0)
        after = moved.wave_release_mono_ns(wave, 0)
        assert after - before == 945 * 1000000
    assert moved.armed_at_wall == armed.armed_at_wall
    assert moved.armed_at_mono_ns == armed.armed_at_mono_ns


def test_shift_t_accumulates_and_can_move_earlier() -> None:
    now = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    armed = _armed_at(now, now + timedelta(seconds=60), [Wave(0, 0.0, 1, 0, 0)])

    moved = schedule.shift_t(schedule.shift_t(armed, 945.0), -1000.0)

    assert moved.clock_shift_ms == pytest.approx(-55.0)
    assert moved.t_mono_ns - armed.t_mono_ns == -55 * 1000000


def test_offsets_after_a_shift_are_measured_from_the_moved_t() -> None:
    now = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    armed = _armed_at(now, now + timedelta(seconds=60), [Wave(0, 0.0, 1, 0, 0)])
    moved = schedule.shift_t(armed, 945.0)

    assert moved.offset_from_t_ms(moved.t_mono_ns) == 0.0
    assert moved.offset_from_t_ms(armed.t_mono_ns) == pytest.approx(-945.0)


def test_wall_at_reads_the_host_clock_off_the_arming_anchor() -> None:
    """One wall/monotonic pair for the whole run; a shift does not move the anchor."""
    now = datetime(2026, 11, 20, 20, 59, 0, tzinfo=timezone.utc)
    armed = _armed_at(now, now + timedelta(seconds=60), [])

    assert armed.wall_at(armed.armed_at_mono_ns) == now
    assert armed.wall_at(armed.armed_at_mono_ns + 1500000000) == now + timedelta(
        seconds=1.5
    )
    assert armed.wall_at(armed.t_mono_ns) == armed.t_wall
    moved = schedule.shift_t(armed, 945.0)
    assert moved.wall_at(armed.t_mono_ns) == armed.t_wall
