"""CLI behaviour: the four subcommands, the ``--set`` overrides and the exit codes.

Every test here runs ``main()`` in-process with an argv list, and ``requests.Session.request``
is replaced for the whole module with a function that fails the test if it is ever called.
That substitution *is* the assertion for ``parse-page`` and ``plan``: those two are specified
as network-free, and the only way to prove it is to make a socket impossible rather than to
inspect what was printed.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests

from conftest import (
    AUCTION_ID,
    BEFOREBID_HTML,
    BID_HTML,
    EAUCTION_DOC_ID,
    FIXTURE_AUCTION_ID,
    FIXTURE_DOC_ID,
    FIXTURES,
    LAUNCH_PAGE,
    OPPORTUNITY_PAGE,
    REPO_ROOT,
    require_real_page,
)
from secop_firstbid import __main__ as cli
from secop_firstbid import records
from secop_firstbid.config import Config
from secop_firstbid.errors import Failure, SecopError

STALE_DATE = "2000-01-01T00:00:00.000Z"

# The three build-report warnings the synthetic GetAuction produces at bid_value 7500000:
# one item at quantity 12, one criterion item left out of items[], one division that does
# not terminate. Under cron or nohup stdout is discarded, so a run directory that does not
# carry them cannot be read after the fact.
BUILD_WARNING_MARKERS = (
    "NON-PRICE-LIST ITEMS EXCLUDED",
    "UNVERIFIED QUANTITY FACTOR",
    "INEXACT DIVISION",
)

# getauction_synthetic.json: the one item carrying isPriceListItem false, and the one whose
# initialValue does not divide exactly by the bid value.
EXCLUDED_ITEM_ID = "c47f2b18-6d05-4e93-b7aa-90e1c3fd5b62"
INEXACT_ITEM_ID = "8552d67b-29ce-4b30-877b-e361fd876072"

WIRE_KEY_ORDER = [
    "auctionId",
    "currency",
    "Date",
    "ranking",
    "items",
    "totalValue",
    "languageCode",
]


FAKE_COMPANY_CODE = "999999"


def _fake_jwt(seconds_remaining: int = 7200) -> str:
    """A syntactically real, cryptographically worthless token. Never sent anywhere."""
    exp = int(
        (datetime.now(timezone.utc) + timedelta(seconds=seconds_remaining)).timestamp()
    )
    claims = {
        "exp": exp,
        "aud": "https://subastas.secop.gov.co/",
        "http://www.nextway.com/NextWay.STS.Claim/LoggedInCompanyCode": FAKE_COMPANY_CODE,
    }

    def segment(obj: Dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return "%s.%s.%s" % (segment({"alg": "RS256"}), segment(claims), "c2lnbmF0dXJl")


def _fire_time(seconds_ahead: int = 900) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the CLI opened a socket: requests.Session.request%r" % (args[1:],)
        )

    monkeypatch.setattr(requests.Session, "request", forbidden)


@pytest.fixture()
def write_config(tmp_path):
    def write(**overrides: Any) -> str:
        values = {
            "jwt": _fake_jwt(),
            "auction_page_html_path": str(OPPORTUNITY_PAGE),
            "bid_value": "7500000",
            "fire_time": _fire_time(),
            "run_root": str(tmp_path / "runs"),
            # A real run gives the server half a minute to work through what was sent
            # before reading its record; a test has no server to wait for.
            "secop_record_delay_s": 0,
        }
        values.update(overrides)
        path = tmp_path / "run.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return str(path)

    return write


def _only_run_dir(tmp_path) -> Path:
    runs = sorted((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    return runs[0]


def _events(run_dir: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _payload_built(run_dir: Path) -> Dict[str, Any]:
    built = [record for record in _events(run_dir) if record["event"] == "payload_built"]
    assert len(built) == 1
    return built[0]


def _run_dir_files(run_dir: Path) -> List[Path]:
    return [path for path in sorted(run_dir.rglob("*")) if path.is_file()]


def test_parse_page_resolves_the_opportunity_page_without_a_socket(capsys) -> None:
    assert cli.main(["parse-page", "--page", str(OPPORTUNITY_PAGE)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert FIXTURE_AUCTION_ID in out
    assert FIXTURE_DOC_ID in out
    assert "onclick" in out
    assert "es-CO" in out
    assert "GovCo" in out
    assert "Terminado" in out


def test_parse_page_says_absent_state_means_unknown(capsys) -> None:
    assert cli.main(["parse-page", "--page", str(LAUNCH_PAGE)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "iframe" in out
    assert "UNKNOWN" in out


def test_parse_page_resolves_the_real_beforebid(capsys) -> None:
    require_real_page(BEFOREBID_HTML)
    assert cli.main(["parse-page", "--page", str(BEFOREBID_HTML)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert AUCTION_ID in out
    assert EAUCTION_DOC_ID in out
    assert "onclick" in out
    assert "Terminado" in out


def test_parse_page_on_the_real_bid_page_reports_unknown_state(capsys) -> None:
    require_real_page(BID_HTML)
    assert cli.main(["parse-page", "--page", str(BID_HTML)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert AUCTION_ID in out
    assert "iframe" in out
    assert "UNKNOWN" in out


def test_parse_page_missing_file_exits_parse(tmp_path) -> None:
    missing = tmp_path / "nothing.html"
    assert cli.main(["parse-page", "--page", str(missing)]) == cli.EXIT_PARSE


def test_parse_page_override_absent_from_page_exits_parse(capsys) -> None:
    code = cli.main(
        [
            "parse-page",
            "--page",
            str(OPPORTUNITY_PAGE),
            "--auction-id",
            "00000000-0000-0000-0000-000000000000",
        ]
    )
    assert code == cli.EXIT_PARSE
    assert "PARSE_FAILED" in capsys.readouterr().err


def test_plan_is_network_free_and_prints_schedule_payload_and_footprint(
    write_config, capsys
) -> None:
    assert cli.main(["plan", "--config", write_config()]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert FIXTURE_AUCTION_ID in out
    assert "wave 0" in out
    assert "footprint" in out
    assert '"bidValue": "7500000"' in out
    assert '"totalValue": "7500000.000000"' in out
    assert "PROVISIONAL" in out


def test_plan_burst_offsets_follow_the_documented_formula(write_config, capsys) -> None:
    code = cli.main(
        [
            "plan",
            "--config",
            write_config(),
            "--set",
            "mode=burst",
            "--set",
            "bid_count=4",
            "--set",
            "pre_ms=60",
            "--set",
            "interval_ms=30",
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    for label in ("T-60.0 ms", "T-30.0 ms", "T+30.0 ms"):
        assert label in out
    assert "4 wave(s), span 90.0 ms" in out


def test_plan_writes_a_run_directory_under_run_root(write_config, tmp_path) -> None:
    assert cli.main(["plan", "--config", write_config()]) == cli.EXIT_OK
    runs = sorted((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    summary = json.loads((runs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "NOT_FIRED"
    assert summary["config_redacted"]["jwt"].count("…") == 1


def test_plan_with_saved_bodies_builds_the_real_payload(write_config, capsys) -> None:
    # getauction_synthetic.json is the client-code-inferred item shape, not a captured
    # response; this asserts the plumbing, not the field mapping (docs/open-questions Q7).
    code = cli.main(
        [
            "plan",
            "--config",
            write_config(),
            "--auction-simple",
            str(FIXTURES / "getauctionsimple.json"),
            "--auction",
            str(FIXTURES / "getauction_synthetic.json"),
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "PROVISIONAL" not in out
    assert '"currency": "COP"' in out
    assert cli.PLACEHOLDER_ITEM_ID not in out


def _plan_with_bodies(config: str) -> List[str]:
    return [
        "plan",
        "--config",
        config,
        "--auction-simple",
        str(FIXTURES / "getauctionsimple.json"),
        "--auction",
        str(FIXTURES / "getauction_synthetic.json"),
    ]


def test_plan_puts_the_build_report_in_the_run_directory_not_only_on_stdout(
    write_config, tmp_path, capsys
) -> None:
    """A cron or nohup run keeps no stdout; the exclusions still have to be readable."""
    assert cli.main(_plan_with_bodies(write_config())) == cli.EXIT_OK
    capsys.readouterr()

    run_dir = _only_run_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    for marker in BUILD_WARNING_MARKERS:
        assert any(marker in warning for warning in summary["warnings"]), marker

    record = _payload_built(run_dir)
    assert record["stage"] == "build"
    assert record["level"] == "WARNING"
    detail = record["detail"]
    for marker in BUILD_WARNING_MARKERS:
        assert any(marker in warning for warning in detail["warnings"]), marker
    assert detail["excluded_items"] == [EXCLUDED_ITEM_ID]
    assert detail["excluded_total_value"] == "7500000.000000"
    assert detail["inexact_items"] == [INEXACT_ITEM_ID]
    assert detail["residuals"][INEXACT_ITEM_ID] != "0"
    assert detail["invariant_ok"] is True
    assert detail["items"] == 2
    assert detail["total_value"] == "97500000.000000"


def test_plan_writes_the_payload_it_would_have_bid(
    write_config, tmp_path, capsys
) -> None:
    assert cli.main(_plan_with_bodies(write_config())) == cli.EXIT_OK
    capsys.readouterr()

    record = json.loads(
        (_only_run_dir(tmp_path) / "payload.json").read_text(encoding="utf-8")
    )
    sent = json.loads(record["wire"])

    assert record["wire_bytes"] == len(record["wire"].encode("utf-8"))
    assert sent["auctionId"] == FIXTURE_AUCTION_ID
    assert sent["totalValue"] == "97500000.000000"
    assert [item["bidValue"] for item in sent["items"]] == ["7500000", "7500000"]
    assert EXCLUDED_ITEM_ID not in record["wire"]
    # The whole-file sink sorts keys, so the payload travels as the serialized bytes: an
    # object here would be written in an order the server never sees.
    assert list(sent) == WIRE_KEY_ORDER


def test_a_provisional_plan_says_so_where_the_run_directory_can_be_read(
    write_config, tmp_path, capsys
) -> None:
    """The placeholder detail's own warnings describe the stand-in, not the auction."""
    assert cli.main(["plan", "--config", write_config()]) == cli.EXIT_OK
    capsys.readouterr()

    run_dir = _only_run_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    record = json.loads((run_dir / "payload.json").read_text(encoding="utf-8"))

    assert any("PROVISIONAL" in warning for warning in summary["warnings"])
    assert not any("ZERO SPECIAL CASE" in warning for warning in summary["warnings"])
    assert record["payload_source"] == cli.PROVISIONAL_SOURCE
    assert _payload_built(run_dir)["detail"]["payload_source"] == cli.PROVISIONAL_SOURCE


def test_plan_refuses_half_a_detail(write_config) -> None:
    code = cli.main(
        [
            "plan",
            "--config",
            write_config(),
            "--auction-simple",
            str(FIXTURES / "getauctionsimple.json"),
        ]
    )
    assert code == cli.EXIT_CONFIG


def test_fire_time_in_the_past_exits_config(write_config, capsys) -> None:
    assert cli.main(["plan", "--config", write_config(fire_time=_fire_time(-60))]) == (
        cli.EXIT_CONFIG
    )
    assert "FIRE_TIME_PAST" in capsys.readouterr().err


def test_naive_fire_time_exits_config(write_config) -> None:
    code = cli.main(["plan", "--config", write_config(fire_time="2099-01-01T10:00:00")])
    assert code == cli.EXIT_CONFIG


def test_missing_config_file_exits_config(tmp_path) -> None:
    assert cli.main(["plan", "--config", str(tmp_path / "absent.json")]) == cli.EXIT_CONFIG


def test_unknown_set_key_is_a_hard_error(write_config, capsys) -> None:
    code = cli.main(
        ["plan", "--config", write_config(), "--set", "fire_tim=2099-01-01T00:00:00Z"]
    )
    assert code == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "fire_tim" in err
    assert "fire_time" in err


def test_set_value_of_the_wrong_type_is_a_hard_error(write_config) -> None:
    assert cli.main(["plan", "--config", write_config(), "--set", "n_direct=two"]) == (
        cli.EXIT_CONFIG
    )


def test_set_coerces_to_the_config_field_type(write_config) -> None:
    overrides = cli.parse_set_overrides(
        [
            "dry_run=true",
            "n_direct=3",
            "bid_value=7813974.32",
            "auction_id=",
            "schedule_offsets_ms=-100,-50,0,20",
        ]
    )
    assert overrides == {
        "dry_run": True,
        "n_direct": 3,
        "bid_value": "7813974.32",
        "auction_id": None,
        "schedule_offsets_ms": [-100, -50, 0, 20],
    }


def _capture_execute(monkeypatch: pytest.MonkeyPatch) -> List[Config]:
    seen = []  # type: List[Config]

    def fake_execute(cfg: Config, *args: Any, **kwargs: Any) -> int:
        seen.append(cfg)
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_execute_run", fake_execute)
    return seen


def test_run_without_the_flag_leaves_confirm_live_false(
    write_config, monkeypatch, capsys
) -> None:
    seen = _capture_execute(monkeypatch)
    assert cli.main(["run", "--config", write_config()]) == cli.EXIT_OK
    assert len(seen) == 1
    assert seen[0].confirm_live is False
    assert seen[0].dry_run is False
    assert "--yes-i-mean-it" in capsys.readouterr().out


def test_run_with_the_flag_sets_confirm_live(write_config, monkeypatch) -> None:
    seen = _capture_execute(monkeypatch)
    assert cli.main(["run", "--config", write_config(), "--yes-i-mean-it"]) == cli.EXIT_OK
    assert seen[0].confirm_live is True


def test_confirm_live_in_the_config_file_is_overruled(
    write_config, monkeypatch, capsys
) -> None:
    seen = _capture_execute(monkeypatch)
    assert cli.main(["run", "--config", write_config(confirm_live=True)]) == cli.EXIT_OK
    assert seen[0].confirm_live is False
    assert "overruled" in capsys.readouterr().out


def test_set_confirm_live_does_not_arm_a_run(write_config, monkeypatch) -> None:
    seen = _capture_execute(monkeypatch)
    code = cli.main(["run", "--config", write_config(), "--set", "confirm_live=true"])
    assert code == cli.EXIT_OK
    assert seen[0].confirm_live is False


def test_run_creates_a_run_directory_before_executing(
    write_config, monkeypatch, tmp_path
) -> None:
    _capture_execute(monkeypatch)
    assert cli.main(["run", "--config", write_config()]) == cli.EXIT_OK
    assert len(list((tmp_path / "runs").iterdir())) == 1


class _Response:
    """The three attributes ``session.HttpClient._send`` reads off a response."""

    def __init__(self, status: int, body: str) -> None:
        self.status_code = status
        self.headers = {
            "Date": "Tue, 28 Jul 2026 21:00:00 GMT",
            "Content-Type": "application/json",
        }
        self.text = body


class _Wire:
    """Everything the fake transport saw, in order: request URLs and request bodies."""

    def __init__(self) -> None:
        self.urls = []  # type: List[str]
        self.bodies = []  # type: List[Optional[bytes]]
        # How far the fake server's log clock sits from this host's, and the rows it
        # holds. A start audit appends one, stamped on that clock, as the real one does.
        self.log_clock_offset_ms = -945.0
        self.event_log = []  # type: List[Dict[str, Any]]
        self.supplier_bids = []  # type: List[Dict[str, Any]]

    def bodies_for(self, marker: str) -> List[Optional[bytes]]:
        return [body for url, body in zip(self.urls, self.bodies) if marker in url]

    def stamp(self) -> str:
        now = datetime.now(timezone.utc) + timedelta(milliseconds=self.log_clock_offset_ms)
        return now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def _fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    auction_simple: Dict[str, Any],
    bids_total_info: Dict[str, Any],
    validatebid_responses: List[Dict[str, Any]],
) -> _Wire:
    """Answer every route from the captured fixtures; record what was requested."""
    wire = _Wire()
    urls = wire.urls
    items = json.loads(
        (FIXTURES / "getauction_synthetic.json").read_text(encoding="utf-8")
    )

    def request(self: Any, method: str, url: str, **kwargs: Any) -> _Response:
        urls.append(url)
        wire.bodies.append(kwargs.get("data"))
        if "GetAuctionSimple" in url:
            return _Response(200, json.dumps(auction_simple))
        if "GetAuction/" in url:
            return _Response(200, json.dumps(items))
        if "GetBidsTotalInfo" in url:
            return _Response(200, json.dumps(bids_total_info))
        if "ValidateBid" in url:
            return _Response(200, json.dumps(validatebid_responses[0]))
        if "GetEventLogs" in url:
            rows = sorted(wire.event_log, key=lambda row: row["registerDate"], reverse=True)
            return _Response(
                200,
                json.dumps(
                    {"draw": 1, "recordsTotal": len(rows), "recordsFiltered": len(rows),
                     "data": rows}
                ),
            )
        if "GetSupplierBids" in url:
            return _Response(200, json.dumps(wire.supplier_bids))
        if "Audit" in url:
            wire.event_log.append(
                {
                    "eventHistoryTypeCode": "Logs_Search_Bid",
                    "eventHistorySubTypeCode": "Logs_Search_StartBidding",
                    "registerDate": wire.stamp(),
                    "description": "Comenzar Lances",
                    "ipAddress": "203.0.113.7",
                    "userCode": FAKE_COMPANY_CODE,
                }
            )
            return _Response(200, "")
        raise AssertionError("the run called an unexpected route: %s" % url)

    monkeypatch.setattr(requests.Session, "request", request)
    return wire


def test_preflight_reads_detail_and_clock_without_bidding(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    config = write_config(rtt_samples=4)
    assert cli.main(["preflight", "--config", config]) == cli.EXIT_OK

    out = capsys.readouterr().out
    for section in ("--- TOKEN ---", "--- AUCTION ---", "--- PAYLOAD ---", "--- TIMING ---"):
        assert section in out
    assert not any("ValidateBid" in url or "CreateBid" in url for url in wire.urls)
    assert not any("Audit" in url for url in wire.urls)

    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["clock"]["applied"] is False
    assert summary["clock"]["date_header"]["samples"] == 4
    assert summary["submissions_sent"] == 0


def test_run_probe_only_completes_and_writes_a_summary(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    config = write_config(
        fire_time=_fire_time(2),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    assert any("ValidateBid" in url for url in wire.urls)
    assert not any("CreateBid" in url for url in wire.urls)

    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "NOT_FIRED"
    assert summary["submissions_sent"] == 0
    assert summary["failure_taxonomy"] == ["BID_ERR_MIN"]
    assert "NOT_FIRED" in capsys.readouterr().out


def test_preflight_puts_the_payload_and_its_build_report_in_the_run_directory(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    _fake_transport(monkeypatch, auction_simple, bids_total_info, validatebid_responses)
    assert cli.main(["preflight", "--config", write_config()]) == cli.EXIT_OK
    capsys.readouterr()

    run_dir = _only_run_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    record = json.loads((run_dir / "payload.json").read_text(encoding="utf-8"))
    detail = _payload_built(run_dir)["detail"]

    for marker in BUILD_WARNING_MARKERS:
        assert any(marker in warning for warning in summary["warnings"]), marker
        assert any(marker in warning for warning in detail["warnings"]), marker
    assert detail["excluded_items"] == [EXCLUDED_ITEM_ID]
    assert json.loads(record["wire"])["totalValue"] == "97500000.000000"
    assert list(json.loads(record["wire"])) == WIRE_KEY_ORDER


def test_preflight_config_json_carries_the_decoded_claims_and_never_the_token(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    _fake_transport(monkeypatch, auction_simple, bids_total_info, validatebid_responses)
    token = _fake_jwt()
    assert cli.main(["preflight", "--config", write_config(jwt=token)]) == cli.EXIT_OK
    capsys.readouterr()

    run_dir = _only_run_dir(tmp_path)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    claims = config["token_claims"]

    assert config["jwt"] != token
    assert claims["company_code"] == "999999"
    assert claims["exp"] is not None
    assert set(claims) >= {
        "exp",
        "seconds_remaining",
        "company_code",
        "company_name",
        "user_id",
        "login_guid",
    }
    # Every claim above is decoded from the token; none of them may be the token.
    for path in _run_dir_files(run_dir):
        assert token not in path.read_text(encoding="utf-8"), path.name


def test_run_carries_the_build_warnings_into_the_summary(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    _fake_transport(monkeypatch, auction_simple, bids_total_info, validatebid_responses)
    config = write_config(
        fire_time=_fire_time(2),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK
    capsys.readouterr()

    run_dir = _only_run_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    detail = _payload_built(run_dir)["detail"]
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))

    for marker in BUILD_WARNING_MARKERS:
        assert any(marker in warning for warning in summary["warnings"]), marker
        assert any(marker in warning for warning in detail["warnings"]), marker
    assert detail["excluded_items"] == [EXCLUDED_ITEM_ID]
    assert detail["excluded_total_value"] == "7500000.000000"
    # run inspects the token too, so its config record carries the claims preflight's does.
    assert config["token_claims"]["company_code"] == "999999"


def test_run_without_the_flag_refuses_to_submit(
    write_config,
    monkeypatch,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    config = write_config(
        fire_time=_fire_time(30), n_direct=1, gate_mode="off", clock_probe="none"
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_CONFIG
    assert not any("CreateBid" in url for url in wire.urls)
    # It refuses before the start audit goes out, not after: thirty seconds from T is
    # inside the audit's lead, so a later refusal would already have written the row.
    assert not any("Audit" in url for url in wire.urls)
    assert "confirm_live" in capsys.readouterr().err


def test_run_sends_one_start_audit_and_reads_secops_record_back(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """The start audit goes out once, its stamp places SECOP's clock, and the summary
    carries what the server recorded — without ``T`` moving, because nobody asked."""
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    wire.supplier_bids = [
        {
            "bidId": 1,
            "company": "900000002",
            "date": "2026-11-20T21:00:00.0443673Z",
            "totalValueStr": "0.000000000000",
            "isValid": True,
        }
    ]
    config = write_config(
        fire_time=_fire_time(2),
        n_direct=0,
        n_validate_only=2,
        gate_mode="off",
        clock_probe="none",
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    assert sum(1 for url in wire.urls if "SetStartBiddingAudit" in url) == 1
    assert sum(1 for url in wire.urls if "ValidateBid" in url) == 2

    summary = json.loads(
        (_only_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8")
    )
    stamped = summary["clock"]["event_log"]
    assert stamped["candidates"] == 1
    assert abs(stamped["offset_ms"] - wire.log_clock_offset_ms) < 100.0
    assert summary["clock"]["applied"] is False
    assert summary["clock"]["shift_ms"] == 0.0

    record = summary["secop_record"]
    assert record["company_code"] == FAKE_COMPANY_CODE
    assert record["bids"][0]["company"] == "900000002"
    assert record["bids"][0]["own"] is False
    assert record["own_start_rows"] == 1
    assert record["unread"] == []

    out = capsys.readouterr().out
    assert "SECOP says" in out
    assert "by company 900000002 (not yours)" in out
    assert "SECOP clock" in out and "behind this host" in out


def test_run_moves_t_onto_secops_clock_only_when_asked(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """With ``apply_clock_offset`` the probe leaves when SECOP's clock reads ``T``.

    The fake server's log clock is 945 ms behind, so the instant it calls ``T`` arrives
    945 ms later here and the one ValidateBid is sent that much after ``fire_time``.
    """
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    config = write_config(
        fire_time=_fire_time(6),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
        apply_clock_offset=True,
        clock_window_ms=1000,
        keepalive_offsets_ms=[],
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    summary = json.loads(
        (_only_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["clock"]["applied"] is True
    assert abs(summary["clock"]["shift_ms"] - 945.0) < 100.0

    fire_time = datetime.strptime(summary["fire_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    sends = [
        record
        for record in _events(_only_run_dir(tmp_path))
        if record["event"] == "http_response" and "ValidateBid" in record["detail"]["url"]
    ]
    assert len(sends) == 1
    sent_at = datetime.strptime(
        sends[0]["ts_wall"][:26], "%Y-%m-%dT%H:%M:%S.%f"
    ).replace(tzinfo=timezone.utc)
    late_ms = (sent_at - fire_time).total_seconds() * 1000.0
    assert 800.0 < late_ms < 1300.0, late_ms

    assert "T moved" in capsys.readouterr().out


def test_run_declines_to_move_t_when_the_clock_was_not_placed(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """A measurement outside the window is not acted on, and the run says so."""
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    wire.log_clock_offset_ms = -30000.0
    config = write_config(
        fire_time=_fire_time(6),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
        apply_clock_offset=True,
        clock_window_ms=1000,
        keepalive_offsets_ms=[],
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    summary = json.loads(
        (_only_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["clock"]["applied"] is False
    assert summary["clock"]["shift_ms"] == 0.0
    assert any("T was NOT moved" in warning for warning in summary["warnings"])
    assert "T was NOT moved" in capsys.readouterr().out


def test_clock_probe_sends_one_audit_and_no_bid(
    write_config,
    monkeypatch,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    assert cli.main(["clock-probe", "--config", write_config()]) == cli.EXIT_OK

    assert sum(1 for url in wire.urls if "SetStartBiddingAudit" in url) == 1
    assert not any("ValidateBid" in url or "CreateBid" in url for url in wire.urls)

    out = capsys.readouterr().out
    assert "--- SECOP CLOCK ---" in out
    assert "behind this host" in out
    assert "ONE SetStartBiddingAudit" in out


def test_clock_probe_says_so_when_the_row_cannot_be_read_back(
    write_config,
    monkeypatch,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    wire.log_clock_offset_ms = -60000.0
    monkeypatch.setattr(records, "_READBACK_WAITS_S", (0.0, 0.0))
    assert cli.main(["clock-probe", "--config", write_config()]) == cli.EXIT_NETWORK

    assert "not placed" in capsys.readouterr().out


def test_every_failure_has_exactly_one_exit_code() -> None:
    for failure in Failure:
        assert failure in cli.EXIT_BY_FAILURE, failure
        assert cli.EXIT_BY_FAILURE[failure] in (0, 1, 2, 3, 4, 5, 6)
    assert cli.exit_code_for(Failure.CONFIG_INVALID) == cli.EXIT_CONFIG
    assert cli.exit_code_for(Failure.PARSE_AMBIGUOUS) == cli.EXIT_PARSE
    assert cli.exit_code_for(Failure.AUTH_EXPIRED) == cli.EXIT_AUTH
    assert cli.exit_code_for(Failure.BID_ERR_MIN) == cli.EXIT_ALL_REJECTED
    assert cli.exit_code_for(Failure.NETWORK) == cli.EXIT_NETWORK


def test_no_subcommand_prints_help_and_exits_config(capsys) -> None:
    assert cli.main([]) == cli.EXIT_CONFIG
    assert "parse-page" in capsys.readouterr().out


class _FireOutcome:
    def __init__(
        self,
        winner: Optional[object],
        results: List[Any],
        submissions_sent: int,
    ) -> None:
        self.winner = winner
        self.results = results
        self.submissions_sent = submissions_sent


class _Result:
    def __init__(self, failure: Failure) -> None:
        self.failure = failure


def test_outcome_exit_codes() -> None:
    assert cli._outcome_exit_code(_FireOutcome(object(), [], 1)) == cli.EXIT_OK
    assert (
        cli._outcome_exit_code(_FireOutcome(None, [_Result(Failure.BID_ERR_MIN)], 1))
        == cli.EXIT_ALL_REJECTED
    )
    assert (
        cli._outcome_exit_code(_FireOutcome(None, [_Result(Failure.NETWORK)], 1))
        == cli.EXIT_NETWORK
    )
    assert (
        cli._outcome_exit_code(_FireOutcome(None, [_Result(Failure.AUTH_EXPIRED)], 1))
        == cli.EXIT_AUTH
    )
    # A dry run or a probe-only schedule submitted nothing, so nothing was rejected.
    assert (
        cli._outcome_exit_code(_FireOutcome(None, [_Result(Failure.BID_ERR_MIN)], 0))
        == cli.EXIT_OK
    )


def _loaded_config(path: str) -> Config:
    """The config a run would hold, without ``validate`` — the caller sets what it breaks."""
    return cli.load(path, {})


def test_an_unusable_fire_time_costs_zero_requests(
    write_config,
    monkeypatch,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """Every request before T is a round trip and most are buyer-visible audit rows."""
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    cfg = _loaded_config(write_config())
    cfg.fire_time = "2099-01-01 10:00:00 America/Bogota"
    ref = cli.page_parse.parse_auction_page(cfg.auction_page_html_path)
    waves = cli.build_waves(cfg)
    bid_value = cli.numbers.parse_user_decimal(cfg.bid_value)

    logger = cli.RunLogger(cfg)
    try:
        with pytest.raises(SecopError) as excinfo:
            cli._execute_run(cfg, ref, waves, bid_value, logger, [])
    finally:
        logger.close()

    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert wire.urls == []


def test_the_wire_date_is_restamped_at_arming_not_at_payload_build(
    write_config,
    monkeypatch,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """The payload is built ahead of the countdown; its ``Date`` must not age with it."""
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    build_payload = cli.build_payload

    def stale_build(ref: Any, detail: Any, bid_value: Any, now: Any = None) -> Any:
        payload, build_report = build_payload(ref, detail, bid_value, now)
        return (dataclasses.replace(payload, date=STALE_DATE), build_report)

    monkeypatch.setattr(cli, "build_payload", stale_build)

    config = write_config(
        fire_time=_fire_time(2),
        n_direct=0,
        n_validate_only=2,
        gate_mode="off",
        clock_probe="none",
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    submitted = wire.bodies_for("ValidateBid")
    assert len(submitted) == 2
    # serialize once: every thread puts the same bytes on the wire
    assert len(set(submitted)) == 1

    sent = json.loads(submitted[0].decode("utf-8"))
    assert sent["Date"] != STALE_DATE
    stamped = datetime.strptime(sent["Date"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 60.0


def test_preflight_names_the_header_that_does_not_match_the_capture(
    write_config,
    monkeypatch,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    assert cli.main(["preflight", "--config", write_config()]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "--- HEADERS ---" in out
    for name, (sent, captured) in cli.endpoints.header_divergence().items():
        assert name in out
        assert sent in out
        assert captured in out


def test_the_suite_does_not_need_the_package_to_be_installed() -> None:
    """A bare ``pytest`` must work: nothing here installs ``secop_firstbid``."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = pyproject.split("[tool.pytest.ini_options]", 1)[1].split("\n[", 1)[0]

    assert 'pythonpath = ["."]' in section
    assert Path(cli.__file__).resolve().parent.parent == REPO_ROOT


def _run_summary(tmp_path) -> Dict[str, Any]:
    return json.loads((_only_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))


def test_a_declined_live_reading_falls_back_to_the_clock_probe_shift(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    """The run that cannot place SECOP's clock does not fall back to real time.

    The fake log clock is 30 s off, so the live reading is outside the window and
    declined; the configured 945 ms from clock-probe moves T instead, and the probe is
    sent that much after fire_time.
    """
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    wire.log_clock_offset_ms = -30000.0
    config = write_config(
        fire_time=_fire_time(6),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
        apply_clock_offset=True,
        clock_window_ms=1000,
        clock_fallback_shift_ms=945,
        keepalive_offsets_ms=[],
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    summary = _run_summary(tmp_path)
    assert summary["clock"]["applied"] is True
    assert summary["clock"]["shift_ms"] == 945.0
    assert any("clock_fallback_shift_ms" in w for w in summary["warnings"])

    fire_time = datetime.strptime(summary["fire_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    sends = [
        record
        for record in _events(_only_run_dir(tmp_path))
        if record["event"] == "http_response" and "ValidateBid" in record["detail"]["url"]
    ]
    sent_at = datetime.strptime(sends[0]["ts_wall"][:26], "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc
    )
    late_ms = (sent_at - fire_time).total_seconds() * 1000.0
    assert 900.0 < late_ms < 1100.0, late_ms

    out = capsys.readouterr().out
    assert "T moved +945 ms by the clock-probe fallback" in out
    assert "T was NOT moved" not in out


def test_a_usable_live_reading_wins_over_the_fallback_and_a_gap_is_reported(
    write_config,
    monkeypatch,
    tmp_path,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    wire = _fake_transport(
        monkeypatch, auction_simple, bids_total_info, validatebid_responses
    )
    config = write_config(
        fire_time=_fire_time(6),
        n_direct=0,
        n_validate_only=1,
        gate_mode="off",
        clock_probe="none",
        apply_clock_offset=True,
        clock_window_ms=1000,
        clock_fallback_shift_ms=600,
        keepalive_offsets_ms=[],
    )
    assert cli.main(["run", "--config", config]) == cli.EXIT_OK

    summary = _run_summary(tmp_path)
    assert abs(summary["clock"]["shift_ms"] - 945.0) < 100.0
    assert any("The live reading was used" in w for w in summary["warnings"])
    assert any(
        r["event"] == "clock_disagreement" for r in _events(_only_run_dir(tmp_path))
    )


def test_set_accepts_and_clears_the_fallback() -> None:
    assert cli.parse_set_overrides(["clock_fallback_shift_ms=945"]) == {
        "clock_fallback_shift_ms": 945
    }
    assert cli.parse_set_overrides(["clock_fallback_shift_ms=null"]) == {
        "clock_fallback_shift_ms": None
    }
    with pytest.raises(SecopError):
        cli.parse_set_overrides(["clock_fallback_shift_ms=soon"])


def test_clock_probe_prints_the_fallback_line(
    write_config,
    monkeypatch,
    capsys,
    auction_simple,
    bids_total_info,
    validatebid_responses,
) -> None:
    _fake_transport(monkeypatch, auction_simple, bids_total_info, validatebid_responses)
    assert cli.main(["clock-probe", "--config", write_config()]) == cli.EXIT_OK
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "set clock_fallback_shift_ms:" in l]
    assert len(line) == 1
    value = int(line[0].rsplit(":", 1)[1])
    assert 900 <= value <= 1000
