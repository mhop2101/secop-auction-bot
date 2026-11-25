"""Transport tests (plan §3, §11.2).

Everything here runs offline: a recording :class:`requests.adapters.HTTPAdapter` is mounted
on the client's own Session, so the real ``requests`` pipeline — header merging, cookie
attachment, body preparation, ``Content-Length`` — executes and only the socket is missing.
The assertions are about the properties that cost a bid when they regress: the timeout on
every call, the absence of retries, the shared cookie jar, the exact shape of the audit
POST, and a prewarm that is concurrent rather than a loop.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from secop_firstbid import endpoints, session as session_mod
from secop_firstbid.config import Config
from secop_firstbid.logging_setup import RunLogger
from secop_firstbid.models import AuctionRef
from secop_firstbid.session import (
    HttpClient,
    HttpOutcome,
    keep_warm,
    load_json,
    make_pool,
    prewarm,
)

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    + ("VGhpc0lzTm90QVJlYWxUb2tlblBheWxvYWRf" * 20)
    + ".c2lnbmF0dXJlLWJ5dGVzLWdvLWhlcmVfLTAxMjM0NTY3ODk"
)

CONNECT_TIMEOUT_MS = 1234
READ_TIMEOUT_MS = 5678
EXPECTED_TIMEOUT = (1.234, 5.678)

DATE_HEADER = "Tue, 28 Jul 2026 21:19:31 GMT"

BID_BODY = json.dumps(
    {
        "isValid": False,
        "errorType": "BidErrMin",
        "minBid": "7.813.974,32 COP",
        "lastBid": 0.00,
    }
).encode("utf-8")


class RecordingAdapter(HTTPAdapter):
    """Stands in for the socket: records the prepared request and the send kwargs.

    Mounted on an already-constructed client, so the request it sees is the one the real
    ``requests`` machinery built.
    """

    def __init__(
        self,
        status: int = 200,
        content: bytes = BID_BODY,
        headers: Optional[Dict[str, str]] = None,
        exc: Optional[Exception] = None,
        barrier: Optional[threading.Barrier] = None,
        calls: Optional[List[Dict[str, Any]]] = None,
        lock: Optional[threading.Lock] = None,
    ) -> None:
        super().__init__(pool_connections=1, pool_maxsize=1, max_retries=0)
        self.status = status
        self.content = content
        self.headers = headers or {
            "Date": DATE_HEADER,
            "Content-Type": "application/json; charset=utf-8",
        }
        self.exc = exc
        self.barrier = barrier
        self.calls = [] if calls is None else calls
        self.lock = lock or threading.Lock()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        with self.lock:
            self.calls.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "headers": dict(request.headers),
                    "body": request.body,
                    "timeout": timeout,
                }
            )
        if self.barrier is not None:
            # Times out into BrokenBarrierError unless every client is in flight at once.
            self.barrier.wait(timeout=10.0)
        if self.exc is not None:
            raise self.exc
        response = requests.Response()
        response.status_code = self.status
        response._content = self.content
        response.headers = CaseInsensitiveDict(self.headers)
        response.url = request.url
        response.request = request
        response.raw = None
        return response


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="1",
        fire_time="2026-07-28T21:00:00Z",
        run_root=str(tmp_path / "runs"),
        connect_timeout_ms=CONNECT_TIMEOUT_MS,
        read_timeout_ms=READ_TIMEOUT_MS,
    )


@pytest.fixture
def ref() -> AuctionRef:
    return AuctionRef(auction_id="d6a49a23-3556-4b3d-9bb4-3e209279b9f8")


@pytest.fixture
def logger(cfg: Config) -> Iterator[RunLogger]:
    run_logger = RunLogger(cfg)
    yield run_logger
    run_logger.close()


def _client(cfg: Config, ref: AuctionRef, logger: RunLogger, jar=None, **adapter_kwargs):
    client = HttpClient(cfg, ref, jar if jar is not None else RequestsCookieJar(), logger)
    adapter = RecordingAdapter(**adapter_kwargs)
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client, adapter


def _body_len(body: Any) -> int:
    if body is None:
        return 0
    return len(body if isinstance(body, bytes) else str(body).encode("utf-8"))


def _events(logger: RunLogger) -> List[Dict[str, Any]]:
    logger.flush()
    path = logger.run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_call_carries_the_configured_timeout_tuple(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger)

    client.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight")
    client.post_json(endpoints.validate_bid(), b"{}", stage="fire", thread_id="t1", role="V")
    client.post_empty(
        endpoints.set_start_bidding_audit(ref.auction_id), stage="fire", thread_id="t1", role="V"
    )

    assert len(adapter.calls) == 3
    for call in adapter.calls:
        assert call["timeout"] == EXPECTED_TIMEOUT


def test_a_connection_error_is_not_retried(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger, exc=requests.exceptions.ConnectionError("refused"))

    outcome = client.post_json(endpoints.validate_bid(), b"{}", stage="fire", thread_id="t1", role="D")

    assert len(adapter.calls) == 1
    assert outcome.status is None
    assert isinstance(outcome.exception, requests.exceptions.ConnectionError)


def test_the_adapter_is_mounted_with_retries_disabled(cfg, ref, logger) -> None:
    client = HttpClient(cfg, ref, RequestsCookieJar(), logger)

    for prefix in ("https://", "http://"):
        retries = client.session.adapters[prefix].max_retries
        assert retries.total == 0
        assert retries.connect in (0, None)
        assert retries.read in (0, False, None)

    pool = make_pool(cfg)
    assert pool.adapters["https://"].max_retries.total == 0
    pool.close()


def test_clients_share_one_cookie_jar(cfg, ref, logger) -> None:
    jar = RequestsCookieJar()
    jar.set("ApplicationGatewayAffinity", "backend-7", domain="subastas.secop.gov.co", path="/")

    first, first_adapter = _client(cfg, ref, logger, jar=jar)
    second, second_adapter = _client(cfg, ref, logger, jar=jar)

    assert first.session.cookies is jar
    assert second.session.cookies is jar

    first.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight")
    second.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight")

    for adapter in (first_adapter, second_adapter):
        assert "ApplicationGatewayAffinity=backend-7" in adapter.calls[0]["headers"]["Cookie"]

    # A cookie the gateway sets on one client's response must be visible to the others.
    jar.set("ApplicationGatewayAffinityCORS", "backend-7", domain="subastas.secop.gov.co", path="/")
    second.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight")
    assert "ApplicationGatewayAffinityCORS=backend-7" in second_adapter.calls[1]["headers"]["Cookie"]


def test_post_empty_sends_an_empty_body_with_content_length_zero(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger, content=b"")

    outcome = client.post_empty(
        endpoints.set_start_bidding_audit(ref.auction_id), stage="fire", thread_id="t1", role="D"
    )

    call = adapter.calls[0]
    # requests normalises a zero-length body to None; what reaches the wire is the header
    # plus no bytes, which is the captured request either way.
    assert _body_len(call["body"]) == 0
    assert call["headers"]["Content-Length"] == "0"
    assert call["headers"]["Content-Type"] == "application/json;charset=utf-8"
    assert call["headers"]["Origin"] == endpoints.SITE_ORIGIN
    assert "auctionId=" + ref.auction_id in call["url"]

    # 200 with an empty body is the observed answer; it must not become a parse failure.
    assert outcome.status == 200
    assert outcome.body is None
    assert outcome.text == ""


def test_post_json_sends_the_prebuilt_bytes_with_the_uppercase_charset(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger)
    payload = '{"auctionId":"x","totalValue":"10.000000"}'.encode("utf-8")

    client.post_json(endpoints.validate_bid(), payload, stage="fire", thread_id="t1", role="D")

    call = adapter.calls[0]
    assert call["body"] == payload
    assert call["headers"]["Content-Length"] == str(len(payload))
    assert call["headers"]["Content-Type"] == "application/json;charset=UTF-8"
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN
    assert call["url"] == endpoints.API_BASE + "bid/ValidateBid"


def test_get_headers_and_the_absence_of_origin(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger)

    client.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight")
    client.get(endpoints.get_auction_simple(ref.auction_id), stage="preflight", conditional=False)

    conditional, plain = adapter.calls
    assert conditional["headers"]["If-Modified-Since"] == endpoints.IF_MODIFIED_SINCE
    assert conditional["headers"]["Cache-Control"] == endpoints.CACHE_CONTROL
    assert "If-Modified-Since" not in plain["headers"]
    for call in adapter.calls:
        assert "Origin" not in call["headers"]
        assert call["headers"]["Referer"] == endpoints.referer(ref)
        assert call["headers"]["User-Agent"] == endpoints.DEFAULT_USER_AGENT


def test_an_exception_is_logged_as_an_outcome_with_status_none(cfg, ref, logger) -> None:
    client, _adapter = _client(
        cfg, ref, logger, exc=requests.exceptions.ReadTimeout("read timed out")
    )

    outcome = client.post_json(
        endpoints.create_bid(), b"{}", stage="fire", thread_id="t3", role="D"
    )

    assert outcome.status is None
    assert outcome.body is None
    assert outcome.text is None
    assert outcome.date_header is None
    assert outcome.t_recv_mono_ns >= outcome.t_send_mono_ns
    assert outcome.rtt_ms is not None

    records = [rec for rec in _events(logger) if rec.get("corr_id") == outcome.corr_id]
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "http_error"
    assert record["level"] == "ERROR"
    assert record["detail"]["status"] is None
    assert "ReadTimeout" in record["detail"]["exception"]
    assert record["thread_id"] == "t3"


def test_a_successful_exchange_is_logged_in_full_and_redacted(cfg, ref, logger) -> None:
    client, _adapter = _client(cfg, ref, logger)

    outcome = client.post_json(
        endpoints.validate_bid(), b'{"bidValue":"1"}', stage="fire", thread_id="t2", role="V"
    )

    assert outcome.status == 200
    assert outcome.body["errorType"] == "BidErrMin"
    assert outcome.date_header == DATE_HEADER
    assert outcome.headers["Date"] == DATE_HEADER
    assert outcome.corr_id

    records = [rec for rec in _events(logger) if rec.get("corr_id") == outcome.corr_id]
    assert len(records) == 1
    detail = records[0]["detail"]
    assert detail["status"] == 200
    assert detail["request_body"] == '{"bidValue":"1"}'
    assert detail["response_body"]["errorType"] == "BidErrMin"
    assert detail["request_headers"]["Authorization"] == "Bearer <redacted>"
    assert TOKEN not in (logger.run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_bodies_parse_with_decimal_floats() -> None:
    body = load_json('{"myLastBid": 0.0, "lastBid": 0.00, "score": 1.5}')

    assert body["lastBid"] == Decimal("0.00")
    assert isinstance(body["score"], Decimal)
    assert str(body["score"]) == "1.5"

    assert load_json("") is None
    assert load_json(None) is None
    assert load_json("<html>blocked</html>") is None


def test_prewarm_issues_one_concurrent_request_per_client(cfg, ref, logger) -> None:
    count = 4
    jar = RequestsCookieJar()
    barrier = threading.Barrier(count)
    calls: List[Dict[str, Any]] = []
    lock = threading.Lock()

    clients = []
    for _ in range(count):
        client, _adapter = _client(cfg, ref, logger, jar=jar, barrier=barrier, calls=calls, lock=lock)
        clients.append(client)

    # The barrier is the assertion: a sequential warm-up loop can never fill it, so the
    # first client would raise BrokenBarrierError and land with status None below.
    prewarm(clients, ref, logger)

    assert len(calls) == count
    assert not barrier.broken
    for call in calls:
        assert call["method"] == "GET"
        assert call["url"] == endpoints.API_BASE + endpoints.get_auction_simple(ref.auction_id)
        assert call["timeout"] == EXPECTED_TIMEOUT

    records = _events(logger)
    completions = [rec for rec in records if rec["event"] == "prewarm_complete"]
    assert len(completions) == 1
    assert completions[0]["level"] == "INFO"
    assert completions[0]["detail"]["statuses"] == [200] * count
    assert completions[0]["detail"]["failed_indices"] == []

    http_records = [rec for rec in records if rec.get("stage") == session_mod.PREWARM_STAGE
                    and rec["event"] == "http_response"]
    assert len(http_records) == count
    assert sorted(rec["thread_id"] for rec in http_records) == [
        "prewarm-%02d" % index for index in range(count)
    ]

    for client in clients:
        client.close()


def test_prewarm_survives_a_failing_client(cfg, ref, logger) -> None:
    jar = RequestsCookieJar()
    good, _good_adapter = _client(cfg, ref, logger, jar=jar)
    bad, _bad_adapter = _client(
        cfg, ref, logger, jar=jar, exc=requests.exceptions.ConnectTimeout("no route")
    )

    prewarm([good, bad], ref, logger)

    completions = [rec for rec in _events(logger) if rec["event"] == "prewarm_complete"]
    assert len(completions) == 1
    assert completions[0]["level"] == "WARNING"
    assert completions[0]["detail"]["failed_indices"] == [1]
    assert completions[0]["detail"]["statuses"] == [200, None]


def test_prewarm_with_no_clients_is_a_no_op(cfg, ref, logger) -> None:
    prewarm([], ref, logger)

    assert [rec for rec in _events(logger) if rec["event"] == "prewarm_complete"] == []


def test_redirects_are_recorded_and_never_followed(cfg, ref, logger) -> None:
    client, adapter = _client(
        cfg,
        ref,
        logger,
        status=302,
        content=b"",
        headers={"Location": "https://subastas.secop.gov.co/login", "Date": DATE_HEADER},
    )

    outcome = client.post_json(endpoints.create_bid(), b"{}", stage="fire", thread_id="t1", role="D")

    assert len(adapter.calls) == 1
    assert outcome.status == 302
    assert outcome.headers["Location"].endswith("/login")


def test_outcome_defaults_keep_the_contract_field_order() -> None:
    outcome = HttpOutcome(status=200, body=None, text="", headers={}, t_send_mono_ns=1)

    assert outcome.t_recv_mono_ns is None
    assert outcome.rtt_ms is None
    assert outcome.corr_id == ""
    assert outcome.exception is None
    assert outcome.date_header is None


# ------------------------------------------------------------------------ keep-alive


def _warm_clients(cfg, ref, logger, count: int, **adapter_kwargs):
    jar = RequestsCookieJar()
    calls: List[Dict[str, Any]] = []
    lock = threading.Lock()
    clients = []
    for _ in range(count):
        client, _adapter = _client(
            cfg, ref, logger, jar=jar, calls=calls, lock=lock, **adapter_kwargs
        )
        clients.append(client)
    return clients, calls


def test_get_uses_the_clients_timeout_unless_one_is_given(cfg, ref, logger) -> None:
    client, adapter = _client(cfg, ref, logger)

    client.get(endpoints.get_auction_simple(ref.auction_id), stage="detail")
    client.get(
        endpoints.get_auction_simple(ref.auction_id), stage="keepalive", timeout=(0.5, 0.75)
    )

    assert [call["timeout"] for call in adapter.calls] == [EXPECTED_TIMEOUT, (0.5, 0.75)]
    client.close()


def test_keep_warm_reads_every_client_at_each_instant_and_is_over_before_the_release(
    cfg, ref, logger
) -> None:
    """One read per client per round, on the keep-alive's own short timeout.

    The timeout is the point: a keep-alive rides on a bid's connection, so it gets a
    budget far below the bid's and has to be finished before the first release.
    """
    cfg.keepalive_offsets_ms = [-220, -110]
    cfg.keepalive_timeout_ms = 700
    count = 3
    clients, calls = _warm_clients(cfg, ref, logger, count)
    first_release = time.monotonic_ns() + 400 * 1000000

    rounds = keep_warm(cfg, clients, ref, logger, first_release)

    assert time.monotonic_ns() < first_release
    assert rounds == 2
    assert len(calls) == 2 * count
    for call in calls:
        assert call["method"] == "GET"
        assert call["url"] == endpoints.API_BASE + endpoints.get_auction_simple(ref.auction_id)
        assert call["timeout"] == (0.7, 0.7)

    completions = [rec for rec in _events(logger) if rec["event"] == "keepalive_complete"]
    assert [rec["detail"]["offset_from_first_release_ms"] for rec in completions] == [
        -220,
        -110,
    ]
    assert all(rec["detail"]["statuses"] == [200] * count for rec in completions)
    assert all(rec["stage"] == session_mod.KEEPALIVE_STAGE for rec in completions)

    for client in clients:
        client.close()


def test_a_keepalive_whose_instant_has_passed_is_skipped_not_sent_late(
    cfg, ref, logger
) -> None:
    """Sent late it would sit closer to the release than the config was checked for."""
    cfg.keepalive_offsets_ms = [-5000, -100]
    clients, calls = _warm_clients(cfg, ref, logger, 2)
    first_release = time.monotonic_ns() + 300 * 1000000

    rounds = keep_warm(cfg, clients, ref, logger, first_release)

    assert rounds == 1
    assert len(calls) == 2
    skipped = [rec for rec in _events(logger) if rec["event"] == "keepalive_skipped"]
    assert [rec["detail"]["offset_from_first_release_ms"] for rec in skipped] == [-5000]

    for client in clients:
        client.close()


def test_keep_warm_with_no_offsets_or_no_clients_sends_nothing(cfg, ref, logger) -> None:
    clients, calls = _warm_clients(cfg, ref, logger, 2)
    far = time.monotonic_ns() + 60 * 1000000000

    cfg.keepalive_offsets_ms = []
    assert keep_warm(cfg, clients, ref, logger, far) == 0
    cfg.keepalive_offsets_ms = [-100]
    assert keep_warm(cfg, [], ref, logger, far) == 0

    assert calls == []
    for client in clients:
        client.close()


def test_a_keepalive_that_fails_is_logged_and_never_raised(cfg, ref, logger) -> None:
    """A dropped keep-alive is a colder connection, not a reason to abandon an armed run."""
    cfg.keepalive_offsets_ms = [-100]
    clients, _calls = _warm_clients(
        cfg, ref, logger, 2, exc=requests.exceptions.ConnectTimeout("timed out")
    )
    first_release = time.monotonic_ns() + 250 * 1000000

    assert keep_warm(cfg, clients, ref, logger, first_release) == 1

    completions = [rec for rec in _events(logger) if rec["event"] == "keepalive_complete"]
    assert len(completions) == 1
    assert completions[0]["level"] == "WARNING"
    assert completions[0]["detail"]["statuses"] == [None, None]
    assert completions[0]["detail"]["failed_indices"] == [0, 1]

    for client in clients:
        client.close()


def test_keep_warm_stops_when_cancelled(cfg, ref, logger) -> None:
    cfg.keepalive_offsets_ms = [-100]
    clients, calls = _warm_clients(cfg, ref, logger, 1)
    cancel = threading.Event()
    cancel.set()

    assert keep_warm(
        cfg, clients, ref, logger, time.monotonic_ns() + 400 * 1000000, cancel=cancel
    ) == 0
    assert calls == []
    clients[0].close()
