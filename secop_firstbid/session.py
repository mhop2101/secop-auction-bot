"""HTTP transport for the fire path: one Session per thread over one shared cookie jar
(plan §3 and §11.2).

Three properties of this module are load-bearing at the fire instant and are the reason it
exists instead of bare ``requests`` calls at the call sites:

* **One client per thread, one jar for all of them.** ``requests.Session`` is not
  thread-safe, and the Azure Application Gateway in front of the API pins a backend with
  its affinity cookies, so the burst has to be N private Sessions sharing a single jar
  object.
* **A timeout on every call and a retry on none.** ``requests`` blocks forever by default,
  and one hung socket in a barrier-synchronised burst holds the whole run past the auction
  window. A retry, meanwhile, lands at an unknown time and writes another buyer-visible
  audit row.
* **Every exchange is logged in full**, including the ones that raise. Nothing is sampled,
  nothing is dropped, and the failure path carries ``status=None`` plus the exception.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar

from . import endpoints
from .auth import auth_header
from .config import Config
from .logging_setup import RunLogger
from .models import AuctionRef
from .schedule import sleep_until_mono

PREWARM_STAGE = "prewarm"
KEEPALIVE_STAGE = "keepalive"

# How far past its instant a keep-alive may still be sent. A run armed after the instant
# skips it: sent late it would sit closer to the release than the config was checked for.
_KEEPALIVE_LATE_TOLERANCE_NS = 250 * 1000000

# A prewarm thread cannot outlive its own request by much; this is only the margin for
# thread start-up and the logging queue, on top of the per-call timeout.
_PREWARM_JOIN_MARGIN_S = 2.0


@dataclass
class HttpOutcome:
    """One exchange. ``exception`` is set exactly when ``status`` is ``None``.

    ``t_recv_mono_ns`` on the failure path is the instant the exception surfaced, so
    ``rtt_ms`` there measures how long the thread waited before giving up.

    ``date_header`` is the server ``Date`` verbatim; :mod:`secop_firstbid.clock` consumes it
    and needs the unparsed string, whole-second resolution included.
    """

    status: Optional[int]
    body: Optional[Any]
    text: Optional[str]
    headers: Dict[str, str]
    t_send_mono_ns: int
    t_recv_mono_ns: Optional[int] = None
    rtt_ms: Optional[float] = None
    corr_id: str = ""
    exception: Optional[BaseException] = None
    date_header: Optional[str] = None


def load_json(text: Optional[str]) -> Optional[Any]:
    """Parse one response body, turning every fractional JSON number into a ``Decimal``.

    ``parse_float=Decimal`` keeps server money exact: ``lastBid`` and ``score`` arrive as
    bare JSON numbers (``0.00``), and binary floats cannot round-trip them.

    Returns ``None`` when the body is empty or is not JSON at all — the 200 with an empty
    body that ``SetStartBiddingAudit`` answers with, and a WAF block page, both take that
    path. The caller keeps the raw ``text`` either way.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped, parse_float=Decimal)
    except ValueError:
        return None


def _new_session(pool_maxsize: int) -> requests.Session:
    session = requests.Session()
    # max_retries=0: a retried request lands at an unknown time and writes a second
    # buyer-visible row into the auction's event log.
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=pool_maxsize, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _planned_threads(cfg: Config) -> int:
    if cfg.mode == "single":
        planned = cfg.n_direct + cfg.n_validate_submit + cfg.n_validate_only
    else:
        planned = cfg.wave_direct + cfg.wave_validate + cfg.wave_validate_only
    return max(1, planned)


def make_pool(cfg: Config, pool_size: Optional[int] = None) -> requests.Session:
    """A retry-free Session whose pool is sized for the threads that will share it.

    Defaults to one wave's composition, which is the peak concurrency of every mode: in
    ``burst``/``custom`` the waves are separated in time and each carries the same
    ``wave_*`` counts. Pass ``pool_size`` when the caller knows better.

    The fire path does not use this — each :class:`HttpClient` owns a single-connection
    Session of its own. This is for the single-threaded stages (preflight, gate, report)
    and for any caller that wants one pool with the same no-retry policy.
    """
    size = _planned_threads(cfg) if pool_size is None else int(pool_size)
    return _new_session(max(1, size))


class HttpClient:
    """One HTTP client, to be used by exactly one thread.

    ``requests.Session`` is **not thread-safe** — its cookie jar, its adapter map and its
    connection pool are all mutated per request — so the run builds one ``HttpClient``, and
    therefore one Session and one connection pool, per thread.

    All clients nonetheless share **one** ``RequestsCookieJar`` *object*, stored by
    reference and never copied. The API sits behind an Azure Application Gateway that pins
    a backend with ``ApplicationGatewayAffinity``/``ApplicationGatewayAffinityCORS``; a
    per-client jar would scatter the burst across backends and let the threads read
    inconsistent auction state. ``http.cookiejar`` locks its own mutations, so the sharing
    is safe in the direction that matters.

    Every call carries the configured ``(connect, read)`` timeout and no request is ever
    retried; both properties are set up in :func:`_new_session` and :meth:`_send` and must
    not be relaxed.
    """

    def __init__(
        self,
        cfg: Config,
        ref: AuctionRef,
        jar: RequestsCookieJar,
        logger: RunLogger,
        pool_maxsize: int = 1,
    ) -> None:
        self.cfg = cfg
        self.ref = ref
        self.jar = jar
        self.logger = logger
        self.timeout = (
            cfg.connect_timeout_ms / 1000.0,
            cfg.read_timeout_ms / 1000.0,
        )  # type: Tuple[float, float]
        self.session = _new_session(pool_maxsize)
        self.session.cookies = jar
        self._auth = auth_header(cfg.jwt)

    def get(
        self,
        path: str,
        *,
        stage: str,
        thread_id: str = "main",
        role: Optional[str] = None,
        conditional: bool = True,
        timeout: Optional[Tuple[float, float]] = None
    ) -> HttpOutcome:
        """``timeout`` overrides the client's ``(connect, read)`` pair for this one call.

        It exists for the keep-alive, which must be over well before the bid needs the
        connection and so cannot be allowed the bid's own, longer, budget.
        """
        return self._send(
            "GET",
            path,
            endpoints.get_headers(self.ref, self.cfg.user_agent, conditional=conditional),
            None,
            stage=stage,
            thread_id=thread_id,
            role=role,
            timeout=timeout,
        )

    def post_json(
        self,
        path: str,
        body_bytes: bytes,
        *,
        stage: str,
        thread_id: str = "main",
        role: Optional[str] = None
    ) -> HttpOutcome:
        return self._send(
            "POST",
            path,
            endpoints.post_headers(self.ref, self.cfg.user_agent),
            body_bytes,
            stage=stage,
            thread_id=thread_id,
            role=role,
        )

    def post_empty(
        self,
        path: str,
        *,
        stage: str,
        thread_id: str = "main",
        role: Optional[str] = None
    ) -> HttpOutcome:
        """The audit POST: no body bytes, ``Content-Length: 0``, lowercase ``utf-8``.

        That is the observed shape of ``SetStartBiddingAudit`` (8/8 in the capture), and its
        response is a 200 with an empty body.

        ``requests`` normalizes an empty ``data`` to ``body=None`` and then emits
        ``Content-Length: 0`` itself for a bodyless POST, so neither ``data=b""`` nor this
        explicit header is what makes the request correct today. Both are pinned anyway:
        the header is the part the server actually sees, and stating it here keeps the
        request right regardless of how a future ``requests`` prepares an empty body.
        """
        headers = endpoints.audit_post_headers(self.ref, self.cfg.user_agent)
        headers["Content-Length"] = "0"
        return self._send(
            "POST",
            path,
            headers,
            b"",
            stage=stage,
            thread_id=thread_id,
            role=role,
        )

    def close(self) -> None:
        self.session.close()

    def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        data: Optional[bytes],
        *,
        stage: str,
        thread_id: str,
        role: Optional[str],
        timeout: Optional[Tuple[float, float]] = None
    ) -> HttpOutcome:
        target = endpoints.url(path)
        request_headers = OrderedDict(headers)  # type: OrderedDict[str, str]
        request_headers.update(self._auth)
        corr_id = self.logger.next_corr_id()

        status = None  # type: Optional[int]
        body = None  # type: Optional[Any]
        text = None  # type: Optional[str]
        response_headers = {}  # type: Dict[str, str]
        date_header = None  # type: Optional[str]
        exception = None  # type: Optional[BaseException]
        response = None  # type: Optional[requests.Response]

        t_send = time.monotonic_ns()
        try:
            response = self.session.request(
                method,
                target,
                headers=request_headers,
                data=data,
                timeout=self.timeout if timeout is None else timeout,
                # A redirect is a second request at an unknown time carrying the bearer
                # token to whatever host the Location names; record it, never follow it.
                allow_redirects=False,
            )
        except Exception as caught:
            exception = caught
        t_recv = time.monotonic_ns()

        if response is not None:
            status = response.status_code
            response_headers = dict(response.headers)
            date_header = response.headers.get("Date")
            text = response.text
            body = load_json(text)

        self.logger.http(
            stage=stage,
            corr_id=corr_id,
            thread_id=thread_id,
            role=role,
            method=method,
            url=target,
            request_headers=request_headers,
            request_body=data,
            status=status,
            response_headers=response_headers,
            response_body=body if body is not None else text,
            t_send_mono_ns=t_send,
            t_recv_mono_ns=t_recv,
            exception=exception,
        )

        return HttpOutcome(
            status=status,
            body=body,
            text=text,
            headers=response_headers,
            t_send_mono_ns=t_send,
            t_recv_mono_ns=t_recv,
            rtt_ms=(t_recv - t_send) / 1e6,
            corr_id=corr_id,
            exception=exception,
            date_header=date_header,
        )


def _warm_round(
    clients: List[HttpClient],
    ref: AuctionRef,
    logger: RunLogger,
    *,
    stage: str,
    label: str,
    timeout: Optional[Tuple[float, float]] = None,
    detail: Optional[Dict[str, Any]] = None
) -> List[Optional[int]]:
    """One ``GetAuctionSimple`` per client, all at once, each on its own thread.

    Returns the status each client got, ``None`` where the request failed or is still out.
    """
    path = endpoints.get_auction_simple(ref.auction_id)
    outcomes = [None] * len(clients)  # type: List[Optional[HttpOutcome]]

    def warm(index: int) -> None:
        try:
            outcomes[index] = clients[index].get(
                path,
                stage=stage,
                thread_id="%s-%02d" % (label, index),
                role=label,
                timeout=timeout,
            )
        except Exception as exc:  # a client that cannot even log must not kill the join
            logger.event(
                stage=stage,
                event="%s_error" % label,
                level="ERROR",
                thread_id="%s-%02d" % (label, index),
                detail={"exception": "%s: %s" % (type(exc).__name__, exc)},
            )

    threads = [
        threading.Thread(
            target=warm, args=(index,), name="%s-%02d" % (label, index), daemon=True
        )
        for index in range(len(clients))
    ]
    for thread in threads:
        thread.start()

    budgets = [client.timeout if timeout is None else timeout for client in clients]
    join_timeout = max(budget[0] + budget[1] for budget in budgets) + _PREWARM_JOIN_MARGIN_S
    for thread in threads:
        thread.join(timeout=join_timeout)

    statuses = [None if outcome is None else outcome.status for outcome in outcomes]
    failed = [index for index, status in enumerate(statuses) if status is None]
    still_running = [thread.name for thread in threads if thread.is_alive()]
    record = {
        "clients": len(clients),
        "statuses": statuses,
        "failed_indices": failed,
        "still_running": still_running,
    }  # type: Dict[str, Any]
    if detail:
        record.update(detail)
    logger.event(
        stage=stage,
        event="%s_complete" % label,
        level="WARNING" if failed or still_running else "INFO",
        detail=record,
    )
    return statuses


def prewarm(clients: List[HttpClient], ref: AuctionRef, logger: RunLogger) -> None:
    """Open every client's TCP+TLS connection, N requests **at once**, one per client.

    Deliberately not a loop. urllib3 has no thread-affine connection — a pool hands out
    whichever connection is free — so a sequential warm-up leaves exactly one pooled
    connection and the other N−1 threads pay a fresh handshake inline at T. The capture
    prices that at 167–184 ms of connect+TLS, roughly twice the recommended schedule span,
    and it is paid by exactly the threads meant to win a timestamp tiebreak.

    A warm-up failure is logged and never raised: a cold connection is a slower bid, not a
    reason to abandon an armed run.

    This runs at arming, which can be many minutes before ``T``, and an idle connection
    does not last that long: on 2026-11-20 the connections warmed here were gone by the
    fire instant. :func:`keep_warm` is what holds them open through the countdown.
    """
    if not clients:
        return
    _warm_round(clients, ref, logger, stage=PREWARM_STAGE, label="prewarm")


def keep_warm(
    cfg: Config,
    clients: List[HttpClient],
    ref: AuctionRef,
    logger: RunLogger,
    first_release_mono_ns: int,
    cancel: Optional[threading.Event] = None,
) -> int:
    """Re-open and hold every fire client's connection through the last of the countdown.

    At each of ``keepalive_offsets_ms`` — instants before the first release — every client
    sends one cheap read, all at once, exactly as :func:`prewarm` does. A connection that
    idled out since arming is re-established here, *before* the seconds in which the
    server starts refusing connections, instead of by the bid itself at ``T``; one that is
    still open is kept from idling out.

    **A keep-alive is a request on a bid's own connection**, so it must be over before the
    bid needs it. Two things guarantee that: each call carries ``keepalive_timeout_ms`` as
    both its connect and its read timeout, far below the bid's, and
    :func:`~secop_firstbid.config.validate` refuses a config whose last keep-alive could
    still be in flight at the first release. A call that fails closes its connection;
    the next round, or the bid, opens a new one.

    A run armed after an instant skips that round rather than sending it late. Returns how
    many rounds were sent. Blocks until the last one is over.

    How long this gateway keeps an idle connection has not been measured, so the default
    spacing is a guess (``docs/open-questions.md`` Q17).
    """
    if not clients or not cfg.keepalive_offsets_ms:
        return 0

    timeout = (cfg.keepalive_timeout_ms / 1000.0, cfg.keepalive_timeout_ms / 1000.0)
    rounds = 0
    for offset_ms in sorted(cfg.keepalive_offsets_ms):
        instant = first_release_mono_ns + int(offset_ms) * 1000000
        if time.monotonic_ns() > instant + _KEEPALIVE_LATE_TOLERANCE_NS:
            logger.event(
                stage=KEEPALIVE_STAGE,
                event="keepalive_skipped",
                detail={
                    "offset_from_first_release_ms": offset_ms,
                    "reason": "the run reached this point after the instant had passed",
                },
            )
            continue
        sleep_until_mono(instant, cancel=cancel)
        if cancel is not None and cancel.is_set():
            break
        _warm_round(
            clients,
            ref,
            logger,
            stage=KEEPALIVE_STAGE,
            label="keepalive",
            timeout=timeout,
            detail={"offset_from_first_release_ms": offset_ms},
        )
        rounds += 1
    return rounds


__all__ = [
    "HttpClient",
    "HttpOutcome",
    "KEEPALIVE_STAGE",
    "PREWARM_STAGE",
    "keep_warm",
    "load_json",
    "make_pool",
    "prewarm",
]
