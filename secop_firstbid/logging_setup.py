"""Central redaction and the single-writer run logger (plan §12.1–§12.5).

Two things this module has to get right.

**Redaction is not a header transform, and it is not a shape test either.** On the
SignalR path the bearer token is not a header at all — it rides as a ``token=<2370
chars>`` *query parameter* on every poll, send and ping URL, and §12.2 mandates logging
``url`` on every exchange while §12.1 mandates a ``capture.har`` of every request. A
``redact()`` written against header dicts therefore writes a live 24 h token into
``events.jsonl``, ``http/*.json`` and ``capture.har`` — the artifact most likely to be
attached to a bug report. So :func:`redact_url` rewrites the JWT-bearing query keys, and
:func:`redact` applies it to any URL-shaped string it meets anywhere in a record, at any
depth. A token is also not always a whole value or a query parameter: it turns up inside
a request or response body, a free-text ``detail`` string, an exception message and a URL
path, so on top of the URL rewrite *every* string gets a JWT search-and-replace
regardless of its shape.

**One writer.** ``events.jsonl``, ``run.log``, ``timeline.jsonl``, ``capture.har`` and
``http/NNN_*.json`` are all written from a synchronized burst of threads at the fire
instant, so every one of them goes through a single :class:`queue.Queue` drained by one
writer thread; otherwise they interleave-corrupt exactly the records they exist to
capture. The ``NNN`` file counter and the ``corr_id`` come from the same lock-protected
counter and are assigned at *send* time, so a request that never returns is still
traceable. Redaction happens on the writer thread, immediately before serialization: no
unredacted byte reaches disk.
"""

from __future__ import annotations

import binascii
import dataclasses
import os
import queue
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping as _AbcMapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from .capture import (
    HarWriter,
    TimelineWriter,
    body_text,
    dumps,
    dumps_pretty,
    har_entry,
    iso_micros,
)

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

JWT_QUERY_KEYS = ("token", "access_token", "jwt", "id_token")

REDACTED = "<redacted>"

_KEEP = 6
_MIN_ELLIPSIS_LEN = 16
_ELLIPSIS = "…"

# Unanchored: a token embedded in a longer string leaks just as well as a bare one.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*")

_NORM_RE = re.compile(r"[^a-z0-9]")

_SECRET_KEYS = frozenset(
    (
        "jwt",
        "token",
        "accesstoken",
        "idtoken",
        "refreshtoken",
        "bearertoken",
        "apikey",
        "xapikey",
        "xauthtoken",
        "xaccesstoken",
        "password",
        "passwd",
        "secret",
        "clientsecret",
    )
)

_SECRET_HEADER_NAMES = frozenset(
    ("xauthtoken", "xapikey", "apikey", "xaccesstoken", "xcsrftoken", "xxsrftoken")
)

_AUTH_KEYS = frozenset(("authorization", "proxyauthorization"))

_URL_KEYS = frozenset(
    (
        "url",
        "uri",
        "href",
        "requesturl",
        "responseurl",
        "redirecturl",
        "location",
        "referer",
        "referrer",
    )
)

_HEADER_KEYS = frozenset(("headers", "requestheaders", "responseheaders"))

_ID_SEGMENT_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d+)$"
)

_UNSAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]")

_JWT_QUERY_KEYS_LOWER = frozenset(key.lower() for key in JWT_QUERY_KEYS)


def _norm_key(name: Any) -> str:
    return _NORM_RE.sub("", str(name).lower())


def _already_redacted(value: str) -> bool:
    return value == REDACTED or _ELLIPSIS in value


def redact_token(tok: str) -> str:
    """``first6…last6``; anything too short to hide that way becomes ``<redacted>``.

    Returns an already-redacted marker unchanged so every caller above stays idempotent.
    """
    if not isinstance(tok, str):
        return REDACTED
    if _already_redacted(tok):
        return tok
    if len(tok) < _MIN_ELLIPSIS_LEN:
        return REDACTED
    return tok[:_KEEP] + _ELLIPSIS + tok[-_KEEP:]


def redact_jwts(value: str) -> str:
    """Replace every JWT occurrence in a string, wherever in it they sit.

    Runs on every logged record from a synchronized burst of threads, so the ``eyJ``
    membership test guards the regex: the overwhelming majority of strings never reach it.
    """
    if not isinstance(value, str) or "eyJ" not in value:
        return value
    return _JWT_RE.sub(lambda match: redact_token(match.group(0)), value)


def _looks_like_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://", "//")) or "://" in lowered or value.startswith("/")


def redact_url(u: str) -> str:
    """Rewrite the JWT-bearing query parameters of a URL, leaving everything else byte-identical.

    The SignalR poll/send/ping URLs carry the bearer token as ``token=``; the rest of the
    query string (``connectionToken``, ``connectionData``, ``messageId``) is diagnostic
    and must survive intact for the capture to be replayable.
    """
    if not isinstance(u, str) or "?" not in u:
        return u
    parts = urllib.parse.urlsplit(u)
    if not parts.query:
        return u

    rewritten: List[str] = []
    changed = False
    for pair in parts.query.split("&"):
        name, sep, value = pair.partition("=")
        if sep and name.lower() in _JWT_QUERY_KEYS_LOWER and value:
            decoded = urllib.parse.unquote(value)
            new_pair = name + "=" + urllib.parse.quote(redact_token(decoded), safe="")
            if new_pair != pair:
                changed = True
            rewritten.append(new_pair)
        else:
            rewritten.append(pair)

    if not changed:
        return u
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(rewritten), parts.fragment)
    )


def _redact_authorization(value: Any) -> str:
    if not isinstance(value, str):
        return REDACTED
    scheme, sep, _rest = value.partition(" ")
    if sep and scheme:
        return scheme + " " + REDACTED
    return REDACTED


def _redact_cookie(value: Any) -> Any:
    if not isinstance(value, str):
        return REDACTED
    out = []
    for chunk in value.split(";"):
        item = chunk.strip()
        if not item:
            continue
        name, sep, _ = item.partition("=")
        out.append(name.strip() + "=" if sep else name.strip())
    return "; ".join(out)


def _redact_set_cookie(value: Any) -> Any:
    if not isinstance(value, str):
        return REDACTED
    chunks = value.split(";")
    first = chunks[0].strip()
    name, sep, _ = first.partition("=")
    head = name.strip() + "=" if sep else name.strip()
    rest = [chunk.strip() for chunk in chunks[1:] if chunk.strip()]
    return "; ".join([head] + rest)


def _redact_header_value(name: str, value: Any) -> Any:
    norm = _norm_key(name)
    if norm in _AUTH_KEYS:
        return _redact_authorization(value)
    if norm == "cookie":
        return _redact_cookie(value)
    if norm == "setcookie":
        return _redact_set_cookie(value)
    if norm in _SECRET_HEADER_NAMES:
        return redact_token(value) if isinstance(value, str) else REDACTED
    if isinstance(value, str):
        return _redact_str(value)
    return redact(value)


def redact_headers(h: Mapping[str, str]) -> Dict[str, str]:
    """``Authorization`` keeps its scheme only; cookie names survive with blanked values."""
    if not isinstance(h, _AbcMapping):
        return {}
    return dict(
        (_redact_key(name), _redact_header_value(name, value)) for name, value in h.items()
    )


def _redact_str(value: str) -> str:
    if _looks_like_url(value):
        value = redact_url(value)
    return redact_jwts(value)


def _redact_key(key: Any) -> Any:
    """A mapping key is written out too, so it gets the same scrub as a value.

    Only ``str`` keys are touched: the other kinds :func:`json.dumps` accepts as keys —
    ``int``, ``float``, ``bool``, ``None`` — have nowhere for a token to hide.
    """
    if isinstance(key, str):
        return _redact_str(key)
    return key


def _redact_bytes(value: Any) -> Any:
    """Scrub a byte string, leaving one that cannot contain a JWT byte-identical.

    ``capture.json_default`` writes these out as ``utf-8``-with-replacement text, so an
    unscrubbed one is plaintext on disk. Decoding is only safe *because* it is gated on
    the ``eyJ`` marker: a body with no token is passed through untouched rather than
    round-tripped through lossy replacement characters.
    """
    if b"eyJ" not in value:
        return value
    return _redact_str(bytes(value).decode("utf-8", "replace")).encode("utf-8")


def _redact_for_key(key: Any, value: Any) -> Any:
    norm = _norm_key(key)
    if norm in _HEADER_KEYS and isinstance(value, _AbcMapping):
        return redact_headers(value)
    if norm in _AUTH_KEYS:
        return _redact_authorization(value)
    if norm == "cookie":
        return _redact_cookie(value)
    if norm == "setcookie":
        return _redact_set_cookie(value)
    if norm in _SECRET_KEYS:
        return redact_token(value) if isinstance(value, str) else redact(value)
    if norm in _URL_KEYS and isinstance(value, str):
        return redact_jwts(redact_url(value))
    return redact(value)


def redact(obj: Any) -> Any:
    """Recursively scrub a record. Idempotent: ``redact(redact(x)) == redact(x)``.

    Applies :func:`redact_url` to URL-shaped strings wherever they appear, not only to
    keys named ``url``, because the token travels as a query parameter and a record may
    carry a URL inside a body, a header value or a free-text message. Then
    :func:`redact_jwts` runs over every string whatever its shape, because a token also
    arrives embedded — in a body, a ``detail`` value, an exception message, a URL path —
    where neither the key name nor the shape of the value gives it away.

    Every container ``capture.json_default`` knows how to serialise is walked, not just
    the ones the app happens to log today: a mapping's **keys** as well as its values,
    and ``bytes``/``bytearray`` and ``set``/``frozenset``, each of which that function
    turns into plaintext. The classifier still keys off the *original* key, so scrubbing
    the key cannot change how its value is treated. ``set`` and ``tuple`` come back as
    lists, in the order ``json_default`` would have emitted them.
    """
    if isinstance(obj, _AbcMapping):
        return dict(
            (_redact_key(key), _redact_for_key(key, value)) for key, value in obj.items()
        )
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    if isinstance(obj, (set, frozenset)):
        return [redact(item) for item in sorted(obj, key=str)]
    if isinstance(obj, (bytes, bytearray)):
        return _redact_bytes(obj)
    if isinstance(obj, str):
        return _redact_str(obj)
    return obj


def new_run_id() -> str:
    """Short random hex naming one run directory.

    NOT for security: it is a collision-avoidance label, never a capability or a secret.
    """
    return binascii.hexlify(os.urandom(4)).decode("ascii")


def _short_hex() -> str:
    return binascii.hexlify(os.urandom(4)).decode("ascii")


def _seq_from_corr_id(corr_id: Optional[str]) -> Optional[int]:
    if not isinstance(corr_id, str):
        return None
    head = corr_id.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def _safe_component(value: Any) -> str:
    text = _UNSAFE_COMPONENT_RE.sub("_", str(value))
    return text[:60] or "x"


def _endpoint_slug(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    names = [seg for seg in path.split("/") if seg and not _ID_SEGMENT_RE.match(seg)]
    tail = names[-2:] if len(names) >= 2 else names
    return _safe_component("_".join(tail) or "request")


def _config_snapshot(cfg: Any) -> Dict[str, Any]:
    if cfg is None:
        return {}
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        return dataclasses.asdict(cfg)
    if isinstance(cfg, _AbcMapping):
        return dict(cfg)
    return dict(getattr(cfg, "__dict__", {}) or {})


class RunLogger:
    """The run directory and every sink in it, fed by one queue and one writer thread.

    Safe to call from any thread. ``event``/``http``/``timeline`` only enqueue; all
    formatting, redaction and I/O happen on the writer thread, which keeps the fire
    instant free of syscalls and makes an unredacted write structurally impossible.

    Usable as a context manager; ``close()`` is idempotent and drains what is queued.
    """

    def __init__(self, cfg: "Config", run_id: Optional[str] = None) -> None:
        self.run_id = run_id or new_run_id()
        self.started_at = datetime.now(timezone.utc)

        run_root = getattr(cfg, "run_root", None)
        if run_root is None and isinstance(cfg, _AbcMapping):
            run_root = cfg.get("run_root")
        run_root = run_root or "runs"
        stamp = self.started_at.strftime("%Y%m%dT%H%M%S") + "Z"
        self.run_dir = Path(str(run_root)) / (stamp + "_" + self.run_id)
        self.http_dir = self.run_dir / "http"
        self.http_dir.mkdir(parents=True, exist_ok=True)

        self._counter_lock = threading.Lock()
        self._seq = 0
        self._queue: "queue.Queue[Optional[Tuple[str, Any]]]" = queue.Queue()
        self._closed = False
        self._close_lock = threading.Lock()

        self._events_fh = (self.run_dir / "events.jsonl").open("a", encoding="utf-8")
        self._log_fh = (self.run_dir / "run.log").open("a", encoding="utf-8")
        self._timeline = TimelineWriter(self.run_dir / "timeline.jsonl")
        self._har = HarWriter(self.run_dir / "capture.har")
        self.writer_errors: List[str] = []

        self._writer = threading.Thread(
            target=self._drain, name="runlogger-" + self.run_id, daemon=True
        )
        self._writer.start()

        self.write_json("config.json", _config_snapshot(cfg))
        self.event(
            stage="preflight",
            event="run_started",
            detail={"run_id": self.run_id, "run_dir": str(self.run_dir)},
        )

    def next_corr_id(self) -> str:
        """Mint the correlation id **and** the ``NNN`` file number for one exchange.

        Called before the request leaves, so an exchange that never returns still has a
        file name and a log identity.
        """
        with self._counter_lock:
            self._seq += 1
            seq = self._seq
        return "%03d-%s" % (seq, _short_hex())

    def event(
        self,
        *,
        stage: str,
        event: str,
        level: str = "INFO",
        thread_id: str = "main",
        role: Optional[str] = None,
        corr_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = {
            "ts_wall": iso_micros(datetime.now(timezone.utc)),
            "ts_mono_ns": time.monotonic_ns(),
            "run_id": self.run_id,
            "stage": stage,
            "level": level,
            "event": event,
            "thread_id": thread_id,
            "role": role,
            "corr_id": corr_id,
            "detail": dict(detail) if detail else {},
        }
        self._put(("event", record))

    def http(
        self,
        *,
        stage: str,
        corr_id: Optional[str],
        thread_id: str,
        role: Optional[str],
        method: str,
        url: str,
        request_headers: Optional[Mapping[str, str]],
        request_body: Any,
        status: Optional[int],
        response_headers: Optional[Mapping[str, str]],
        response_body: Any,
        t_send_mono_ns: Optional[int],
        t_recv_mono_ns: Optional[int],
        exception: Optional[Any] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        seq = _seq_from_corr_id(corr_id)
        if seq is None:
            corr_id = self.next_corr_id()
            seq = _seq_from_corr_id(corr_id)

        now_mono = time.monotonic_ns()
        now_wall = datetime.now(timezone.utc)
        if t_send_mono_ns is None:
            started_wall = now_wall
        else:
            started_wall = now_wall - timedelta(
                microseconds=(now_mono - t_send_mono_ns) / 1000.0
            )

        rtt_ms = None
        if t_send_mono_ns is not None and t_recv_mono_ns is not None:
            rtt_ms = (t_recv_mono_ns - t_send_mono_ns) / 1e6

        payload = {
            "seq": seq,
            "stage": stage,
            "corr_id": corr_id,
            "thread_id": thread_id,
            "role": role,
            "method": method,
            "url": url,
            "request_headers": dict(request_headers) if request_headers else {},
            "request_body": body_text(request_body),
            "status": status,
            "response_headers": dict(response_headers) if response_headers else {},
            "response_body": response_body,
            "t_send_mono_ns": t_send_mono_ns,
            "t_recv_mono_ns": t_recv_mono_ns,
            "rtt_ms": rtt_ms,
            "exception": None if exception is None else _exception_text(exception),
            "extra": dict(extra) if extra else {},
            "ts_wall": iso_micros(now_wall),
            "ts_mono_ns": now_mono,
            "started_wall": started_wall,
        }
        self._put(("http", payload))

    def timeline(self, **fields: Any) -> None:
        record = {"ts_mono_ns": time.monotonic_ns(), "run_id": self.run_id}
        record.update(fields)
        self._put(("timeline", record))

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.run_dir / name
        self._put(("file", (path, obj)))
        return path

    def flush(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        done = threading.Event()
        self._put(("flush", done))
        done.wait(timeout)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=30.0)

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self.event(
                stage="report",
                event="error",
                level="ERROR",
                detail={"exception_type": exc_type.__name__, "exception": repr(exc)},
            )
        self.close()
        return False

    def _put(self, task: Tuple[str, Any]) -> None:
        if self._closed:
            return
        self._queue.put(task)

    def _drain(self) -> None:
        try:
            while True:
                try:
                    task = self._queue.get(timeout=0.25)
                except queue.Empty:
                    self._flush_sinks()
                    continue
                if task is None:
                    break
                self._handle(task)
        finally:
            self._drain_remaining()
            self._close_sinks()

    def _drain_remaining(self) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return
            if task is None:
                continue
            self._handle(task)

    def _handle(self, task: Tuple[str, Any]) -> None:
        kind, payload = task
        try:
            if kind == "event":
                self._write_event(payload)
            elif kind == "http":
                self._write_http(payload)
            elif kind == "timeline":
                self._timeline.add(redact(payload))
            elif kind == "file":
                self._write_file(payload[0], payload[1])
            elif kind == "flush":
                self._flush_sinks()
                payload.set()
        except Exception as exc:  # a logging failure must not kill the run
            self._note_writer_error(kind, exc)

    def _note_writer_error(self, kind: str, exc: BaseException) -> None:
        message = "writer error on %s: %r" % (kind, exc)
        self.writer_errors.append(message)
        try:
            self._log_fh.write(
                iso_micros(datetime.now(timezone.utc)) + " ERROR [log] " + message + "\n"
            )
            self._log_fh.flush()
        except Exception:
            pass

    def _write_event(self, record: Dict[str, Any]) -> None:
        clean = redact(record)
        self._events_fh.write(dumps(clean) + "\n")
        self._events_fh.flush()
        self._log_fh.write(_human_line(clean) + "\n")
        self._log_fh.flush()

    def _write_http(self, payload: Dict[str, Any]) -> None:
        url = redact_jwts(redact_url(payload["url"]))
        exception = redact_jwts(payload["exception"])
        request_headers = redact_headers(payload["request_headers"])
        response_headers = redact_headers(payload["response_headers"])
        request_body = redact(payload["request_body"])
        response_body = redact(payload["response_body"])

        detail: Dict[str, Any] = dict(redact(payload["extra"]))
        detail.update(
            {
                "method": payload["method"],
                "url": url,
                "request_headers": request_headers,
                "request_body": request_body,
                "status": payload["status"],
                "response_headers": response_headers,
                "response_body": response_body,
                "t_send_mono_ns": payload["t_send_mono_ns"],
                "t_recv_mono_ns": payload["t_recv_mono_ns"],
                "rtt_ms": payload["rtt_ms"],
                "exception": exception,
            }
        )

        record = {
            "ts_wall": payload["ts_wall"],
            "ts_mono_ns": payload["ts_mono_ns"],
            "run_id": self.run_id,
            "stage": payload["stage"],
            "level": "ERROR" if exception else "INFO",
            "event": "http_error" if exception else "http_response",
            "thread_id": payload["thread_id"],
            "role": payload["role"],
            "corr_id": payload["corr_id"],
            "detail": detail,
        }
        self._events_fh.write(dumps(record) + "\n")
        self._events_fh.flush()
        self._log_fh.write(_human_line(record) + "\n")
        self._log_fh.flush()

        name = "%03d_%s_%s_%s.json" % (
            payload["seq"],
            _safe_component(payload["role"] or "main"),
            _safe_component(payload["thread_id"]),
            _endpoint_slug(url),
        )
        exchange = {
            "seq": payload["seq"],
            "corr_id": payload["corr_id"],
            "run_id": self.run_id,
            "stage": payload["stage"],
            "thread_id": payload["thread_id"],
            "role": payload["role"],
            "ts_wall": payload["ts_wall"],
        }
        exchange.update(detail)
        (self.http_dir / name).write_text(
            dumps(exchange) + "\n", encoding="utf-8"
        )

        self._timeline.add(
            {
                "ts_mono_ns": payload["t_send_mono_ns"],
                "run_id": self.run_id,
                "kind": "send",
                "corr_id": payload["corr_id"],
                "thread_id": payload["thread_id"],
                "role": payload["role"],
                "stage": payload["stage"],
                "method": payload["method"],
                "url": url,
            }
        )
        self._timeline.add(
            {
                "ts_mono_ns": payload["t_recv_mono_ns"],
                "run_id": self.run_id,
                "kind": "recv" if exception is None else "fail",
                "corr_id": payload["corr_id"],
                "thread_id": payload["thread_id"],
                "role": payload["role"],
                "stage": payload["stage"],
                "status": payload["status"],
                "rtt_ms": payload["rtt_ms"],
            }
        )

        self._har.add(
            har_entry(
                started_wall=payload["started_wall"],
                method=payload["method"],
                url=url,
                request_headers=request_headers,
                request_body=request_body,
                status=payload["status"],
                response_headers=response_headers,
                response_body=response_body,
                time_ms=payload["rtt_ms"],
            )
        )

    def _write_file(self, path: Path, obj: Any) -> None:
        path.write_text(dumps_pretty(redact(obj)) + "\n", encoding="utf-8")

    def _flush_sinks(self) -> None:
        try:
            self._events_fh.flush()
            self._log_fh.flush()
            self._timeline.flush()
            self._har.flush()
        except Exception as exc:
            self._note_writer_error("flush", exc)

    def _close_sinks(self) -> None:
        try:
            self._har.flush()
        except Exception:
            pass
        for closer in (self._timeline.close, self._events_fh.close, self._log_fh.close):
            try:
                closer()
            except Exception:
                pass


def _exception_text(exc: Any) -> str:
    if isinstance(exc, str):
        return exc
    return "%s: %s" % (type(exc).__name__, exc)


def _human_line(record: Mapping[str, Any]) -> str:
    return "%s %-5s [%s] %-6s %s corr=%s %s" % (
        record.get("ts_wall"),
        record.get("level"),
        record.get("stage"),
        record.get("thread_id"),
        record.get("event"),
        record.get("corr_id") or "-",
        dumps(record.get("detail") or {}),
    )


__all__ = [
    "JWT_QUERY_KEYS",
    "REDACTED",
    "RunLogger",
    "new_run_id",
    "redact",
    "redact_headers",
    "redact_jwts",
    "redact_token",
    "redact_url",
]
