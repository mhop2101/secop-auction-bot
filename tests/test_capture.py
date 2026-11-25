"""Run-directory sink tests (plan §12.1, §12.5).

The concurrency test is the point of the module: at the fire instant every sink is
written from a synchronized burst of threads, and the failure mode being guarded against
is a half-written line in the very record the sink exists to capture. So the test fires
a barrier-synchronized burst and then parses *every* line of every sink back.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

from secop_firstbid.capture import (
    HAR_VERSION,
    UNKNOWN_TIMING,
    HarWriter,
    TimelineWriter,
    har_entry,
)
from secop_firstbid.logging_setup import RunLogger, new_run_id

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    + ("QWxsVGhlUGF5bG9hZEJ5dGVzR29IZXJlXy0" * 40)
    + ".c2lnbmF0dXJlLWJ5dGVzLWdvLWhlcmVfLTAxMjM0NTY3ODk"
)
NEEDLE = TOKEN[40:400]

BID_URL = "https://subastas.secop.gov.co/eauctions.api/v1//bid/ValidateBid"
POLL_URL = (
    "https://subastas.secop.gov.co/eauctions.api/signalr/signalr/poll"
    "?transport=longPolling&connectionData=%5B%7B%22name%22%3A%22auction%22%7D%5D"
    "&token=" + TOKEN
)

REQUEST_HEADERS = {
    "Authorization": "Bearer " + TOKEN,
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://subastas.secop.gov.co",
    "Cookie": "ARRAffinity=8f2c1de4bb; .ASPXAUTH=A1B2C3D4",
}
RESPONSE_HEADERS = {
    "Date": "Tue, 28 Jul 2026 21:19:31 GMT",
    "Content-Type": "application/json; charset=utf-8",
    "Set-Cookie": "ARRAffinity=8f2c1de4bb; Path=/; HttpOnly",
}
RESPONSE_BODY = {
    "isValid": False,
    "errorType": "BidErrMin",
    "minBid": "7.813.974,32 COP",
    "bidId": 0,
    "score": Decimal("0.0"),
}
REQUEST_BODY = b'{"auctionId":"d6a49a23-3556-4b3d-9bb4-3e209279b9f8","bidValue":"1"}'

THREADS = 8
PER_THREAD = 20


@dataclasses.dataclass
class StubConfig:
    """Stands in for :class:`secop_firstbid.config.Config`.

    ``RunLogger`` must be constructible from a run root alone — no network, no other
    module — so the test does not import the real config dataclass.
    """

    run_root: str
    jwt: str = TOKEN
    bid_value: str = "1"
    dry_run: bool = True
    n_direct: int = 1


def _iter_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert line.strip(), "%s line %d is blank" % (path.name, lineno)
        try:
            record = json.loads(line)
        except ValueError as exc:  # an interleaved write shows up exactly here
            raise AssertionError("%s line %d is not JSON: %s" % (path.name, lineno, exc))
        assert isinstance(record, dict)
        records.append(record)
    return records


def test_har_writer_produces_a_parseable_har(tmp_path: Path) -> None:
    writer = HarWriter(tmp_path / "capture.har")
    writer.add(
        har_entry(
            started_wall=datetime(2026, 7, 28, 21, 19, 31, 751000, tzinfo=timezone.utc),
            method="POST",
            url=BID_URL,
            request_headers={"Content-Type": "application/json;charset=UTF-8"},
            request_body=REQUEST_BODY,
            status=200,
            response_headers=RESPONSE_HEADERS,
            response_body=RESPONSE_BODY,
            time_ms=614.4,
        )
    )
    writer.flush()

    doc = json.loads((tmp_path / "capture.har").read_text(encoding="utf-8"))

    assert doc["log"]["version"] == HAR_VERSION
    assert doc["log"]["creator"]["name"] == "secop_firstbid"
    assert doc["log"]["creator"]["version"]
    assert len(doc["log"]["entries"]) == 1

    entry = doc["log"]["entries"][0]
    assert entry["startedDateTime"] == "2026-07-28T21:19:31.751Z"
    assert entry["time"] == 614.4
    assert entry["request"]["method"] == "POST"
    assert entry["request"]["url"] == BID_URL
    assert entry["request"]["postData"]["text"] == REQUEST_BODY.decode("utf-8")
    assert entry["response"]["status"] == 200
    assert entry["response"]["statusText"] == "OK"
    assert entry["response"]["redirectURL"] == ""
    assert json.loads(entry["response"]["content"]["text"])["errorType"] == "BidErrMin"
    assert entry["timings"]["wait"] == 614.4
    assert entry["timings"]["connect"] == UNKNOWN_TIMING
    for required in ("cookies", "headers", "queryString", "headersSize", "bodySize"):
        assert required in entry["request"]


def test_har_writer_flushes_an_empty_but_valid_document(tmp_path: Path) -> None:
    writer = HarWriter(tmp_path / "capture.har")
    writer.flush()

    doc = json.loads((tmp_path / "capture.har").read_text(encoding="utf-8"))

    assert doc["log"]["entries"] == []
    assert not (tmp_path / "capture.har.tmp").exists()


def test_har_entry_uses_minus_one_for_unknown_timings() -> None:
    entry = har_entry(
        started_wall=datetime(2026, 7, 28, 21, 19, 31, tzinfo=timezone.utc),
        method="GET",
        url=BID_URL,
        request_headers=None,
        request_body=None,
        status=None,
        response_headers=None,
        response_body=None,
        time_ms=None,
    )

    assert entry["time"] == UNKNOWN_TIMING
    assert set(entry["timings"].values()) == {float(UNKNOWN_TIMING)}
    assert entry["response"]["status"] == 0
    assert "postData" not in entry["request"]


def test_har_entry_records_the_query_string() -> None:
    entry = har_entry(
        started_wall=datetime.now(timezone.utc),
        method="POST",
        url="https://h/x?transport=longPolling&tid=6",
        request_headers={},
        request_body=None,
        status=200,
        response_headers={},
        response_body="",
        time_ms=1.0,
    )

    assert entry["request"]["queryString"] == [
        {"name": "transport", "value": "longPolling"},
        {"name": "tid", "value": "6"},
    ]


def test_timeline_writer_lines_parse(tmp_path: Path) -> None:
    writer = TimelineWriter(tmp_path / "timeline.jsonl")
    for index in range(5):
        writer.add({"ts_mono_ns": index, "kind": "send", "value": Decimal("1.000000")})
    writer.close()

    records = _jsonl(tmp_path / "timeline.jsonl")

    assert [rec["ts_mono_ns"] for rec in records] == [0, 1, 2, 3, 4]
    assert records[0]["value"] == "1.000000"


def test_new_run_id_is_short_hex_and_varies() -> None:
    ids = set(new_run_id() for _ in range(50))

    assert len(ids) > 40
    for value in ids:
        assert re.match(r"^[0-9a-f]{8}$", value)


def test_run_logger_creates_the_expected_file_set(tmp_path: Path) -> None:
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir
        logger.event(stage="preflight", event="thread_start", detail={"n": 1})

    names = set(path.name for path in _iter_files(run_dir))

    assert {"config.json", "events.jsonl", "run.log", "timeline.jsonl", "capture.har"} <= names
    assert (run_dir / "http").is_dir()
    assert run_dir.parent == tmp_path
    assert run_dir.name.endswith("_" + logger.run_id)


def test_run_logger_config_json_is_redacted(tmp_path: Path) -> None:
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir

    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))

    assert config["jwt"] == TOKEN[:6] + "…" + TOKEN[-6:]
    assert config["bid_value"] == "1"
    assert NEEDLE not in (run_dir / "config.json").read_text(encoding="utf-8")


def test_close_is_idempotent(tmp_path: Path) -> None:
    logger = RunLogger(StubConfig(run_root=str(tmp_path)))
    logger.close()
    logger.close()

    assert _jsonl(logger.run_dir / "events.jsonl")


def test_context_manager_records_an_escaping_exception(tmp_path: Path) -> None:
    logger = RunLogger(StubConfig(run_root=str(tmp_path)))
    with pytest.raises(RuntimeError):
        with logger:
            raise RuntimeError("boom")

    records = _jsonl(logger.run_dir / "events.jsonl")
    errors = [rec for rec in records if rec["level"] == "ERROR"]

    assert errors and errors[-1]["detail"]["exception_type"] == "RuntimeError"


def test_http_exchange_without_a_response_is_still_logged(tmp_path: Path) -> None:
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir
        corr_id = logger.next_corr_id()
        logger.http(
            stage="fire",
            corr_id=corr_id,
            thread_id="D-0",
            role="D",
            method="POST",
            url=BID_URL,
            request_headers=REQUEST_HEADERS,
            request_body=REQUEST_BODY,
            status=None,
            response_headers=None,
            response_body=None,
            t_send_mono_ns=time.monotonic_ns(),
            t_recv_mono_ns=None,
            exception=OSError("read timed out"),
        )

    records = [
        rec for rec in _jsonl(run_dir / "events.jsonl") if rec["event"] == "http_error"
    ]

    assert len(records) == 1
    assert records[0]["corr_id"] == corr_id
    assert records[0]["detail"]["status"] is None
    assert "read timed out" in records[0]["detail"]["exception"]

    files = list((run_dir / "http").glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith(corr_id.split("-")[0] + "_D_D-0_")

    har = json.loads((run_dir / "capture.har").read_text(encoding="utf-8"))
    assert har["log"]["entries"][0]["response"]["status"] == 0


def test_signalr_exchange_reaches_the_har_without_its_token(tmp_path: Path) -> None:
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir
        sent = time.monotonic_ns()
        logger.http(
            stage="clock",
            corr_id=logger.next_corr_id(),
            thread_id="main",
            role=None,
            method="POST",
            url=POLL_URL,
            request_headers={"X-Requested-With": "XMLHttpRequest"},
            request_body="data=%7B%22H%22%3A%22auction%22%7D",
            status=200,
            response_headers={"Content-Type": "application/json"},
            response_body={"C": "d-C00F3698-Ch,0|BHP,27", "M": []},
            t_send_mono_ns=sent,
            t_recv_mono_ns=sent + 30_000_000,
        )

    har_text = (run_dir / "capture.har").read_text(encoding="utf-8")
    assert NEEDLE not in har_text

    entry = json.loads(har_text)["log"]["entries"][0]
    query = dict((item["name"], item["value"]) for item in entry["request"]["queryString"])
    assert query["transport"] == "longPolling"
    assert query["token"] == TOKEN[:6] + "…" + TOKEN[-6:]

    files = list((run_dir / "http").glob("*.json"))
    assert files[0].name == "001_main_main_signalr_poll.json"


def _burst(logger: RunLogger, barrier: threading.Barrier, index: int) -> None:
    thread_id = "D-%d" % index
    barrier.wait()
    for step in range(PER_THREAD):
        corr_id = logger.next_corr_id()
        sent = time.monotonic_ns()
        logger.event(
            stage="fire",
            event="thread_start",
            thread_id=thread_id,
            role="D",
            corr_id=corr_id,
            detail={"step": step, "url": POLL_URL},
        )
        logger.timeline(kind="release", thread_id=thread_id, wave=step)
        logger.http(
            stage="fire",
            corr_id=corr_id,
            thread_id=thread_id,
            role="D",
            method="POST",
            url=BID_URL,
            request_headers=REQUEST_HEADERS,
            request_body=REQUEST_BODY,
            status=200,
            response_headers=RESPONSE_HEADERS,
            response_body=RESPONSE_BODY,
            t_send_mono_ns=sent,
            t_recv_mono_ns=sent + 123_400_000,
            extra={"isValid": False, "errorType": "BidErrMin"},
        )


def test_concurrent_burst_writes_no_corrupt_records(tmp_path: Path) -> None:
    barrier = threading.Barrier(THREADS)
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir
        threads = [
            threading.Thread(target=_burst, args=(logger, barrier, index))
            for index in range(THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert logger.writer_errors == []

    exchanges = THREADS * PER_THREAD

    events = _jsonl(run_dir / "events.jsonl")
    http_records = [rec for rec in events if rec["event"] == "http_response"]
    assert len(http_records) == exchanges
    assert len(events) == 1 + exchanges * 2

    log_lines = (run_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == len(events)
    for line in log_lines:
        assert line.strip()

    timeline = _jsonl(run_dir / "timeline.jsonl")
    assert len(timeline) == exchanges * 3
    assert sum(1 for rec in timeline if rec["kind"] == "send") == exchanges
    assert sum(1 for rec in timeline if rec["kind"] == "recv") == exchanges

    har = json.loads((run_dir / "capture.har").read_text(encoding="utf-8"))
    assert har["log"]["version"] == HAR_VERSION
    assert len(har["log"]["entries"]) == exchanges

    files = sorted((run_dir / "http").glob("*.json"))
    assert len(files) == exchanges
    seqs = []
    for path in files:
        assert re.match(r"^\d{3}_D_D-\d_bid_ValidateBid\.json$", path.name), path.name
        record = json.loads(path.read_text(encoding="utf-8"))
        seqs.append(record["seq"])
        assert record["status"] == 200
        assert record["errorType"] == "BidErrMin"
    assert sorted(seqs) == list(range(1, exchanges + 1))

    corr_ids = [rec["corr_id"] for rec in http_records]
    assert len(set(corr_ids)) == exchanges


def test_burst_leaks_nothing_into_any_artifact(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    with RunLogger(StubConfig(run_root=str(tmp_path))) as logger:
        run_dir = logger.run_dir
        threads = [
            threading.Thread(target=_burst, args=(logger, barrier, index))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    files = _iter_files(run_dir)
    assert len(files) > 5
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert NEEDLE not in text, path.name
        assert TOKEN not in text, path.name
        assert "A1B2C3D4" not in text, path.name
        assert "Bearer <redacted>" in text or path.name in (
            "timeline.jsonl",
            "config.json",
        )
