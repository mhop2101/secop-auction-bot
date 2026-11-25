"""Failure taxonomy, the app's exception type, and the single response classifier (plan §12.6).

Every failure anywhere in the app is tagged with exactly one :class:`Failure`, produced by
:func:`classify` or carried on a :class:`SecopError`. Raw evidence (status, body, exception
text, unknown ``errorType`` values) is preserved by the *caller* in the logs; ``classify``
only maps.

:func:`is_success` lives here rather than beside its callers because it is both halves of
one rule: it decides whether a ``CreateBid`` won *and* whether ``classify`` may answer
``OK``. Stated in two places the two readings drift, and a run whose attempt is tagged
``OK`` while its outcome is ``ALL_REJECTED`` cannot say why the bid was not counted.
``bidder`` re-exports it, so a caller may import it from either module.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping as _AbcMapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class Failure(str, Enum):
    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_EXPIRING = "AUTH_EXPIRING"
    STATE_NOT_STARTED = "STATE_NOT_STARTED"
    STATE_PAUSED = "STATE_PAUSED"
    STATE_FINISHED = "STATE_FINISHED"
    STATE_UNKNOWN = "STATE_UNKNOWN"
    BID_ERR_MIN = "BID_ERR_MIN"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    WAF_BLOCK = "WAF_BLOCK"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK = "NETWORK"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    DUPLICATE = "DUPLICATE"
    CLOCK_SYNC_FAILED = "CLOCK_SYNC_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    PARSE_AMBIGUOUS = "PARSE_AMBIGUOUS"
    CONFIG_INVALID = "CONFIG_INVALID"
    FIRE_TIME_PAST = "FIRE_TIME_PAST"
    FIRE_TIME_TOO_FAR = "FIRE_TIME_TOO_FAR"
    UNSUPPORTED_AUCTION_VERSION = "UNSUPPORTED_AUCTION_VERSION"
    OK = "OK"
    UNKNOWN = "UNKNOWN"


class SecopError(Exception):
    """Application error carrying a :class:`Failure` code and structured detail.

    ``detail`` is copied and must already be redacted by the raiser: it is written to
    ``events.jsonl`` verbatim.
    """

    def __init__(
        self,
        code: Failure,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail) if detail else {}

    def __repr__(self) -> str:
        return "SecopError(code=%s, message=%r, detail=%r)" % (
            self.code.value,
            self.message,
            self.detail,
        )


@dataclass(frozen=True)
class RunState:
    """Local run facts the classifier needs.

    ``DUPLICATE`` is not distinguishable from ``BID_ERR_MIN`` by ``(status, body)`` alone:
    the server returns a byte-identical response for a post-win bounce and a genuine
    minimum-bid rejection. Local send ordering is the only thing that separates them.
    """

    first_success_seen: bool = False
    sent_after_first_success: bool = False


_NETWORK_EXC_NAMES = frozenset(
    (
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionError",
        "ConnectTimeoutError",
        "NewConnectionError",
        "MaxRetryError",
        "ProtocolError",
        "ProxyError",
        "SSLError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "RetryError",
    )
)

_STDLIB_NETWORK_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    ssl.SSLError,
    socket.timeout,
    socket.gaierror,
    socket.herror,
)

_SCHEMA_MARKERS = (
    "modelstate",
    "validationerrors",
    "the json value could not be converted",
    "could not be converted",
    "is not valid for",
    "invalid json",
    "unexpected character",
    "required property",
    "one or more validation errors",
)

_STATE_TOKENS = (
    (
        Failure.STATE_NOT_STARTED,
        (
            "notstarted",
            "notyetstarted",
            "not started",
            "hasnotstarted",
            "readytostart",
            "notopen",
            "notbegun",
            "noiniciad",
            "sininiciar",
        ),
    ),
    (
        Failure.STATE_PAUSED,
        ("paused", "pause", "suspended", "suspend", "onhold", "pausad", "suspendid"),
    ),
    (
        Failure.STATE_FINISHED,
        (
            "finished",
            "finalized",
            "closed",
            "ended",
            "expired",
            "terminad",
            "finalizad",
            "cerrad",
        ),
    ),
)  # NOT_STARTED is probed first: "notstarted" also contains "started"


def _is_network_exception(exc: BaseException) -> bool:
    if isinstance(exc, _STDLIB_NETWORK_EXCEPTIONS):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _NETWORK_EXC_NAMES:
            return True
    return False


def _looks_schema_error(body: Optional[Any], raw_text: Optional[str]) -> bool:
    haystacks = []
    if isinstance(body, dict):
        for key in body:
            haystacks.append(str(key).lower())
        if "errors" in body or "ModelState" in body or "modelState" in body:
            return True
        for key in ("title", "detail", "message", "Message", "ExceptionMessage"):
            value = body.get(key)
            if isinstance(value, str):
                haystacks.append(value.lower())
    elif isinstance(body, str):
        haystacks.append(body.lower())
    if raw_text:
        haystacks.append(raw_text.lower())
    for hay in haystacks:
        for marker in _SCHEMA_MARKERS:
            if marker in hay:
                return True
    return False


def _is_bid_shaped(body: Any) -> bool:
    return isinstance(body, dict) and (
        "isValid" in body or "errorType" in body or "bidId" in body
    )


def _state_failure(body: Dict[str, Any]) -> Optional[Failure]:
    parts = []
    for key in ("errorType", "errorMessageLocale"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value.lower().replace("_", "").replace("-", ""))
    if not parts:
        return None
    hay = " ".join(parts)
    for failure, tokens in _STATE_TOKENS:
        for token in tokens:
            if token in hay:
                return failure
    return None


def is_success(body: Mapping[str, Any]) -> bool:
    """The run's only success predicate: ``isValid`` true **and** an empty ``errorMessage``.

    Nothing in the capture resolves what an ``isValid:true`` carrying a non-empty
    ``errorMessage`` means — all 8 observed responses are ``isValid:false`` — so it is read
    as *not a win*: treating it as one cancels the remaining waves on a message nobody has
    ever seen the text of. See ``docs/open-questions.md`` Q5 — what a successful
    ``CreateBid`` returns has never been observed.

    A body that is not a mapping (an empty 200, a WAF interstitial, a connection failure)
    is not a success.
    """
    if not isinstance(body, _AbcMapping):
        return False
    return body.get("isValid") is True and not str(body.get("errorMessage") or "").strip()


def classify(
    status: Optional[int],
    body: Optional[Any],
    exc: Optional[BaseException] = None,
    run_state: Optional[RunState] = None,
    raw_text: Optional[str] = None,
) -> Failure:
    """Map one HTTP outcome to one :class:`Failure`.

    ``run_state.sent_after_first_success`` is checked **before** ``errorType`` because a
    post-win duplicate and a genuine ``BidErrMin`` rejection are byte-identical on the wire
    — same status, same ``errorMessage``, same ``minBid``. Local send ordering is the only
    thing that separates them, so the caller must pass a
    ``RunState(sent_after_first_success=True)`` for every attempt released after the first
    success was marked; otherwise every post-win wave is mis-tagged ``BID_ERR_MIN``.

    ``OK`` is exactly :func:`is_success`, the same predicate that decides a win, so the tag
    and the win condition cannot disagree about one response. An ``isValid:true`` carrying a
    non-empty ``errorMessage`` is therefore not ``OK``; it is ``VALIDATION_FAILED``, which
    is the tag that puts the reason the bid was not counted into the run's taxonomy. That
    tag covers both bodies the run refuses to count as a win — ``isValid:false`` with no
    recognised ``errorType``, and ``isValid:true`` with a warning attached.

    A 2xx whose body is empty or is not JSON is ``UNKNOWN``, not ``OK``: on the bid
    endpoints a response carrying no verdict is not evidence a bid was accepted. The empty
    200 that ``SetStartBiddingAudit`` answers with never reaches here — its caller discards
    the outcome — so no classified response depends on that shape.

    Unknown ``errorType`` values are not invented here — the caller logs them verbatim.
    """
    if exc is not None and _is_network_exception(exc):
        return Failure.NETWORK
    if status is None:
        return Failure.NETWORK

    if status == 401:
        return Failure.AUTH_EXPIRED
    if status == 429:
        return Failure.RATE_LIMIT
    if status == 403:
        return Failure.WAF_BLOCK

    bid_shaped_4xx = False
    if status in (400, 422):
        if _looks_schema_error(body, raw_text):
            return Failure.MALFORMED_PAYLOAD
        if not _is_bid_shaped(body):
            return Failure.MALFORMED_PAYLOAD
        bid_shaped_4xx = True

    if 200 <= status < 300 or bid_shaped_4xx:
        if isinstance(body, dict):
            if is_success(body):
                return Failure.OK
            if body.get("isValid") is True:
                return Failure.VALIDATION_FAILED
            if run_state is not None and run_state.sent_after_first_success:
                return Failure.DUPLICATE
            if body.get("errorType") == "BidErrMin":
                return Failure.BID_ERR_MIN
            state = _state_failure(body)
            if state is not None:
                return state
            if body.get("isValid") is False:
                return Failure.VALIDATION_FAILED

    return Failure.UNKNOWN


__all__ = ["Failure", "SecopError", "RunState", "classify", "is_success"]
