"""Fire-path tests (plan §11).

The whole schedule runs for real — real threads, real :class:`~secop_firstbid.session.HttpClient`
objects, the real ``requests`` request pipeline, the real :class:`~secop_firstbid.logging_setup.RunLogger`
— with a scripted adapter mounted in place of the socket. Nothing here touches the network.

The assertions are the claims that decide whether a run's report is true: that ``dry_run``
sends no ``CreateBid``, that a post-win attempt is tagged ``DUPLICATE`` rather than the
byte-identical ``BID_ERR_MIN``, that an ``isValid:true`` carrying a warning is neither a win
nor tagged ``OK``, that a success-shaped body behind a 5xx, a 401, a 403 or a 429 is not a
win either, that a live submission cannot leave the process without ``confirm_live``, that
nothing of the thread's own is still occupying its connection when the release instant
arrives, and that every request that went out came back as a recorded result — including
the one whose thread was still running when the join deadline passed.

Wave offsets are a few milliseconds so the suite stays fast; the one test that needs an
earlier wave's verdict to land before a later wave releases spaces them wide enough that an
instant scripted response cannot lose the race.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from secop_firstbid import bidder
from secop_firstbid.bidder import (
    ROLE_DIRECT,
    ROLE_VALIDATE_ONLY,
    ROLE_VALIDATE_SUBMIT,
    FireOutcome,
    FirstSuccess,
    StartAudit,
    first_release_mono_ns,
    is_success,
    require_live_confirmation,
    run_schedule,
)
from secop_firstbid.config import Config
from secop_firstbid.errors import Failure, SecopError
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionDetail, AuctionRef, BidResult, Wave
from secop_firstbid.report import OUTCOME_BID_PLACED, build_summary
from secop_firstbid.schedule import Armed, arm, build_waves
from secop_firstbid.session import HttpClient

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    + ("VGhpc0lzTm90QVJlYWxUb2tlblBheWxvYWRf" * 20)
    + ".c2lnbmF0dXJlLWJ5dGVzLWdvLWhlcmVfLTAxMjM0NTY3ODk"
)

AUCTION_ID = "d6a49a23-3556-4b3d-9bb4-3e209279b9f8"

PAYLOAD_BYTES = (
    '{"auctionId":"' + AUCTION_ID + '","currency":"COP",'
    '"Date":"2026-07-28T21:19:31.751Z","ranking":0,"items":[],'
    '"totalValue":"10.000000","languageCode":"es-CO"}'
).encode("utf-8")

# The real captured rejection: a post-win duplicate is byte-identical to this.
BID_ERR_MIN = {
    "bidId": 0,
    "currentRankingPosition": 2,
    "isValid": False,
    "errorMessage": (
        "El valor total tiene que ser inferior al valor de su último lance para la "
        "subasta actual."
    ),
    "errorMessageLocale": "BidWarn_Total_MinBid_Diff_Desc",
    "minBid": "7.813.974,32 COP",
    "errorType": "BidErrMin",
}

ACCEPTED = {
    "bidId": 4417231,
    "currentRankingPosition": 1,
    "isValid": True,
    "errorMessage": "",
    "errorMessageLocale": None,
    "minBid": None,
    "errorType": None,
}

VALID_NO_SUBMIT_YET = {
    "bidId": 0,
    "currentRankingPosition": 1,
    "isValid": True,
    "errorMessage": "",
    "errorType": None,
}

# The case the plan left undecided and this app reads as "not a win".
VALID_WITH_WARNING = dict(ACCEPTED, errorMessage="Su lance supera el presupuesto oficial")

# A body that passes the success predicate behind a status that says the request was not
# answered. Nothing in the capture is a successful CreateBid, so neither half can be
# inferred from the other.
SUCCESS_SHAPED = {"isValid": True, "errorMessage": "", "bidId": 9}

# The tag classify gives each of those statuses; a win must agree with the tag.
FAILED_STATUSES = [
    (500, Failure.UNKNOWN),
    (502, Failure.UNKNOWN),
    (503, Failure.UNKNOWN),
    (401, Failure.AUTH_EXPIRED),
    (403, Failure.WAF_BLOCK),
    (429, Failure.RATE_LIMIT),
]

# T is set this far ahead so every thread is created, started and asleep before release.
ARM_LEAD_MS = 120


class ScriptedAdapter(HTTPAdapter):
    """Stands in for the socket, answering by route.

    One instance is mounted on every client's Session, so it is called from all the fire
    threads at once and serialises its own bookkeeping.

    ``create_bodies`` is consumed one entry per ``CreateBid``; the last entry repeats, which
    is how "first one wins, the rest bounce" is scripted.
    """

    def __init__(
        self,
        create_bodies: Optional[List[Dict[str, Any]]] = None,
        validate_body: Optional[Dict[str, Any]] = None,
        create_delay_s: float = 0.0,
    ) -> None:
        super().__init__(pool_connections=1, pool_maxsize=8, max_retries=0)
        self.create_bodies = list(create_bodies or [ACCEPTED])
        self.validate_body = validate_body if validate_body is not None else VALID_NO_SUBMIT_YET
        self.create_delay_s = create_delay_s
        self.calls: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self._create_index = 0

    def urls(self, needle: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [call for call in self.calls if needle in call["url"]]

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        url = request.url
        with self.lock:
            self.calls.append({"method": request.method, "url": url, "body": request.body})
            if "CreateBid" in url:
                index = min(self._create_index, len(self.create_bodies) - 1)
                self._create_index += 1
                body = self.create_bodies[index]
            elif "ValidateBid" in url:
                body = self.validate_body
            else:
                body = None

        response = requests.Response()
        response.status_code = 200
        response._content = b"" if body is None else json.dumps(body).encode("utf-8")
        response.headers = CaseInsensitiveDict(
            {"Date": "Tue, 28 Jul 2026 21:19:31 GMT", "Content-Type": "application/json"}
        )
        response.url = url
        response.request = request
        response.raw = None
        return response


class TimedAdapter(ScriptedAdapter):
    """Records when each request left, and makes the audit route cost a round trip.

    ``audit_delay_s`` stands in for what ``SetStartBiddingAudit`` costs on the real API —
    168.5 ms at the fastest anywhere in the capture, and its whole connect or read timeout
    when the server stalls in the seconds before an opening. It is the delay that used to
    be paid in front of a submission, when the audit and the bid shared a thread's one
    pooled connection.

    Its own lock, not ``ScriptedAdapter.lock``: ``threading.Lock`` is not reentrant and the
    base ``send`` takes that one.
    """

    def __init__(self, audit_delay_s: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audit_delay_s = audit_delay_s
        self.sent_lock = threading.Lock()
        self.sent: List[Tuple[str, int]] = []

    def send(self, request, **kwargs):  # type: ignore[override]
        with self.sent_lock:
            self.sent.append((request.url, time.monotonic_ns()))
        if "Audit" in request.url:
            time.sleep(self.audit_delay_s)
        return super().send(request, **kwargs)

    def sent_matching(self, needle: str) -> List[int]:
        with self.sent_lock:
            return [ns for url, ns in self.sent if needle in url]


class StatusAdapter(ScriptedAdapter):
    """Answers with the scripted bodies under one status other than 200."""

    def __init__(self, status: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.status = status

    def send(self, request, **kwargs):  # type: ignore[override]
        response = super().send(request, **kwargs)
        response.status_code = self.status
        return response


class AlwaysProceed:
    """The ``gate_mode="off"`` verdict shape; :mod:`secop_firstbid.gate` owns the real one."""

    mode = "off"
    issued_offset_ms = None
    state = None
    exec_state = None
    rtt_ms = None
    proceed = True
    reason = "gate disabled"
    failure = None


class Blocked(AlwaysProceed):
    proceed = False
    reason = "auction is Finished"
    failure = Failure.STATE_FINISHED


DETAIL = AuctionDetail(
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


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    values: Dict[str, Any] = dict(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="1",
        fire_time="2026-07-28T21:00:00Z",
        run_root=str(tmp_path / "runs"),
        connect_timeout_ms=500,
        read_timeout_ms=500,
        do_start_audit=False,
        stop_on_success=False,
    )
    values.update(overrides)
    return Config(**values)


def arm_now(cfg: Config, lead_ms: int = ARM_LEAD_MS) -> Armed:
    """Arm against an injected ``now`` so ``T`` is a fixed, tiny distance ahead."""
    now_wall = datetime.now(timezone.utc)
    cfg.fire_time = (now_wall + timedelta(milliseconds=lead_ms)).isoformat().replace(
        "+00:00", "Z"
    )
    return arm(cfg, build_waves(cfg), now_wall=now_wall)


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id=AUCTION_ID)


@pytest.fixture
def logger_factory(tmp_path: Path) -> Iterator[Any]:
    made: List[RunLogger] = []

    def build(cfg: Config) -> RunLogger:
        run_logger = RunLogger(cfg)
        made.append(run_logger)
        return run_logger

    yield build
    for run_logger in made:
        run_logger.close()


def make_clients(
    cfg: Config, ref: AuctionRef, logger: RunLogger, adapter: ScriptedAdapter, count: int
) -> List[HttpClient]:
    jar = RequestsCookieJar()
    clients = []
    for _ in range(count):
        client = HttpClient(cfg, ref, jar, logger)
        client.session.mount("https://", adapter)
        client.session.mount("http://", adapter)
        clients.append(client)
    return clients


def fire(
    cfg: Config,
    ref: AuctionRef,
    logger: RunLogger,
    adapter: ScriptedAdapter,
    gate: Any = None,
    lead_ms: int = ARM_LEAD_MS,
) -> FireOutcome:
    armed = arm_now(cfg, lead_ms)
    threads = sum(
        wave.direct + wave.validate + wave.validate_only for wave in armed.waves
    )
    # One client more than there are fire threads: the start audit gets the last one, as
    # it does in a real run, and never borrows a bid's connection.
    clients = make_clients(cfg, ref, logger, adapter, max(threads, 1) + 1)
    audit = StartAudit(cfg, armed, ref, clients[-1], logger)
    audit.start()
    try:
        return run_schedule(
            cfg,
            armed,
            ref,
            DETAIL,
            PAYLOAD_BYTES,
            clients[:-1],
            logger,
            gate if gate is not None else AlwaysProceed(),
            start_audit=audit,
        )
    finally:
        audit.join(timeout_s=5.0)
        for client in clients:
            client.close()


def events(logger: RunLogger) -> List[Dict[str, Any]]:
    logger.flush()
    path = logger.run_dir / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def named(logger: RunLogger, event: str) -> List[Dict[str, Any]]:
    return [record for record in events(logger) if record["event"] == event]


def test_is_success_needs_an_empty_error_message() -> None:
    assert is_success(ACCEPTED) is True
    assert is_success({"isValid": True}) is True
    assert is_success({"isValid": True, "errorMessage": ""}) is True
    assert is_success({"isValid": True, "errorMessage": "   "}) is True
    assert is_success({"isValid": True, "errorMessage": None}) is True

    # The whole point of the single predicate: a warning alongside isValid:true is not a win.
    assert is_success(VALID_WITH_WARNING) is False
    assert is_success(BID_ERR_MIN) is False
    assert is_success({"isValid": "true"}) is False
    assert is_success({}) is False
    assert is_success(None) is False  # type: ignore[arg-type]


def test_first_success_is_compare_and_set() -> None:
    latch = FirstSuccess()
    assert latch.seen() is False
    assert latch.winner is None

    def result(thread_id: str) -> BidResult:
        return BidResult(
            thread_id=thread_id,
            role=ROLE_DIRECT,
            wave_index=0,
            scheduled_offset_ms=0.0,
            actual_offset_ms=0.0,
            endpoint=bidder.ENDPOINT_CREATE,
            status=200,
            is_valid=True,
            bid_id=1,
            error_type=None,
            error_message=None,
            error_message_locale=None,
            min_bid=None,
            ranking=1,
            t_send_mono_ns=0,
            t_recv_mono_ns=1,
            rtt_ms=0.001,
        )

    first = result("w00-D00")
    assert latch.mark(first) is True
    assert latch.mark(result("w00-D01")) is False
    assert latch.seen() is True
    assert latch.winner is first


def test_dry_run_sends_no_create_bid(tmp_path: Path, ref: AuctionRef, logger_factory) -> None:
    cfg = make_config(
        tmp_path,
        mode="burst",
        bid_count=3,
        pre_ms=0,
        interval_ms=4,
        wave_direct=1,
        wave_validate=1,
        wave_validate_only=1,
        dry_run=True,
        do_start_audit=True,
        v_gate_on_valid=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    outcome = fire(cfg, ref, logger, adapter)

    assert adapter.urls("CreateBid") == []
    assert outcome.submissions_sent == 0
    assert outcome.winner is None
    # V and P still go out: dry_run skips the submission, not the validation.
    assert len(adapter.urls("ValidateBid")) == 6
    assert len(outcome.results) == 6
    assert {result.endpoint for result in outcome.results} == {bidder.ENDPOINT_VALIDATE}

    # One start audit for the run, not one per thread: V and P threads still send, so the
    # run still writes it.
    assert len(adapter.urls("SetStartBiddingAudit")) == 1

    skips = [
        record
        for record in named(logger, "thread_decision")
        if record["detail"]["decision"] == "dry_run_skip_create_bid"
    ]
    assert sorted(record["thread_id"] for record in skips) == [
        "w00-D00",
        "w00-V00",
        "w01-D00",
        "w01-V00",
        "w02-D00",
        "w02-V00",
    ]
    assert {record["role"] for record in skips} == {ROLE_DIRECT, ROLE_VALIDATE_SUBMIT}
    for record in skips:
        assert record["detail"]["reason"]

    footprint = named(logger, "dry_run_footprint")
    assert len(footprint) == 1
    assert footprint[0]["level"] == "WARNING"
    assert footprint[0]["detail"]["create_bid_sent"] == 0
    assert footprint[0]["detail"]["validate_bid_sent"] == 6
    assert "Logs_Search_NewBid" in footprint[0]["detail"]["note"]
    assert "Logs_Search_StartBidding" in footprint[0]["detail"]["note"]


def test_confirm_live_false_blocks_a_live_submit(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=2, dry_run=False, confirm_live=False)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    with pytest.raises(SecopError) as excinfo:
        fire(cfg, ref, logger, adapter)

    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert excinfo.value.detail["submissions_planned"] == 2
    # Refused before a single request left the process.
    assert adapter.calls == []

    # The same schedule with confirm_live on does submit, which is what makes the block a
    # gate rather than an unrelated failure.
    cfg.confirm_live = True
    outcome = fire(cfg, ref, logger, adapter)
    assert outcome.submissions_sent == 2


def test_a_probe_only_schedule_does_not_need_confirm_live(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path, n_direct=0, n_validate_submit=0, n_validate_only=2, confirm_live=False
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    outcome = fire(cfg, ref, logger, adapter)

    assert adapter.urls("CreateBid") == []
    assert outcome.submissions_sent == 0
    assert len(outcome.results) == 2
    assert {result.role for result in outcome.results} == {ROLE_VALIDATE_ONLY}


def test_first_success_records_the_winning_wave_and_role(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    # The waves are 100 ms apart, not the few milliseconds the rest of the file uses: this
    # is the one assertion about which wave reaches the wire first, and a scripted adapter
    # answers so fast that the gap has to exceed the cost of building a request.
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 100],
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        confirm_live=True,
        stop_on_success=False,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[ACCEPTED, BID_ERR_MIN])

    outcome = fire(cfg, ref, logger, adapter)

    winner = outcome.winner
    assert winner is not None
    assert winner.wave_index == 0
    assert winner.role == ROLE_DIRECT
    assert winner.endpoint == bidder.ENDPOINT_CREATE
    assert winner.is_valid is True
    assert winner.bid_id == 4417231
    assert winner.ranking == 1
    assert winner.failure is Failure.OK
    assert winner.scheduled_offset_ms == pytest.approx(0.0)
    assert winner.actual_offset_ms is not None

    announced = named(logger, "first_success")
    assert len(announced) == 1
    assert announced[0]["detail"]["wave_index"] == 0
    assert announced[0]["role"] == ROLE_DIRECT
    assert announced[0]["detail"]["bid_id"] == 4417231


def test_an_is_valid_true_with_a_warning_is_not_a_win(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=1, confirm_live=True)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[VALID_WITH_WARNING])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.submissions_sent == 1
    assert outcome.winner is None
    assert named(logger, "first_success") == []
    assert outcome.results[0].is_valid is True
    # The tag and the win condition are the same predicate: an attempt tagged OK inside a
    # run that latched no winner is a summary that cannot say why the bid was not placed.
    assert outcome.results[0].failure is not Failure.OK
    assert outcome.results[0].failure is Failure.VALIDATION_FAILED
    assert outcome.results[0].error_message == VALID_WITH_WARNING["errorMessage"]

    logged = named(logger, "bid_result")
    assert logged[0]["detail"]["failure"] == Failure.VALIDATION_FAILED.value
    assert logged[0]["level"] == "WARNING"


@pytest.mark.parametrize(
    "status,failure", FAILED_STATUSES, ids=[str(s) for s, _f in FAILED_STATUSES]
)
def test_a_success_body_under_a_failed_status_is_not_a_win(
    tmp_path: Path, ref: AuctionRef, logger_factory, status: int, failure: Failure
) -> None:
    """The win is the classifier's verdict, so it cannot contradict the attempt's own tag.

    Read from the body alone, every one of these responses latches the winner and the run
    reports BID_PLACED while tagging that same response AUTH_EXPIRED, WAF_BLOCK, RATE_LIMIT
    or UNKNOWN — a bid the run says it placed and cannot show a verdict for.
    """
    cfg = make_config(tmp_path, n_direct=1, confirm_live=True)
    logger = logger_factory(cfg)
    adapter = StatusAdapter(status, create_bodies=[SUCCESS_SHAPED])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.submissions_sent == 1
    assert outcome.winner is None
    assert named(logger, "first_success") == []

    result = outcome.results[0]
    assert result.status == status
    assert result.is_valid is True
    assert result.failure is failure
    assert result.failure is not Failure.OK

    assert build_summary(cfg, fire=outcome)["outcome"] != OUTCOME_BID_PLACED


def test_a_success_body_under_a_failed_status_cancels_no_wave(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """``stop_on_success`` must not spend the remaining waves on a response nobody won."""
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 250],
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        confirm_live=True,
        stop_on_success=True,
    )
    logger = logger_factory(cfg)
    adapter = StatusAdapter(503, create_bodies=[SUCCESS_SHAPED])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.winner is None
    assert named(logger, "stop_on_success_triggered") == []
    assert outcome.waves_fired == 2
    assert outcome.waves_dropped == 0
    assert len(adapter.urls("CreateBid")) == 2


def test_a_post_win_attempt_is_a_duplicate_not_a_min_bid_rejection(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 200],
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        confirm_live=True,
        stop_on_success=False,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[ACCEPTED, BID_ERR_MIN])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.winner is not None and outcome.winner.wave_index == 0
    assert outcome.waves_fired == 2 and outcome.waves_dropped == 0

    late = [result for result in outcome.results if result.wave_index == 1]
    assert len(late) == 1
    # Byte-identical to a genuine rejection: only the local send ordering separates them.
    assert late[0].error_type == "BidErrMin"
    assert late[0].min_bid == "7.813.974,32 COP"
    assert late[0].failure is Failure.DUPLICATE
    assert late[0].failure is not Failure.BID_ERR_MIN

    logged = [
        record
        for record in named(logger, "bid_result")
        if record["detail"]["wave_index"] == 1
    ]
    assert logged[0]["detail"]["sent_after_first_success"] is True
    assert logged[0]["detail"]["failure"] == Failure.DUPLICATE.value
    assert logged[0]["detail"]["error_type"] == "BidErrMin"


def test_a_pre_win_rejection_keeps_its_bid_err_min_tag(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=1, confirm_live=True)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[BID_ERR_MIN])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.winner is None
    assert outcome.results[0].failure is Failure.BID_ERR_MIN


def test_every_planned_submission_produced_a_logged_result(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        mode="burst",
        bid_count=3,
        pre_ms=8,
        interval_ms=4,
        wave_direct=2,
        wave_validate=1,
        wave_validate_only=1,
        confirm_live=True,
        do_start_audit=True,
        stop_on_success=False,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[BID_ERR_MIN])

    outcome = fire(cfg, ref, logger, adapter)

    planned_submissions = 3 * (2 + 1)
    planned_validates = 3 * (1 + 1)

    assert outcome.submissions_sent == planned_submissions
    assert len(adapter.urls("CreateBid")) == planned_submissions
    assert len(adapter.urls("ValidateBid")) == planned_validates
    # One start audit for the whole run, however many threads fire.
    assert len(adapter.urls("SetStartBiddingAudit")) == 1

    creates = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_CREATE]
    validates = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_VALIDATE]
    assert len(creates) == planned_submissions
    assert len(validates) == planned_validates
    assert len(outcome.results) == planned_submissions + planned_validates

    # Every result is placed on the schedule, and every request is in the event log.
    for result in outcome.results:
        assert result.scheduled_offset_ms is not None
        assert result.actual_offset_ms is not None
        assert result.corr_id
        assert result.status == 200

    corr_ids = {result.corr_id for result in outcome.results}
    logged = {
        record["corr_id"]
        for record in events(logger)
        if record["event"] == "http_response" and record["stage"] == bidder.STAGE
    }
    assert corr_ids <= logged

    assert {r.wave_index for r in outcome.results} == {0, 1, 2}
    assert {r.role for r in creates} == {ROLE_DIRECT, ROLE_VALIDATE_SUBMIT}
    assert {r.role for r in validates} == {ROLE_VALIDATE_SUBMIT, ROLE_VALIDATE_ONLY}


def test_a_start_audit_that_stalls_past_the_release_does_not_delay_the_bid(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """The audit is on a connection of its own, so a hung audit costs no bid its instant.

    A thread owns one connection, so anything it sends before its bid is serial with it.
    When the audit rode on the bid's connection, a lead only helped while the server
    answered: on 2026-11-20 audits that hung for their whole timeout took their thread's
    bid with them, by up to ten seconds. Here the audit is still unanswered when the
    release instant arrives, and the bid leaves on time anyway.
    """
    cfg = make_config(
        tmp_path,
        n_direct=1,
        do_start_audit=True,
        start_audit_lead_ms=100,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = TimedAdapter(audit_delay_s=0.450)

    outcome = fire(cfg, ref, logger, adapter, lead_ms=300)

    audits = adapter.sent_matching("SetStartBiddingAudit")
    assert len(audits) == 1

    creates = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_CREATE]
    assert len(creates) == 1
    create = creates[0]

    # The audit went out first and was still in flight when the bid left: 100 ms of lead
    # against a 450 ms answer.
    assert audits[0] < create.t_send_mono_ns
    assert (create.t_send_mono_ns - audits[0]) / 1e6 < 450.0

    slip_ms = create.actual_offset_ms - create.scheduled_offset_ms
    assert slip_ms < 60.0, (
        "CreateBid left %.1f ms after its scheduled instant; the stalled audit is back "
        "in front of it" % slip_ms
    )


def test_fire_threads_send_nothing_but_bids(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """No fire client ever carries the audit, whatever the roles."""
    cfg = make_config(
        tmp_path,
        mode="burst",
        bid_count=2,
        pre_ms=0,
        interval_ms=4,
        wave_direct=1,
        wave_validate=1,
        wave_validate_only=1,
        do_start_audit=True,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[BID_ERR_MIN])

    fire(cfg, ref, logger, adapter)

    audit_threads = {
        record["thread_id"]
        for record in events(logger)
        if record["event"] == "http_response" and "Audit" in record["detail"]["url"]
    }
    assert audit_threads == {bidder.START_AUDIT_THREAD_ID}


def test_a_run_with_no_start_audit_sends_none(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=1, do_start_audit=False, confirm_live=True)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[BID_ERR_MIN])

    fire(cfg, ref, logger, adapter)

    assert adapter.urls("SetStartBiddingAudit") == []


def test_a_dry_run_of_direct_threads_sends_no_start_audit(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """A D thread's only request is the submission a dry run skips, so the run is silent."""
    cfg = make_config(tmp_path, n_direct=2, do_start_audit=True, dry_run=True)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    fire(cfg, ref, logger, adapter)

    assert adapter.calls == []


def test_a_cancelled_wave_writes_no_row_of_its_own(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """A wave cancelled before release sent nothing; the run's one audit is not its row."""
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 900],
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        do_start_audit=True,
        stop_on_success=True,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[ACCEPTED])

    outcome = fire(cfg, ref, logger, adapter, lead_ms=200)

    assert outcome.winner is not None
    cancelled = [
        record
        for record in named(logger, "thread_decision")
        if record["detail"]["decision"] == "wave_cancelled"
    ]
    assert [record["detail"]["wave_index"] for record in cancelled] == [1]
    assert "wrote no row" in cancelled[0]["detail"]["reason"]
    assert len(adapter.urls("CreateBid")) == 1
    assert len(adapter.urls("SetStartBiddingAudit")) == 1


def test_a_validate_that_fails_the_predicate_stops_its_own_submission(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        n_direct=0,
        n_validate_submit=1,
        n_validate_only=0,
        v_gate_on_valid=True,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(validate_body=BID_ERR_MIN)

    outcome = fire(cfg, ref, logger, adapter)

    assert adapter.urls("CreateBid") == []
    assert outcome.submissions_sent == 0
    assert len(outcome.results) == 1
    assert outcome.results[0].endpoint == bidder.ENDPOINT_VALIDATE

    blocked = [
        record
        for record in named(logger, "thread_decision")
        if record["detail"]["decision"] == "validate_gate_blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0]["detail"]["error_type"] == "BidErrMin"


def test_validate_lead_moves_the_validate_call_ahead_of_its_wave(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        n_direct=0,
        n_validate_submit=1,
        n_validate_only=0,
        validate_lead_ms=25,
        v_gate_on_valid=True,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    outcome = fire(cfg, ref, logger, adapter)

    validate = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_VALIDATE][0]
    create = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_CREATE][0]
    assert validate.scheduled_offset_ms == pytest.approx(-25.0)
    # The submission is scheduled at the wave's own instant, which is what validate_lead_ms
    # exists to make room for.
    assert create.scheduled_offset_ms == pytest.approx(0.0)
    assert validate.actual_offset_ms < create.actual_offset_ms


def test_stop_on_success_drops_a_wave_only_when_the_span_allows_it(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 250],
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        confirm_live=True,
        stop_on_success=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[ACCEPTED, BID_ERR_MIN])

    outcome = fire(cfg, ref, logger, adapter)

    assert outcome.winner is not None
    assert outcome.waves_fired == 1
    assert outcome.waves_dropped == 1
    assert outcome.submissions_sent == 1
    assert len(adapter.urls("CreateBid")) == 1

    cancelled = [
        record
        for record in named(logger, "thread_decision")
        if record["detail"]["decision"] == "wave_cancelled"
    ]
    assert [record["detail"]["wave_index"] for record in cancelled] == [1]
    assert named(logger, "stop_on_success_triggered")

    complete = named(logger, "fire_complete")[0]["detail"]
    assert complete["waves_fired"] == 1
    assert complete["waves_dropped"] == 1
    assert complete["dropped_wave_indices"] == [1]


def test_stop_on_success_reports_that_it_drops_nothing_at_the_recommended_burst(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    # The plan's recommended clock-uncertain shape: bids at T-60, T-30, T, T+30.
    cfg = make_config(
        tmp_path,
        mode="burst",
        bid_count=4,
        pre_ms=60,
        interval_ms=30,
        wave_direct=1,
        wave_validate=0,
        wave_validate_only=0,
        confirm_live=True,
        stop_on_success=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter(create_bodies=[BID_ERR_MIN])
    armed = arm_now(cfg)

    assert [wave.offset_ms for wave in armed.waves] == [-60.0, -30.0, 0.0, 30.0]

    clients = make_clients(cfg, ref, logger, adapter, 4)
    try:
        outcome = run_schedule(
            cfg, armed, ref, DETAIL, PAYLOAD_BYTES, clients, logger, AlwaysProceed()
        )
    finally:
        for client in clients:
            client.close()

    assert outcome.waves_fired == 4
    assert outcome.waves_dropped == 0

    reach = named(logger, "stop_on_success_reach")
    assert len(reach) == 1
    assert reach[0]["level"] == "INFO"
    detail = reach[0]["detail"]
    assert detail["stop_on_success"] is True
    assert detail["schedule_span_ms"] == pytest.approx(90.0)
    assert detail["observed_min_bid_rtt_ms"] == bidder.OBSERVED_MIN_BID_RTT_MS
    assert detail["observed_p50_bid_rtt_ms"] == bidder.OBSERVED_P50_BID_RTT_MS
    # 90 ms of schedule against a 168.5 ms floor on any verdict: nothing is droppable.
    assert detail["waves_droppable_at_min_rtt"] == []


def test_a_blocking_gate_fires_nothing(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path,
        mode="burst",
        bid_count=2,
        pre_ms=4,
        interval_ms=4,
        wave_direct=1,
        confirm_live=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    outcome = fire(cfg, ref, logger, adapter, gate=Blocked())

    assert adapter.calls == []
    assert outcome.results == []
    assert outcome.winner is None
    assert outcome.waves_fired == 0
    assert outcome.waves_dropped == 2

    blocked = named(logger, "fire_blocked_by_gate")
    assert len(blocked) == 1
    assert blocked[0]["detail"]["failure"] == Failure.STATE_FINISHED.value


def test_a_network_failure_is_a_recorded_result_not_a_lost_thread(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=1, confirm_live=True)
    logger = logger_factory(cfg)

    class FailingAdapter(ScriptedAdapter):
        def send(self, request, **kwargs):  # type: ignore[override]
            if "CreateBid" in request.url:
                raise requests.exceptions.ReadTimeout("read timed out")
            return super().send(request, **kwargs)

    outcome = fire(cfg, ref, logger, FailingAdapter())

    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.status is None
    assert result.failure is Failure.NETWORK
    assert "ReadTimeout" in (result.exception or "")
    assert outcome.winner is None
    # Counted at send time: the request may still have reached the server.
    assert outcome.submissions_sent == 1


def test_a_create_bid_answering_200_with_no_body_is_not_a_success(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """A 2xx carrying no verdict is silence; only a body can say a bid was accepted."""
    cfg = make_config(tmp_path, n_direct=1, confirm_live=True)
    logger = logger_factory(cfg)

    class EmptyBodyAdapter(ScriptedAdapter):
        def send(self, request, **kwargs):  # type: ignore[override]
            response = super().send(request, **kwargs)
            if "CreateBid" in request.url:
                response._content = b""
            return response

    outcome = fire(cfg, ref, logger, EmptyBodyAdapter())

    assert outcome.winner is None
    assert named(logger, "first_success") == []
    assert outcome.results[0].failure is not Failure.OK
    assert outcome.results[0].failure is Failure.UNKNOWN
    assert named(logger, "bid_result")[0]["level"] == "WARNING"


def test_a_schedule_of_waves_with_no_threads_reports_instead_of_raising(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    """Waves whose composition is all zero plan no thread and no release instant.

    ``config.validate`` refuses this composition, so the only way here is a caller that
    built its own ``Armed``; the run still has to end in a summary rather than in a
    ValueError the exit-code table has no entry for.
    """
    cfg = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[0, 5],
        wave_direct=0,
        wave_validate=0,
        wave_validate_only=0,
        dry_run=True,
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()

    outcome = fire(cfg, ref, logger, adapter)

    assert adapter.calls == []
    assert outcome.results == []
    assert outcome.winner is None
    assert outcome.submissions_sent == 0
    assert outcome.waves_fired == 0


def test_an_abandoned_thread_still_produces_an_attempt_row(
    tmp_path: Path, ref: AuctionRef, logger_factory, monkeypatch
) -> None:
    """A thread past the join deadline may have bid; the report must not deny it.

    Its ``CreateBid`` was counted at send time and the caller closes the Sessions and the
    RunLogger the moment the join returns, so anything the thread queues afterwards is
    dropped. A submission with no attempt row, no verdict and no error is the one shape of
    report that contradicts itself.
    """
    monkeypatch.setattr(bidder, "_JOIN_MARGIN_S", 0.2)
    cfg = make_config(
        tmp_path, n_direct=1, confirm_live=True, connect_timeout_ms=1, read_timeout_ms=1
    )
    logger = logger_factory(cfg)
    released = threading.Event()

    class StuckAdapter(ScriptedAdapter):
        def send(self, request, **kwargs):  # type: ignore[override]
            if "CreateBid" in request.url:
                released.wait(timeout=30.0)
            return super().send(request, **kwargs)

    try:
        outcome = fire(cfg, ref, logger, StuckAdapter())
    finally:
        released.set()

    abandoned = named(logger, "threads_abandoned")
    assert len(abandoned) == 1
    assert abandoned[0]["detail"]["threads"] == ["w00-D00"]

    assert outcome.submissions_sent == 1
    creates = [r for r in outcome.results if r.endpoint == bidder.ENDPOINT_CREATE]
    assert len(creates) == outcome.submissions_sent

    result = creates[0]
    assert result.thread_id == "w00-D00"
    assert result.status is None
    assert result.is_valid is None
    assert result.failure is Failure.NETWORK
    assert "abandoned past the join deadline" in (result.exception or "")
    # It left at a known instant: the row places the request on the schedule even though
    # no verdict came back.
    assert result.actual_offset_ms is not None
    assert outcome.winner is None


# ------------------------------------------------- what has to be over before the bids


def test_the_first_release_is_the_earliest_instant_any_thread_wakes(tmp_path: Path) -> None:
    """The start audit and the keep-alives are placed against this, not against T.

    A custom schedule can open before T, and a V or P thread opens ``validate_lead_ms``
    before its wave, so T itself can be seconds after the first request leaves.
    """
    cfg = make_config(
        tmp_path, mode="custom", schedule_offsets_ms=[120, -50, 0], wave_direct=1
    )
    armed = arm_now(cfg, lead_ms=5000)
    assert first_release_mono_ns(cfg, armed) == armed.t_mono_ns - 50 * 1000000

    probing = make_config(
        tmp_path,
        mode="custom",
        schedule_offsets_ms=[-50, 0],
        wave_direct=1,
        wave_validate_only=1,
        validate_lead_ms=300,
    )
    armed = arm_now(probing, lead_ms=5000)
    assert first_release_mono_ns(probing, armed) == armed.t_mono_ns - 350 * 1000000


def test_a_schedule_with_no_thread_has_no_first_release(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, n_direct=1)
    armed = arm_now(cfg, lead_ms=5000)
    empty = dataclasses.replace(armed, waves=[Wave(0, 0.0, 0, 0, 0)])

    assert first_release_mono_ns(cfg, empty) is None


def test_the_start_audit_waits_for_its_lead_and_is_sent_once(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path, n_direct=1, do_start_audit=True, start_audit_lead_ms=150, confirm_live=True
    )
    logger = logger_factory(cfg)
    adapter = TimedAdapter(audit_delay_s=0.0)
    armed = arm_now(cfg, lead_ms=450)
    client = make_clients(cfg, ref, logger, adapter, 1)[0]

    audit = StartAudit(cfg, armed, ref, client, logger)
    assert audit.planned is True
    assert audit.sent == 0
    audit.start()
    audit.start()
    assert audit.join(timeout_s=5.0)

    sent = adapter.sent_matching("SetStartBiddingAudit")
    assert len(sent) == 1
    assert audit.sent == 1
    assert audit.outcome is not None and audit.outcome.status == 200
    lead_ms = (armed.t_mono_ns - sent[0]) / 1e6
    assert 100.0 < lead_ms < 200.0, lead_ms
    client.close()


def test_a_start_audit_armed_inside_its_lead_is_sent_at_once(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path, n_direct=1, do_start_audit=True, start_audit_lead_ms=60000, confirm_live=True
    )
    logger = logger_factory(cfg)
    adapter = TimedAdapter(audit_delay_s=0.0)
    armed = arm_now(cfg, lead_ms=2000)
    client = make_clients(cfg, ref, logger, adapter, 1)[0]
    started = time.monotonic_ns()

    audit = StartAudit(cfg, armed, ref, client, logger)
    audit.start()
    assert audit.join(timeout_s=5.0)

    sent = adapter.sent_matching("SetStartBiddingAudit")
    assert len(sent) == 1
    assert (sent[0] - started) / 1e6 < 500.0
    client.close()


def test_the_read_back_runs_after_the_audit_and_its_failure_costs_only_itself(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(tmp_path, n_direct=1, do_start_audit=True, confirm_live=True)
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()
    armed = arm_now(cfg, lead_ms=2000)
    clients = make_clients(cfg, ref, logger, adapter, 2)

    seen = []

    def read_back(outcome):
        seen.append(outcome.status)
        return "placed"

    audit = StartAudit(cfg, armed, ref, clients[0], logger, on_sent=read_back)
    audit.start()
    assert audit.join(timeout_s=5.0)
    assert seen == [200]
    assert audit.readback == "placed"

    def broken(outcome):
        raise RuntimeError("log unreadable")

    failing = StartAudit(cfg, armed, ref, clients[1], logger, on_sent=broken)
    failing.start()
    assert failing.join(timeout_s=5.0)
    assert failing.sent == 1
    assert failing.readback is None
    assert [r["detail"]["exception"] for r in named(logger, "start_audit_error")] == [
        "RuntimeError: log unreadable"
    ]
    for client in clients:
        client.close()


def test_a_cancelled_start_audit_is_never_sent(
    tmp_path: Path, ref: AuctionRef, logger_factory
) -> None:
    cfg = make_config(
        tmp_path, n_direct=1, do_start_audit=True, start_audit_lead_ms=100, confirm_live=True
    )
    logger = logger_factory(cfg)
    adapter = ScriptedAdapter()
    armed = arm_now(cfg, lead_ms=3000)
    client = make_clients(cfg, ref, logger, adapter, 1)[0]

    audit = StartAudit(cfg, armed, ref, client, logger)
    audit.start()
    audit.cancel.set()
    assert audit.join(timeout_s=5.0)

    assert adapter.calls == []
    assert audit.sent == 0
    client.close()


def test_a_run_that_will_refuse_to_submit_can_be_refused_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    """The check is callable on its own, ahead of the start audit and the keep-alives."""
    cfg = make_config(tmp_path, n_direct=1, confirm_live=False)
    with pytest.raises(SecopError) as excinfo:
        require_live_confirmation(cfg, build_waves(cfg))
    assert excinfo.value.code is Failure.CONFIG_INVALID

    require_live_confirmation(make_config(tmp_path, n_direct=1, dry_run=True), build_waves(cfg))
