"""API surface: base URL, route builders and header sets (plan §1 and §10).

Every literal in this module was taken from ``www.secop.gov.co.har`` and must stay
byte-exact: the base path's double slash, the ``Bid`` vs ``bid`` casing, the two different
``charset`` spellings, and the client-hint / fetch-metadata headers a UA-sniffing WAF
cross-checks against the spoofed ``User-Agent``.

**One header does not match the capture, on purpose: ``Accept-Encoding``.** Chrome sent
``gzip, deflate, br, zstd``; this process advertises only the codings the installed
urllib3 can actually decode, which is usually ``gzip, deflate``. Advertising ``br`` or
``zstd`` without a decoder makes every response body unreadable if the server honours it —
a certain harm in exchange for a speculative one, since no 403 was ever observed and all
429 captured responses are 200. The divergence is recorded rather than hidden:
:data:`ACCEPT_ENCODING_CAPTURED` holds Chrome's value and :func:`header_divergence`
reports the difference so the CLI can print it at preflight. If a WAF ever does
cross-check this against the ``User-Agent``, install ``brotli`` and ``zstandard`` and the
advertised set widens to match on its own.

Route builders return **relative** paths; only :func:`url` produces an absolute URL.
:data:`OBSERVED_ON_WIRE` records which of them were actually seen in the capture.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Tuple
from urllib.parse import urlencode

from .models import AuctionRef

API_HOST = "https://subastas.secop.gov.co"
SITE_ORIGIN = "https://subastas.secop.gov.co"

# The SPA builds apiBaseUrl + apiVersion + '/', producing "…/v1//". All 146 observed API
# paths carry it; do not collapse it.
API_BASE = "https://subastas.secop.gov.co/eauctions.api/v1//"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

ACCEPT = "application/json, text/plain, */*"
ACCEPT_LANGUAGE = "es-US,es-419;q=0.9,es;q=0.8,en;q=0.7"

ACCEPT_ENCODING_CAPTURED = "gzip, deflate, br, zstd"


def _decodable_encodings() -> Tuple[str, ...]:
    """The content codings the installed stack can actually decode.

    urllib3 performs the decoding and publishes exactly that set as its own default
    ``Accept-Encoding``; ``br`` and ``zstd`` appear there only when their optional
    packages are installed and the urllib3 version supports them. Asking it rather than
    listing codings here keeps the advertisement and the decoder from drifting apart.
    """
    codings = ["gzip", "deflate"]
    try:
        from urllib3.util.request import ACCEPT_ENCODING as _urllib3_default
    except Exception:
        return tuple(codings)
    available = [part.strip() for part in _urllib3_default.split(",")]
    for coding in ("br", "zstd"):
        if coding in available:
            codings.append(coding)
    return tuple(codings)


ACCEPT_ENCODING = ", ".join(_decodable_encodings())

SEC_CH_UA ='"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_PLATFORM = '"macOS"'
SEC_FETCH_DEST = "empty"
SEC_FETCH_MODE = "cors"
SEC_FETCH_SITE = "same-origin"

CONTENT_TYPE_JSON = "application/json;charset=UTF-8"
CONTENT_TYPE_JSON_AUDIT = "application/json;charset=utf-8"

CACHE_CONTROL = "max-age=0"
IF_MODIFIED_SINCE = "Mon, 26 Jul 1997 05:00:00 GMT"

REFERER_TEMPLATE = (
    API_HOST + "/eauctions.site/?auctionId={auction_id}"
    "&languageCode={lang}&marketCode={market}"
)

_EVENT_LOG_COLUMNS = (
    "registerDate",
    "eventHistoryTypeCode",
    "eventHistorySubTypeCode",
    "description",
    "userCode",
    "ipAddress",
)


def referer(ref: AuctionRef) -> str:
    return REFERER_TEMPLATE.format(
        auction_id=ref.auction_id, lang=ref.lang, market=ref.market
    )


def url(path: str) -> str:
    """Join ``API_BASE`` and a relative path with plain concatenation.

    No slash normalisation: ``urljoin`` and most path helpers collapse ``v1//`` to ``v1/``,
    which is not the path the server was observed to answer.
    """
    return API_BASE + path


def get_auction_simple(auction_id: str) -> str:
    return "auction/GetAuctionSimple/{0}".format(auction_id)


def get_auction(auction_id: str, lang: str) -> str:
    """Item list. Never captured — the response shape behind it is client-code-only (§15 Q7)."""
    return "auction/GetAuction/{0}/{1}".format(auction_id, lang)


def get_bids_total_info(auction_id: str) -> str:
    """Capital ``Bid/`` — the only bid route observed with an upper-case segment."""
    return "Bid/GetBidsTotalInfo/{0}".format(auction_id)


def get_supplier_bids(auction_id: str, company_code: str, last_bid_id: int = 0) -> str:
    """The auction's bid list as one supplier sees it.

    Not in the original capture. The auction page requested it in the browser captures of
    2026-11-20, and a read with ``last_bid_id=0`` after an auction returned the accepted
    bid of another company — its company code, value and the server's own timestamp —
    which is what makes it the record of who won and when.
    """
    return "bid/GetSupplierBids/{0}/{1}/{2}".format(auction_id, company_code, last_bid_id)


def create_bid() -> str:
    """The real submission. The route resolves; its success response is still unobserved.

    No ``CreateBid`` request exists in the capture, but the path was confirmed live on
    2026-07-29 without submitting anything: a ``GET`` here answers **405** (resolved,
    wrong method) while a bogus sibling answers 404, and ``bid/createbid`` also answers 405,
    so routing is case-insensitive and a wrong-casing 404 at the fire instant is not a risk.

    On 2026-11-20 the server processed the submissions this app sent through it in two live
    auctions — each left a row in the event log — so the path, headers and payload are
    accepted as a bid. Not one was answered inside its timeout.

    What remains unknown is therefore the *response* to a POST — see
    ``docs/open-questions.md`` Q5. ``OBSERVED_ON_WIRE`` stays ``False`` for this route: no
    response from it has ever been seen.
    """
    return "bid/CreateBid"


def validate_bid() -> str:
    return "bid/ValidateBid"


def get_min_bid() -> str:
    """Never captured; path from ``apiService.js``."""
    return "bid/GetMinBid"


def set_start_bidding_audit(auction_id: str) -> str:
    """Observed once per bid attempt in the browser, paired with ValidateBid at Δ ≤ 1 ms (8/8).

    This app sends it once per run; see :class:`secop_firstbid.bidder.StartAudit`.
    """
    return "audit/SetStartBiddingAudit?auctionId={0}".format(auction_id)


def set_submit_bidding_audit(auction_id: str) -> str:
    """Never observed in the 429 captured entries; its trigger is unknown.

    The SPA's function names are inverted relative to the endpoints they post to, so this is
    more likely the session-open audit than the post-submit one. ``do_submit_audit`` therefore
    defaults to false.
    """
    return "audit/SetSubmitBiddingAudit?auctionId={0}".format(auction_id)


def get_event_logs(
    auction_id: str,
    lang: str = "es-CO",
    display_start: int = 0,
    display_length: int = 10,
    is_owner: bool = False,
) -> str:
    """Buyer-visible audit log (carries the bidder's public IP and company code).

    The route is a DataTables server-side endpoint: the whole ``sEcho``/``iColumns``/
    ``mDataProp_*`` block is required, in the observed order.
    """
    params = OrderedDict()  # type: OrderedDict[str, str]
    params["sEcho"] = "1"
    params["iColumns"] = str(len(_EVENT_LOG_COLUMNS))
    params["sColumns"] = "," * (len(_EVENT_LOG_COLUMNS) - 1)
    params["iDisplayStart"] = str(display_start)
    params["iDisplayLength"] = str(display_length)
    for index, column in enumerate(_EVENT_LOG_COLUMNS):
        params["mDataProp_{0}".format(index)] = column
        params["sSearch_{0}".format(index)] = ""
        params["bRegex_{0}".format(index)] = "false"
        params["bSearchable_{0}".format(index)] = "true"
        params["bSortable_{0}".format(index)] = "true"
    params["sSearch"] = ""
    params["bRegex"] = "false"
    params["iSortCol_0"] = "0"
    params["sSortDir_0"] = "desc"
    params["iSortingCols"] = "1"
    params["auctionId"] = auction_id
    params["languageCode"] = lang
    params["isOwner"] = "true" if is_owner else "false"
    params["advancedFilter"] = "{}"
    return "audit/GetEventLogs?" + urlencode(params)


def get_system_health() -> str:
    """Liveness. Never captured; path from ``apiService.js``, trailing slash included."""
    return "common/GetSystemHealth/"


OBSERVED_ON_WIRE = OrderedDict(
    (
        ("get_auction_simple", True),
        ("get_auction", False),
        ("get_bids_total_info", True),
        ("get_supplier_bids", True),
        ("create_bid", False),
        ("validate_bid", True),
        ("get_min_bid", False),
        ("set_start_bidding_audit", True),
        ("set_submit_bidding_audit", False),
        ("get_event_logs", True),
        ("get_system_health", False),
    )
)  # type: OrderedDict[str, bool]

# Response latency of the bid endpoints over the 429 captured entries. The minimum is the
# floor on how quickly any verdict can come back; the median is what a request on this API
# typically costs end to end.
#
# The audit routes were never timed separately, so a caller asking "how long might one
# request still be holding this client's connection" uses the median as its conservative
# proxy rather than a number measured on the route it is actually about.
OBSERVED_MIN_BID_RTT_MS = 168.5
OBSERVED_P50_BID_RTT_MS = 614.4

# connect+TLS for a fresh connection to this host, from the capture. It is what a request
# pays when it cannot get a pooled connection and has to open one inline.
OBSERVED_CONNECT_TLS_MS = (167.0, 184.0)


def header_divergence() -> "OrderedDict[str, Tuple[str, str]]":
    """Headers this process sends that differ from the captured request: ``(sent, captured)``.

    Empty when every header matches. Anything listed here is a place a WAF comparing the
    request against a real Chrome fingerprint has something to notice.
    """
    out = OrderedDict()  # type: OrderedDict[str, Tuple[str, str]]
    if ACCEPT_ENCODING != ACCEPT_ENCODING_CAPTURED:
        out["Accept-Encoding"] = (ACCEPT_ENCODING, ACCEPT_ENCODING_CAPTURED)
    return out


def base_headers(ref: AuctionRef, user_agent: str = DEFAULT_USER_AGENT) -> Dict[str, str]:
    """The always-on set.

    ``Authorization`` is added by ``auth.auth_header``; ``Host``, ``Connection`` and
    ``Content-Length`` are emitted by the HTTP library and must not be set here.
    ``X-Requested-With`` is deliberately absent: it appears only on SignalR and
    ``GetEventLogs``, never on a bid call.

    ``Accept-Encoding`` *is* set, and is the one header that does not reproduce the
    capture. The library would send the same value silently; stating it puts it in the
    logged request headers, where :func:`header_divergence` can be checked against it.
    """
    return OrderedDict(
        (
            ("Accept", ACCEPT),
            ("Accept-Encoding", ACCEPT_ENCODING),
            ("Accept-Language", ACCEPT_LANGUAGE),
            ("Referer", referer(ref)),
            ("Sec-Fetch-Dest", SEC_FETCH_DEST),
            ("Sec-Fetch-Mode", SEC_FETCH_MODE),
            ("Sec-Fetch-Site", SEC_FETCH_SITE),
            ("User-Agent", user_agent),
            ("sec-ch-ua", SEC_CH_UA),
            ("sec-ch-ua-mobile", SEC_CH_UA_MOBILE),
            ("sec-ch-ua-platform", SEC_CH_UA_PLATFORM),
        )
    )


def get_headers(
    ref: AuctionRef,
    user_agent: str = DEFAULT_USER_AGENT,
    *,
    conditional: bool = True
) -> Dict[str, str]:
    """GET set: no ``Origin`` (absent from all 142 captured GETs), plus the conditional pair."""
    headers = base_headers(ref, user_agent)
    if conditional:
        headers["Cache-Control"] = CACHE_CONTROL
        headers["If-Modified-Since"] = IF_MODIFIED_SINCE
    return headers


def post_headers(ref: AuctionRef, user_agent: str = DEFAULT_USER_AGENT) -> Dict[str, str]:
    headers = base_headers(ref, user_agent)
    headers["Content-Type"] = CONTENT_TYPE_JSON
    headers["Origin"] = SITE_ORIGIN
    return headers


def audit_post_headers(
    ref: AuctionRef, user_agent: str = DEFAULT_USER_AGENT
) -> Dict[str, str]:
    """Audit POST set: ``charset=utf-8`` lower-case, as the SPA sends it.

    Paired with an empty body (``data=b""``), which is what produces the observed
    ``Content-Length: 0``.
    """
    headers = base_headers(ref, user_agent)
    headers["Content-Type"] = CONTENT_TYPE_JSON_AUDIT
    headers["Origin"] = SITE_ORIGIN
    return headers


__all__ = [
    "API_HOST",
    "SITE_ORIGIN",
    "API_BASE",
    "DEFAULT_USER_AGENT",
    "ACCEPT_ENCODING",
    "ACCEPT_ENCODING_CAPTURED",
    "REFERER_TEMPLATE",
    "OBSERVED_ON_WIRE",
    "header_divergence",
    "referer",
    "url",
    "get_auction_simple",
    "get_auction",
    "get_bids_total_info",
    "create_bid",
    "validate_bid",
    "get_min_bid",
    "set_start_bidding_audit",
    "set_submit_bidding_audit",
    "get_event_logs",
    "get_system_health",
    "base_headers",
    "get_headers",
    "post_headers",
    "audit_post_headers",
]
