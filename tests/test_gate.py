"""State gate tests (plan §9.4).

Offline by construction: the gate talks to a fake client with :class:`HttpClient`'s ``get``
signature, and :func:`secop_firstbid.schedule.sleep_until_mono` is replaced by a fake that
advances a fake monotonic clock instead of blocking. That pairing is what lets the timing
assertions be exact — the gate must leave at ``T − gate_lead_ms``, not "about there" — and
keeps the suite free of both sockets and real sleeps.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
import requests

from secop_firstbid import endpoints, gate as gate_mod
from secop_firstbid.config import Config
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.gate import GateVerdict, run_gate, verdict_detail
from secop_firstbid.models import AuctionRef
from secop_firstbid.schedule import arm, build_waves
from secop_firstbid.session import HttpOutcome

TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"

FIRE_TIME = "2026-07-28T21:00:00Z"
ARMED_AT = datetime(2026, 7, 28, 20, 59, 0, tzinfo=timezone.utc)  # T - 60 s
ARMED_MONO_NS = 1000000000000
T_MONO_NS = ARMED_MONO_NS + 60 * 1000000000

GATE_LEAD_MS = 1500
GATE_RTT_MS = 135.3


class FakeClock:
    """The monotonic clock the fake sleep and the fake client agree on."""

    def __init__(self, now_ns: int) -> None:
        self.now_ns = now_ns


class FakeClient:
    """Stands in for :class:`secop_firstbid.session.HttpClient`, same ``get`` signature."""

    def __init__(
        self,
        clock: FakeClock,
        status: Optional[int] = 200,
        body: Optional[Any] = None,
        text: Optional[str] = None,
        exception: Optional[BaseException] = None,
        rtt_ms: float = GATE_RTT_MS,
    ) -> None:
        self.clock = clock
        self.status = status
        self.body = body
        self.text = text
        self.exception = exception
        self.rtt_ms = rtt_ms
        self.calls: List[Dict[str, Any]] = []

    def get(
        self,
        path: str,
        *,
        stage: str,
        thread_id: str = "main",
        role: Optional[str] = None,
        conditional: bool = True
    ) -> HttpOutcome:
        self.calls.append(
            {
                "path": path,
                "stage": stage,
                "thread_id": thread_id,
                "role": role,
                "conditional": conditional,
                "t_send_mono_ns": self.clock.now_ns,
            }
        )
        t_send = self.clock.now_ns
        t_recv = t_send + int(self.rtt_ms * 1000000)
        self.clock.now_ns = t_recv
        return HttpOutcome(
            status=self.status,
            body=self.body,
            text=self.text,
            headers={},
            t_send_mono_ns=t_send,
            t_recv_mono_ns=t_recv,
            rtt_ms=self.rtt_ms,
            corr_id="007-abcdef",
            exception=self.exception,
        )


class RecordingLogger:
    """Only ``event`` is exercised: the exchange itself is logged inside ``HttpClient``."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def event(
        self,
        *,
        stage: str,
        event: str,
        level: str = "INFO",
        thread_id: str = "main",
        role: Optional[str] = None,
        corr_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None
    ) -> None:
        self.events.append(
            {
                "stage": stage,
                "event": event,
                "level": level,
                "thread_id": thread_id,
                "role": role,
                "corr_id": corr_id,
                "detail": dict(detail) if detail else {},
            }
        )

    def named(self, name: str) -> List[Dict[str, Any]]:
        return [record for record in self.events if record["event"] == name]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ARMED_MONO_NS)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> List[int]:
    """Replace the real spin-wait: record the target and jump the fake clock to it."""
    targets: List[int] = []

    def fake_sleep(target_mono_ns: int, spin_ms: float = 2.0, cancel: Any = None) -> None:
        targets.append(target_mono_ns)
        clock.now_ns = max(clock.now_ns, target_mono_ns)

    monkeypatch.setattr(gate_mod, "sleep_until_mono", fake_sleep)
    return targets


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id="d6a49a23-3556-4b3d-9bb4-3e209279b9f8")


def make_cfg(**overrides: Any) -> Config:
    kwargs: Dict[str, Any] = dict(
        jwt=TOKEN,
        auction_page_html_path="beforebid.html",
        bid_value="1",
        fire_time=FIRE_TIME,
        gate_lead_ms=GATE_LEAD_MS,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def make_armed(cfg: Config):
    return arm(
        cfg,
        build_waves(cfg),
        now_wall=ARMED_AT,
        now_mono_ns=ARMED_MONO_NS,
    )


def body_with(state: Optional[str], exec_state: str = "Running") -> Dict[str, Any]:
    body: Dict[str, Any] = {"auctionExecutionStateCode": exec_state}
    if state is not None:
        body["auctionStateCode"] = state
    return body


def fire(
    cfg: Config,
    client: FakeClient,
    logger: RecordingLogger,
    ref: AuctionRef,
) -> GateVerdict:
    return run_gate(cfg, make_armed(cfg), client, ref, logger)


def test_gate_off_makes_no_request(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    cfg = make_cfg(gate_mode="off")
    client = FakeClient(clock, body=body_with("Finished"))

    verdict = fire(cfg, client, logger, ref)

    assert client.calls == []
    assert sleeps == []
    assert verdict.proceed is True
    assert verdict.reason == "gate disabled"
    assert verdict.mode == "off"
    assert verdict.failure is None
    assert verdict.state is None
    assert verdict.exec_state is None
    assert verdict.rtt_ms is None
    assert verdict.issued_offset_ms is None
    assert len(logger.named("gate_skipped")) == 1
    assert logger.named("gate_verdict") == []


def test_gate_is_issued_at_t_minus_gate_lead(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    cfg = make_cfg()
    armed = make_armed(cfg)
    client = FakeClient(clock, body=body_with("ReadyToStart"))

    verdict = run_gate(cfg, armed, client, ref, logger)

    assert armed.t_mono_ns == T_MONO_NS
    assert sleeps == [T_MONO_NS - GATE_LEAD_MS * 1000000]
    assert client.calls[0]["t_send_mono_ns"] == T_MONO_NS - GATE_LEAD_MS * 1000000
    assert verdict.issued_offset_ms == pytest.approx(-GATE_LEAD_MS)
    # The response has to land before T or the schedule is released late.
    assert verdict.issued_offset_ms + GATE_RTT_MS < 0.0


def test_gate_asks_for_get_auction_simple_on_the_gate_stage(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    cfg = make_cfg()
    client = FakeClient(clock, body=body_with("Started"))

    fire(cfg, client, logger, ref)

    call = client.calls[0]
    assert call["path"] == endpoints.get_auction_simple(ref.auction_id)
    assert call["path"] == "auction/GetAuctionSimple/" + ref.auction_id
    assert call["stage"] == "gate"
    assert call["role"] == "gate"


def test_gate_runs_once_for_the_whole_schedule(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """One request and one sleep, whatever the schedule — never one per wave (§9.4)."""
    cfg = make_cfg(mode="burst", bid_count=4, pre_ms=60, interval_ms=30)
    armed = make_armed(cfg)
    client = FakeClient(clock, body=body_with("ReadyToStart"))

    verdict = run_gate(cfg, armed, client, ref, logger)

    assert len(armed.waves) == 4
    assert len(client.calls) == 1
    assert len(sleeps) == 1
    assert verdict.proceed is True
    assert logger.named("gate_scheduled")[0]["detail"]["waves_covered"] == 4
    assert logger.named("gate_verdict")[0]["detail"]["waves_covered"] == 4


def test_verdict_is_immutable_so_the_cache_cannot_drift(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    verdict = fire(make_cfg(), FakeClient(clock, body=body_with("Started")), logger, ref)

    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.proceed = False  # type: ignore[misc]


def test_ready_to_start_proceeds(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """The expected pre-open reading at T − 1.5 s, and explicitly not a failure."""
    client = FakeClient(clock, body=body_with("ReadyToStart"))

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state == "ReadyToStart"
    assert logger.named("gate_verdict")[0]["level"] == "INFO"


def test_started_proceeds(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    verdict = fire(make_cfg(), FakeClient(clock, body=body_with("Started")), logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state == "Started"
    assert verdict.rtt_ms == GATE_RTT_MS


@pytest.mark.parametrize("state", ["Finished", "Canceled", "Deprecated"])
def test_terminal_states_abort(
    state: str,
    clock: FakeClock,
    sleeps: List[int],
    logger: RecordingLogger,
    ref: AuctionRef,
) -> None:
    client = FakeClient(clock, body=body_with(state))

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is False
    assert verdict.failure is Failure.STATE_FINISHED
    assert verdict.state == state
    assert state in verdict.reason
    assert logger.named("gate_verdict")[0]["level"] == "ERROR"


def test_paused_proceeds_loudly(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """A pause is reversible and the server is the authority; the gate warns, not aborts."""
    verdict = fire(make_cfg(), FakeClient(clock, body=body_with("Paused")), logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state == "Paused"
    assert logger.named("gate_verdict")[0]["level"] == "WARNING"


def test_unknown_state_proceeds(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    client = FakeClient(clock, body=body_with("SomethingNew"))

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state == "SomethingNew"
    assert logger.named("gate_verdict")[0]["level"] == "WARNING"


def test_missing_state_field_proceeds(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    client = FakeClient(clock, body=body_with(None))

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state is None
    assert verdict.exec_state == "Running"


def test_dead_token_aborts(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    client = FakeClient(clock, status=401, body=None, text="")

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is False
    assert verdict.failure is Failure.AUTH_EXPIRED
    assert logger.named("gate_verdict")[0]["level"] == "ERROR"


@pytest.mark.parametrize(
    "kwargs, transport",
    [
        ({"status": 403, "text": "<html>blocked</html>"}, Failure.WAF_BLOCK),
        ({"status": 429, "text": ""}, Failure.RATE_LIMIT),
        ({"status": 503, "text": "unavailable"}, Failure.UNKNOWN),
        (
            {"status": None, "exception": requests.exceptions.ConnectTimeout("boom")},
            Failure.NETWORK,
        ),
    ],
)
def test_an_unreadable_gate_proceeds(
    kwargs: Dict[str, Any],
    transport: Failure,
    clock: FakeClock,
    sleeps: List[int],
    logger: RecordingLogger,
    ref: AuctionRef,
) -> None:
    """A failed gate is not evidence the auction is closed — it must not cost the run."""
    client = FakeClient(clock, body=None, **kwargs)

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.proceed is True
    assert verdict.failure is None
    assert verdict.state is None
    record = logger.named("gate_verdict")[0]
    assert record["level"] == "WARNING"
    assert record["detail"]["transport_failure"] == transport.value


def test_execution_state_never_decides(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """``Running`` held on 128/128 responses, including all 125 after the flip to Finished.

    So it can neither rescue a terminal auction nor condemn a live one: both readings below
    say ``Running`` and the verdicts differ only by ``auctionStateCode``.
    """
    finished = fire(
        make_cfg(),
        FakeClient(clock, body=body_with("Finished", exec_state="Running")),
        logger,
        ref,
    )
    started = fire(
        make_cfg(),
        FakeClient(clock, body=body_with("Started", exec_state="Running")),
        logger,
        ref,
    )

    assert finished.exec_state == started.exec_state == "Running"
    assert finished.proceed is False
    assert started.proceed is True


def test_real_auction_simple_fixture_is_read_the_same_way(
    auction_simple: Dict[str, Any],
    clock: FakeClock,
    sleeps: List[int],
    logger: RecordingLogger,
    ref: AuctionRef,
) -> None:
    """The 59-key capture, whole, through the field names the gate actually reads."""
    client = FakeClient(clock, body=dict(auction_simple))

    verdict = fire(make_cfg(), client, logger, ref)

    assert verdict.state == "Started"
    assert verdict.exec_state == "Running"
    assert verdict.proceed is True
    assert verdict.failure is None


def test_non_positive_gate_lead_is_flagged(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """gate_lead_ms <= 0 issues the gate at or after T — the inline gate §9.4 rules out."""
    cfg = make_cfg(gate_lead_ms=0)
    client = FakeClient(clock, body=body_with("Started"))

    verdict = fire(cfg, client, logger, ref)

    assert sleeps == [T_MONO_NS]
    assert verdict.issued_offset_ms == pytest.approx(0.0)
    assert logger.named("gate_scheduled")[0]["level"] == "WARNING"


def test_a_late_gate_response_is_flagged(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """gate_lead_ms under the observed max RTT (1138.3 ms) lets the response land past T."""
    cfg = make_cfg(gate_lead_ms=100)
    client = FakeClient(clock, body=body_with("Started"), rtt_ms=1138.3)

    verdict = fire(cfg, client, logger, ref)

    record = logger.named("gate_verdict")[0]
    assert verdict.proceed is True
    assert record["level"] == "WARNING"
    assert "late_gate" in record["detail"]
    assert record["detail"]["landed_offset_ms"] == pytest.approx(1038.3, abs=0.5)


def test_unknown_gate_mode_is_a_config_error(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    cfg = make_cfg(gate_mode="every_wave")
    client = FakeClient(clock, body=body_with("Started"))

    with pytest.raises(SecopError) as excinfo:
        fire(cfg, client, logger, ref)

    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert client.calls == []


def test_verdict_detail_is_json_safe(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    import json

    verdict = fire(make_cfg(), FakeClient(clock, body=body_with("Finished")), logger, ref)
    detail = verdict_detail(verdict)

    assert detail["failure"] == "STATE_FINISHED"
    assert json.loads(json.dumps(detail))["state"] == "Finished"


def test_gate_never_moves_the_fire_instant(
    clock: FakeClock, sleeps: List[int], logger: RecordingLogger, ref: AuctionRef
) -> None:
    """The gate reads state; ``T`` comes from ``fire_time`` and nothing here may touch it."""
    cfg = make_cfg()
    armed = make_armed(cfg)
    before = (armed.t_mono_ns, armed.t_wall)

    run_gate(cfg, armed, FakeClient(clock, body=body_with("Started")), ref, logger)

    assert (armed.t_mono_ns, armed.t_wall) == before
    assert armed.t_wall == datetime(2026, 7, 28, 21, 0, tzinfo=timezone.utc)
    assert armed.t_wall - ARMED_AT == timedelta(seconds=60)
