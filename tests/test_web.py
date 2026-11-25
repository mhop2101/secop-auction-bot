"""The web app: input parsing, the arm safeguards, the schedule, and an HTTP round trip.

Offline like every other test. The scheduler is driven with an injected clock and a fake
``spawn``, so no bot process starts and nothing waits; the HTTP test runs the real server on
a local port without TLS.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import pytest

from conftest import FIXTURE_AUCTION_ID, OPPORTUNITY_PAGE
from secop_web import auth, jobs
from secop_web.jobs import JobError, Runner, Settings, Store
from secop_web.server import App, make_handler

COMPANY = "900000001"


def fake_jwt(hours: float = 20, company: str = COMPANY) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp())
    claims = {
        "exp": exp,
        "aud": "https://subastas.secop.gov.co/",
        "http://www.nextway.com/NextWay.STS.Claim/LoggedInCompanyCode": company,
        "http://www.nextway.com/NextWay.STS.Claim/LoggedInUserName": "OPERADOR DE PRUEBA",
    }

    def seg(obj: Dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return "%s.%s.%s" % (seg({"alg": "RS256"}), seg(claims), "c2lnbmF0dXJl")


def bogota_text(dt: datetime) -> str:
    return dt.astimezone(jobs.BOGOTA).strftime("%Y-%m-%dT%H:%M:%S")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def __call__(self) -> datetime:
        return self.value


class FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self.code = None  # type: Optional[int]
        self.signals = []  # type: List[int]

    def poll(self) -> Optional[int]:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


def ready_job(store: Store, start: datetime, token: Optional[str] = None, **settings: Any) -> str:
    job = store.create()
    values = dict(start_local=bogota_text(start), label="test")
    values.update(settings)

    def apply(j: jobs.Job) -> None:
        for key, value in values.items():
            setattr(j.settings, key, value)

    store.update(job.id, apply)
    store.write_private(job.id, "page.html", OPPORTUNITY_PAGE.read_text(encoding="utf-8"))
    store.write_private(job.id, "token.txt", token or fake_jwt())
    return job.id


# -------------------------------------------------------------------- input parsing


def test_offsets_are_parsed_sorted_and_bounded() -> None:
    assert jobs.parse_offsets("10, -50 ,-20;0") == [-50, -20, 0, 10]
    with pytest.raises(JobError):
        jobs.parse_offsets("")
    with pytest.raises(JobError):
        jobs.parse_offsets("10, soon")
    with pytest.raises(JobError):
        jobs.parse_offsets("20000")


def test_the_opening_is_read_as_bogota_time() -> None:
    assert jobs.parse_start_local("2026-11-23T13:30") == datetime(
        2026, 11, 23, 18, 30, tzinfo=timezone.utc
    )
    assert jobs.parse_start_local("2026-11-23 13:30:05") == datetime(
        2026, 11, 23, 18, 30, 5, tzinfo=timezone.utc
    )
    with pytest.raises(JobError):
        jobs.parse_start_local("23/11/2026 1:30 PM")


def test_a_pasted_bearer_token_is_cleaned() -> None:
    token = fake_jwt()
    assert jobs.clean_token("Bearer " + token[:20] + "\n" + token[20:] + "  ") == token
    with pytest.raises(JobError):
        jobs.clean_token("not a token")


def test_the_form_keeps_only_sane_settings() -> None:
    form = {
        "label": "Fiscalía",
        "bid_value": "0",
        "start_local": "2026-11-23T13:30",
        "offsets_ms": "-50,-20,-5,10,25,50,90",
        "bids_per_offset": "2",
        "read_timeout_s": "60",
        "apply_clock_offset": "on",
        "auto_clock_probe": "on",
        "probe_minutes_before": "9",
        "arm_minutes_before": "5",
        "keepalive_s": "-45,-30,-15",
    }
    s = jobs.settings_from_form(form, Settings())
    assert s.bids_per_offset == 2
    assert s.apply_clock_offset and s.auto_clock_probe
    assert s.clock_fallback_ms is None

    with pytest.raises(JobError):
        jobs.settings_from_form(dict(form, bids_per_offset="9"), Settings())
    with pytest.raises(JobError):
        jobs.settings_from_form(dict(form, probe_minutes_before="4"), Settings())
    with pytest.raises(JobError):
        jobs.settings_from_form(dict(form, keepalive_s="-45,-5"), Settings())
    with pytest.raises(JobError):
        jobs.settings_from_form(dict(form, bid_value="-1"), Settings())


# ------------------------------------------------------------------------- the config


def test_the_config_is_a_valid_bot_config_carrying_the_settings(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    job_id = ready_job(store, start, offsets_ms=[-20, 0, 30], bids_per_offset=2,
                       clock_fallback_ms=905)

    path, warnings = jobs.write_config(store, store.load(job_id))

    cfg = json.loads(open(path, encoding="utf-8").read())
    assert cfg["schedule_offsets_ms"] == [-20, 0, 30]
    assert cfg["wave_direct"] == 2
    assert cfg["read_timeout_ms"] == 60000
    assert cfg["apply_clock_offset"] is True
    assert cfg["clock_fallback_shift_ms"] == 905
    assert cfg["keepalive_offsets_ms"] == [-45000, -30000, -15000]
    assert cfg["auction_page_html_path"].endswith("page.html")
    assert "confirm_live" not in cfg
    assert any("ZERO" in w for w in warnings)
    assert oct(__import__("os").stat(path).st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------ arming safeguards


def test_arming_needs_the_confirmation_and_the_bid_retyped(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    job_id = ready_job(store, start)
    runner = Runner(store, spawn=lambda args, out: FakeProc())

    with pytest.raises(JobError):
        runner.arm(job_id, confirm=False, retyped_bid="0")
    with pytest.raises(JobError):
        runner.arm(job_id, confirm=True, retyped_bid="1")
    runner.arm(job_id, confirm=True, retyped_bid="0")

    job = store.load(job_id)
    assert job.state == "armed"
    assert job.company_code == COMPANY
    assert job.audit_lead_s == 60


def test_arming_too_late_or_with_a_dying_token_is_refused(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    late = ready_job(store, soon)
    runner = Runner(store)
    with pytest.raises(JobError, match="too late"):
        runner.arm(late, True, "0")

    later = datetime.now(timezone.utc) + timedelta(hours=3)
    dying = ready_job(store, later, token=fake_jwt(hours=1))
    with pytest.raises(JobError, match="expires"):
        runner.arm(dying, True, "0")


def test_two_runs_of_one_company_get_start_audits_apart(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    first = ready_job(store, start)
    second = ready_job(store, start)
    runner = Runner(store)
    runner.arm(first, True, "0")
    runner.arm(second, True, "0")

    assert store.load(first).audit_lead_s == 60
    assert store.load(second).audit_lead_s == 71
    cfg = json.loads(store.read_text(second, "config.json"))
    assert cfg["start_audit_lead_ms"] == 71000


# ---------------------------------------------------------------------- the schedule


def test_the_schedule_probes_then_launches_then_finishes(tmp_path, monkeypatch) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime(2030, 1, 1, 18, 30, tzinfo=timezone.utc)
    clock = Clock(start - timedelta(minutes=20))
    job_id = ready_job(store, start, token=fake_jwt(hours=24 * 365 * 5))
    spawned = []  # type: List[Tuple[List[str], str]]
    proc = FakeProc()

    def spawn(args: List[str], out: str) -> FakeProc:
        spawned.append((args, out))
        return proc

    probes = []  # type: List[List[str]]

    def fake_capture(store_: Store, jid: str, name: str, args: List[str], timeout: int) -> int:
        probes.append(args)
        store_.write_private(jid, name, "SECOP clock    904.9 ms behind this host\n"
                                        "As the fallback ..., set clock_fallback_shift_ms: 905\n")
        return 0

    monkeypatch.setattr(jobs, "run_capture", fake_capture)
    runner = Runner(store, now=clock, spawn=spawn)
    runner.arm(job_id, True, "0")

    runner.tick()
    assert store.load(job_id).state == "armed" and not probes

    clock.value = start - timedelta(minutes=9)
    runner.tick()
    for _ in range(100):
        if not store.load(job_id).busy:
            break
        threading.Event().wait(0.02)
    job = store.load(job_id)
    assert probes and probes[0][0] == "clock-probe"
    assert job.settings.clock_fallback_ms == 905
    assert job.state == "armed"

    clock.value = start - timedelta(minutes=5)
    runner.tick()
    job = store.load(job_id)
    assert job.state == "running"
    assert spawned[0][0][0] == "run" and "--yes-i-mean-it" in spawned[0][0]
    cfg = json.loads(store.read_text(job_id, "config.json"))
    assert cfg["clock_fallback_shift_ms"] == 905

    clock.value = start + timedelta(minutes=2)
    runner.tick()
    assert store.load(job_id).state == "running"
    proc.code = 0
    runner.tick()
    job = store.load(job_id)
    assert job.state == "done" and job.exit_code == 0


def test_cancel_stops_a_launched_bot_only_before_the_opening(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime(2030, 1, 1, 18, 30, tzinfo=timezone.utc)
    clock = Clock(start - timedelta(minutes=10))
    job_id = ready_job(store, start, token=fake_jwt(hours=24 * 365 * 5), auto_clock_probe=False)
    proc = FakeProc()
    runner = Runner(store, now=clock, spawn=lambda args, out: proc)
    runner.arm(job_id, True, "0")
    clock.value = start - timedelta(minutes=5)
    runner.tick()
    assert store.load(job_id).state == "running"

    clock.value = start - timedelta(seconds=1)
    with pytest.raises(JobError, match="already going out"):
        runner.cancel(job_id)

    clock.value = start - timedelta(minutes=2)
    runner.cancel(job_id)
    assert proc.signals and store.load(job_id).state == "cancelled"


def test_a_missed_launch_fails_instead_of_firing_late(tmp_path) -> None:
    store = Store(str(tmp_path / "webjobs"))
    start = datetime(2030, 1, 1, 18, 30, tzinfo=timezone.utc)
    clock = Clock(start - timedelta(minutes=10))
    job_id = ready_job(store, start, token=fake_jwt(hours=24 * 365 * 5), auto_clock_probe=False)
    runner = Runner(store, now=clock, spawn=lambda args, out: FakeProc())
    runner.arm(job_id, True, "0")

    clock.value = start - timedelta(seconds=30)
    runner.tick()
    assert store.load(job_id).state == "failed"


def test_a_probe_reading_is_parsed() -> None:
    assert jobs.parse_probe("SECOP clock    894.6 ms behind this host (x)\n"
                            "As the fallback for a run, set clock_fallback_shift_ms: 895\n") == (
        895, "894.6 ms behind this host (x)")
    assert jobs.parse_probe("nothing") == (None, None)


# ---------------------------------------------------------------------------- login


def test_passwords_are_hashed_and_checked() -> None:
    record = auth.hash_password("correct horse battery")
    user = auth.UserRecord("op", bytes.fromhex(record["salt"]), bytes.fromhex(record["hash"]),
                           int(record["iterations"]))
    assert user.check("op", "correct horse battery")
    assert not user.check("op", "wrong")
    assert not user.check("other", "correct horse battery")


def test_logins_are_throttled_per_address() -> None:
    sessions = auth.Sessions()
    for _ in range(auth.LOGIN_FAILURES_ALLOWED):
        assert not sessions.throttled("198.51.100.4")
        sessions.record_failure("198.51.100.4")
    assert sessions.throttled("198.51.100.4")
    assert not sessions.throttled("198.51.100.5")


# --------------------------------------------------------------------- HTTP round trip


@pytest.fixture
def server(tmp_path) -> Any:
    users = tmp_path / "users.json"
    auth.write_user_file(str(users), "operador", "una-clave-larga-de-prueba")
    store = Store(str(tmp_path / "webjobs"))
    runner = Runner(store, spawn=lambda args, out: FakeProc())
    app = App(store, runner, auth.load_user_file(str(users)), secure=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1], store
    httpd.shutdown()


def request(port: int, method: str, path: str, form: Optional[Dict[str, str]] = None,
            cookie: Optional[str] = None) -> Tuple[int, Dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    body = None
    if form is not None:
        body = urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text


def login(port: int) -> Tuple[str, str]:
    status, headers, _ = request(port, "POST", "/login",
                                 {"username": "operador", "password": "una-clave-larga-de-prueba"})
    assert status == 303
    cookie = headers["set-cookie"].split(";")[0]
    status, _, text = request(port, "GET", "/", cookie=cookie)
    assert status == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', text).group(1)
    return cookie, csrf


def test_every_page_needs_a_login(server) -> None:
    port, _store = server
    status, headers, _ = request(port, "GET", "/")
    assert status == 303 and headers["location"] == "/login"
    status, _, _ = request(port, "POST", "/login", {"username": "operador", "password": "nope"})
    assert status == 401


def test_a_post_without_the_csrf_token_is_refused(server) -> None:
    port, _store = server
    cookie, _csrf = login(port)
    status, _, _ = request(port, "POST", "/jobs", {}, cookie)
    assert status == 403


def test_a_job_is_created_filled_in_and_shown_without_its_token(server) -> None:
    port, store = server
    cookie, csrf = login(port)
    status, headers, _ = request(port, "POST", "/jobs", {"csrf": csrf}, cookie)
    assert status == 303
    path = headers["location"]

    token = fake_jwt()
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    form = {
        "csrf": csrf,
        "label": "prueba",
        "start_local": bogota_text(start)[:16],
        "bid_value": "0",
        "offsets_ms": "-50,-20,-5,10,25,50,90",
        "bids_per_offset": "1",
        "read_timeout_s": "60",
        "arm_minutes_before": "5",
        "apply_clock_offset": "on",
        "auto_clock_probe": "on",
        "probe_minutes_before": "9",
        "clock_fallback_ms": "",
        "keepalive_s": "-45,-30,-15",
        "page_html": OPPORTUNITY_PAGE.read_text(encoding="utf-8"),
        "token": "Bearer " + token,
    }
    status, _, text = request(port, "POST", path + "/settings", form, cookie)
    assert status == 200, text
    assert FIXTURE_AUCTION_ID in text
    assert COMPANY in text
    assert token not in text
    assert "Saved." in text

    status, _, text = request(port, "POST", path + "/action",
                              {"csrf": csrf, "action": "arm", "confirm": "yes", "retyped_bid": "1"},
                              cookie)
    assert status == 400 and "retype the bid value" in text

    status, _, text = request(port, "POST", path + "/action",
                              {"csrf": csrf, "action": "arm", "confirm": "yes", "retyped_bid": "0"},
                              cookie)
    assert status == 200 and "Armed." in text
    job_id = path.rsplit("/", 1)[1]
    assert store.load(job_id).state == "armed"

    status, _, text = request(port, "POST", path + "/settings", form, cookie)
    assert status == 400 and "locked" in text
