"""HAR 1.2 and monotonic-timeline sinks for the run directory (plan §12.1).

``capture.har`` exists so a run can be replayed and inspected in the same tools this
project started from, and ``timeline.jsonl`` so barrier release / send / receive /
first-success can be ordered on the monotonic clock rather than the wall clock.

Both writers are owned by the single :class:`secop_firstbid.logging_setup.RunLogger`
writer thread and are **not** thread-safe on their own: workers hand records to that
thread through its queue instead of touching these objects.

Everything handed to :func:`har_entry` must already be redacted — this module performs
no redaction (:mod:`secop_firstbid.logging_setup` owns that and imports this one, so the
dependency cannot run the other way).
"""

from __future__ import annotations

import http.client
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qsl, urlsplit

from . import __version__

HAR_VERSION = "1.2"
CREATOR_NAME = "secop_firstbid"

# HAR 1.2 permits -1 for a timing that was not measured; the source capture uses it on
# every entry. Never substitute 0 — that asserts a measurement of zero.
UNKNOWN_TIMING = -1

_TIMING_KEYS = ("blocked", "dns", "connect", "send", "wait", "receive", "ssl")


def iso_millis(dt: datetime) -> str:
    """HAR ``startedDateTime``: ISO 8601 UTC with exactly 3 fractional digits."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def iso_micros(dt: datetime) -> str:
    """``events.jsonl`` ``ts_wall``: ISO 8601 UTC with 6 fractional digits."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def json_default(obj: Any) -> Any:
    """``json.dumps`` fallback: never raise while writing a log record."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return iso_micros(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", "replace")
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(str(item) for item in obj)
    if isinstance(obj, Enum):
        return obj.value
    return repr(obj)


def dumps(obj: Any) -> str:
    """One-line JSON for a log record."""
    return json.dumps(obj, ensure_ascii=False, default=json_default)


def dumps_pretty(obj: Any) -> str:
    """Indented JSON for whole-file sinks (``config.json``, ``summary.json``)."""
    return json.dumps(obj, ensure_ascii=False, default=json_default, indent=2, sort_keys=True)


def _get_ci(headers: Mapping[str, str], name: str) -> Optional[str]:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return None


def _name_value(headers: Mapping[str, str]) -> List[Dict[str, str]]:
    return [{"name": str(k), "value": "" if v is None else str(v)} for k, v in headers.items()]


def _query_string(url: str) -> List[Dict[str, str]]:
    query = urlsplit(url).query
    if not query:
        return []
    return [{"name": k, "value": v} for k, v in parse_qsl(query, keep_blank_values=True)]


def body_text(body: Any) -> Optional[str]:
    """Coerce a request/response body to the HAR's ``text`` field."""
    if body is None:
        return None
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    return dumps(body)


def _byte_len(text: Optional[str]) -> int:
    return 0 if text is None else len(text.encode("utf-8"))


def _status_text(status: Optional[int]) -> str:
    if status is None:
        return ""
    return http.client.responses.get(int(status), "")


def _mime(headers: Mapping[str, str]) -> str:
    return _get_ci(headers, "Content-Type") or ""


def _timings(
    timings: Optional[Mapping[str, float]], time_ms: Optional[float]
) -> Dict[str, float]:
    out = dict((key, float(UNKNOWN_TIMING)) for key in _TIMING_KEYS)
    if timings:
        for key, value in timings.items():
            if key in out and value is not None:
                out[key] = float(value)
        return out
    if time_ms is not None:
        out["wait"] = float(time_ms)
    return out


def har_entry(
    *,
    started_wall: datetime,
    method: str,
    url: str,
    request_headers: Optional[Mapping[str, str]],
    request_body: Any,
    status: Optional[int],
    response_headers: Optional[Mapping[str, str]],
    response_body: Any,
    time_ms: Optional[float],
    timings: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Build one HAR 1.2 ``log.entries[]`` object from an already-redacted exchange.

    A request that never got a response is a legal entry: ``status=None`` becomes status
    ``0``, which is what a browser devtools capture writes for a failed exchange, and
    every unmeasured timing stays at ``-1``.
    """
    req_headers = dict(request_headers or {})
    resp_headers = dict(response_headers or {})
    req_text = body_text(request_body)
    resp_text = body_text(response_body)

    entry = {
        "startedDateTime": iso_millis(started_wall),
        "time": float(UNKNOWN_TIMING) if time_ms is None else float(time_ms),
        "request": {
            "method": method,
            "url": url,
            "httpVersion": "",
            "cookies": [],
            "headers": _name_value(req_headers),
            "queryString": _query_string(url),
            "headersSize": UNKNOWN_TIMING,
            "bodySize": _byte_len(req_text),
        },
        "response": {
            "status": 0 if status is None else int(status),
            "statusText": _status_text(status),
            "httpVersion": "",
            "cookies": [],
            "headers": _name_value(resp_headers),
            "content": {
                "size": _byte_len(resp_text),
                "mimeType": _mime(resp_headers),
                "text": "" if resp_text is None else resp_text,
            },
            "redirectURL": _get_ci(resp_headers, "Location") or "",
            "headersSize": UNKNOWN_TIMING,
            "bodySize": _byte_len(resp_text),
        },
        "cache": {},
        "timings": _timings(timings, time_ms),
    }
    if req_text is not None:
        entry["request"]["postData"] = {"mimeType": _mime(req_headers), "text": req_text}
    return entry


class HarWriter:
    """Accumulates HAR entries and rewrites ``capture.har`` whole on :meth:`flush`.

    The rewrite goes through a temp file plus ``os.replace`` so an interrupted flush
    leaves the previous complete document in place rather than a truncated one.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: List[Dict[str, Any]] = []
        self._dirty = True

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return self._entries

    def add(self, entry: Dict[str, Any]) -> None:
        self._entries.append(entry)
        self._dirty = True

    def document(self) -> Dict[str, Any]:
        return {
            "log": {
                "version": HAR_VERSION,
                "creator": {"name": CREATOR_NAME, "version": __version__},
                "pages": [],
                "entries": self._entries,
            }
        }

    def flush(self) -> None:
        if not self._dirty:
            return
        tmp = self.path.parent / (self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(
                self.document(), handle, ensure_ascii=False, default=json_default, indent=1
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(self.path))
        self._dirty = False

    def close(self) -> None:
        self.flush()


class TimelineWriter:
    """Line-delimited JSON of monotonic-clock events."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = self.path.open("a", encoding="utf-8")

    def add(self, rec: Dict[str, Any]) -> None:
        self._handle.write(dumps(rec) + "\n")

    def flush(self) -> None:
        if not self._handle.closed:
            self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()


__all__ = [
    "HAR_VERSION",
    "CREATOR_NAME",
    "UNKNOWN_TIMING",
    "HarWriter",
    "TimelineWriter",
    "har_entry",
    "body_text",
    "dumps",
    "dumps_pretty",
    "iso_millis",
    "iso_micros",
    "json_default",
]
