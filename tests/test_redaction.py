"""Redaction tests (plan §12.4).

The leak these tests exist to catch: on the SignalR path the bearer token is a *query
parameter* — ``token=<2370 chars>`` on 281 poll/send/ping URLs — while §12.2 mandates
logging ``url`` on every exchange and §12.1 mandates a ``capture.har`` of every request.
A redactor built as a header-dict transform passes those URLs straight through into
``events.jsonl``, ``http/*.json`` and ``capture.har``. So the poll URL below is shaped
like the real thing and is checked at the top level, nested, and after a second pass.

The second shape of the same leak: a redactor that only recognises a token when it *is*
the whole value, or a URL only when it carries a query string, passes through every token
that arrives embedded — in a body, in free text, in a URL path, in an exception message.
The last test here is the end-to-end version of that: a real ``RunLogger`` and a real
``HttpClient`` over a stub adapter, with every file in the run directory searched.

The token is synthesised here, not lifted from the HAR: the HAR is 22 MB and holds a
live credential, and nothing about the leak depends on the token's actual bytes.
"""

from __future__ import annotations

import string
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from secop_firstbid import endpoints
from secop_firstbid.capture import dumps
from secop_firstbid.config import Config
from secop_firstbid.logging_setup import (
    JWT_QUERY_KEYS,
    REDACTED,
    RunLogger,
    redact,
    redact_headers,
    redact_jwts,
    redact_token,
    redact_url,
)
from secop_firstbid.models import AuctionRef
from secop_firstbid.session import HttpClient

_B64URL = string.ascii_letters + string.digits + "-_"

TOKEN_LEN = 2370


def _fake_jwt(total_len: int = TOKEN_LEN) -> str:
    header = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9"
    signature = "".join(_B64URL[(i * 7 + 3) % len(_B64URL)] for i in range(342))
    fill = total_len - len(header) - len(signature) - 2
    payload = "".join(_B64URL[(i * 13 + 5) % len(_B64URL)] for i in range(fill))
    return header + "." + payload + "." + signature


TOKEN = _fake_jwt()

CONNECTION_TOKEN = "".join(_B64URL[(i * 5 + 11) % len(_B64URL)] for i in range(182))

POLL_URL = (
    "https://subastas.secop.gov.co/eauctions.api/signalr/signalr/poll"
    "?transport=longPolling"
    "&clientProtocol=1.5"
    "&connectionToken=" + quote(CONNECTION_TOKEN, safe="") + "&connectionData="
    + quote('[{"name":"auction"}]', safe="")
    + "&token=" + TOKEN
    + "&messageId=d-C00F3698-Ch%2C0%7CBHP%2C27"
    + "&tid=6"
)

NEEDLE = TOKEN[40:400]

SHORT_TOKEN = TOKEN[:6] + "…" + TOKEN[-6:]

# No query string, token in the path: nothing for a query-parameter rewrite to bite on.
PATH_URL = "https://subastas.secop.gov.co/eauctions.api/signalr/hub/" + TOKEN + "/poll"


def test_synthetic_token_matches_the_observed_shape() -> None:
    assert len(TOKEN) == TOKEN_LEN
    assert TOKEN.count(".") == 2
    assert TOKEN.startswith("eyJ")


def test_signalr_poll_url_loses_the_token() -> None:
    out = redact_url(POLL_URL)

    assert TOKEN not in out
    assert NEEDLE not in out
    assert len(out) < len(POLL_URL) - 2000

    params = parse_qs(urlsplit(out).query, keep_blank_values=True)
    assert params["token"] == [TOKEN[:6] + "…" + TOKEN[-6:]]


def test_signalr_poll_url_keeps_every_diagnostic_parameter() -> None:
    out = redact_url(POLL_URL)
    params = parse_qs(urlsplit(out).query, keep_blank_values=True)

    assert params["transport"] == ["longPolling"]
    assert params["clientProtocol"] == ["1.5"]
    assert params["connectionToken"] == [CONNECTION_TOKEN]
    assert params["connectionData"] == ['[{"name":"auction"}]']
    assert params["messageId"] == ["d-C00F3698-Ch,0|BHP,27"]
    assert params["tid"] == ["6"]
    assert urlsplit(out).path == "/eauctions.api/signalr/signalr/poll"


@pytest.mark.parametrize("key", JWT_QUERY_KEYS)
def test_every_jwt_query_key_is_rewritten(key: str) -> None:
    url = "https://subastas.secop.gov.co/x?a=1&" + key + "=" + TOKEN + "&b=2"
    out = redact_url(url)

    assert NEEDLE not in out
    assert "a=1" in out and "b=2" in out


def test_url_encoded_token_value_is_rewritten() -> None:
    url = "https://subastas.secop.gov.co/x?token=" + quote(TOKEN, safe="")
    out = redact_url(url)

    assert NEEDLE not in out


def test_unrelated_urls_are_returned_untouched() -> None:
    plain = "https://subastas.secop.gov.co/eauctions.api/v1//bid/ValidateBid"
    query = "https://subastas.secop.gov.co/eauctions.site/?auctionId=abc&languageCode=es-CO"

    assert redact_url(plain) == plain
    assert redact_url(query) == query
    assert redact_url("audit/SetStartBiddingAudit?auctionId=abc") == (
        "audit/SetStartBiddingAudit?auctionId=abc"
    )


def test_nested_structure_carrying_the_poll_url_is_scrubbed() -> None:
    record = {
        "stage": "clock",
        "detail": {
            "url": POLL_URL,
            "history": [{"href": POLL_URL}, {"note": "retried " + POLL_URL}],
            "request_headers": {"Referer": POLL_URL},
        },
    }

    clean = redact(record)

    assert NEEDLE not in dumps(clean)
    assert clean["detail"]["url"].startswith(
        "https://subastas.secop.gov.co/eauctions.api/signalr/signalr/poll?"
    )
    assert "connectionToken=" + CONNECTION_TOKEN in clean["detail"]["url"]


def test_url_in_a_plain_string_value_is_scrubbed_too() -> None:
    # The key is not named "url" — a redactor keyed on field names alone leaks here.
    assert NEEDLE not in dumps(redact({"message": "poll failed for " + POLL_URL}))
    assert NEEDLE not in dumps(redact([POLL_URL]))
    assert NEEDLE not in redact(POLL_URL)


def test_token_embedded_in_free_text_is_scrubbed() -> None:
    text = "retry after reauth: bearer " + TOKEN + " expired at T-60ms"

    out = redact({"detail": {"message": text}})["detail"]["message"]

    assert NEEDLE not in out
    assert out == "retry after reauth: bearer " + SHORT_TOKEN + " expired at T-60ms"


def test_token_embedded_in_a_body_is_scrubbed() -> None:
    record = {
        "request_body": dumps({"auctionId": "d6a49a23", "note": "token " + TOKEN}),
        "response_body": {"isValid": False, "hint": "reauth with " + TOKEN},
    }

    clean = redact(record)

    assert NEEDLE not in dumps(clean)
    assert '"auctionId": "d6a49a23"' in clean["request_body"]
    assert clean["response_body"]["isValid"] is False


def test_token_in_a_url_path_is_scrubbed() -> None:
    assert redact(PATH_URL) == (
        "https://subastas.secop.gov.co/eauctions.api/signalr/hub/" + SHORT_TOKEN + "/poll"
    )
    assert NEEDLE not in dumps(redact({"url": PATH_URL}))
    assert NEEDLE not in dumps(redact({"headers": {"Referer": PATH_URL}}))
    assert NEEDLE not in dumps(redact({"detail": [{"note": "GET " + PATH_URL}]}))


def test_token_in_an_exception_message_is_scrubbed() -> None:
    message = "InvalidURL: failed to parse /hub/" + TOKEN + "/poll"

    assert NEEDLE not in redact({"exception": message})["exception"]
    assert NEEDLE not in redact_jwts(message)


def test_a_string_with_no_token_is_returned_unchanged() -> None:
    plain = "bid rejected: BidErrMin, minBid 7.813.974,32 COP"

    assert redact_jwts(plain) is plain
    assert redact({"detail": plain})["detail"] == plain


def test_authorization_header_keeps_only_the_scheme() -> None:
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json"}

    out = redact_headers(headers)

    assert out["Authorization"] == "Bearer " + REDACTED
    assert out["Accept"] == "application/json"


def test_authorization_header_is_case_insensitive() -> None:
    assert redact_headers({"authorization": "Bearer " + TOKEN})["authorization"] == (
        "Bearer " + REDACTED
    )


def test_cookie_names_survive_with_blanked_values() -> None:
    cookie = "ARRAffinity=8f2c1de4bb; ARRAffinitySameSite=8f2c1de4bb; .ASPXAUTH=A1B2C3D4"

    out = redact_headers({"Cookie": cookie})["Cookie"]

    assert out == "ARRAffinity=; ARRAffinitySameSite=; .ASPXAUTH="
    assert "8f2c1de4bb" not in out
    assert "A1B2C3D4" not in out


def test_set_cookie_keeps_attributes_and_drops_the_value() -> None:
    out = redact_headers(
        {"Set-Cookie": "ARRAffinity=8f2c1de4bb; Path=/; HttpOnly; Secure"}
    )["Set-Cookie"]

    assert out == "ARRAffinity=; Path=/; HttpOnly; Secure"


def test_referer_header_carrying_a_token_is_scrubbed() -> None:
    out = redact_headers({"Referer": POLL_URL})["Referer"]

    assert NEEDLE not in out


def test_bare_jwt_string_becomes_first6_last6() -> None:
    expected = TOKEN[:6] + "…" + TOKEN[-6:]

    assert redact_token(TOKEN) == expected
    assert redact({"jwt": TOKEN})["jwt"] == expected
    assert redact({"note": TOKEN})["note"] == expected
    assert redact(TOKEN) == expected


def test_short_secrets_are_replaced_wholesale() -> None:
    assert redact_token("abc") == REDACTED
    assert redact_token("") == REDACTED
    assert redact({"password": "hunter2"})["password"] == REDACTED


def test_config_shaped_mapping_is_scrubbed_by_key() -> None:
    clean = redact({"jwt": TOKEN, "bid_value": "1", "auction_id": "d6a49a23"})

    assert NEEDLE not in dumps(clean)
    assert clean["bid_value"] == "1"
    assert clean["auction_id"] == "d6a49a23"


def test_a_mapping_key_carrying_a_token_is_scrubbed() -> None:
    """Keys are serialised too, so walking values alone writes the token out verbatim."""
    assert NEEDLE not in dumps(redact({TOKEN: "seen"}))
    assert NEEDLE not in dumps(redact({"seen_urls": {POLL_URL: 3}}))
    assert NEEDLE not in dumps(redact({"detail": [{"GET " + PATH_URL: 1}]}))
    assert NEEDLE not in dumps(redact({"headers": {"X-" + TOKEN: "1"}}))

    assert redact({TOKEN: "seen"}) == {SHORT_TOKEN: "seen"}
    # The key still decides how its own value is treated, redacted or not.
    assert redact({"password": "hunter2"}) == {"password": REDACTED}


def test_bytes_and_bytearray_bodies_are_scrubbed() -> None:
    """``capture.json_default`` decodes these to text, so an unscrubbed one is plaintext."""
    raw = ("token " + TOKEN + " ok").encode("utf-8")

    assert NEEDLE not in dumps(redact({"request_body": raw}))
    assert NEEDLE not in dumps(redact({"request_body": bytearray(raw)}))
    assert NEEDLE not in dumps(redact({"headers": {"X-Auth": raw}}))
    assert NEEDLE not in dumps(redact([raw]))
    assert redact(raw) == ("token " + SHORT_TOKEN + " ok").encode("utf-8")

    # A body that cannot hold a JWT is passed through byte-identical: decoding with
    # errors="replace" and re-encoding would rewrite bytes that were never a secret.
    binary = b"\x89PNG\r\n\x1a\n\xff\xfe"
    assert redact(binary) is binary
    assert redact({"response_body": binary})["response_body"] is binary


def test_set_and_frozenset_members_are_scrubbed() -> None:
    """``json_default`` turns these into JSON arrays, one plaintext member each."""
    assert NEEDLE not in dumps(redact({"seen": {TOKEN}}))
    assert NEEDLE not in dumps(redact({"seen": frozenset([POLL_URL])}))
    assert NEEDLE not in dumps(redact({"seen": {"a", TOKEN, PATH_URL}}))

    assert redact({"seen": {TOKEN}})["seen"] == [SHORT_TOKEN]
    assert redact({"seen": frozenset(["b", "a"])})["seen"] == ["a", "b"]


def test_redact_is_idempotent() -> None:
    samples = [
        POLL_URL,
        TOKEN,
        PATH_URL,
        "sent " + TOKEN + " and " + TOKEN,
        {"url": POLL_URL},
        {"url": PATH_URL},
        {"headers": {"Authorization": "Bearer " + TOKEN, "Cookie": "a=1; b=2"}},
        {"detail": {"nested": [{"url": POLL_URL}, {"jwt": TOKEN}]}},
        {"exception": "InvalidURL: /hub/" + TOKEN + "/poll"},
        {"jwt": TOKEN, "run_root": "runs"},
        {TOKEN: "seen"},
        {"seen_urls": {POLL_URL: 3}},
        {"request_body": ("token " + TOKEN).encode("utf-8")},
        {"request_body": bytearray(b"\x89PNG\r\n\x1a\n")},
        {"seen": {TOKEN, "a"}},
        {"seen": frozenset([POLL_URL])},
    ]

    for sample in samples:
        once = redact(sample)
        assert redact(once) == once

    assert redact_url(redact_url(POLL_URL)) == redact_url(POLL_URL)
    assert redact_jwts(redact_jwts(PATH_URL)) == redact_jwts(PATH_URL)
    assert redact_headers(redact_headers({"Cookie": "a=1; b=2"})) == redact_headers(
        {"Cookie": "a=1; b=2"}
    )


def test_awkward_shapes_survive_the_real_writer_without_leaking(tmp_path: Path) -> None:
    """The choke point is the writer thread, so the guarantee is only worth what it writes."""
    cfg = Config(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="1",
        fire_time="2026-07-28T21:00:00Z",
        run_root=str(tmp_path / "runs"),
    )
    logger = RunLogger(cfg)
    try:
        logger.event(
            stage="fire",
            event="odd_shapes",
            detail={
                "by_url": {POLL_URL: 3},
                "body": ("token " + TOKEN).encode("utf-8"),
                "seen": {TOKEN, PATH_URL},
                "buffer": bytearray(("bearer " + TOKEN).encode("utf-8")),
            },
        )
        logger.write_json(
            "odd.json", {TOKEN: "key", "set": frozenset([TOKEN]), "raw": TOKEN.encode()}
        )
        logger.flush()
    finally:
        logger.close()

    assert logger.writer_errors == []
    for path in sorted(logger.run_dir.rglob("*")):
        if path.is_file():
            assert NEEDLE not in path.read_text(encoding="utf-8", errors="replace")


class _StubAdapter(HTTPAdapter):
    """Stands in for the socket, so the real ``requests`` pipeline still runs."""

    def __init__(self, content: bytes) -> None:
        super().__init__(pool_connections=1, pool_maxsize=1, max_retries=0)
        self.content = content

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        response = requests.Response()
        response.status_code = 200
        response._content = self.content
        response.headers = CaseInsensitiveDict(
            {"Date": "Tue, 28 Jul 2026 21:19:31 GMT", "Content-Type": "application/json"}
        )
        response.url = request.url
        response.request = request
        response.raw = None
        return response


def test_no_file_in_a_real_run_dir_carries_a_raw_token(tmp_path: Path) -> None:
    cfg = Config(
        jwt=TOKEN,
        auction_page_html_path=str(tmp_path / "page.html"),
        bid_value="1",
        fire_time="2026-07-28T21:00:00Z",
        run_root=str(tmp_path / "runs"),
    )
    ref = AuctionRef(auction_id="d6a49a23-3556-4b3d-9bb4-3e209279b9f8")
    logger = RunLogger(cfg)
    try:
        client = HttpClient(cfg, ref, RequestsCookieJar(), logger)
        adapter = _StubAdapter(
            dumps({"isValid": False, "hint": "reauth with " + TOKEN}).encode("utf-8")
        )
        client.session.mount("https://", adapter)
        client.session.mount("http://", adapter)

        client.post_json(
            endpoints.validate_bid(),
            dumps({"auctionId": ref.auction_id, "note": "token " + TOKEN}).encode("utf-8"),
            stage="fire",
            thread_id="t1",
            role="D",
        )
        logger.event(
            stage="fire",
            event="poll_failed",
            detail={"message": "GET " + PATH_URL + " failed"},
        )
        logger.http(
            stage="fire",
            corr_id=None,
            thread_id="t2",
            role="V",
            method="GET",
            url=PATH_URL,
            request_headers={"Accept": "application/json"},
            request_body=None,
            status=None,
            response_headers={},
            response_body=None,
            t_send_mono_ns=1,
            t_recv_mono_ns=2,
            exception=requests.exceptions.InvalidURL("failed to parse " + PATH_URL),
        )
        logger.flush()
    finally:
        logger.close()

    written = [path for path in sorted(logger.run_dir.rglob("*")) if path.is_file()]

    assert [path.name for path in written if path.name == "capture.har"]
    assert len(written) >= 6
    for path in written:
        assert NEEDLE not in path.name
        assert NEEDLE not in path.read_text(encoding="utf-8", errors="replace")
