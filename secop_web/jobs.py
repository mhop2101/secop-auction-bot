"""Jobs: one auction attempt with one token, from form to SECOP's record.

A job is a directory under the data root (``webjobs/<id>/``) holding:

``job.json``     settings, state, timestamps and the event list shown on its page
``page.html``    the pasted auction page (or just its auction row)
``token.txt``    the bearer token, mode 0600, deleted with the job
``config.json``  the bot config built from the settings, rewritten before every command
``*.out``        what each bot command printed
``runs/``        the bot's own run directories

The web app never bids itself. It runs the bot's commands as subprocesses —
``parse-page``, ``preflight``, ``clock-probe``, ``record`` and ``run --yes-i-mean-it`` —
and the scheduler decides only *when*: the clock probe ``probe_minutes_before`` the
opening, the bot ``arm_minutes_before`` it. Arming requires the operator to tick the
confirmation and retype the bid value, which is this app's equivalent of the CLI's
``--yes-i-mean-it``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from secop_firstbid import page_parse
from secop_firstbid.auth import TokenInfo, inspect_token
from secop_firstbid.config import load as load_config
from secop_firstbid.config import validate as validate_config
from secop_firstbid.errors import SecopError

BOGOTA = timezone(timedelta(hours=-5), "Bogota")

DEFAULT_OFFSETS_MS = [-50, -20, -5, 10, 25, 50, 90]
DEFAULT_KEEPALIVE_S = [-45, -30, -15]

# The run has to be launched at least this long before the opening: the start audit goes
# out 60 s before the first bid and must be read back before T can be moved.
MIN_LAUNCH_LEAD_S = 120

# Runs of one company at one opening get start audits this far apart, so each run's row
# can be told from the others' in the event log (two clock windows plus a margin).
AUDIT_STAGGER_S = 11

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATES = ("draft", "armed", "probing", "running", "done", "cancelled", "failed")
ACTIVE_STATES = ("armed", "probing", "running")

_FALLBACK_LINE = re.compile(r"set clock_fallback_shift_ms:\s*(-?\d+)")
_SECOP_CLOCK_LINE = re.compile(r"SECOP clock\s+(.*)")


class JobError(Exception):
    """A request the operator can fix; its message is shown on the page."""


@dataclass
class Settings:
    """Only what an operator has needed to set so far. Everything else is a bot default."""

    label: str = ""
    auction_id: str = ""
    bid_value: str = "0"
    start_local: str = ""  # "YYYY-MM-DDTHH:MM" or with seconds, Bogota time
    offsets_ms: List[int] = field(default_factory=lambda: list(DEFAULT_OFFSETS_MS))
    bids_per_offset: int = 1
    read_timeout_s: int = 60
    connect_timeout_s: int = 8
    apply_clock_offset: bool = True
    clock_fallback_ms: Optional[int] = None
    auto_clock_probe: bool = True
    probe_minutes_before: int = 9
    arm_minutes_before: int = 5
    start_audit_lead_s: int = 60
    keepalive_s: List[int] = field(default_factory=lambda: list(DEFAULT_KEEPALIVE_S))
    record_delay_s: int = 90


@dataclass
class Job:
    id: str
    created_at: str
    state: str = "draft"
    settings: Settings = field(default_factory=Settings)
    busy: Optional[str] = None
    pid: Optional[int] = None
    armed_at: Optional[str] = None
    probe_done: bool = False
    launched_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    audit_lead_s: Optional[int] = None
    company_code: Optional[str] = None
    events: List[Dict[str, str]] = field(default_factory=list)

    def note(self, message: str, level: str = "info") -> None:
        self.events.append(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": level,
             "message": message}
        )
        del self.events[:-200]


# ----------------------------------------------------------------------- parsing input


def parse_offsets(text: str) -> List[int]:
    """``"-50, -20, 0, 10"`` -> ``[-50, -20, 0, 10]``; sorted, duplicates kept."""
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if not parts:
        raise JobError("give at least one offset, e.g. -50, -20, -5, 10, 25")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        raise JobError("offsets must be whole milliseconds separated by commas: %r" % text)
    if len(values) > 30:
        raise JobError("%d offsets is too many; the live runs used 7" % len(values))
    if any(abs(v) > 10000 for v in values):
        raise JobError("offsets must be within ±10000 ms of the opening")
    return sorted(values)


def parse_start_local(text: str) -> datetime:
    """Bogota wall time from the form -> aware UTC datetime."""
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return local.replace(tzinfo=BOGOTA).astimezone(timezone.utc)
    raise JobError("auction time %r is not YYYY-MM-DD HH:MM (Bogota)" % text)


def clean_token(text: str) -> str:
    token = text.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    token = re.sub(r"\s+", "", token)
    if token.count(".") != 2:
        raise JobError("the token does not look like a JWT (three parts separated by dots)")
    return token


def int_field(form: Dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise JobError("%s must be a whole number" % name.replace("_", " "))
    if not lo <= value <= hi:
        raise JobError("%s must be between %d and %d" % (name.replace("_", " "), lo, hi))
    return value


def settings_from_form(form: Dict[str, str], current: Settings) -> Settings:
    s = Settings(**asdict(current))
    s.label = (form.get("label") or "").strip()[:80]
    s.auction_id = (form.get("auction_id") or "").strip()
    bid = (form.get("bid_value") or "0").strip()
    if not re.match(r"^[0-9][0-9.,]*$", bid):
        raise JobError("bid value must be a non-negative number, e.g. 0")
    s.bid_value = bid
    if form.get("start_local"):
        parse_start_local(form["start_local"])
        s.start_local = form["start_local"].strip()
    s.offsets_ms = parse_offsets(form.get("offsets_ms") or ",".join(map(str, current.offsets_ms)))
    s.bids_per_offset = int_field(form, "bids_per_offset", current.bids_per_offset, 1, 5)
    s.read_timeout_s = int_field(form, "read_timeout_s", current.read_timeout_s, 5, 300)
    s.connect_timeout_s = int_field(form, "connect_timeout_s", current.connect_timeout_s, 1, 60)
    s.apply_clock_offset = form.get("apply_clock_offset") == "on"
    s.auto_clock_probe = form.get("auto_clock_probe") == "on"
    raw_fallback = (form.get("clock_fallback_ms") or "").strip()
    if raw_fallback:
        s.clock_fallback_ms = int_field(form, "clock_fallback_ms", 0, -5000, 5000)
    else:
        s.clock_fallback_ms = None
    s.probe_minutes_before = int_field(form, "probe_minutes_before", current.probe_minutes_before, 3, 55)
    s.arm_minutes_before = int_field(form, "arm_minutes_before", current.arm_minutes_before, 3, 55)
    if s.auto_clock_probe and s.probe_minutes_before <= s.arm_minutes_before:
        raise JobError("the clock probe must run before the bot is armed")
    s.start_audit_lead_s = int_field(form, "start_audit_lead_s", current.start_audit_lead_s, 15, 150)
    if form.get("keepalive_s") is not None:
        raw = form.get("keepalive_s", "").strip()
        s.keepalive_s = parse_offsets(raw) if raw else []
        if any(v >= -9 for v in s.keepalive_s):
            raise JobError("keep-alives must be 9 s or more before the first bid")
    s.record_delay_s = int_field(form, "record_delay_s", current.record_delay_s, 0, 600)
    return s


# ------------------------------------------------------------------------- the store


class Store:
    """All jobs on disk, with one lock around every read-modify-write."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.lock = threading.RLock()

    def dir(self, job_id: str) -> str:
        if not re.match(r"^[0-9A-Za-z_-]+$", job_id):
            raise JobError("bad job id")
        return os.path.join(self.root, job_id)

    def path(self, job_id: str, name: str) -> str:
        return os.path.join(self.dir(job_id), name)

    def create(self) -> Job:
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)
        os.makedirs(self.dir(job_id), mode=0o700)
        job = Job(id=job_id, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        with self.lock:
            data = asdict(job)
            tmp = self.path(job.id, "job.json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
            os.replace(tmp, self.path(job.id, "job.json"))

    def load(self, job_id: str) -> Job:
        with self.lock:
            try:
                with open(self.path(job_id, "job.json"), encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError:
                raise JobError("no such job")
        settings = Settings(**data.pop("settings"))
        return Job(settings=settings, **data)

    def all(self) -> List[Job]:
        jobs = []
        for name in sorted(os.listdir(self.root), reverse=True):
            if os.path.exists(os.path.join(self.root, name, "job.json")):
                try:
                    jobs.append(self.load(name))
                except (JobError, ValueError, TypeError):
                    continue
        return jobs

    def update(self, job_id: str, change: Callable[[Job], None]) -> Job:
        with self.lock:
            job = self.load(job_id)
            change(job)
            self.save(job)
            return job

    def write_private(self, job_id: str, name: str, text: str) -> None:
        path = self.path(job_id, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read_text(self, job_id: str, name: str, limit: int = 200000) -> str:
        try:
            with open(self.path(job_id, name), encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except FileNotFoundError:
            return ""
        return text[-limit:]

    def delete(self, job_id: str) -> None:
        with self.lock:
            shutil.rmtree(self.dir(job_id), ignore_errors=True)


# ----------------------------------------------------------------- facts about a job


def token_info(store: Store, job_id: str) -> Optional[TokenInfo]:
    token = store.read_text(job_id, "token.txt").strip()
    if not token:
        return None
    try:
        return inspect_token(token)
    except SecopError:
        return None


def page_ref(store: Store, job: Job) -> Tuple[Optional[Any], Optional[str]]:
    path = store.path(job.id, "page.html")
    if not os.path.exists(path):
        return (None, "no auction page saved yet")
    try:
        return (page_parse.parse_auction_page(path, job.settings.auction_id or None), None)
    except SecopError as exc:
        return (None, exc.message)


def fire_time(job: Job) -> Optional[datetime]:
    if not job.settings.start_local:
        return None
    try:
        return parse_start_local(job.settings.start_local)
    except JobError:
        return None


def schedule_points(job: Job) -> Dict[str, Optional[datetime]]:
    t = fire_time(job)
    if t is None:
        return {"fire": None, "probe": None, "launch": None}
    s = job.settings
    return {
        "fire": t,
        "probe": t - timedelta(minutes=s.probe_minutes_before) if s.auto_clock_probe else None,
        "launch": t - timedelta(minutes=s.arm_minutes_before),
    }


def latest_summary(store: Store, job_id: str) -> Optional[Dict[str, Any]]:
    runs = store.path(job_id, "runs")
    if not os.path.isdir(runs):
        return None
    best = None
    for name in sorted(os.listdir(runs)):
        path = os.path.join(runs, name, "summary.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
        except ValueError:
            continue
        if summary.get("submissions_planned") is not None and (
            summary.get("attempts") or summary.get("outcome") not in (None, "NOT_FIRED")
        ):
            best = summary
    return best


def record_summary(store: Store, job_id: str) -> Optional[Dict[str, Any]]:
    text = store.read_text(job_id, "record.json")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# ------------------------------------------------------------------- building config


def build_config(store: Store, job: Job, audit_lead_s: Optional[int] = None) -> Dict[str, Any]:
    s = job.settings
    t = fire_time(job)
    if t is None:
        raise JobError("set the auction time first")
    token = store.read_text(job.id, "token.txt").strip()
    if not token:
        raise JobError("paste the token first")
    if not os.path.exists(store.path(job.id, "page.html")):
        raise JobError("paste the auction page first")
    lead_s = audit_lead_s if audit_lead_s is not None else (job.audit_lead_s or s.start_audit_lead_s)
    cfg = {
        "jwt": token,
        "auction_page_html_path": store.path(job.id, "page.html"),
        "bid_value": s.bid_value,
        "fire_time": t.astimezone(BOGOTA).isoformat(),
        "mode": "custom",
        "schedule_offsets_ms": list(s.offsets_ms),
        "wave_direct": s.bids_per_offset,
        "gate_mode": "off",
        "apply_clock_offset": s.apply_clock_offset,
        "clock_fallback_shift_ms": s.clock_fallback_ms if s.apply_clock_offset else None,
        "do_start_audit": True,
        "start_audit_lead_ms": lead_s * 1000,
        "keepalive_offsets_ms": [v * 1000 for v in s.keepalive_s],
        "keepalive_timeout_ms": 3000,
        "connect_timeout_ms": s.connect_timeout_s * 1000,
        "read_timeout_ms": s.read_timeout_s * 1000,
        "read_secop_record": True,
        "secop_record_delay_s": s.record_delay_s,
        # Preflight and the clock probe may run days ahead; the scheduler itself never
        # launches the run earlier than arm_minutes_before.
        "max_arm_ahead_s": 7 * 24 * 3600,
        "run_root": store.path(job.id, "runs"),
    }
    if s.auction_id:
        cfg["auction_id"] = s.auction_id
    return cfg


def write_config(
    store: Store,
    job: Job,
    audit_lead_s: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[str, List[str]]:
    """Write ``config.json`` and return its path and the bot's own warnings about it."""
    cfg = build_config(store, job, audit_lead_s)
    store.write_private(job.id, "config.json", json.dumps(cfg, indent=1))
    path = store.path(job.id, "config.json")
    try:
        loaded = load_config(path)
        warnings = validate_config(loaded, now=now)
    except SecopError as exc:
        raise JobError(exc.message)
    return (path, warnings)


# ---------------------------------------------------------------- running commands


def bot_command(args: List[str]) -> List[str]:
    return [sys.executable, "-W", "ignore", "-m", "secop_firstbid"] + args


def run_capture(store: Store, job_id: str, out_name: str, args: List[str], timeout_s: int) -> int:
    """Run one bot command to completion, its output into ``out_name``."""
    out_path = store.path(job_id, out_name)
    with open(out_path, "w", encoding="utf-8") as out:
        try:
            proc = subprocess.run(
                bot_command(args),
                cwd=REPO_ROOT,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
            )
            return proc.returncode
        except subprocess.TimeoutExpired:
            out.write("\n[web] command timed out after %d s\n" % timeout_s)
            return 124


def parse_probe(text: str) -> Tuple[Optional[int], Optional[str]]:
    shift = _FALLBACK_LINE.search(text)
    line = _SECOP_CLOCK_LINE.search(text)
    return (
        int(shift.group(1)) if shift else None,
        line.group(1).strip() if line else None,
    )


def server_clock() -> Dict[str, Any]:
    """This host's own clock against NTP, from chrony when it is there."""
    info = {"synchronized": None, "offset": None, "source": None, "detail": ""}  # type: Dict[str, Any]
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        if "NTPSynchronized=" in out:
            info["synchronized"] = out.strip().endswith("yes")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(
            ["chronyc", "tracking"], capture_output=True, text=True, timeout=5
        ).stdout
        info["detail"] = out.strip()
        for line in out.splitlines():
            if line.startswith("System time"):
                info["offset"] = line.split(":", 1)[1].strip()
            if line.startswith("Reference ID"):
                info["source"] = line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# ------------------------------------------------------------------------ the scheduler


class Runner:
    """Starts commands for jobs and keeps the arm schedule. One per server process.

    ``spawn`` and ``now`` are injectable so the schedule can be tested without
    subprocesses or waiting.
    """

    def __init__(
        self,
        store: Store,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        spawn: Optional[Callable[[List[str], str], Any]] = None,
    ) -> None:
        self.store = store
        self.now = now
        self.spawn = spawn or self._spawn
        self._procs = {}  # type: Dict[str, Any]
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]

    # -- background actions -------------------------------------------------------

    def background(self, job_id: str, name: str, work: Callable[[], None]) -> None:
        def mark(job: Job) -> None:
            if job.busy:
                raise JobError("%s is still running" % job.busy)
            job.busy = name

        self.store.update(job_id, mark)

        def wrapped() -> None:
            try:
                work()
            except Exception as exc:  # shown on the page rather than lost
                self.store.update(job_id, lambda j: j.note("%s failed: %s" % (name, exc), "error"))
            finally:
                self.store.update(job_id, lambda j: setattr(j, "busy", None))

        threading.Thread(target=wrapped, name="%s-%s" % (name, job_id), daemon=True).start()

    def preflight(self, job_id: str) -> None:
        job = self.store.load(job_id)
        path, _warnings = write_config(self.store, job, now=self.now())

        def work() -> None:
            code = run_capture(self.store, job_id, "preflight.out", ["preflight", "--config", path], 180)
            self.store.update(job_id, lambda j: j.note("preflight finished (exit %d)" % code,
                                                        "info" if code == 0 else "error"))

        self.background(job_id, "preflight", work)

    def clock_probe(self, job_id: str, scheduled: bool = False) -> None:
        job = self.store.load(job_id)
        path, _warnings = write_config(self.store, job, now=self.now())

        def work() -> None:
            code = run_capture(self.store, job_id, "probe.out", ["clock-probe", "--config", path], 180)
            shift, line = parse_probe(self.store.read_text(job_id, "probe.out"))

            def apply(j: Job) -> None:
                j.probe_done = True
                if shift is not None:
                    j.settings.clock_fallback_ms = shift
                    j.note("clock probe: SECOP %s; fallback set to %+d ms" % (line, shift))
                else:
                    j.note("clock probe could not place SECOP's clock (exit %d); fallback "
                           "left at %s" % (code, j.settings.clock_fallback_ms), "error")
                if scheduled and j.state == "probing":
                    j.state = "armed"

            self.store.update(job_id, apply)

        self.background(job_id, "clock probe", work)

    def read_record(self, job_id: str) -> None:
        job = self.store.load(job_id)
        path, _warnings = write_config(self.store, job, now=self.now())
        summary = latest_summary(self.store, job_id) or {}
        host = (summary.get("secop_record") or {}).get("host_address")
        args = ["record", "--config", path, "--out", self.store.path(job_id, "record.json")]
        if host:
            args += ["--host-address", host]

        def work() -> None:
            code = run_capture(self.store, job_id, "record.out", args, 600)
            self.store.update(job_id, lambda j: j.note("SECOP record read (exit %d)" % code,
                                                        "info" if code == 0 else "error"))

        self.background(job_id, "record read", work)

    # -- arming -------------------------------------------------------------------

    def arm(self, job_id: str, confirm: bool, retyped_bid: str) -> None:
        job = self.store.load(job_id)
        if job.state not in ("draft", "cancelled", "failed", "done"):
            raise JobError("job is already %s" % job.state)
        if not confirm or retyped_bid.strip() != job.settings.bid_value:
            raise JobError(
                "to arm a live bid, tick the confirmation and retype the bid value exactly "
                "(%s)" % job.settings.bid_value
            )
        points = schedule_points(job)
        fire = points["fire"]
        if fire is None:
            raise JobError("set the auction time first")
        now = self.now()
        if (fire - now).total_seconds() < MIN_LAUNCH_LEAD_S:
            raise JobError("too late to arm: the bot needs at least %d s before the opening"
                           % MIN_LAUNCH_LEAD_S)
        info = token_info(self.store, job_id)
        if info is None or info.exp is None:
            raise JobError("the token cannot be read")
        if info.exp < fire + timedelta(minutes=15):
            raise JobError("the token expires at %s, before the auction is over"
                           % info.exp.astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M"))
        ref, problem = page_ref(self.store, job)
        if ref is None:
            raise JobError("auction page: %s" % problem)

        # Other active runs of the same company at the same opening get later audits.
        same = [
            other for other in self.store.all()
            if other.id != job_id
            and other.state in ACTIVE_STATES
            and other.company_code == info.company_code
            and fire_time(other) == fire
        ]
        lead_s = job.settings.start_audit_lead_s + AUDIT_STAGGER_S * len(same)
        write_config(self.store, job, audit_lead_s=lead_s, now=now)

        def apply(j: Job) -> None:
            j.state = "armed"
            j.armed_at = now.isoformat(timespec="seconds")
            j.probe_done = False
            j.audit_lead_s = lead_s
            j.company_code = info.company_code
            j.exit_code = None
            j.pid = None
            j.note("armed: bot launches at %s, %d bid(s), start audit %d s before the first bid"
                   % (points["launch"].astimezone(BOGOTA).strftime("%H:%M:%S"),
                      len(j.settings.offsets_ms) * j.settings.bids_per_offset, lead_s))

        self.store.update(job_id, apply)

    def cancel(self, job_id: str) -> None:
        job = self.store.load(job_id)
        if job.state in ("armed", "probing"):
            self.store.update(job_id, lambda j: (setattr(j, "state", "cancelled"),
                                                 j.note("cancelled before launch")))
            return
        if job.state == "running":
            fire = fire_time(job)
            if fire is not None and self.now() > fire - timedelta(seconds=2):
                raise JobError("the bids are already going out; the run ends by itself")
            self._interrupt(job)
            self.store.update(job_id, lambda j: (setattr(j, "state", "cancelled"),
                                                 j.note("bot stopped before the opening")))
            return
        raise JobError("nothing to cancel")

    def _interrupt(self, job: Job) -> None:
        proc = self._procs.get(job.id)
        try:
            if proc is not None:
                proc.send_signal(signal.SIGINT)
            elif job.pid:
                os.kill(job.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

    # -- the loop -----------------------------------------------------------------

    def _spawn(self, args: List[str], out_path: str) -> Any:
        out = open(out_path, "w", encoding="utf-8")
        return subprocess.Popen(
            bot_command(args),
            cwd=REPO_ROOT,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
            start_new_session=True,
        )

    def launch(self, job: Job) -> None:
        path, _warnings = write_config(self.store, job, now=self.now())
        proc = self.spawn(["run", "--config", path, "--yes-i-mean-it"], self.store.path(job.id, "run.out"))
        self._procs[job.id] = proc

        def apply(j: Job) -> None:
            j.state = "running"
            j.pid = getattr(proc, "pid", None)
            j.launched_at = self.now().isoformat(timespec="seconds")
            j.note("bot launched (pid %s)" % j.pid)

        self.store.update(job.id, apply)

    def tick(self) -> None:
        """One pass of the schedule. Called every second by :meth:`start`."""
        now = self.now()
        for job in self.store.all():
            if job.state == "armed":
                points = schedule_points(job)
                if points["fire"] is None:
                    continue
                if (
                    points["probe"] is not None
                    and not job.probe_done
                    and not job.busy
                    and now >= points["probe"]
                    and now < points["launch"] - timedelta(seconds=30)
                ):
                    self.store.update(job.id, lambda j: (setattr(j, "state", "probing"),
                                                         j.note("running the scheduled clock probe")))
                    self.clock_probe(job.id, scheduled=True)
                    continue
                if now >= points["launch"] and not job.busy:
                    if (points["fire"] - now).total_seconds() < MIN_LAUNCH_LEAD_S / 2:
                        self.store.update(job.id, lambda j: (setattr(j, "state", "failed"),
                                                             j.note("missed the launch time", "error")))
                        continue
                    self.launch(self.store.load(job.id))
            elif job.state == "probing" and not job.busy:
                self.store.update(job.id, lambda j: setattr(j, "state", "armed"))
            elif job.state == "running":
                self._check_running(job)

    def _check_running(self, job: Job) -> None:
        proc = self._procs.get(job.id)
        code = None  # type: Optional[int]
        if proc is not None:
            code = proc.poll()
            if code is None:
                return
        elif job.pid and _alive(job.pid):
            return
        self._procs.pop(job.id, None)

        def apply(j: Job) -> None:
            j.state = "done"
            j.exit_code = code
            j.finished_at = self.now().isoformat(timespec="seconds")
            summary = latest_summary(self.store, j.id) or {}
            j.note("bot finished: %s (exit %s)" % (summary.get("outcome", "?"), code))

        self.store.update(job.id, apply)

    def recover(self) -> None:
        """After a restart: a job left probing goes back to armed; a running one is watched."""
        for job in self.store.all():
            if job.state == "probing" or job.busy:
                self.store.update(job.id, lambda j: (setattr(j, "busy", None),
                                                     setattr(j, "state", "armed" if j.state == "probing" else j.state)))

    def start(self) -> None:
        self.recover()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # the loop must outlive one bad job file
                    sys.stderr.write("scheduler: %s\n" % exc)
                self._stop.wait(1.0)

        self._thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


__all__ = [
    "BOGOTA",
    "DEFAULT_OFFSETS_MS",
    "Job",
    "JobError",
    "Runner",
    "Settings",
    "Store",
    "build_config",
    "clean_token",
    "fire_time",
    "latest_summary",
    "page_ref",
    "parse_offsets",
    "parse_probe",
    "parse_start_local",
    "record_summary",
    "schedule_points",
    "server_clock",
    "settings_from_form",

    "token_info",
    "write_config",
]
